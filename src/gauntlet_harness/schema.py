"""Data shapes shared by every part of the harness.

Test statuses and transition names follow Multi-SWE-bench (Zan et al., 2025): each test is
observed in three runs, `run` (base commit), `test` (base + test patch) and `fix`
(base + test patch + gold patch). A test is graded on how it moves from `test` to `fix`:

    f2p  FAILED  -> PASSED   the fix makes a failing test pass
    n2p  NONE    -> PASSED   the test didn't run before (new, or its module failed to import)
    s2p  SKIPPED -> PASSED   the fix un-skips it
    p2p  PASSED  -> PASSED   regression guard
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Status(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"  # pytest failures and errors both land here
    SKIPPED = "SKIPPED"
    NONE = "NONE"  # not in the results at all


TARGET_KINDS = ("f2p", "n2p", "s2p")
ALL_KINDS = (*TARGET_KINDS, "p2p")

RubricCategory = Literal["essential", "important", "optional", "pitfall"]

# Rubrics as Rewards (Gunjal et al., 2025) style weights. A pitfall is something a good
# patch must NOT do, so it only ever subtracts.
CATEGORY_WEIGHTS: dict[str, float] = {"essential": 1.0, "important": 0.7, "optional": 0.3, "pitfall": 0.9}


class RubricItem(BaseModel):
    id: str
    text: str
    category: RubricCategory = "important"
    kind: Literal["rule", "judge"] = "rule"
    rule: str | None = None  # registered rule name when kind == "rule"
    weight: float | None = None  # overrides CATEGORY_WEIGHTS[category]

    @property
    def effective_weight(self) -> float:
        return self.weight if self.weight is not None else CATEGORY_WEIGHTS[self.category]


class PRInfo(BaseModel):
    repo: str  # owner/name
    number: int
    title: str
    body: str = ""
    url: str
    merged_at: str | None = None  # ISO 8601
    base_sha: str | None = None
    merge_commit_sha: str | None = None
    linked_issues: list[int] = Field(default_factory=list)
    issue_texts: dict[int, str] = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)
    source: Literal["api", "git"] = "api"


class HarnessTask(BaseModel):
    format: str = "gauntlet.harness.task/v1"
    instance_id: str  # owner__name-<pr number>
    repo: str  # clone URL
    pr: PRInfo
    base_commit: str
    solution_commit: str
    problem_statement: str
    statement_source: Literal["issue", "pr"]
    image: str
    workdir: str = "/workspace/repo"
    env_fingerprint: str
    gold_patch: str
    test_patch: str
    test_files: list[str]
    test_scope: list[str] = Field(default_factory=list)  # pytest targets, empty means the full suite
    f2p: list[str] = Field(default_factory=list)
    n2p: list[str] = Field(default_factory=list)
    s2p: list[str] = Field(default_factory=list)
    p2p: list[str] = Field(default_factory=list)
    flaky: list[str] = Field(default_factory=list)  # dropped from grading
    rubric: list[RubricItem] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)

    @property
    def targets(self) -> list[str]:
        return [*self.f2p, *self.n2p, *self.s2p]

    def to_repo2env(self) -> Any:
        """The same task in repo2env's shape, so its open_task / extract_patch / strip helpers work."""
        from gauntlet_repo2env import Task

        return Task(
            instance_id=self.instance_id,
            repo=self.repo,
            image=self.image,
            workdir=self.workdir,
            base_commit=self.base_commit,
            solution_commit=self.solution_commit,
            problem_statement=self.problem_statement,
            gold_patch=self.gold_patch,
            test_patch=self.test_patch,
            test_files=self.test_files,
            fail_to_pass=self.targets,
            pass_to_pass=self.p2p,
        )


class Rejected(BaseModel):
    """A PR that didn't make a valid task, and why. Kept so the funnel can be reported."""

    instance_id: str
    stage: str  # collect, filter, env, statement, runs, transitions, leak
    reason: str
    detail: dict[str, Any] = Field(default_factory=dict)


class GradeResult(BaseModel):
    instance_id: str
    patch_applied: bool
    ignored_files: list[str] = Field(default_factory=list)
    compile_errors: list[str] = Field(default_factory=list)
    statuses: dict[str, str] = Field(default_factory=dict)  # test id -> Status value after the patch
    passed: dict[str, dict[str, bool]] = Field(default_factory=dict)  # kind -> test id -> passed
    resolved: bool = False
    tampered: list[str] = Field(default_factory=list)  # canary or integrity failures; never resolved
    error: str | None = None

    def rate(self, kind: str) -> float | None:
        tests = self.passed.get(kind) or {}
        return sum(tests.values()) / len(tests) if tests else None

    def rates(self) -> dict[str, float | None]:
        return {k: self.rate(k) for k in ALL_KINDS}

    @property
    def target_rate(self) -> float:
        hits = [ok for k in TARGET_KINDS for ok in (self.passed.get(k) or {}).values()]
        return sum(hits) / len(hits) if hits else 0.0


class ItemScore(BaseModel):
    id: str
    category: RubricCategory
    kind: Literal["rule", "judge"]
    met: bool  # for a pitfall, met means the patch fell into it
    score: float = Field(ge=0.0, le=1.0)  # graded satisfaction, 1.0 = fully met
    rationale: str = ""


class RubricScore(BaseModel):
    items: list[ItemScore] = Field(default_factory=list)
    score: float = 0.0  # normalized to [0, 1]
    judge_model: str | None = None
    skipped: list[str] = Field(default_factory=list)  # judge items we couldn't score (no client)


class Provenance(BaseModel):
    """What produced a number, so two numbers can be compared honestly.

    A results file says a policy scored 0.42. Whether that is comparable to another 0.42
    depends on the harness that graded it, the image the task ran in, and what was asked of
    the model. None of that used to be written down, so a run from last month and a run from
    today read as the same measurement even when they were not.
    """

    harness_version: str = ""
    policy_params: dict[str, Any] = Field(default_factory=dict)
    image: str = ""
    env_fingerprint: str = ""
    grading_seed: str | None = None  # set means the grading randomness was pinned
    ran_at: str = ""

    def key(self) -> tuple:
        """The parts that have to match for two episodes to be measuring the same thing."""
        return (
            self.harness_version,
            self.image,
            self.env_fingerprint,
            tuple(sorted(self.policy_params.items(), key=lambda kv: kv[0])),
        )


class EpisodeResult(BaseModel):
    format: str = "gauntlet.harness.episode/v1"
    episode_id: str
    instance_id: str
    policy: str  # e.g. "claude:claude-opus-5-5", "oracle"
    attempt: int = 0
    steps: int = 0
    seconds: float = 0.0
    usage: dict[str, int] = Field(default_factory=dict)
    patch: str = ""
    grade: GradeResult | None = None
    rubric: RubricScore | None = None
    cheat_verdict: str | None = None
    cheat_findings: list[dict[str, Any]] = Field(default_factory=list)
    reward: float = 0.0
    reward_breakdown: dict[str, float] = Field(default_factory=dict)
    truncated: bool = False
    trajectory_path: str | None = None
    rl_path: str | None = None
    merged_at: str | None = None
    provenance: Provenance | None = None
    error: str | None = None
