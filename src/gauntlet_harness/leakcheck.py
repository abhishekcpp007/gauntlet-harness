"""Prove an agent sandbox can't see the answer.

Boots the exact sandbox an agent gets and looks for every way the hidden tests or the gold
fix could reach it: git history, the filesystem (including whatever the env image carries),
environment variables, the network, grader scratch paths and the problem statement itself.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from pydantic import BaseModel, Field

from .sandboxing import boot, cached_clone
from .schema import HarnessTask

MIN_LINE = 20
_BORING = re.compile(r"^[\W_]*$|^(?:return|pass|else:|try:|finally:|continue|break)\b.{0,12}$")
_IMPORT = re.compile(r"^(?:from\s+\S+\s+)?import\s")  # imports name things, they don't identify a fix
_TEST_DEF = re.compile(r"^\+\s*(?:async\s+)?def\s+(test\w*)\s*\(", re.MULTILINE)


class Check(BaseModel):
    ok: bool
    detail: str = ""


class LeakReport(BaseModel):
    ok: bool
    checks: dict[str, Check] = Field(default_factory=dict)

    def failures(self) -> dict[str, str]:
        return {name: c.detail for name, c in self.checks.items() if not c.ok}


def distinctive_added_lines(patch: str, *, min_len: int = MIN_LINE) -> list[str]:
    """Lines a patch adds that are long and specific enough to identify it.

    Skips lines the patch also removes (moves, reindents), short lines and boilerplate.
    """
    added: list[str] = []
    removed: set[str] = set()
    for raw in patch.splitlines():
        if raw.startswith(("+++", "---")):
            continue
        if raw.startswith("+"):
            added.append(raw[1:].strip())
        elif raw.startswith("-"):
            removed.add(raw[1:].strip())
    seen: set[str] = set()
    out: list[str] = []
    for line in added:
        if (
            len(line) < min_len
            or line in removed
            or line in seen
            or _BORING.match(line)
            or _IMPORT.match(line)
        ):
            continue
        if not re.search(r"[A-Za-z0-9]", line):
            continue
        seen.add(line)
        out.append(line)
    return out


def new_test_names(test_patch: str) -> list[str]:
    return sorted(set(_TEST_DEF.findall(test_patch)))


def statement_leaks(statement: str, gold_patch: str) -> list[str]:
    """Gold lines that appear verbatim in the problem statement."""
    return [line for line in distinctive_added_lines(gold_patch) if line in statement]


def _present_at_base(repo_path: Path, sha: str, needles: list[str]) -> set[str]:
    """Needles that already exist somewhere in the base tree, so finding them proves nothing."""
    if not needles:
        return set()
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("\n".join(needles) + "\n")
        pattern_file = f.name
    # whole matching lines, not -o: -o prints one match per spot, so a needle that is a substring
    # of another needle (a test name inside its own def line) would never be reported
    cmd = ["git", "-C", str(repo_path), "grep", "-F", "-h", "-I", "--no-color", "-f", pattern_file, sha]
    try:
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode > 1:  # 1 is "no match"; anything above is an error, usually a missing commit
            subprocess.run(["git", "-C", str(repo_path), "fetch", "-q", "origin", sha], capture_output=True)
            proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode > 1:
            # failing open here would flag every pre-existing line as a leak, or worse, hide one
            raise RuntimeError(f"git grep at {sha} failed: {proc.stderr.decode(errors='replace')[:300]}")
    finally:
        Path(pattern_file).unlink(missing_ok=True)
    lines = proc.stdout.decode(errors="replace").splitlines()
    return {n for n in needles if any(n in line for line in lines)}


# Python's own stdlib ships with the base image, which predates every PR, so a line found
# there is a common idiom and can't be this PR's fix. site-packages is not exempt.
_STDLIB = re.compile(r"^/usr/(local/)?lib/python3\.\d+/(?!site-packages/|dist-packages/)")


def from_base_image(path: str) -> bool:
    return bool(_STDLIB.match(path))


def project_packages(task: HarnessTask) -> set[str]:
    """Import names the PR's code lives under: src/packaging/x.py and packaging/x.py give packaging."""
    names: set[str] = set()
    for path in re.findall(r"^\+\+\+ b/(\S+\.py)$", task.gold_patch, re.MULTILINE):
        parts = [p for p in path.split("/") if p]
        if parts and parts[0] == "src":
            parts = parts[1:]
        if parts:
            names.add(parts[0] if len(parts) > 1 else parts[0].removesuffix(".py"))
    return names


def counts_as_leak(path: str, packages: set[str]) -> bool:
    """Whether a needle found at `path` means something. Third-party code shares idioms with every
    project (`return self.__class__(`), so installed packages only count when they are a copy of
    this project itself, from PyPI or vendored (pip/_vendor/packaging). Everything else counts."""
    if from_base_image(path):
        return False
    parts = path.split("/")
    for marker in ("site-packages", "dist-packages"):
        if marker in parts:
            inside = parts[parts.index(marker) + 1 :]
            return any(seg in packages or seg.split("-")[0] in packages for seg in inside)
    return True


def leakcheck(task: HarnessTask, *, cache_dir: Path | None = None, grep_timeout: int = 180) -> LeakReport:
    checks: dict[str, Check] = {}

    # 1. the statement, no container needed
    leaked = statement_leaks(task.problem_statement, task.gold_patch)
    checks["statement"] = Check(
        ok=not leaked, detail=f"gold lines in the statement: {leaked[:3]}" if leaked else "clean"
    )

    repo = cached_clone(task.repo, cache_dir)
    hidden = distinctive_added_lines(task.test_patch)
    gold = distinctive_added_lines(task.gold_patch)
    names = [f"def {n}(" for n in new_test_names(task.test_patch)]
    at_base = _present_at_base(repo.path, task.base_commit, [*hidden, *gold, *names])
    hidden = [n for n in hidden if n not in at_base]
    gold = [n for n in gold if n not in at_base]
    names = [n for n in names if n not in at_base]
    needles = [*dict.fromkeys([*hidden, *gold, *names])]
    packages = project_packages(task)

    sb = boot(task, apply_tests=False, baseline_git=True, record=False, cache_dir=cache_dir)
    try:
        wd = task.workdir

        # 2. grader scratch paths (checked first, before we write our own needle file)
        res = sb.exec("ls -d /tmp/gauntlet-* 2>/dev/null")
        checks["grader_paths"] = Check(ok=not res.stdout.strip(), detail=res.stdout.strip() or "none")

        # 3. git history: only the one scratch commit, and no other repos lying around
        res = sb.exec("git rev-list --all --count", workdir=wd)
        extra = sb.exec(
            f"find / -xdev -name .git -not -path '{wd}/.git' 2>/dev/null | grep -v '^/proc' | head -5"
        )
        count = res.stdout.strip()
        checks["git_history"] = Check(
            ok=count == "1" and not extra.stdout.strip(),
            detail=f"{count} commit(s) visible"
            + (f", other repos: {extra.stdout.split()}" if extra.stdout.strip() else ""),
        )

        # 4. the network
        res = sb.exec(
            'python -c "import socket; socket.setdefaulttimeout(3); '
            "socket.create_connection(('1.1.1.1', 443))\" 2>/dev/null && echo OPEN || echo BLOCKED",
            timeout=20,
        )
        checks["network"] = Check(ok="BLOCKED" in res.stdout, detail=res.stdout.strip())

        # 5. environment variables
        env = sb.exec("env").stdout
        in_env = [n for n in needles if n in env]
        checks["env_vars"] = Check(ok=not in_env, detail=f"found: {in_env[:3]}" if in_env else "clean")

        # 6. the whole readable filesystem
        if needles:
            needle_file = f"/tmp/.leakcheck-{uuid.uuid4().hex}"
            sb.write_file(needle_file, "\n".join(needles) + "\n")
            script = (
                "for d in /*; do case $d in /proc|/sys|/dev) ;; *) "
                f"grep -rIoF --exclude='.leakcheck-*' -f {needle_file} \"$d\" 2>/dev/null;; esac; done; true"
            )
            res = sb.exec(f"timeout {grep_timeout} sh -c {_sh(script)}", timeout=grep_timeout + 30)
            sb.exec(f"rm -f {needle_file}")
            hits: dict[str, set[str]] = {}
            for line in res.stdout.splitlines():
                path, _, match = line.partition(":")
                if match and counts_as_leak(path, packages):
                    hits.setdefault(match, set()).add(path)
            timed_out = res.timed_out or res.exit_code == 124

            def report(group: list[str], label: str) -> Check:
                found = {n: sorted(hits[n])[:3] for n in group if n in hits}
                if found:
                    first = next(iter(found.items()))
                    return Check(ok=False, detail=f"{len(found)} {label} line(s) readable, e.g. {first}")
                if timed_out:
                    return Check(ok=False, detail="filesystem scan timed out, can't vouch for it")
                return Check(ok=True, detail=f"{len(group)} {label} line(s) checked, none found")

            checks["hidden_tests_on_disk"] = report(hidden, "hidden test")
            checks["gold_on_disk"] = report(gold, "gold patch")
            checks["new_test_names_on_disk"] = report(names, "new test name")
        else:
            checks["filesystem"] = Check(ok=True, detail="no distinctive lines to look for")
    finally:
        sb.close()

    return LeakReport(ok=all(c.ok for c in checks.values()), checks=checks)


def _sh(script: str) -> str:
    return "'" + script.replace("'", "'\"'\"'") + "'"
