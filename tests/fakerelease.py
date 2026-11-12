"""A local repo with release tags, for long-horizon (release range) tasks.

    v0.1.0        Initial calc
                  PR #1  merge commit: fix div, add mul and VERSION (three target tests)
    v0.2.0rc1     a pre-release tag, ignored unless asked for
                  direct commit: a header comment in calc, no PR number
                  PR #4  squash: add neg (one target test)
    v0.2.0        the release

    v0.1.1        a maintenance tag on a side branch off v0.1.0, made after v0.2.0. It pairs
                  with v0.1.0, and v0.2.0 must still pair with v0.1.0, never with v0.1.1.

The range v0.1.0 -> v0.2.0 is the LHT task: two milestones, pr-1 with three tests and pr-4
with one, and test_add as the P2P guard.
"""

from __future__ import annotations

from pathlib import Path

from .fakerepo import BASE_CALC, BASE_TESTS, FIXED_CALC, FIXED_TESTS, MUL_TESTS, PYPROJECT, _git, _write

REPO = "acme/calc"
HEADER = "# calc: a tiny calculator\n"
NEG = "\n\ndef neg(a):\n    return -a\n"
NEG_TESTS = "from calc import neg\n\n\ndef test_neg_flips_sign():\n    assert neg(2) == -2\n"


def make_release_repo(root: Path) -> Path:
    repo = root / "calc-releases"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _write(
        repo, {"pyproject.toml": PYPROJECT, "calc/__init__.py": BASE_CALC, "tests/test_calc.py": BASE_TESTS}
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Initial calc")
    _git(repo, "tag", "v0.1.0")

    _git(repo, "checkout", "-q", "-b", "fix-div")
    _write(
        repo,
        {"calc/__init__.py": FIXED_CALC, "tests/test_calc.py": FIXED_TESTS, "tests/test_mul.py": MUL_TESTS},
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Fix div, add mul and VERSION")
    _git(repo, "checkout", "-q", "main")
    _git(
        repo, "merge", "-q", "--no-ff", "fix-div",
        "-m", "Merge pull request #1 from dev/fix-div",
        "-m", "Fix division and add mul\n\nDivision rounds down.",
    )  # fmt: skip
    _git(repo, "tag", "v0.2.0rc1")

    _write(repo, {"calc/__init__.py": HEADER + FIXED_CALC})
    _git(repo, "commit", "-q", "-am", "Describe calc at the top of the module")

    _write(repo, {"calc/__init__.py": HEADER + FIXED_CALC + NEG, "tests/test_neg.py": NEG_TESTS})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Add neg (#4)", "-m", "A function that flips the sign.")
    _git(repo, "tag", "v0.2.0")

    _git(repo, "checkout", "-q", "-b", "maint-0.1", "v0.1.0")
    _write(repo, {"README.md": "maintenance\n"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Maintenance note")
    _git(repo, "tag", "v0.1.1")
    _git(repo, "checkout", "-q", "main")
    return repo
