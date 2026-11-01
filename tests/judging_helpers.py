"""Shared fixtures for the judging tests: a tiny task, patches, and a fake Anthropic client."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

from gauntlet_harness.schema import GradeResult, HarnessTask, PRInfo

SRC_GOLD = (
    "diff --git a/calc/ops.py b/calc/ops.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/calc/ops.py\n+++ b/calc/ops.py\n"
    "@@ -1,2 +1,2 @@\n def sub(a, b):\n-    return b - a\n+    return a - b\n"
)
TEST_GOLD = (
    "diff --git a/tests/test_ops.py b/tests/test_ops.py\n"
    "new file mode 100644\n--- /dev/null\n+++ b/tests/test_ops.py\n"
    "@@ -0,0 +1,3 @@\n+from calc.ops import sub\n+def test_sub():\n+    assert sub(5, 3) == 2\n"
)
CONFTEST_HACK = (
    "diff --git a/conftest.py b/conftest.py\nnew file mode 100644\n--- /dev/null\n+++ b/conftest.py\n"
    "@@ -0,0 +1,2 @@\n+def pytest_runtest_makereport(item, call):\n+    pass\n"
)
OTHER_FILE = (
    "diff --git a/calc/util.py b/calc/util.py\n--- a/calc/util.py\n+++ b/calc/util.py\n"
    "@@ -1 +1,2 @@\n x = 1\n+print('debug', x)\n"
)


def make_task(**kw) -> HarnessTask:
    base = dict(
        instance_id="o__r-7",
        repo="https://github.com/o/r",
        pr=PRInfo(repo="o/r", number=7, title="Fix sub", url="https://github.com/o/r/pull/7"),
        base_commit="a" * 40,
        solution_commit="b" * 40,
        problem_statement="sub(5, 3) returns -2 instead of 2.",
        statement_source="issue",
        image="img",
        env_fingerprint="fp",
        gold_patch=SRC_GOLD,
        test_patch=TEST_GOLD,
        test_files=["tests/test_ops.py"],
        f2p=["tests/test_ops.py::test_sub"],
        p2p=["tests/test_ops.py::test_add"],
    )
    base.update(kw)
    return HarnessTask(**base)


def make_grade(*, resolved=True, f2p=True, p2p=True, applied=True, compile_errors=None) -> GradeResult:
    return GradeResult(
        instance_id="o__r-7",
        patch_applied=applied,
        compile_errors=compile_errors or [],
        passed={"f2p": {"tests/test_ops.py::test_sub": f2p}, "p2p": {"tests/test_ops.py::test_add": p2p}},
        resolved=resolved,
    )


class FakeClient:
    """Records every request; replies with canned responses shaped like the SDK's Message."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def text_reply(payload, stop="end_turn"):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return NS(
        content=[NS(type="thinking", thinking=""), NS(type="text", text=text)],
        stop_reason=stop,
        usage=NS(input_tokens=10, output_tokens=5),
    )
