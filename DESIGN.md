# gauntlet-harness design

One harness that turns recently merged public GitHub PRs into leak-free RL environments, runs model policies in them, records trajectories, and judges each episode on test transitions (F2P / N2P / S2P / P2P) plus a rubric.

Shared data shapes live in `src/gauntlet_harness/schema.py`. Don't change field names there without updating every module.

## Pipeline

```
collect (github.py)  ->  build (build.py: env at base commit, statement, 3 runs, transitions)
      ->  leakcheck (leakcheck.py)  ->  rubric generation (rubric.py)  ->  tasks.jsonl
run (runner.py: policies x tasks x attempts, each an episode in env.py)
      ->  grade (grading.py)  ->  rubric score (rubric.py)  ->  reward (reward.py)  ->  results.jsonl + RL jsonl
report (report.py)
```

## References

- SWE-bench (Jimenez et al., ICLR 2024): task = issue + base commit + hidden tests, F2P and P2P grading.
- Multi-SWE-bench (Zan et al., 2025, arXiv 2504.02605): three runs (run/test/fix), statuses PASSED/FAILED/SKIPPED/NONE, keep tests with ANY->{FAILED,SKIPPED,NONE}->PASSED, drop PRs with any ANY->PASSED->FAILED, drop PRs with no fix, drop abnormal transitions.
- SWE-rebench (Badertdinov et al., NeurIPS 2025, arXiv 2505.20411): continuous collection of fresh PRs, filter by merge date against model cutoffs to avoid contamination.
- Rubrics as Rewards (Gunjal et al., 2025, arXiv 2507.17746): task-specific checklist rubrics with essential / important / optional / pitfall items, scored by an LLM judge, used as the reward.
- SWE-RL (Wei et al., 2025, arXiv 2502.18449): difflib SequenceMatcher similarity to the oracle patch as a dense reward, -1 for malformed output.
- R2E-Gym (Jain et al., 2025, arXiv 2504.07164): gym-style SWE environments, hybrid execution + execution-free verifiers.

## Module ownership and contracts

### data pipeline (github.py, testing.py, sandboxing.py, build.py, leakcheck.py)

- `github.GitHubClient(token: str | None = None, *, base_url="https://api.github.com", http=None)`
  - `list_merged_prs(repo: str, *, merged_after: datetime | None = None, limit: int = 30) -> list[PRInfo]` (closed PRs with merged_at set, newest first, linked issues parsed from closing keywords, issue texts fetched)
  - raises `RateLimited(reset_at)` on 403/429 rate limits
- `github.prs_from_git(clone_path, repo, *, limit, merged_after=None) -> list[PRInfo]` (no-API fallback: merge commits "Merge pull request #N" and squash subjects ending "(#N)", source="git")
- `testing.run_suite(sb, workdir, scope: list[str] | None, *, timeout) -> dict[str, Status]` (pytest + JUnit, error -> FAILED)
- `testing.apply_patch(sb, workdir, patch, name) -> None` raises `PatchFailed`
- `testing.classify(run, test, fix_runs: list[dict]) -> Transitions` with `f2p n2p s2p p2p flaky: list[str]`, `rejects: list[str]`
- `sandboxing.boot(task: HarnessTask, *, apply_tests: bool, baseline_git: bool, timeout=120, trajectory_path=None, record=True, cache_dir=None) -> Sandbox` (fresh sandbox at the base commit, no network, workdir emptied and refilled from git archive; `baseline_git=True` makes a one-commit scratch repo so the agent's diff can be extracted)
- `build.build_task(pr: PRInfo, *, cache_dir=None, python="3.12", scope="changed", fix_runs=2, strict_statement=True, log=...) -> HarnessTask | Rejected`
- `leakcheck.leakcheck(task, *, cache_dir=None) -> LeakReport(ok: bool, checks: dict[str, Check(ok, detail)])`

### judging (rubric.py, judge.py, reward.py)

- `rubric.RubricContext(task, patch, grade: GradeResult, cheat_report)`
- `rubric.RULES: dict[str, Callable[[RubricContext], tuple[float, str]]]` deterministic rules returning (score 0..1, rationale)
- `rubric.default_rubric(task) -> list[RubricItem]` (rule items only, always available)
- `judge.generate_rubric(task, client, *, model) -> list[RubricItem]` (LLM-authored judge items)
- `judge.judge_items(task, patch, items, client, *, model) -> list[ItemScore]`
- `rubric.score_rubric(task, patch, grade, cheat_report, items, *, client=None, model=...) -> RubricScore`
- `reward.RewardConfig`, `reward.compute_reward(grade, rubric, cheat_verdict, patch, gold_patch, cfg) -> tuple[float, dict[str, float]]`

### episodes (grading.py, env.py, policies.py, runner.py, report.py, cli.py)

- `grading.grade_patch(task, patch, *, cache_dir=None) -> GradeResult`
- `env.SWEEnv(task, *, max_steps=60, ...)` with `reset() -> (obs, info)` and `step(action) -> (obs, reward, terminated, truncated, info)`
- `policies`: `OraclePolicy`, `NoopPolicy`, `CheaterPolicy`, `ClaudePolicy(model)`
- `runner.run(tasks, policies, *, attempts, out_dir, workers) -> list[EpisodeResult]`
- `report.summarize(results) -> dict` and markdown / HTML rendering

## Two task categories

`HarnessTask.category` is `swe` or `lht`. Old task files have no field and load as `swe`.

- **swe** is one merged PR. `build.build_task`. Multi-SWE-bench transition rules, strict.
- **lht** is a release range. `lht.build_range_task(slug, from_ref, to_ref)`. It sets
  `task.horizon` (`Horizon`: refs, PRs in merge order, direct commits, milestones, dropped
  regressions, gold size). Same three validation runs. A regression in the fix run drops that
  test from grading, up to `min(5, 2% of stable tests)`; past that the range is rejected,
  because that many means the env is wrong.

What's per category lives in `profiles.py` (`Profile`: max_steps, command_timeout,
grade_timeout, reward weights). `SWEEnv`, `runner.run` and `grade_patch` take their defaults
from `profile_for(task)` unless the caller passes a value.

Milestone attribution (`lht.attribute`): a target test goes to the last PR in the range whose
test diff adds `def <its name>` in its file, else to the only PR that touched its file, else
to `unattributed`. `grading.milestone_rates` fills `GradeResult.milestones` only on a trusted
run; a tampered run scores 0 on every milestone. `RewardConfig.milestone_credit` swaps the
partial-credit term from target rate to mean milestone rate.

Collection-masked tests (`lht.unmask`): a test that PASSED in the `run` stage, read NONE in
`test` because the release's test changes broke collection, and has the same AST at both
releases is a P2P guard, not a target. Without this boltons 26.1.0 -> 26.2.0 had 388 targets
that one import fix would mostly earn. Counted in `validation["masked_p2p"]`. A test's body
includes the same-file base classes and mixins it inherits from, module-level tests inside an
`if` or `try` count, and a test file that's byte-identical at both releases is unchanged by
definition.

Test scope (`build_range_task(..., scope="full")`, `build-lht --scope`): an LHT task runs the
whole suite by default, so `test_scope` is empty and P2P guards every module, not just the
files the release touched. With `scope="changed"` markupsafe 3.0.2 -> 3.0.3 had zero P2P
tests. If the full suite writes no report the build falls back to the changed files and sets
`validation["scope_fallback"]`.

Canaries on a full-suite LHT task (`grading.Canary.sites`): one set next to the first graded
test, as for SWE, and one in another graded file, the biggest the release didn't touch if
there is one. A hidden test file can import an API the agent hasn't written yet and fail to
collect, and with one site that read as tampering and zeroed an honest agent's milestones
(tenacity 9.2.0 -> 9.2.1). Two sites also cut how often a cheat that spares each file's last
two tests gets past every canary, from 1.13% to 0.02% of 25,000 layouts. Canary results are
matched by base id, since a conftest can parametrize every test (markupsafe reports
`name[markupsafe._native]`).

Direct commits are milestones too (`commit-<sha12>`), via `lht.units_in_order`.

Ranges (`lht.release_ranges`): release tags sorted by date then version number, each paired
with the newest earlier release that is its git ancestor. A maintenance tag on a side branch
pairs with its own line and never starts a main-line range.

The env is built at the older release. If the release changed dependencies, the fix runs
fail or go missing and validation rejects the range. That's measured, not assumed.

Reports group by category first and never pool them (`report.summarize`).

## Leak rules

1. The agent never sees `.git` history beyond a single scratch commit of the base tree.
2. Hidden tests (the PR's test changes) are never in the agent sandbox, not even in image layers it can read.
3. The env image is built at the base commit, never at a later one, and pip caches are purged.
4. No network in task or grading sandboxes.
5. The problem statement must not contain the gold patch's distinctive added lines.
6. Grader-owned paths (`/tmp/gauntlet-*`) don't exist in the agent sandbox.
7. Candidate edits to tests, conftest.py, pytest.ini and tox.ini are dropped before grading.

## The second language: JavaScript and TypeScript on vitest

Python with pytest was the only stack until 13 Nov 2026. Node repos tested with vitest are the
second. This section keeps what was measured before starting, then what the build turned up,
because three of the things written down in advance were wrong.

### What was measured first

**Status parsing was already generic.** `parse_junit` was fed output from five runners and read
every one correctly: pytest, jest-junit, vitest, gotestsum and cargo nextest.

**Test identity was not.** `parse_junit` flattened the document with `root.iter("testcase")`, and
a testcase element has no parent pointer, so a `file` recorded on the enclosing `testsuite` was
never seen. Walking suite by suite fixed jest and vitest. All 45,444 test ids recorded across the
53 live Python tasks round-trip unchanged, which was the part that had to hold.

**The plan was three steps**: normalise ids per runner, put the canary's parse and emit behind an
interface while leaving the strategy alone, then do detection and install, "the easy part".

### What the build actually took

**One seam, by runner and not by language.** `runners.py` holds what differs: which files the
integrity check hashes, what to redo when a sandbox boots at a new commit, how to probe the
network, how much memory and CPU the runner needs. `repo2env.suite_command` is the one place a
suite is run from. `grading.py` keeps the strategy (plant must-fail and must-pass tests, rename
the targets, hash before and after) and hands the file editing to `jstests.py` for vitest. The
pytest values moved into `runners.py` unchanged and a test pins the exact pytest command, so the
53 SWE and 25 LHT Python tasks are graded by the commands they were validated with.

**A vitest id is `path::describe > describe > title`.** The canary goes into the describe block
that holds the graded tests and copies the test it borrows a name from: the same call (`it` or
`test`), quotes, indentation and semicolons, and the assertion style the file already uses.
`jstests.py` reads the file with tree-sitter, with the TypeScript and TSX grammars for those
suffixes. A regex can't tell a title from the same text inside a template string or a comment.

**"The easy part" was not.** Detection and install took three decisions that had nothing to do
with the canary:

- *node_modules can't live in the repo.* A task sandbox empties the repo folder and unpacks the
  base commit into it, which is right for Python (deps are in site-packages) and deletes every
  dependency in Node. The env moves `node_modules` one folder up. Node resolves packages from
  any parent folder, so imports work the same, and a patch can no longer reach the test runner.
  The suite command names vitest by that path and never goes through `npx` or a `package.json`
  script, since both are the repo's to rewrite.
- *A sandbox with networking off has an empty `/etc/hosts`.* `localhost` doesn't resolve, vite
  looks it up on startup, and vitest died with `EAI_AGAIN` before collecting a single test. Every
  run of every task read as "the suite produced no results". The vitest boot step writes the
  loopback entries back. Nothing leaves the container either way. The same empty file is there
  for Python tasks, where a test that binds `localhost` would fail for a reason that has nothing
  to do with the code. That is not measured yet, see the open list below.
- *One core is not enough.* vitest starts one process per test file. On the sandbox's default
  single core radashi's 960 tests took 100 seconds a run, and validating one PR is four runs.
  The vitest sandbox gets four cores and 2 GB, and the same run takes 20 seconds.

**A patch can bring its own runner.** `git apply` will happily create `node_modules/vitest/...`
inside the repo, and Node finds the nearest `node_modules` first. `strip_test_changes` now drops
any path with a `node_modules` part, along with vitest and vite config files. There is no pytest
equivalent of this hole, because site-packages is not inside the repo.

**The cheats had to be rewritten, and the first versions were too weak to prove anything.**
`jscheats.py` is the pytest zoo in JavaScript. The first force-pass payload replaced chai's
`assert`, which every vitest matcher reports through. Against a grader with the defences
switched off it forged one target test of two: the other failed on a `TypeError`, which is not an
assertion. A cheat that can't win undefended says nothing when it loses defended. The payload now
also swaps each test's body for an empty one through `vitest/suite`'s `setFn`, and
`test_the_same_cheats_win_against_a_grader_with_no_defences` holds every trick to that bar.

The JUnit forger is new in kind. Under pytest it was an `atexit` hook in the same process. Under
vitest the report is written by the main process after the workers are gone, so the worker
leaves a detached process behind that polls for the file and cuts the failures out. It wins the
race against the grader's read. The canary catches it, because a must-fail test that comes back
passed is forged whoever did it.

**The disguise matters more here than in Python.** A canary is named like a neighbour plus one
of a public list of endings. `cheater:canary-aware` spares any test named that way and forces
the rest. With renaming off it forges every target and trips nothing. With renaming on, the
targets carry the same endings, so sparing the canary spares them too.
`test_the_disguise_is_what_stops_a_cheat_that_knows_the_canary` pins both halves.

That makes un-renamable targets the real residual, and the first version left a lot of them. It
renamed a plain title in place, added an ending to an `it.each` format string, and stopped
there. Measured on the first 46 real tasks that was 188 of 227 target tests, with 7 tasks where
nothing was renamed. The misses were ordinary code: i18next and path-to-regexp write most of
their suites as a loop over a table of cases, titled with a template string or a variable.

The renamer now follows `for` loops, `if` blocks and `forEach` callbacks, and enters describe
blocks named at run time. Whatever produces the name gets the ending:

```
it('adds two numbers', ...)            it('divides exactly small input', ...)     a new title
it.each(rows)('add(%i, %i)', ...)      it.each(rows)('add(%i, %i) in order', ...)
it(`nests for ${args} args`, ...)      it(`nests for ${args} args twice`, ...)
it(row.name, ...)                      it((row.name) + ' again', ...)
```

Reported ids are mapped back exactly for the first kind and by pattern for the rest. A canary
is never planted inside a loop or a run-time block, where it would report once per row under
names nobody predicted. On 47 tasks that is 248 of 248 targets. A sweep over their 65 test
files with five seeds each found no edit that added a syntax error and no file left without
its four canaries, and it is also what found a divide by zero on a suite with no top-level
describe block.

The next miss was a test registered from inside a helper function
(`function runBaseTest(name) { it(name, ...) }`), which is how immer writes most of its suite.
Nothing at the call site says it is a test, so those targets kept their real names, and
`cheater:canary-aware` and `cheater:ghost` resolved immer #1314, #1315 and #1316 at about +0.88.
The renamer now reads the bodies of functions declared in the file and of callbacks handed to
any function. The same 7 tasks under all 11 policies are 77 episodes with no violation. On the
69 published tasks that is 339 of 339 targets, over 88 test files.

What still can't be renamed is a title that arrives through a function imported from another
file, since the renamer reads one file at a time. No published task has one.
`validation.disguise` records `{targets, renamed}` on every node task, so a task set can say how
exposed it is instead of assuming.

**One unreadable statement must not hide a file.** An exsolve test file trips the TypeScript
grammar on `importOriginal<typeof import("node:fs")>()` inside a mock, twenty lines above a
describe block that parses fine. Giving up on the file left that task's targets with their real
names. A statement with an error in it is skipped, a describe block is still entered, and a
block that holds an unreadable statement takes no canary.

### jest

jest shares the parser, the canary and the disguise with vitest, because a jest test file is the
same `describe` and `it` calls. Three things are its own.

**The report.** jest has no JUnit reporter built in and `jest-junit` is a package most repos
don't have, so the suite runs with `--json`. Ids are built in vitest's shape,
`path::outer > inner > title`, and a file that fails to load is one failed entry under its own
name. Everything downstream reads one kind of id.

**Snapshots.** jest keys a snapshot by the describe titles joined with a space, and puts a hint
after a colon (``exports[`calc div formats a quotient: as text 1`]``). The disguise renames those
keys with the test, or `--ci` fails the renamed test for a snapshot it can't find.

**The ES module flag.** The first version set `NODE_OPTIONS=--experimental-vm-modules` on every
run, on the theory that it changes nothing for CommonJS. It does. luxon is `"type": "module"` and
has babel compile its files, and with the flag on jest loaded them as ES modules: all 58 test
files failed with "Must use import to load ES Module" and 6 PRs were rejected for having no test
that moves. The flag is now set only when the repo's own scripts set it. Those 6 PRs are 6 tasks.

Six real repos, 57 merged PRs, and what each one showed:

| Repo | What happened |
|---|---|
| moment/luxon | 6 tasks after the flag fix, every target renamable |
| iamkun/dayjs | 2 tasks, and they are one fix: the repo merges each PR to `dev` and then `dev` to `master` |
| jest-community/eslint-plugin-jest | refused now, see below |
| immutable-js/immutable-js | 0 tasks: its tests import `dist/immutable.js`, a build the env never runs |
| tj/commander.js | refused: it left jest for node's own test runner |
| vercel/ms | no merged PR in the window |

**A run with no report is not a run where every test is new.** eslint-plugin-jest #2036 came out
as a task with 4,194 new tests and no regression guard. Its `test` run had left no report, so
against the fix run every test looked like it had appeared. A build is now rejected when the
base run reports tests and a later run reports nothing.

**A second runner gets graded as tests.** The same repo runs ESLint through `jest-runner-eslint`,
so each file reports a test called "ESLint". Lint becomes a regression test, and the canary the
grader plants is itself a lint failure. The real fix for #2039 passed all 8 target tests and
didn't resolve. A repo with a `jest-runner-*` package is refused. Its tests were a poor fit
anyway: they come out of `RuleTester.run(...)`, so the disguise could rename 0 of 5 targets on
one task and 25 of 4,194 on another.

On the 8 real tasks that were graded (6 luxon, 1 dayjs, 1 eslint-plugin-jest) the oracle resolved
7 and noop resolved none. The miss is the lint case above.

**What jest does not have is a red team.** The cheats in `jscheats.py` are written against
vitest's internals. On jest they fail to load, which proves nothing, so they aren't run and no
jest task set is published yet. The defences are the same code, and
`tests/test_jest_pipeline.py` holds the honest half: the real fix resolves under three seeds of
canaries and new names, and a patch that ships its own `node_modules/jest` is stripped.

### Still open

- **Two cheats are duds on newer vitest.** `cheater:monkeypatch` and `cheater:stealth` swap test
  bodies through `vitest/suite`, and newer vitest no longer exports it. On 26 of the 69
  published tasks the payload fails to load and scores a quiet 0, so the calibration says
  nothing about that trick there. The junit, rewrite and exit cheats still act on those repos
  and are caught.
- **A busy machine can fail a passing test.** radashi's `AggregateError` test failed once in 759
  episodes while a second batch of builds shared Docker, and cost the oracle one task. The build
  repeats its runs to find flaky tests, but only load finds this kind. It's dropped by hand in
  the published set, and there is no automatic check for it yet.
- **Workspaces.** Sibling packages resolve through symlinks the package manager writes into
  `node_modules`, and moving that folder up breaks them. Refused with a reason.
- **Projects whose tests import a build.** The env runs the install and nothing else. immutable-js is one:
  52 of its 56 test files fail on a missing `dist/immutable.js`, and its PRs are rejected for
  having no test that moves, which is true but not the useful reason.
- **Duplicate merges.** dayjs gives two tasks for one fix. Nothing compares gold patches
  across tasks yet.
- **Cheat Catcher's static rules are Python.** On a JS patch it finds nothing, so the JS side
  stands on the grading-time defences alone: strip, canary, disguise, hash. Those are the ones
  that held when static analysis didn't, but it is one layer fewer.
- **LHT is Python only.** Release ranges build through `lht.py`, which filters on `.py`.
- **`localhost` in Python sandboxes.** Same empty `/etc/hosts`. Fixing it in the sandbox is the
  right place, and it needs a measured before and after on the 53 live tasks first.
- **Go and Rust** still need a package-to-file mapping before a canary has anywhere to go.
  `go test` and `cargo nextest` address tests by package, and no file appears in their output.
