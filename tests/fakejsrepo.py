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


F2P_SNAP = "test/calc.test.js::calc > div > formats a quotient"

# jest: the same tests with the runner's globals in place of the vitest import, and one more
# target that reads a stored snapshot, which is keyed by the test's name
JEST_SNAPSHOT_TEST = """\

    it('formats a quotient', () => {
      expect({ q: calc.div(5, 2) }).toMatchSnapshot()
    })
"""
JEST_SNAP = """\
// Jest Snapshot v1, https://goo.gl/fbAQLP

exports[`calc div formats a quotient 1`] = `
{
  "q": 2.5,
}
`;
"""


def _for_jest(text: str) -> str:
    # the leading semicolon matters: without it the line reads as a call on the block above
    lines = [line for line in text.splitlines(keepends=True) if "from 'vitest'" not in line]
    text = "".join(lines).replace("it.skipIf(!calc.VERSION)(", ";(calc.VERSION ? it : it.skip)(")
    marker = "      expect(calc.div(5, 2)).toBe(2.5)\n    })\n"
    return text.replace(marker, marker + JEST_SNAPSHOT_TEST)


def make_js_pr_repo(root: Path, runner: str = "vitest") -> tuple[Path, list[PRInfo]]:
    jest = runner == "jest"
    adapt = _for_jest if jest else (lambda text: text)
    package = PACKAGE
    if jest:
        # an ES module repo with no compile step has to turn jest's module support on itself
        package = PACKAGE.replace('"vitest": "^3.2.0"', '"jest": "^29.7.0"').replace(
            '"vitest run"', '"NODE_OPTIONS=--experimental-vm-modules jest"'
        )
    repo = root / "jscalc"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _write(
        repo,
        {
            "package.json": package,
            ".gitignore": "node_modules/\n",
            "src/calc.js": BASE_CALC,
            "test/calc.test.js": adapt(BASE_TESTS),
        },
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Initial calc")

    # PR 1: a real merge commit from a branch
    _git(repo, "checkout", "-q", "-b", "fix-div")
    fixed = {
        "src/calc.js": FIXED_CALC,
        "test/calc.test.js": adapt(FIXED_TESTS),
        "test/mul.test.js": adapt(MUL_TESTS),
    }
    if jest:
        fixed["test/__snapshots__/calc.test.js.snap"] = JEST_SNAP
    _write(repo, fixed)
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
                adapt(
                    "import { expect, it } from 'vitest'\nimport { neg } from '../src/calc.js'\n\n"
                    "it('negates', () => {\n  expect(neg(2)).toBe(-2)\n})\n"
                )
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
