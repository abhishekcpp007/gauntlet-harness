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

## Leak rules

1. The agent never sees `.git` history beyond a single scratch commit of the base tree.
2. Hidden tests (the PR's test changes) are never in the agent sandbox, not even in image layers it can read.
3. The env image is built at the base commit, never at a later one, and pip caches are purged.
4. No network in task or grading sandboxes.
5. The problem statement must not contain the gold patch's distinctive added lines.
6. Grader-owned paths (`/tmp/gauntlet-*`) don't exist in the agent sandbox.
7. Candidate edits to tests, conftest.py, pytest.ini and tox.ini are dropped before grading.
