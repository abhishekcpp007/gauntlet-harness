"""What changes between the two task categories, kept in one place.

SWE and LHT tasks share the sandbox, the grader, the canary and the cheat checks. What they
don't share is the size of the job, so they don't share budgets, reward weights, or a
leaderboard either.

    swe   one merged PR. Short horizon: find the bug, fix it, submit. Graded pass/fail on the
          PR's hidden tests, with partial credit as a fallback. Similarity to the gold patch
          means something here because the gold patch is small.

    lht   a release range, X to Y. Dozens of PRs, hundreds of hidden tests, thousands of gold
          lines. A binary reward almost never fires on a task this big, so the test term is
          the mean pass rate over milestones (one per PR in the range). Similarity is off: a
          SequenceMatcher over a 5,000 line diff measures noise, and paying for it teaches a
          model to copy shape rather than behaviour.

The numbers are starting points I can defend, not tuned values. Anything here can be
overridden per run, and the episode's provenance records what was used.
"""

from __future__ import annotations

from pydantic import BaseModel

from .reward import RewardConfig
from .schema import HarnessTask


class Profile(BaseModel):
    category: str
    max_steps: int
    command_timeout: float  # one bash action inside the episode
    grade_timeout: float  # the hidden test run
    reward: RewardConfig


SWE = Profile(
    category="swe",
    max_steps=60,
    command_timeout=120,
    grade_timeout=900,
    reward=RewardConfig(),
)

LHT = Profile(
    category="lht",
    # A release range is tens of PRs. 60 steps is enough to read the statement and not much
    # more, so the budget scales with the work, and a single test run can take minutes.
    max_steps=400,
    command_timeout=600,
    grade_timeout=3600,
    reward=RewardConfig(w_tests=0.8, w_rubric=0.2, w_similarity=0.0, milestone_credit=True),
)

PROFILES: dict[str, Profile] = {"swe": SWE, "lht": LHT}


def profile_for(task: HarnessTask) -> Profile:
    return PROFILES[task.category]
