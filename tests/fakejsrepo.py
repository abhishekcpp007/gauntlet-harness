"""The JavaScript twin of fakerepo.py: a local git repo with the same three PRs, tested by vitest.

    from tests.fakejsrepo import make_js_pr_repo
    repo_path, prs = make_js_pr_repo(tmp_path)

PR #1  "Fix division and add mul", merge commit, closes issue #10. Valid: one f2p (divides
       exactly), one n2p (multiplies, its file can't load before the fix), one s2p (the version
       test is skipped until VERSION exists), one p2p (adds two numbers).
PR #2  "Make add count from one (#2)", squash merge. Breaks the add test, so validation must
       reject it as a regression.
PR #3  "Describe calc in the README (#3)", squash merge, docs only. Rejected at the filter.

The test file is written the way a real one is on purpose, with a nested describe and a top
level test, so the canary and the disguise have every kind of container to deal with.
"""

from __future__ import annotations

import json
from pathlib import Path

from gauntlet_harness.github import prs_from_git
from gauntlet_harness.schema import PRInfo

from .fakerepo import _git, _write

REPO = "acme/jscalc"

ISSUE_10 = (
    "div gives the wrong answer for odd numbers\n\n"
    "div(5, 2) returns 2 but it should return the exact quotient. Also there's no way to "
    "multiply, and no version marker."
)

PACKAGE = (
    json.dumps(
        {
            "name": "jscalc",
            "version": "0.1.0",
            "type": "module",
            "scripts": {"test": "vitest run"},
            "devDependencies": {"vitest": "^3.2.0"},
        },
        indent=2,
    )
    + "\n"
)

BASE_CALC = """\
export function add(a, b) {
  return a + b
}

export function div(a, b) {
  return Math.floor(a / b)
}
"""

BASE_TESTS = """\
import { describe, expect, it, test } from 'vitest'
import * as calc from '../src/calc.js'

test('exports a function', () => {
  expect(typeof calc.add).toBe('function')
})

describe('calc', () => {
  describe('add', () => {
    it('adds two numbers', () => {
      expect(calc.add(2, 3)).toBe(5)
    })
  })
})
"""

FIXED_CALC = """\
export const VERSION = '1.0'

export function add(a, b) {
  return a + b
}

export function div(a, b) {
  const exactQuotient = Number(a) / Number(b)
  return exactQuotient
}

export function mul(a, b) {
  const productOfBoth = a * b
  return productOfBoth
}
"""

FIXED_TESTS = """\
import { describe, expect, it, test } from 'vitest'
import * as calc from '../src/calc.js'

test('exports a function', () => {
  expect(typeof calc.add).toBe('function')
})

describe('calc', () => {
  describe('add', () => {
    it('adds two numbers', () => {
      expect(calc.add(2, 3)).toBe(5)
    })
  })

  describe('div', () => {
    it('divides exactly', () => {
      expect(calc.div(5, 2)).toBe(2.5)
    })
  })

  it.skipIf(!calc.VERSION)('has a version marker', () => {
    expect(calc.VERSION.startsWith('1.')).toBe(true)
  })
})
"""

MUL_TESTS = """\
import { expect, it } from 'vitest'
import { mul } from '../src/calc.js'

it('multiplies two numbers', () => {
  expect(mul(3, 4)).toBe(12)
})
"""

GOLD_LINE = "const exactQuotient = Number(a) / Number(b)"  # distinctive enough for leak checks

F2P = "test/calc.test.js::calc > div > divides exactly"
N2P = "test/mul.test.js::multiplies two numbers"
S2P = "test/calc.test.js::calc > has a version marker"
P2P = ["test/calc.test.js::calc > add > adds two numbers", "test/calc.test.js::exports a function"]


def make_js_pr_repo(root: Path) -> tuple[Path, list[PRInfo]]:
    repo = root / "jscalc"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _write(
        repo,
        {
            "package.json": PACKAGE,
            ".gitignore": "node_modules/\n",
            "src/calc.js": BASE_CALC,
            "test/calc.test.js": BASE_TESTS,
        },
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Initial calc")

    # PR 1: a real merge commit from a branch
    _git(repo, "checkout", "-q", "-b", "fix-div")
    _write(repo, {"src/calc.js": FIXED_CALC, "test/calc.test.js": FIXED_TESTS, "test/mul.test.js": MUL_TESTS})
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
            "src/calc.js": FIXED_CALC.replace("return a + b", "return a + b + 1")
            + "\nexport function neg(a) {\n  return -a\n}\n",
            "test/neg.test.js": (
                "import { expect, it } from 'vitest'\nimport { neg } from '../src/calc.js'\n\n"
                "it('negates', () => {\n  expect(neg(2)).toBe(-2)\n})\n"
            ),
        },
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Make add count from one (#2)")

    # PR 3: docs only
    _write(repo, {"README.md": "# jscalc\n\nA tiny calculator.\n"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Describe calc in the README (#3)")

    prs = prs_from_git(repo, REPO)
    for pr in prs:
        if pr.number == 1:
            pr.issue_texts[10] = ISSUE_10
    return repo, prs
