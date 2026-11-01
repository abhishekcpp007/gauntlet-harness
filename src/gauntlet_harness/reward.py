"""Turn one graded episode into a scalar reward for RL.

    malformed (empty patch, or it didn't apply)   -> malformed_penalty           (SWE-RL, -1)
    cheat catcher verdict == "cheating"           -> cheat_penalty               (no credit for gaming)
    otherwise:
        tests      = 1.0 if resolved
                     else target_rate * p2p_rate   (partial credit, if enabled; else 0)
        rubric     = RubricScore.score             (Rubrics as Rewards, Gunjal et al. 2025)
        similarity = SequenceMatcher(candidate, gold) on source diffs (SWE-RL, Wei et al. 2025)
        reward     = w_tests * tests + w_rubric * rubric + w_similarity * similarity

When no rubric score is available its weight is spread over the other two terms in
proportion, so the reward stays on the same [0, 1] scale.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .rubric import similarity
from .schema import GradeResult, RubricScore


class RewardConfig(BaseModel):
    w_tests: float = Field(0.6, ge=0)
    w_rubric: float = Field(0.25, ge=0)
    w_similarity: float = Field(0.15, ge=0)
    partial_credit: bool = True
    cheat_penalty: float = -1.0
    malformed_penalty: float = -1.0


def compute_reward(
    grade: GradeResult | None,
    rubric: RubricScore | None,
    cheat_verdict: str | None,
    patch: str,
    gold_patch: str,
    cfg: RewardConfig | None = None,
    *,
    task: Any = None,
) -> tuple[float, dict[str, float]]:
    """Returns (reward, breakdown). `task` is only needed to keep test edits out of similarity."""
    cfg = cfg or RewardConfig()
    breakdown: dict[str, float] = {"tests": 0.0, "rubric": 0.0, "similarity": 0.0, "penalty": 0.0}

    if not patch.strip() or (grade is not None and not grade.patch_applied):
        breakdown["penalty"] = cfg.malformed_penalty
        return cfg.malformed_penalty, breakdown
    if cheat_verdict == "cheating":
        breakdown["penalty"] = cfg.cheat_penalty
        return cfg.cheat_penalty, breakdown

    if grade is not None:
        if grade.resolved:
            tests = 1.0
        elif cfg.partial_credit:
            p2p = grade.rate("p2p")
            tests = grade.target_rate * (1.0 if p2p is None else p2p)
        else:
            tests = 0.0
    else:
        tests = 0.0
    breakdown["tests"] = tests

    if task is not None:
        sim = similarity(patch, gold_patch, task)
    else:
        from difflib import SequenceMatcher

        sim = SequenceMatcher(None, patch, gold_patch, autojunk=False).ratio() if gold_patch else 0.0
    breakdown["similarity"] = sim

    weights = {"tests": cfg.w_tests, "rubric": cfg.w_rubric, "similarity": cfg.w_similarity}
    if rubric is None:
        weights.pop("rubric")
    else:
        breakdown["rubric"] = rubric.score
    total_w = sum(weights.values()) or 1.0
    full_w = cfg.w_tests + cfg.w_rubric + cfg.w_similarity or 1.0
    scale = full_w / total_w  # spread a missing rubric's weight over the rest

    reward = sum(w * scale * breakdown[k] for k, w in weights.items())
    for k, w in weights.items():
        breakdown[f"w_{k}"] = w * scale
    return reward, breakdown
