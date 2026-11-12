"""Long-horizon tasks: bring a repo from one release to the next.

An SWE task is one merged PR. An LHT task is a release range, X to Y, which is every PR that
merged between two tags. The agent starts at X and the hidden tests are Y's. It's the same
idea as the `X_to_Y` range configs in multi-swe-bench, built on this harness's validation,
canary and cheat checks instead of on trust.

Why a separate category and not just "a big SWE task":

- **The reward has to have shape.** A range with 40 PRs and 600 target tests is almost never
  fully resolved, so pass/fail gives an RL run nothing to climb. Every target test is
  attributed to a milestone (one PR or direct commit in the range) and the reward is the
  mean milestone pass rate. Equal weight per milestone, so one PR with 300 parametrized cases can't drown out
  ten small ones.
- **Validation can't be all or nothing.** Multi-SWE-bench drops a PR outright when the fix
  breaks one passing test. Across a release that rule would reject nearly every range for
  one flaky or environment-bound test. Here a regressed test is dropped from grading and
  written down in `horizon.dropped_regressions`, up to a small cap. Past the cap the range
  is rejected, because that many regressions means the env is wrong, not the tests.
- **The budget is different**, see profiles.py.
- **It never shares a leaderboard with SWE**, see report.py.

Milestone attribution, in order:

    name   the target test's function is defined (`def test_x`) in the added lines of
           exactly one PR's test diff, or of several, in which case the last one wins
           because the hidden tests check the behaviour as of Y
    file   otherwise, the test's file was touched by exactly one PR in the range
    none   otherwise it goes to the "unattributed" milestone, so it still counts once

The env is built at X. When the release changed dependencies the gold fix may not run in
that env, and the three validation runs catch it as missing or failing tests. That's the
point of validating rather than assuming.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from docker.errors import APIError
from gauntlet_repo2env import BuildFailed, PatchFailed, Repo2EnvError, UnsupportedRepo
from gauntlet_repo2env.repo import (
    GitError,
    Repo,
    changed_files,
    clone,
    diff,
    git,
    is_test_module,
    is_test_path,
)
from gauntlet_sandbox import SandboxError

from .build import Log, ensure_env, sanitize
from .github import MERGE_SUBJECT_RE, SQUASH_SUBJECT_RE, parse_time
from .leakcheck import statement_leaks
from .sandboxing import boot
from .schema import HarnessTask, Horizon, Milestone, PRInfo, Rejected
from .testing import apply_patch, classify, run_suite, summarize

# A tag that looks like a release: v1.2.3, 1.2, 2024.10.1, release-1.0. Pre-releases are
# skipped by default because rc -> final ranges are usually a version bump and nothing else.
RELEASE_TAG = re.compile(r"^(?:v|release-|rel-)?\d+(?:\.\d+)+$")
PRERELEASE_TAG = re.compile(r"^(?:v|release-|rel-)?\d+(?:\.\d+)+[-.]?(?:a|b|rc|alpha|beta|dev|pre)\d*", re.I)

MAX_PRS = 40  # past this a task stops being one agent's job and turns into a migration
MAX_GOLD_LINES = 6000
MAX_REGRESSIONS = 5
MAX_REGRESSION_SHARE = 0.02  # of the stable tests in scope
ITEM_CHARS = 1500  # per change in the statement, so one essay of a PR body can't bury the rest


def range_id(repo: str, from_ref: str, to_ref: str) -> str:
    owner, _, name = repo.partition("/")

    def clean(ref: str) -> str:
        return re.sub(r"[^A-Za-z0-9.]+", "-", ref).strip("-")

    return f"{owner}__{name}-{clean(from_ref)}_to_{clean(to_ref)}"


# ---------------------------------------------------------------------- finding ranges


def _version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", tag))


def release_tags(repo: Repo, *, prereleases: bool = False) -> list[str]:
    """Release tags, oldest first: by the date of what they point at, then by version number,
    because tags made in the same second (scripted releases, test repos) otherwise sort by
    name and v0.10.0 lands before v0.2.0."""
    out = git(repo.path, "tag", "--format=%(creatordate:unix) %(refname:short)").split("\n")
    keep = []
    for line in out:
        stamp, _, tag = line.strip().partition(" ")
        if tag and (RELEASE_TAG.match(tag) or (prereleases and PRERELEASE_TAG.match(tag))):
            keep.append((int(stamp or 0), _version_key(tag), tag))
    return [tag for *_, tag in sorted(keep)]


def _is_ancestor(repo: Repo, a: str, b: str) -> bool:
    res = subprocess.run(
        ["git", "-C", str(repo.path), "merge-base", "--is-ancestor", a, b], capture_output=True
    )
    return res.returncode == 0


def release_ranges(
    repo: Repo, *, last: int | None = None, prereleases: bool = False, lookback: int = 20
) -> list[tuple[str, str]]:
    """One range per release: from the newest earlier release that is its ancestor, to it.

    "Newest earlier ancestor" rather than "the tag before it" because release lines branch.
    A 1.4.7 maintenance tag cut after 2.0.0 pairs with 1.4.6, and 2.0.1 pairs with 2.0.0,
    never with 1.4.7: a diff across two branches isn't a release anyone shipped. `lookback`
    bounds the search so a repo with 500 tags doesn't cost 500 squared git calls.
    """
    tags = release_tags(repo, prereleases=prereleases)
    commit = {t: git(repo.path, "rev-parse", f"{t}^{{commit}}").strip() for t in tags}
    pairs: list[tuple[str, str]] = []
    for i, tag in enumerate(tags):
        for prev in reversed(tags[max(0, i - lookback) : i]):
            if commit[prev] != commit[tag] and _is_ancestor(repo, commit[prev], commit[tag]):
                pairs.append((prev, tag))
                break
    return pairs[-last:] if last else pairs


# ---------------------------------------------------------------------- what's in a range


def prs_in_range(repo: Repo, slug: str, base: str, solution: str) -> tuple[list[PRInfo], list[str]]:
    """(PRs oldest first, direct commits that touched source). First-parent only, so a PR
    counts once, as its merge or squash commit, not as each commit on its branch."""
    out = subprocess.run(
        [
            "git", "-C", str(repo.path), "log", "--first-parent", "--reverse",
            "--format=%H%x00%P%x00%cI%x00%B%x1e", f"{base}..{solution}",
        ],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", errors="replace")  # fmt: skip
    prs: list[PRInfo] = []
    direct: list[str] = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, parents, date, message = record.split("\x00", 3)
        lines = message.strip().splitlines()
        subject = lines[0] if lines else ""
        rest = "\n".join(lines[1:]).strip()
        first_parent = parents.split()[0] if parents.strip() else None
        if m := MERGE_SUBJECT_RE.match(subject):
            number, title = int(m.group(1)), (rest.splitlines()[0] if rest else subject)
            body = "\n".join(rest.splitlines()[1:]).strip()
        elif m := SQUASH_SUBJECT_RE.match(subject):
            number, title, body = int(m.group("num")), m.group("title"), rest
        else:
            touched = changed_files(repo, first_parent, sha) if first_parent else []
            if any(f.endswith(".py") and not is_test_path(f) for f in touched):
                direct.append(f"{sha[:12]} {subject}")
            continue
        merged = parse_time(date)
        prs.append(
            PRInfo(
                repo=slug,
                number=number,
                title=title,
                body=body,
                url=f"https://github.com/{slug}/pull/{number}",
                merged_at=merged.isoformat() if merged else None,
                base_sha=first_parent,
                merge_commit_sha=sha,
                source="git",
            )
        )
    return prs, direct


_DEF = re.compile(r"^\+\s*(?:async\s+)?def\s+(test\w*)\s*\(", re.M)


def _unit_tests(repo: Repo, before: str | None, after: str | None) -> tuple[set[str], set[str]]:
    """(test files a change touched, test function names it added or changed)."""
    if not before or not after:
        return set(), set()
    files = {f for f in changed_files(repo, before, after) if is_test_path(f)}
    names: set[str] = set()
    if files:
        patch = diff(repo, before, after, sorted(files))
        names = set(_DEF.findall(patch))
    return files, names


def units_in_order(
    repo: Repo, base: str, solution: str, prs: list[PRInfo], direct: list[str]
) -> list[tuple[str, str, list[int], tuple[set[str], set[str]]]]:
    """Every change in the range that can own tests, in merge order:
    (milestone id, title, PR numbers, (test files, test names)).

    A direct commit is a unit too. Some maintainers never open PRs (cachetools pushes
    straight to master), and a range made only of direct commits is still a release.
    """
    by_sha = {pr.merge_commit_sha: pr for pr in prs}
    by_short = {c.split(" ", 1)[0]: c for c in direct}
    order = git(repo.path, "log", "--first-parent", "--reverse", "--format=%H", f"{base}..{solution}").split()
    units = []
    for sha in order:
        if sha in by_sha:
            pr = by_sha[sha]
            units.append((f"pr-{pr.number}", pr.title, [pr.number], _unit_tests(repo, pr.base_sha, sha)))
        elif sha[:12] in by_short:
            subject = by_short[sha[:12]].partition(" ")[2]
            units.append((f"commit-{sha[:12]}", subject, [], _unit_tests(repo, f"{sha}^", sha)))
    return units


def _split(test_id: str) -> tuple[str, str]:
    path, *rest = test_id.split("[")[0].split("::")
    return path, rest[-1] if rest else ""


def attribute(
    targets: list[str],
    touched: dict[str, tuple[set[str], set[str]]],
    titles: dict[str, str],
    prs: dict[str, list[int]] | None = None,
) -> list[Milestone]:
    """Group target tests into milestones, one per change, by the rules in the module docstring.

    `touched` is milestone id -> (test files, test names), in merge order.
    """
    order = list(touched)
    owned: dict[str, list[str]] = {n: [] for n in order}
    how: dict[str, set[str]] = {n: set() for n in order}
    loose: list[str] = []
    for test in targets:
        path, name = _split(test)
        by_name = [n for n in order if name and name in touched[n][1] and path in touched[n][0]]
        if by_name:
            owned[by_name[-1]].append(test)
            how[by_name[-1]].add("name")
            continue
        by_file = [n for n in order if path in touched[n][0]]
        if len(by_file) == 1:
            owned[by_file[0]].append(test)
            how[by_file[0]].add("file")
            continue
        loose.append(test)

    milestones = [
        Milestone(
            id=n,
            title=titles.get(n, ""),
            prs=(prs or {}).get(n, []),
            tests=sorted(owned[n]),
            attribution="name" if how[n] == {"name"} else "file",
        )
        for n in order
        if owned[n]
    ]
    if loose:
        milestones.append(
            Milestone(
                id="unattributed",
                title="tests no single change owns",
                tests=sorted(loose),
                attribution="none",
            )
        )
    return milestones


def range_statement(slug: str, from_ref: str, to_ref: str, prs: list[PRInfo], direct: list[str]) -> str:
    """Release notes as the agent sees them: one numbered item per PR, issue text when we
    have it, else the PR's own description, all sanitized the same way SWE statements are."""
    parts = [f"Changes between {from_ref} and {to_ref}:"]
    for i, pr in enumerate(prs, 1):
        issues = [pr.issue_texts[n] for n in pr.linked_issues if pr.issue_texts.get(n)]
        detail = sanitize("\n\n".join(issues) if issues else pr.body, slug)
        if len(detail) > ITEM_CHARS:
            detail = detail[:ITEM_CHARS].rsplit(" ", 1)[0] + " [...]"
        item = f"{i}. {pr.title.strip()}"
        if detail:
            item += "\n" + "\n".join(f"   {line}" if line else "" for line in detail.splitlines())
        parts.append(item)
    if direct:
        parts.append("Other commits in the range:")
        parts.extend(f"- {c.split(' ', 1)[1] if ' ' in c else c}" for c in direct)
    return "\n\n".join(parts)


def _gold_lines(patch: str) -> int:
    return sum(1 for line in patch.splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---")))


# ---------------------------------------------------------------------- the build


def build_range_task(
    slug: str,
    from_ref: str,
    to_ref: str,
    *,
    cache_dir: Path | None = None,
    python: str = "3.12",
    fix_runs: int = 2,
    strict_statement: bool = True,
    repo_url: str | None = None,
    test_timeout: float = 3600,
    max_prs: int = MAX_PRS,
    max_gold_lines: int = MAX_GOLD_LINES,
    issue_texts: dict[int, dict[int, str]] | None = None,
    log: Log = lambda _: None,
) -> HarnessTask | Rejected:
    """Validate one release range end to end. Expected failures come back as Rejected.

    `issue_texts` is PR number -> {issue number: text}, for callers that fetched issues
    from the API. Without it the statement is built from PR titles and descriptions only.
    """
    iid = range_id(slug, from_ref, to_ref)
    url = repo_url or f"https://github.com/{slug}.git"
    timings: dict[str, float] = {}
    started = time.monotonic()

    def reject(stage: str, reason: str, **detail) -> Rejected:
        log(f"{iid} rejected at {stage}: {reason}")
        return Rejected(instance_id=iid, stage=stage, reason=reason, detail=detail)

    # 1. the range
    try:
        repo = clone(url, cache_dir)
        base = git(repo.path, "rev-parse", f"{from_ref}^{{commit}}").strip()
        solution = git(repo.path, "rev-parse", f"{to_ref}^{{commit}}").strip()
    except Repo2EnvError as e:
        return reject("collect", str(e))
    if not _is_ancestor(repo, base, solution):
        return reject("collect", f"{from_ref} is not an ancestor of {to_ref}, so this isn't a release range")
    prs, direct = prs_in_range(repo, slug, base, solution)
    for pr in prs:
        for issue, text in (issue_texts or {}).get(pr.number, {}).items():
            pr.issue_texts[issue] = text
            if issue not in pr.linked_issues:
                pr.linked_issues.append(issue)
    if not prs and not direct:
        return reject("filter", "no source changes in the range")
    if len(prs) > max_prs:
        return reject("filter", f"{len(prs)} PRs in the range, the cap is {max_prs}", prs=len(prs))

    files = changed_files(repo, base, solution)
    tests = [f for f in files if is_test_path(f)]
    source = [f for f in files if not is_test_path(f)]
    test_modules = [f for f in tests if is_test_module(f)]
    if not any(f.endswith(".py") for f in source):
        return reject("filter", "no Python source changes", files=files)
    if not test_modules:
        return reject("filter", "no test module changes", files=files)
    gold_patch = diff(repo, base, solution, source)
    test_patch = diff(repo, base, solution, tests)
    gold_lines = _gold_lines(gold_patch)
    if gold_lines > max_gold_lines:
        return reject("filter", f"the source diff is {gold_lines} lines, the cap is {max_gold_lines}")

    # 2. statement
    statement = range_statement(slug, from_ref, to_ref, prs, direct)
    leaked = statement_leaks(statement, gold_patch)
    if leaked and strict_statement:
        return reject("statement", "the statement quotes the fix", lines=leaked[:5])

    # 3. env at the older release
    t = time.monotonic()
    try:
        image, fingerprint, reused = ensure_env(url, repo, base, python=python, cache_dir=cache_dir, log=log)
    except (UnsupportedRepo, BuildFailed, Repo2EnvError, SandboxError, APIError, GitError) as e:
        return reject("env", str(e)[:2000])
    timings["env"] = round(time.monotonic() - t, 2)

    date = git(repo.path, "show", "-s", "--format=%cI", solution).strip()
    task = HarnessTask(
        category="lht",
        instance_id=iid,
        repo=url,
        pr=PRInfo(
            repo=slug,
            number=0,
            title=f"{from_ref} to {to_ref}",
            url=f"https://github.com/{slug}/compare/{from_ref}...{to_ref}",
            merged_at=date or None,
            base_sha=base,
            merge_commit_sha=solution,
            source="git",
        ),
        base_commit=base,
        solution_commit=solution,
        problem_statement=statement,
        statement_source="pr",
        image=image,
        env_fingerprint=fingerprint,
        gold_patch=gold_patch,
        test_patch=test_patch,
        test_files=tests,
        test_scope=test_modules,
    )

    # 4. the three runs
    def run_once(apply_tests: bool, apply_gold: bool) -> dict:
        sb = boot(task, apply_tests=apply_tests, baseline_git=False, record=False, cache_dir=cache_dir)
        try:
            if apply_gold:
                apply_patch(sb, task.workdir, gold_patch, "gold")
            return run_suite(sb, task.workdir, task.test_scope, timeout=test_timeout)
        finally:
            sb.close()

    try:
        t = time.monotonic()
        run = run_once(False, False)
        timings["run"] = round(time.monotonic() - t, 2)
        t = time.monotonic()
        test = run_once(True, False)
        timings["test"] = round(time.monotonic() - t, 2)
        t = time.monotonic()
        fixes = [run_once(True, True) for _ in range(max(1, fix_runs))]
        timings["fix"] = round(time.monotonic() - t, 2)
    except (PatchFailed, SandboxError) as e:
        return reject("runs", str(e)[:2000])

    # 5. transitions, with regressions dropped up to a cap instead of failing the range
    trans = classify(run, test, fixes)
    runs_summary = {"run": summarize(run), "test": summarize(test), "fix": [summarize(f) for f in fixes]}
    stable = len(trans.f2p) + len(trans.n2p) + len(trans.s2p) + len(trans.p2p) + len(trans.regressions)
    cap = min(MAX_REGRESSIONS, int(stable * MAX_REGRESSION_SHARE))
    if len(trans.regressions) > cap:
        return reject(
            "transitions",
            f"the release breaks {len(trans.regressions)} passing test(s) in this env, the cap is {cap}",
            runs=runs_summary,
            regressions=trans.regressions,
        )
    if not fixes[0]:
        return reject("transitions", "the fix run produced no test results", runs=runs_summary)
    if not trans.targets:
        return reject(
            "transitions", "no test goes from failing, skipped or missing to passing", runs=runs_summary
        )

    task.f2p, task.n2p, task.s2p, task.p2p, task.flaky = (
        trans.f2p,
        trans.n2p,
        trans.s2p,
        trans.p2p,
        trans.flaky,
    )

    # 6. milestones
    units = units_in_order(repo, base, solution, prs, direct)
    milestones = attribute(
        task.targets,
        {uid: tests for uid, _, _, tests in units},
        {uid: title for uid, title, _, _ in units},
        {uid: numbers for uid, _, numbers, _ in units},
    )
    task.horizon = Horizon(
        from_ref=from_ref,
        to_ref=to_ref,
        prs=prs,
        direct_commits=direct,
        milestones=milestones,
        dropped_regressions=trans.regressions,
        gold_lines=gold_lines,
    )

    timings["total"] = round(time.monotonic() - started, 2)
    task.validation = {
        "runs": runs_summary,
        "seconds": timings,
        "env_reused": reused,
        "fix_runs": len(fixes),
        "statement_leak": leaked,
        "scope": "changed",
        "rules": "multi-swe-bench, regressions dropped up to a cap",
        "python": python,
        "attributed": sum(len(m.tests) for m in milestones if m.id != "unattributed"),
    }
    log(
        f"{iid} ok: {len(prs)} PRs, {len(direct)} direct commits, {len(milestones)} milestones, "
        f"targets={len(task.targets)} "
        f"p2p={len(task.p2p)} dropped={len(trans.regressions)} gold={gold_lines} lines"
    )
    return task
