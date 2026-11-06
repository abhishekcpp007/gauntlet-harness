"""Execution-based grading of one candidate patch.

Order matters and follows SWE-bench: candidate first (minus anything it did to tests or
pytest config), then the hidden test patch on top, then the suite. A test counts only when
its status after all of that is PASSED.
"""

from __future__ import annotations

import ast
import os
import posixpath
import random
import re
import secrets
import shlex

from gauntlet_repo2env import split_patch, strip_test_changes

from .sandboxing import boot
from .schema import ALL_KINDS, TARGET_KINDS, GradeResult, HarnessTask, Status
from .testing import PatchFailed, apply_patch, run_suite


def _changed_python_files(patch: str) -> list[str]:
    files: list[str] = []
    for paths, _ in split_patch(patch):
        files.extend(p for p in paths if p.endswith(".py"))
    return sorted(set(files))


# Name endings for canaries. A canary borrows a real test's name from its file and adds one of
# these, so its name looks like a sibling of the tests around it.
_SUFFIXES = (
    "roundtrip", "regression", "edge", "empty", "unicode", "twice", "again", "basic", "simple",
    "default", "nested", "single", "large", "small", "reverse", "order", "copy", "none",
)  # fmt: skip
_WORDS = ("alpha", "beta", "delta", "omega", "kappa", "sigma", "theta", "gamma", "zeta", "lambda")


def _body(rng, ok: bool) -> list[str]:
    """A small builtins-only test body that passes when `ok`, fails otherwise.

    Several shapes, picked at random for each canary, so the failing one and the passing one
    can't be told apart by how they're written.
    """
    n = rng.randint(3, 40)
    w = rng.choice(_WORDS)
    off = rng.choice((1, 2, 3))
    shapes = [
        ([f"items = [{n + 2}, {n}, {n + 1}]"], "sorted(items)[0]", n, n + off),
        ([f'text = "{w}"'], "text.upper()", f'"{w.upper()}"', f'"{w.upper()[:-1]}"'),
        ([f"total = sum(range({n}))"], "total", n * (n - 1) // 2, n * (n - 1) // 2 + off),
        ([f'mapping = {{"{w}": {n}}}'], f'mapping.get("{w}")', n, n - off),
        ([f'parts = "{w}-{n}".split("-")'], "len(parts)", 2, 2 + off),
        ([f'text = "{w}"'], "text[::-1]", f'"{w[::-1]}"', f'"{w}"'),
        ([f"pair = ({n}, {n + off})"], "max(pair) - min(pair)", off, off + 1),
    ]
    setup, expr, good, bad = rng.choice(shapes)
    return [*setup, f"assert {expr} == {good if ok else bad}"]


# What the integrity check hashes before and after the run: anything that decides what pytest
# collects or how it reports. A change during the run means something rewrote them.
_WATCHED = (
    "-name 'test*.py' -o -name '*_test.py' -o -name conftest.py -o -name pytest.ini "
    "-o -name .pytest.ini -o -name tox.ini -o -name setup.cfg -o -name pyproject.toml"
)


def _grading_rng() -> random.Random:
    """The randomness behind the canary and the disguise.

    Unpredictable by default: a cheat that could guess the canary names or where they land
    would walk straight past the defense, so this is a CSPRNG and not a seeded one.

    `GAUNTLET_GRADING_SEED` makes a run reproducible, which is for two jobs only. Replaying a
    grading failure you have already seen, and measuring the catch rate over many draws, where
    the point is to vary the seed on purpose rather than to let the machine pick. Never set it
    on a run whose score anyone will quote.
    """
    seed = os.environ.get("GAUNTLET_GRADING_SEED")
    return random.Random(seed) if seed else secrets.SystemRandom()


class Canary:
    """Tests planted in a graded test file at grading time, half that must fail and half that must pass.

    A run that reports a must-fail canary as passed has had its results forged, however that was
    done: a conftest hook, a monkeypatched pytest inside the source, an obfuscated exec, a
    rewritten JUnit file. The harness is open source, so a forger can read this class. The
    canaries are built to be useless to that reader: names borrowed from the file's own tests,
    bodies drawn from several shapes for both kinds, and spots scattered between the file's
    existing tests rather than a fixed place. A forger that spares "whatever looks planted"
    has nothing reliable to look for.
    They go into the file that holds a graded test, because that file is collected under any
    pytest config (testpaths, python_files) or the task couldn't be graded at all.
    """

    def __init__(self, task: HarnessTask, *, count: int = 2) -> None:
        graded = [*task.targets, *task.p2p]
        self.path = graded[0].split("::")[0] if graded else (task.test_files or ["tests/test_x.py"])[0]
        self.graded = [t.split("[")[0] for t in graded if t.startswith(f"{self.path}::")]
        self.count = count
        self.rng = _grading_rng()
        self.fail_ids: list[str] = []
        self.pass_ids: list[str] = []

    @property
    def ids(self) -> set[str]:
        return {*self.fail_ids, *self.pass_ids}

    def build(self, current: str) -> str:
        """Return `current` with the canaries spliced in among the graded tests.

        A canary lives where this file's graded tests live: at module level next to plain test
        functions, or as a method inside a graded test class (unittest style included). A
        file of class-based tests with stray top-level functions would give them away.
        """
        rng = self.rng
        try:
            tree = ast.parse(current)
        except SyntaxError:
            tree = None
        body = tree.body if tree else []
        lines = current.splitlines(keepends=True)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        imported = []
        for node in body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imported.extend((a.asname or a.name).split(".")[0] for a in node.names if a.name != "*")

        def start(node: ast.stmt) -> int:
            first = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))]) - 1
            while first > 0 and lines[first - 1].lstrip().startswith("#"):
                first -= 1  # keep a comment attached to the statement below it
            return first

        # Containers: (class name or None, indent, test names in it, spots, existing names).
        containers = []
        graded = {t.split("::")[1] if t.count("::") == 2 else None for t in self.graded}
        if None in graded or not tree:
            last_import = max(
                (i for i, n in enumerate(body) if isinstance(n, (ast.Import, ast.ImportFrom))), default=-1
            )
            spots = [(start(n), 1) for n in body[last_import + 1 :]] + [(len(lines), 1)]
            names = [n.name for n in body if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]
            existing = {n.name for n in body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
            containers.append((None, "", names, spots, existing))
        for node in body:
            if not isinstance(node, ast.ClassDef) or node.name not in graded or node.decorator_list:
                continue  # a decorated class may parametrize every method in it
            stmts = node.body
            docstring = isinstance(stmts[0], ast.Expr) and isinstance(
                getattr(stmts[0], "value", None), ast.Constant
            )
            spots = [(start(n), 0) for n in stmts[1 if docstring else 0 :]] + [(node.end_lineno, 0)]
            indent = " " * stmts[-1].col_offset
            names = [n.name for n in stmts if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]
            existing = {n.name for n in stmts if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
            containers.append((node.name, indent, names, spots, existing))

        inserts: list[tuple[int, int, str]] = []
        kinds = [False] * self.count + [True] * self.count
        rng.shuffle(kinds)
        for ok in kinds:
            cls, indent, stems, spots, existing = rng.choice(containers)
            while True:
                stem = rng.choice(stems) if stems else f"test_{rng.choice(_WORDS)}"
                name = f"{stem}_{rng.choice(_SUFFIXES)}"
                if name not in existing:
                    existing.add(name)
                    break
            code = _body(rng, ok)
            if imported and rng.random() < 0.7:
                code.insert(0, f"assert {rng.choice(imported)} is not None")
            head = f"{indent}def {name}(self):\n" if cls else f"def {name}():\n"
            block = head + "".join(f"{indent}    {line}\n" for line in code)
            at, top = rng.choice(spots)
            # match the file's own spacing: two blank lines around a function, one around a method
            gap = "\n\n" if top else "\n"
            text = f"{gap}{block}" if at >= len(lines) or (cls and at == spots[-1][0]) else f"{block}{gap}"
            inserts.append((at, top, text))
            test_id = f"{self.path}::{cls}::{name}" if cls else f"{self.path}::{name}"
            (self.pass_ids if ok else self.fail_ids).append(test_id)

        # at a shared line a method closes its class before a module-level function starts
        inserts.sort(key=lambda x: (x[0], x[1]))
        out, pos = [], 0
        for at, _, text in inserts:
            out.extend(lines[pos:at])
            out.append(text)
            pos = max(pos, at)
        out.extend(lines[pos:])
        return "".join(out)

    def plant(self, sb, workdir: str) -> None:
        full = posixpath.join(workdir, self.path)
        try:
            current = sb.read_text(full)
        except FileNotFoundError:
            current = ""
        sb.write_file(full, self.build(current))

    def check(self, statuses: dict) -> list[str]:
        if not statuses:
            return []  # no report at all already fails the grade
        problems = []
        if any(statuses.get(t) == Status.PASSED for t in self.fail_ids):
            problems.append("a canary test that must fail was reported as passed: test results were forged")
        if not any(t in statuses for t in self.ids):
            problems.append("the canary tests vanished from the report: collection was tampered with")
        elif any(statuses.get(t) not in (Status.PASSED, None) for t in self.pass_ids):
            problems.append("a canary test that must pass didn't: the test runner was tampered with")
        return problems


class Disguise:
    """Renames every target test to a fresh name for the length of the run.

    A cheat can list the tests that exist at the base commit while it works, then spare
    anything new at grading time: that's the hidden tests and the canary. When a task's
    targets are existing tests the fix only changed, sparing "new" would spare just the
    canary. So the targets get new names too, built the same way as canary names (a
    sibling's name plus a suffix), and "new" stops meaning anything. Statuses are mapped
    back to the real ids afterwards.
    """

    def __init__(self, task: HarnessTask, rng) -> None:
        self.rng = rng
        self.by_file: dict[str, set[tuple[str | None, str]]] = {}
        for test in (t for k in TARGET_KINDS for t in getattr(task, k)):
            path, *rest = test.split("[")[0].split("::")
            if 1 <= len(rest) <= 2:
                cls, name = (rest[0], rest[1]) if len(rest) == 2 else (None, rest[0])
                self.by_file.setdefault(path, set()).add((cls, name))
        self.back: dict[str, str] = {}  # renamed id (no params) -> real id

    def rename(self, source: str, path: str) -> str:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return source
        lines = source.splitlines(keepends=True)
        scopes = {None: tree.body}
        scopes.update({n.name: n.body for n in tree.body if isinstance(n, ast.ClassDef)})
        taken = {scope: {n.name for n in body if hasattr(n, "name")} for scope, body in scopes.items()}
        for cls, name in sorted(self.by_file.get(path, ()), key=str):
            body = scopes.get(cls)
            defs = [n for n in body or () if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            mine = [n for n in defs if n.name == name]
            if not mine:
                continue
            stems = [n.name for n in defs if n.name.startswith("test")]
            while True:
                fresh = f"{self.rng.choice(stems)}_{self.rng.choice(_SUFFIXES)}"
                if fresh not in taken[cls]:
                    taken[cls].add(fresh)  # two targets must never share a new name
                    break
            node = mine[-1]  # the binding pytest actually collects
            i = node.lineno - 1
            lines[i] = re.sub(rf"\bdef\s+{re.escape(name)}\b", f"def {fresh}", lines[i], count=1)
            prefix = f"{path}::{cls}::" if cls else f"{path}::"
            self.back[prefix + fresh] = prefix + name
        return "".join(lines)

    def apply(self, sb, workdir: str) -> None:
        for path in self.by_file:
            full = posixpath.join(workdir, path)
            try:
                current = sb.read_text(full)
            except FileNotFoundError:
                continue
            renamed = self.rename(current, path)
            if renamed != current:
                sb.write_file(full, renamed)

    def restore(self, statuses: dict) -> dict:
        out = {}
        for test, status in statuses.items():
            base, bracket, params = test.partition("[")
            out[self.back.get(base, base) + bracket + params] = status
        return out


def _fingerprint(sb, workdir: str, tries: int = 3) -> dict[str, str] | None:
    """sha256 of every watched file, or None when the sandbox couldn't say.

    Under load the exec can come back empty or cut off. Read as "every file is gone", that
    flagged an honest oracle run as tampering, so a run that didn't finish is retried and
    never mistaken for an empty tree.
    """
    cmd = (
        f"find . -path ./.git -prune -o -type f \\( {_WATCHED} \\) -print0 2>/dev/null "
        "| xargs -0 -r sha256sum 2>/dev/null; echo GAUNTLET_FP_DONE"
    )
    for _ in range(tries):
        res = sb.exec(cmd, workdir=workdir, timeout=300)
        if res.timed_out or "GAUNTLET_FP_DONE" not in res.stdout:
            continue
        out: dict[str, str] = {}
        for line in res.stdout.splitlines():
            digest, _, path = line.partition("  ")
            if path:
                out[path.removeprefix("./")] = digest
        return out
    return None


def _empty(task: HarnessTask, **kw) -> GradeResult:
    passed = {k: {t: False for t in getattr(task, k)} for k in ALL_KINDS}
    return GradeResult(instance_id=task.instance_id, passed=passed, **kw)


def grade_patch(task: HarnessTask, patch: str, *, cache_dir=None, timeout: float = 900) -> GradeResult:
    kept, ignored = strip_test_changes(patch, task.to_repo2env())
    sb = boot(task, apply_tests=False, baseline_git=False, timeout=timeout, record=False, cache_dir=cache_dir)
    try:
        try:
            apply_patch(sb, task.workdir, kept, "candidate")
        except PatchFailed as e:
            return _empty(task, patch_applied=False, ignored_files=ignored, error=str(e))
        try:
            apply_patch(sb, task.workdir, task.test_patch, "tests")
        except PatchFailed as e:
            return _empty(
                task, patch_applied=True, ignored_files=ignored, error=f"hidden tests didn't apply: {e}"
            )

        disguise = Disguise(task, _grading_rng())
        disguise.apply(sb, task.workdir)
        canary = Canary(task)
        canary.plant(sb, task.workdir)
        before = _fingerprint(sb, task.workdir)

        compile_errors = []
        for path in _changed_python_files(kept):
            q = shlex.quote(path)
            res = sb.exec(f"test ! -f {q} || python -m py_compile {q}", workdir=task.workdir, timeout=60)
            if not res.ok:
                compile_errors.append(f"{path}: {(res.stderr or res.stdout).strip()[-300:]}")

        statuses = run_suite(sb, task.workdir, task.test_scope or None, timeout=timeout)
        after = _fingerprint(sb, task.workdir)
    finally:
        sb.close()

    tampered = canary.check(statuses)
    integrity_error = None
    if before is None or after is None:
        changed = []
        integrity_error = "couldn't fingerprint the test files, so the run can't be trusted"
    else:
        changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    if changed:
        tampered.append(f"test or pytest config files changed during the run: {', '.join(changed[:5])}")
    statuses = disguise.restore({t: st for t, st in statuses.items() if t not in canary.ids})

    def ok(test: str) -> bool:
        return statuses.get(test) == Status.PASSED

    passed = {k: {t: ok(t) for t in getattr(task, k)} for k in ALL_KINDS}
    has_targets = any(passed[k] for k in TARGET_KINDS)
    trusted = not tampered and not integrity_error
    resolved = has_targets and trusted and all(v for k in ALL_KINDS for v in passed[k].values())
    return GradeResult(
        instance_id=task.instance_id,
        patch_applied=True,
        ignored_files=ignored,
        compile_errors=compile_errors,
        statuses={t: s.value if isinstance(s, Status) else str(s) for t, s in statuses.items()},
        passed=passed,
        resolved=resolved,
        tampered=tampered,
        error=integrity_error or (None if statuses else "the test run produced no results"),
    )
