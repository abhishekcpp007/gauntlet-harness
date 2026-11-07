"""How far the shared JUnit reader already goes, and exactly where it stops.

DESIGN.md argues that a second language is blocked on test identity rather than on result
handling. That is an argument worth being able to re-run, so these are the samples it rests on.
Each one is the shape its runner really emits: pytest puts a `file` on every testcase, the
others mostly do not.
"""

from __future__ import annotations

import pytest
from gauntlet_repo2env.testrun import parse_junit

PYTEST = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="2">
<testcase classname="tests.test_calc" file="tests/test_calc.py" name="test_add" time="0.01"/>
<testcase classname="tests.test_calc" file="tests/test_calc.py" name="test_div" time="0.01">
<failure message="assert 1 == 2">boom</failure></testcase>
</testsuite></testsuites>"""

JEST = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites name="jest tests" tests="2" failures="1">
<testsuite name="calc" tests="2" failures="1" file="src/__tests__/calc.test.js">
<testcase classname="calc adds numbers" name="calc adds numbers" time="0.004"/>
<testcase classname="calc divides numbers" name="calc divides numbers" time="0.002">
<failure>Expected 1 but received 2</failure></testcase>
</testsuite></testsuites>"""

VITEST = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites name="vitest tests" tests="2" failures="1">
<testsuite name="src/calc.test.ts" tests="2" failures="1">
<testcase classname="src/calc.test.ts" name="adds numbers" time="0.003"/>
<testcase classname="src/calc.test.ts" name="divides numbers" time="0.001">
<failure message="AssertionError">nope</failure></testcase>
</testsuite></testsuites>"""

GO = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites tests="2" failures="1">
<testsuite name="example.com/calc" tests="2" failures="1">
<testcase classname="example.com/calc" name="TestAdd" time="0.001"/>
<testcase classname="example.com/calc" name="TestDiv" time="0.001">
<failure message="Failed">div wrong</failure></testcase>
</testsuite></testsuites>"""

RUST = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites tests="2" failures="1">
<testsuite name="calc" tests="2" failures="1">
<testcase classname="calc" name="tests::adds" time="0.001"/>
<testcase classname="calc" name="tests::divides" time="0.001">
<failure type="test failure">panicked</failure></testcase>
</testsuite></testsuites>"""

RUNNERS = {"pytest": PYTEST, "jest": JEST, "vitest": VITEST, "go": GO, "rust": RUST}


@pytest.mark.parametrize("runner", sorted(RUNNERS))
def test_every_runner_statuses_read_correctly(runner):
    """Results are already portable: one pass and one failure, whoever wrote the file."""
    parsed = parse_junit(RUNNERS[runner])
    assert len(parsed) == 2, parsed
    assert sorted(parsed.values()) == ["failed", "passed"], parsed


def test_pytest_ids_carry_the_file_which_is_what_the_rest_depends_on():
    assert sorted(parse_junit(PYTEST)) == [
        "tests/test_calc.py::test_add",
        "tests/test_calc.py::test_div",
    ]


@pytest.mark.parametrize("runner", ["jest", "go", "rust"])
def test_other_runners_give_ids_with_no_file_in_them(runner):
    """The blocker, pinned.

    `f2p` and `p2p` are lists of ids, and `_test_file` finds the canary's home by splitting an
    id on `::`. An id with no file in it breaks both. Normalising ids per runner is step one of
    supporting a second language, and this fails the day somebody does it.
    """
    for test_id in parse_junit(RUNNERS[runner]):
        head = test_id.split("::")[0]
        assert "/" not in head or not head.endswith((".js", ".ts", ".go", ".rs")), (
            f"{runner} ids now look file-based ({test_id!r}); if that was deliberate, "
            "update DESIGN.md and the normalisation plan"
        )


def test_jest_duplicates_the_name_into_the_classname():
    """jest-junit sets classname to the full test name, so the id says it twice."""
    ids = sorted(parse_junit(JEST))
    assert ids == [
        "calc adds numbers::calc adds numbers",
        "calc divides numbers::calc divides numbers",
    ]
