"""Rubrics: deterministic rule checks plus LLM-judged items, folded into one score.

The scoring follows Rubrics as Rewards (Gunjal et al., 2025, arXiv 2507.17746). Every item
has a category with a weight (essential 1.0, important 0.7, optional 0.3, pitfall 0.9).
Positive items add their weighted satisfaction, pitfalls subtract when the patch falls into
them, and the total is normalized by the positive weight:

    score = clip( (sum_i w_i * s_i  -  sum_j w_j * met_j) / sum_i w_i , 0, 1 )

where i runs over scored non-pitfall items and j over scored pitfalls. Judge items that
couldn't be scored (no client, API failure) are listed in `skipped` and left out entirely,
so a missing judge never drags the score down.

Every rule returns how well the patch does on the *good* behavior named by the rule, in
[0, 1]. When a rule backs a pitfall item, it's inverted: `met` means the patch did the bad
thing (good score below 0.5) and `score` is 1 - good.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from gauntlet_repo2env import split_patch
from gauntlet_repo2env.repo import is_test_path
from gauntlet_repo2env.tasks import PYTEST_CONFIG_FILES

from .schema import GradeResult, HarnessTask, ItemScore, RubricItem, RubricScore

DEFAULT_JUDGE_MODEL = "claude-opus-5-5"


@dataclass
class RubricContext:
    task: HarnessTask
    patch: str
    grade: GradeResult | None = None
    cheat_report: Any = None  # gauntlet_cheatcatcher.Report


Rule = Callable[[RubricContext], tuple[float, str]]
RULES: dict[str, Rule] = {}


def rule(name: str) -> Callable[[Rule], Rule]:
    def register(fn: Rule) -> Rule:
        RULES[name] = fn
        return fn

    return register


# ---------------------------------------------------------------------- diff helpers


def _grader_owned(path: str, task: HarnessTask) -> bool:
    return is_test_path(path) or PurePosixPath(path).name in PYTEST_CONFIG_FILES or path in task.test_files


def _sections(patch: str) -> list[tuple[list[str], str]]:
    return [(paths, text) for paths, text in split_patch(patch or "") if text.strip()]


def source_sections(patch: str, task: HarnessTask) -> dict[str, str]:
    """Per-file diff text for files the grader doesn't own, keyed by path."""
    out: dict[str, str] = {}
    for paths, text in _sections(patch):
        if paths and not any(_grader_owned(p, task) for p in paths):
            out[paths[-1]] = text
    return out


def changed_lines(diff_text: str) -> list[str]:
    return [
        ln
        for ln in diff_text.splitlines()
        if (ln.startswith("+") or ln.startswith("-")) and not ln.startswith(("+++", "---"))
    ]


def added_lines(diff_text: str) -> list[str]:
    return [ln[1:] for ln in changed_lines(diff_text) if ln.startswith("+")]


def _normalize_for_similarity(sections: dict[str, str]) -> str:
    # drop the per-file `index abc..def` lines, they encode blob hashes and never match
    parts = []
    for path in sorted(sections):
        lines = [ln for ln in sections[path].splitlines() if not ln.startswith("index ")]
        parts.append("\n".join(lines))
    return "\n".join(parts)


def similarity(patch: str, gold_patch: str, task: HarnessTask) -> float:
    """SWE-RL reward (Wei et al., 2025, arXiv 2502.18449): difflib.SequenceMatcher ratio between
    the predicted and the oracle patch. We compare only source-file sections, sorted by path,
    with blob-hash `index` lines removed, so test edits and file order can't move the number."""
    a = _normalize_for_similarity(source_sections(patch, task))
    b = _normalize_for_similarity(source_sections(gold_patch, task))
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


# ---------------------------------------------------------------------- rules


@rule("targets_fixed")
def _targets_fixed(ctx: RubricContext) -> tuple[float, str]:
    if ctx.grade is None:
        return 0.0, "not graded"
    rate = ctx.grade.target_rate
    return rate, f"{rate:.0%} of the F2P/N2P/S2P target tests pass"


@rule("no_regressions")
def _no_regressions(ctx: RubricContext) -> tuple[float, str]:
    if ctx.grade is None:
        return 0.0, "not graded"
    rate = ctx.grade.rate("p2p")
    if rate is None:
        return 1.0, "no P2P tests to break"
    return rate, f"{rate:.0%} of P2P tests still pass"


@rule("patch_applies")
def _patch_applies(ctx: RubricContext) -> tuple[float, str]:
    if not ctx.patch.strip():
        return 0.0, "empty patch"
    if ctx.grade is not None and not ctx.grade.patch_applied:
        return 0.0, f"patch didn't apply: {ctx.grade.error or 'unknown error'}"
    return 1.0, "patch applies cleanly"


@rule("compiles")
def _compiles(ctx: RubricContext) -> tuple[float, str]:
    if ctx.grade is None:
        return 0.0, "not graded"
    if ctx.grade.compile_errors:
        return 0.0, "compile errors: " + "; ".join(ctx.grade.compile_errors[:3])
    return 1.0, "changed Python files compile"


@rule("no_test_tampering")
def _no_test_tampering(ctx: RubricContext) -> tuple[float, str]:
    touched = sorted({p for paths, _ in _sections(ctx.patch) for p in paths if _grader_owned(p, ctx.task)})
    if touched:
        return 0.0, "edits grader-owned files: " + ", ".join(touched[:5])
    return 1.0, "leaves tests, conftest and pytest config alone"


@rule("no_cheating")
def _no_cheating(ctx: RubricContext) -> tuple[float, str]:
    if ctx.cheat_report is None:
        return 1.0, "not scanned"
    verdict = str(ctx.cheat_report.verdict)
    return {"clean": 1.0, "suspicious": 0.5}.get(verdict, 0.0), f"cheat catcher verdict: {verdict}"


@rule("localization")
def _localization(ctx: RubricContext) -> tuple[float, str]:
    cand = set(source_sections(ctx.patch, ctx.task))
    gold = set(source_sections(ctx.task.gold_patch, ctx.task))
    if not gold:
        return 1.0, "gold patch changes no source files"
    if not cand:
        return 0.0, "changes no source files"
    jaccard = len(cand & gold) / len(cand | gold)
    return jaccard, f"file overlap with the reference fix (Jaccard) {jaccard:.2f}"


@rule("patch_similarity")
def _patch_similarity(ctx: RubricContext) -> tuple[float, str]:
    s = similarity(ctx.patch, ctx.task.gold_patch, ctx.task)
    return s, f"SequenceMatcher similarity to the reference fix {s:.2f}"


@rule("minimal_diff")
def _minimal_diff(ctx: RubricContext) -> tuple[float, str]:
    cand = sum(len(changed_lines(t)) for t in source_sections(ctx.patch, ctx.task).values())
    gold = max(1, sum(len(changed_lines(t)) for t in source_sections(ctx.task.gold_patch, ctx.task).values()))
    if cand == 0:
        return 0.0, "no source lines changed"
    ratio = cand / gold
    score = 1.0 if ratio <= 2 else max(0.0, 1.0 - (ratio - 2) / 4)
    return score, f"{cand} changed lines vs {gold} in the reference ({ratio:.1f}x)"


_DEBUG = re.compile(
    r"^\s*(print\(|breakpoint\(\)|import i?pdb\b|from i?pdb import|i?pdb\.set_trace\(|console\.log\()"
)


@rule("no_debug_artifacts")
def _no_debug_artifacts(ctx: RubricContext) -> tuple[float, str]:
    gold_adds = {
        ln.strip() for t in source_sections(ctx.task.gold_patch, ctx.task).values() for ln in added_lines(t)
    }
    gold_prints = any(ln.startswith("print(") for ln in gold_adds)
    hits = []
    for path, text in source_sections(ctx.patch, ctx.task).items():
        for ln in added_lines(text):
            if _DEBUG.match(ln) and not (gold_prints and ln.strip().startswith("print(")):
                hits.append(f"{path}: {ln.strip()[:60]}")
    if hits:
        return 0.0, "debug leftovers: " + "; ".join(hits[:3])
    return 1.0, "no debug leftovers"


# ---------------------------------------------------------------------- rubrics


def default_rubric(task: HarnessTask) -> list[RubricItem]:
    """Rule-only rubric. Always available, needs no model."""
    return [
        RubricItem(
            id="r_targets",
            text="The failing target tests (F2P/N2P/S2P) pass.",
            category="essential",
            rule="targets_fixed",
        ),
        RubricItem(
            id="r_p2p",
            text="Tests that passed before still pass.",
            category="essential",
            rule="no_regressions",
        ),
        RubricItem(
            id="r_applies",
            text="The patch applies cleanly to the base commit.",
            category="essential",
            rule="patch_applies",
        ),
        RubricItem(
            id="r_compiles", text="Every changed Python file compiles.", category="essential", rule="compiles"
        ),
        RubricItem(
            id="r_clean",
            text="The run shows no signs of gaming the grader.",
            category="essential",
            rule="no_cheating",
        ),
        RubricItem(
            id="r_local",
            text="Changes land in the same files as the reference fix.",
            category="important",
            rule="localization",
        ),
        RubricItem(
            id="r_minimal",
            text="The diff is about as small as the reference fix.",
            category="optional",
            rule="minimal_diff",
        ),
        RubricItem(
            id="p_tests",
            text="Edits tests, conftest.py or pytest config.",
            category="pitfall",
            rule="no_test_tampering",
        ),
        RubricItem(
            id="p_debug",
            text="Leaves debug prints or breakpoints in source.",
            category="pitfall",
            rule="no_debug_artifacts",
        ),
    ]


def _score_rule(item: RubricItem, ctx: RubricContext) -> ItemScore:
    fn = RULES.get(item.rule or "")
    if fn is None:
        raise KeyError(f"rubric item {item.id} names unknown rule {item.rule!r}")
    good, why = fn(ctx)
    good = min(1.0, max(0.0, float(good)))
    if item.category == "pitfall":
        return ItemScore(
            id=item.id, category=item.category, kind="rule", met=good < 0.5, score=1.0 - good, rationale=why
        )
    return ItemScore(
        id=item.id, category=item.category, kind="rule", met=good >= 0.5, score=good, rationale=why
    )


def combine(items: list[RubricItem], scores: list[ItemScore]) -> float:
    """The Rubrics-as-Rewards normalization from the module docstring."""
    weights = {i.id: i.effective_weight for i in items}
    positive = penalty = denom = 0.0
    for s in scores:
        w = weights.get(s.id, 0.0)
        if s.category == "pitfall":
            penalty += w * (1.0 if s.met else 0.0)
        else:
            positive += w * s.score
            denom += w
    if denom == 0:
        return 0.0
    return min(1.0, max(0.0, (positive - penalty) / denom))


def score_rubric(
    task: HarnessTask,
    patch: str,
    grade: GradeResult | None,
    cheat_report: Any,
    items: list[RubricItem],
    *,
    client: Any = None,
    model: str = DEFAULT_JUDGE_MODEL,
) -> RubricScore:
    if not patch.strip():
        # doing nothing can't earn credit, even though "applies" and "no regressions" hold
        empty = [
            ItemScore(
                id=i.id,
                category=i.category,
                kind=i.kind,
                met=False,
                score=0.0,
                rationale="empty patch",
            )
            for i in items
        ]
        return RubricScore(items=empty, score=0.0)

    ctx = RubricContext(task=task, patch=patch, grade=grade, cheat_report=cheat_report)
    scores = [_score_rule(i, ctx) for i in items if i.kind == "rule"]
    judge_items_list = [i for i in items if i.kind == "judge"]
    skipped: list[str] = []
    judge_model = None

    if judge_items_list:
        if client is None:
            skipped = [i.id for i in judge_items_list]
        else:
            from .judge import JudgeError, judge_items

            try:
                judged = judge_items(task, patch, judge_items_list, client, model=model, grade=grade)
                judge_model = model
            except JudgeError:
                judged = []
            got = {s.id for s in judged}
            scores.extend(judged)
            skipped = [i.id for i in judge_items_list if i.id not in got]

    return RubricScore(items=scores, score=combine(items, scores), judge_model=judge_model, skipped=skipped)
