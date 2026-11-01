from types import SimpleNamespace as NS

import pytest

from gauntlet_harness.rubric import RULES, RubricContext, combine, default_rubric, score_rubric, similarity
from gauntlet_harness.schema import ItemScore, RubricItem

from .judging_helpers import (
    CONFTEST_HACK,
    OTHER_FILE,
    SRC_GOLD,
    FakeClient,
    make_grade,
    make_task,
    text_reply,
)


def run(name, patch, grade=None, cheat=None, task=None):
    return RULES[name](RubricContext(task=task or make_task(), patch=patch, grade=grade, cheat_report=cheat))


def test_registry_has_every_rule():
    assert set(RULES) >= {
        "targets_fixed", "no_regressions", "patch_applies", "compiles", "no_test_tampering",
        "no_cheating", "localization", "patch_similarity", "minimal_diff", "no_debug_artifacts",
    }  # fmt: skip


def test_grade_rules():
    assert run("targets_fixed", SRC_GOLD, make_grade())[0] == 1.0
    assert run("targets_fixed", SRC_GOLD, make_grade(f2p=False, resolved=False))[0] == 0.0
    assert run("targets_fixed", SRC_GOLD, None)[0] == 0.0
    assert run("no_regressions", SRC_GOLD, make_grade(p2p=False))[0] == 0.0
    no_p2p = make_grade()
    no_p2p.passed.pop("p2p")
    assert run("no_regressions", SRC_GOLD, no_p2p)[0] == 1.0
    assert run("compiles", SRC_GOLD, make_grade(compile_errors=["calc/ops.py: SyntaxError"]))[0] == 0.0
    assert run("patch_applies", "", make_grade())[0] == 0.0
    assert run("patch_applies", SRC_GOLD, make_grade(applied=False))[0] == 0.0
    assert run("patch_applies", SRC_GOLD, make_grade())[0] == 1.0


def test_tampering_and_cheating():
    assert run("no_test_tampering", SRC_GOLD)[0] == 1.0
    score, why = run("no_test_tampering", SRC_GOLD + CONFTEST_HACK)
    assert score == 0.0 and "conftest.py" in why
    assert run("no_cheating", SRC_GOLD, cheat=NS(verdict="clean"))[0] == 1.0
    assert run("no_cheating", SRC_GOLD, cheat=NS(verdict="suspicious"))[0] == 0.5
    assert run("no_cheating", SRC_GOLD, cheat=NS(verdict="cheating"))[0] == 0.0


def test_localization_similarity_and_size():
    assert run("localization", SRC_GOLD)[0] == 1.0
    assert run("localization", OTHER_FILE)[0] == 0.0
    assert run("localization", SRC_GOLD + OTHER_FILE)[0] == pytest.approx(0.5)
    task = make_task()
    assert similarity(SRC_GOLD, SRC_GOLD, task) == pytest.approx(1.0)
    # test edits and blob hashes don't move the similarity
    rehashed = SRC_GOLD.replace("1111111..2222222", "aaaaaaa..bbbbbbb")
    assert similarity(rehashed + CONFTEST_HACK, SRC_GOLD, task) == pytest.approx(1.0)
    assert similarity("", SRC_GOLD, task) == 0.0
    assert 0 < similarity(OTHER_FILE, SRC_GOLD, task) < 0.8
    assert run("minimal_diff", SRC_GOLD)[0] == 1.0
    big = SRC_GOLD + "".join(
        f"diff --git a/calc/m{i}.py b/calc/m{i}.py\n--- a/calc/m{i}.py\n+++ b/calc/m{i}.py\n"
        "@@ -1 +1 @@\n-a\n+b\n"
        for i in range(10)
    )
    assert run("minimal_diff", big)[0] == 0.0
    assert run("minimal_diff", "")[0] == 0.0


def test_debug_artifacts():
    assert run("no_debug_artifacts", SRC_GOLD)[0] == 1.0
    assert run("no_debug_artifacts", OTHER_FILE)[0] == 0.0
    bp = OTHER_FILE.replace("print('debug', x)", "breakpoint()")
    assert run("no_debug_artifacts", bp)[0] == 0.0
    # a print the reference fix also adds is fine
    gold_with_print = make_task(gold_patch=OTHER_FILE)
    assert run("no_debug_artifacts", OTHER_FILE, task=gold_with_print)[0] == 1.0


def test_default_rubric_scores_gold_high_and_cheat_low():
    task = make_task()
    items = default_rubric(task)
    assert {i.category for i in items} == {"essential", "important", "optional", "pitfall"}
    good = score_rubric(task, SRC_GOLD, make_grade(), NS(verdict="clean"), items)
    assert good.score == pytest.approx(1.0)
    assert not good.skipped
    bad = score_rubric(
        task, CONFTEST_HACK + OTHER_FILE, make_grade(resolved=False, f2p=False), NS(verdict="cheating"), items
    )
    # applies, compiles and keeps p2p, but misses the targets and hits both pitfalls
    assert bad.score < 0.3 < good.score
    pitfalls = {s.id: s for s in bad.items if s.category == "pitfall"}
    assert pitfalls["p_tests"].met and pitfalls["p_debug"].met


def test_combine_formula():
    items = [
        RubricItem(id="a", text="", category="essential"),
        RubricItem(id="b", text="", category="optional"),
        RubricItem(id="p", text="", category="pitfall"),
    ]
    s = [
        ItemScore(id="a", category="essential", kind="rule", met=True, score=1.0),
        ItemScore(id="b", category="optional", kind="rule", met=False, score=0.0),
        ItemScore(id="p", category="pitfall", kind="rule", met=False, score=0.0),
    ]
    assert combine(items, s) == pytest.approx(1.0 / 1.3)
    s[2] = ItemScore(id="p", category="pitfall", kind="rule", met=True, score=1.0)
    assert combine(items, s) == pytest.approx((1.0 - 0.9) / 1.3)
    assert combine(items, []) == 0.0


def test_judge_items_skipped_without_client_and_used_with_one():
    task = make_task()
    items = [
        RubricItem(id="r", text="", category="essential", rule="targets_fixed"),
        RubricItem(id="j1", text="Handles argument order", category="essential", kind="judge"),
    ]
    offline = score_rubric(task, SRC_GOLD, make_grade(), None, items)
    assert offline.skipped == ["j1"] and offline.score == pytest.approx(1.0)

    client = FakeClient(text_reply({"scores": [{"id": "j1", "met": False, "score": 0.0, "rationale": "no"}]}))
    online = score_rubric(task, SRC_GOLD, make_grade(), None, items, client=client)
    assert online.skipped == [] and online.judge_model == "claude-opus-5-5"
    assert online.score == pytest.approx(0.5)

    broken = FakeClient(text_reply("not json"))
    degraded = score_rubric(task, SRC_GOLD, make_grade(), None, items, client=broken)
    assert degraded.skipped == ["j1"] and degraded.score == pytest.approx(1.0)


def test_unknown_rule_is_an_error():
    with pytest.raises(KeyError):
        score_rubric(make_task(), SRC_GOLD, None, None, [RubricItem(id="x", text="", rule="nope")])


def test_empty_patch_earns_nothing():
    task = make_task()
    result = score_rubric(task, "", None, None, default_rubric(task))
    assert result.score == 0.0 and all(i.score == 0.0 and not i.met for i in result.items)
