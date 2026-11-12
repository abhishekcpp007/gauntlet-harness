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
that one import fix would mostly earn. Counted in `validation["masked_p2p"]`.

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

## What a second language would actually take

Everything here is Python and pytest. `detect.py` already carries a `Stack.language` with markers
for node, go, rust, java and ruby, and `build_env` raises `UnsupportedRepo` for all of them. This
section is what I measured rather than what I assumed, because I twice wrote down the wrong
summary of it.

**Status parsing is already generic.** `parse_junit` was fed output from five runners and read
every one correctly:

| runner | cases | statuses |
|---|---|---|
| pytest | 2 | passed, failed |
| jest-junit | 2 | passed, failed |
| vitest `--reporter=junit` | 2 | passed, failed |
| go, gotestsum `--junitfile` | 2 | passed, failed |
| cargo nextest, junit profile | 2 | passed, failed |

**Test identity was not, and half of it is now fixed.** `parse_junit` flattened the document
with `root.iter("testcase")`, and a testcase element has no parent pointer, so a `file` recorded
on the enclosing `testsuite` was never seen. jest-junit writes it there and vitest uses the
suite's name for it. Walking suite by suite instead:

```
                before                                  after
pytest          tests/test_calc.py::test_add            unchanged
vitest          src/calc.test.ts::adds numbers          unchanged
jest-junit      calc adds numbers::calc adds numbers    src/__tests__/calc.test.js::calc adds numbers
go              example.com/calc::TestAdd               unchanged, and correctly so
cargo-nextest   calc::tests::adds                       unchanged, and correctly so
```

All 45,444 test ids recorded across the 53 live tasks round-trip unchanged, which was the part
that had to hold.

Go and Rust are not an oversight. `go test` and `cargo nextest` address tests by package and
path within it, and there is no file anywhere in their output. A fallback that invented one
would be a lie, so either language needs a real mapping from package to source file before its
tasks can be graded. That is still open.

Why any of this matters:

- `f2p` and `p2p` are lists of test ids, so an id that is unstable or carries no file breaks task
  matching across runs
- `_test_file(target)` splits an id on `::` to find the file to plant the canary in. With the jest
  shape there is no file in the id at all, so there is nowhere to plant

So the blocker is not result handling and not the canary strategy. It is **per-runner id
normalisation**, and it has to come first.

**The canary generalises further than I claimed.** Its strategy is language-agnostic: plant some
tests that must fail and some that must pass, scatter them among the file's own tests, name them
like their neighbours, then check afterwards that the must-fail ones failed. Only three things are
Python-bound, and each is small: parsing the file to find test functions and classes
(`ast.parse`), emitting a body, and the id format. `Disguise`, which renames the graded tests
during grading, is in the same shape.

**The order to do it in.**

1. Normalise test ids per runner, with the probe above as the test. **Done for pytest, jest and
   vitest.** Still open for Go and Rust, which need a package-to-file mapping rather than a
   fallback. Nothing else can be trusted until an id means the same thing twice.
2. Put the canary's emit and parse behind an interface, leaving the strategy where it is. Do this
   *with* the second language, not before it: a refactor with no second caller is speculative, and
   this is the component least worth breaking on a guess.
3. Then `detect.py` and `install_script` for the language, which is the easy part and the part
   that looks like the whole job from the outside.

Shipping an adapter without step 2 would mean a language whose tasks have no canary, which is
weaker than not supporting it at all. Anyone running the harness would reasonably assume the
anti-cheat applies everywhere.
