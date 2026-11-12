import pytest

from gauntlet_harness.build import problem_statement, sanitize
from gauntlet_harness.leakcheck import distinctive_added_lines, new_test_names, statement_leaks
from gauntlet_harness.schema import PRInfo
from gauntlet_harness.schema import Status as S
from gauntlet_harness.testing import classify

P, F, K = S.PASSED, S.FAILED, S.SKIPPED


def test_classify_all_kinds():
    run = {"a": P, "b": F, "s": K}
    test = {"a": P, "b": F, "s": K, "x": F}  # test module for n2p didn't even import
    fix = {"a": P, "b": P, "s": P, "n": P, "x_gone": P}
    t = classify(run, test, [fix, dict(fix)])
    assert t.ok
    assert t.p2p == ["a"] and t.f2p == ["b"] and t.s2p == ["s"]
    assert t.n2p == ["n", "x_gone"]
    assert t.targets == ["b", "n", "x_gone", "s"]


def test_regression_rejects():
    t = classify({"a": P}, {"a": P, "b": F}, [{"a": F, "b": P}])
    assert not t.ok and t.regressions == ["a"]
    assert "breaks 1 passing test" in t.rejects[0]


def test_abnormal_passed_none_failed_rejects():
    # passed on base, vanished with the test patch, fails after the fix
    t = classify({"a": P}, {}, [{"a": F, "b": P}])
    assert not t.ok and t.regressions == ["a"]


def test_preexisting_failures_are_ignored():
    t = classify({"old": F}, {"old": F, "b": F}, [{"old": F, "b": P}])
    assert t.ok and t.f2p == ["b"] and t.regressions == []


def test_no_target_rejects():
    t = classify({"a": P}, {"a": P}, [{"a": P}])
    assert not t.ok and "no test goes" in t.rejects[0]
    t = classify({}, {}, [{}])
    assert "no test results" in t.rejects[0]


def test_flaky_tests_are_dropped():
    t = classify({}, {"a": F, "b": F, "c": P}, [{"a": P, "b": P, "c": P}, {"a": P, "b": F, "c": F}])
    assert t.flaky == ["b", "c"]
    assert t.f2p == ["a"] and t.p2p == []
    # a flaky regression doesn't reject on its own
    assert t.ok


def test_a_test_that_failed_once_without_the_fix_is_not_a_target():
    """radashi #481: one test timed out in the `test` run on a loaded machine and passed in both
    fix runs. That read as fail-to-pass, and a patch that changed nothing was paid for it."""
    test = {"real": F, "slow": F, "ok": P}
    fixes = [{"real": P, "slow": P, "ok": P}, {"real": P, "slow": P, "ok": P}]
    assert classify({}, test, fixes).f2p == ["real", "slow"]  # one test run can't tell them apart
    t = classify({}, test, fixes, [{"real": F, "slow": P, "ok": P}])
    assert t.f2p == ["real"] and t.flaky == ["slow"] and t.p2p == ["ok"] and t.ok
    # a repeat that produced no report says nothing, and must not mark every test flaky
    assert classify({}, test, fixes, [{}]).f2p == ["real", "slow"]


def test_needs_a_fix_run():
    with pytest.raises(ValueError):
        classify({}, {}, [])


GOLD = """\
diff --git a/pkg/core.py b/pkg/core.py
--- a/pkg/core.py
+++ b/pkg/core.py
@@ -1,3 +1,4 @@
-    return value
+    return normalize_timestamp(value, tz)
+    x = 1
+        old_line_that_moved_somewhere()
-        old_line_that_moved_somewhere()
+    # ------------------------------------
"""


def test_distinctive_lines():
    assert distinctive_added_lines(GOLD) == ["return normalize_timestamp(value, tz)"]
    assert new_test_names("+def test_a():\n+    async def test_b(x):\n def test_c():\n") == [
        "test_a",
        "test_b",
    ]
    assert statement_leaks("please call return normalize_timestamp(value, tz) here", GOLD)
    assert not statement_leaks("timestamps from the future should fail", GOLD)


def test_sanitize_strips_giveaways():
    body = (
        "<!-- PR template: describe your change -->\nFixes the thing.\n\n"
        "```diff\n- a\n+ b\n```\n"
        "See https://github.com/o/r/pull/12 and https://github.com/o/r/commit/abc123 "
        "but keep https://docs.python.org/3/ and https://github.com/other/r/pull/1\n\n\n\nThanks"
    )
    out = sanitize(body, "o/r")
    assert "template" not in out and "```" not in out and "/o/r/pull" not in out and "abc123" not in out
    assert "docs.python.org" in out and "other/r/pull/1" in out
    assert "\n\n\n" not in out and out.endswith("Thanks")


def test_statement_prefers_issue_text():
    pr = PRInfo(
        repo="o/r", number=1, title="Fix it", body="Fixes #2. Changed foo()", url="u", linked_issues=[2]
    )
    assert problem_statement(pr) == ("Fix it\n\nFixes #2. Changed foo()", "pr")
    pr.issue_texts[2] = "Crash on empty input\n\n<!-- template -->Steps to reproduce"
    assert problem_statement(pr) == ("Crash on empty input\n\nSteps to reproduce", "issue")


def test_truncated_junit_counts_as_no_report():
    class FakeSb:
        def exec(self, *a, **k):
            return None

        def read_text(self, path):
            return ""  # pytest died before writing anything

    from gauntlet_harness.testing import run_suite

    assert run_suite(FakeSb(), "/w", None) == {}
