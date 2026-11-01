"""A local git repo whose first-parent history looks like a real GitHub project's merged PRs.

    from tests.fakerepo import make_pr_repo
    repo_path, prs = make_pr_repo(tmp_path)

`prs` comes back newest first, like the API, as PRInfo objects with `repo="acme/calc"`.
Pass `repo_url=str(repo_path)` to build_task so it clones the local path.

PR #1  "Fix division and add mul", merge commit, closes issue #10. Valid: one f2p
       (test_div), one n2p (test_mul, its module can't import before the fix), one s2p
       (test_version is skipped until VERSION exists), one p2p (test_add).
PR #2  "Make add count from one (#2)", squash merge. Adds a passing feature but breaks
       test_add, so validation must reject it as a regression.
PR #3  "Describe calc in the README (#3)", squash merge, docs only. Rejected at the filter.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from gauntlet_harness.github import prs_from_git
from gauntlet_harness.schema import PRInfo

REPO = "acme/calc"

ISSUE_10 = (
    "div gives the wrong answer for odd numbers\n\n"
    "calc.div(5, 2) returns 2 but it should return the exact quotient. Also there's no way to "
    "multiply, and no version marker."
)

PYPROJECT = """\
[project]
name = "calc"
version = "0.1.0"

[project.optional-dependencies]
test = ["pytest"]

[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
packages = ["calc"]
"""

BASE_CALC = "def add(a, b):\n    return a + b\n\n\ndef div(a, b):\n    return a // b\n"
BASE_TESTS = "import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n"

FIXED_CALC = (
    'VERSION = "1.0"\n\n\n'
    "def add(a, b):\n    return a + b\n\n\n"
    "def div(a, b):\n    return float(a) / float(b)\n\n\n"
    "def mul(a, b):\n    product_of_both = a * b\n    return product_of_both\n"
)
FIXED_TESTS = (
    "import pytest\n\nimport calc\n\n\n"
    "def test_add():\n    assert calc.add(2, 3) == 5\n\n\n"
    "def test_div_gives_exact_quotient():\n    assert calc.div(5, 2) == 2.5\n\n\n"
    '@pytest.mark.skipif(not hasattr(calc, "VERSION"), reason="no VERSION yet")\n'
    "def test_version_marker():\n    assert calc.VERSION.startswith('1.')\n"
)
MUL_TESTS = "from calc import mul\n\n\ndef test_mul_multiplies_numbers():\n    assert mul(3, 4) == 12\n"

GOLD_LINE = "return float(a) / float(b)"  # distinctive enough for leak checks


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=dev", "-c", "user.email=dev@example.com", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _write(repo: Path, files: dict[str, str]) -> None:
    for name, body in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


def make_pr_repo(root: Path) -> tuple[Path, list[PRInfo]]:
    repo = root / "calc"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _write(
        repo, {"pyproject.toml": PYPROJECT, "calc/__init__.py": BASE_CALC, "tests/test_calc.py": BASE_TESTS}
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Initial calc")

    # PR 1: a real merge commit from a branch
    _git(repo, "checkout", "-q", "-b", "fix-div")
    _write(
        repo,
        {"calc/__init__.py": FIXED_CALC, "tests/test_calc.py": FIXED_TESTS, "tests/test_mul.py": MUL_TESTS},
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Fix div, add mul and VERSION")
    _git(repo, "checkout", "-q", "main")
    _git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "fix-div",
        "-m",
        "Merge pull request #1 from dev/fix-div",
        "-m",
        "Fix division and add mul\n\nFixes #10",
    )

    # PR 2: squash merge with a regression
    _write(
        repo,
        {
            "calc/__init__.py": FIXED_CALC.replace("return a + b", "return a + b + 1")
            + "\n\ndef neg(a):\n    return -a\n",
            "tests/test_neg.py": "from calc import neg\n\n\ndef test_neg():\n    assert neg(2) == -2\n",
        },
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Make add count from one (#2)")

    # PR 3: docs only
    _write(repo, {"README.md": "# calc\n\nA tiny calculator.\n"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Describe calc in the README (#3)")

    prs = prs_from_git(repo, REPO)
    for pr in prs:
        if pr.number == 1:
            pr.issue_texts[10] = ISSUE_10
    return repo, prs
