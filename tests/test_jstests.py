"""The JavaScript side of the canary and the disguise, with no container: what gets planted,
what gets renamed, and that both read like the file they were put in."""

import random
import re

import pytest

from gauntlet_harness import jstests
from gauntlet_harness.grading import Canary, Disguise
from gauntlet_harness.schema import HarnessTask, PRInfo, Status

from .fakejsrepo import F2P, FIXED_TESTS, N2P, P2P, S2P

FILE = "test/calc.test.js"


def _task(**kw) -> HarnessTask:
    base = dict(
        instance_id="acme__jscalc-1",
        repo="x",
        pr=PRInfo(repo="acme/jscalc", number=1, title="t", url="u"),
        base_commit="a",
        solution_commit="b",
        problem_statement="s",
        statement_source="pr",
        image="i",
        env_fingerprint="f",
        gold_patch="",
        test_patch="",
        test_files=[FILE],
        language="node",
        runner="vitest",
        f2p=[F2P],
        s2p=[S2P],
        p2p=list(P2P),
    )
    base.update(kw)
    return HarnessTask(**base)


def _parses(source: str, path: str = FILE) -> bool:
    return jstests.parse(source, path) is not None


def _titles(source: str, path: str = FILE) -> set[str]:
    parsed = jstests.parse(source, path)
    return {box.test_id(path, t.title) for box in parsed.containers for t in box.tests}


@pytest.mark.parametrize("seed", range(25))
def test_canaries_land_as_real_tests_in_the_right_blocks(seed):
    rng = random.Random(seed)
    out, fail_ids, pass_ids = jstests.plant_canaries(FIXED_TESTS, FILE, [F2P, S2P, *P2P], rng, 2)
    assert len(fail_ids) == len(pass_ids) == 2 and len({*fail_ids, *pass_ids}) == 4
    assert _parses(out)
    # every canary is a test the runner will report under exactly the id we expect
    assert {*fail_ids, *pass_ids} <= _titles(out)
    # and nothing that was there before went missing or moved to another block
    assert _titles(FIXED_TESTS) <= _titles(out)
    for line in FIXED_TESTS.splitlines():
        assert line in out.splitlines()


def test_a_canary_is_written_the_way_its_neighbours_are():
    source = 'import { expect, test } from "vitest";\n\ntest("adds", () => {\n\texpect(1 + 1).toBe(2);\n});\n'
    out, fail_ids, pass_ids = jstests.plant_canaries(
        source, "a.test.ts", ["a.test.ts::adds"], random.Random(1), 2
    )
    planted = [line for line in out.splitlines() if line not in source.splitlines() and line.strip()]
    heads = [line for line in planted if line.startswith("test(")]
    assert len(heads) == 4 and all(h.startswith('test("adds ') for h in heads)  # same call, same quotes
    assert all(line.endswith((";", "{")) for line in planted)  # semicolons, as the file has them
    assert any(line.startswith("\texpect(") for line in planted)  # tabs, and the file's own assertion
    assert "import" not in "".join(planted)  # a canary brings nothing in


def test_the_assertion_is_whatever_the_file_already_uses():
    head = "import { test } from 'vitest'\n"
    plain = head + "\ntest('adds', () => {\n  if (1 + 1 !== 2) throw new Error('no')\n})\n"
    node = (
        head
        + "import assert from 'node:assert'\n\ntest('adds', () => {\n  assert.strictEqual(1 + 1, 2)\n})\n"
    )
    out, *_ = jstests.plant_canaries(plain, "a.test.js", ["a.test.js::adds"], random.Random(0), 1)
    assert "expect(" not in out and "assert." not in out and out.count("throw new Error(") == 3
    out, *_ = jstests.plant_canaries(node, "a.test.js", ["a.test.js::adds"], random.Random(0), 1)
    assert "expect(" not in out and out.count("assert.strictEqual(") == 3
    assert _parses(out, "a.test.js")


def test_a_file_that_does_not_parse_still_gets_canaries():
    source = "import { it } from 'vitest'\n\nit('adds', (): void => {\n  expect(1).toBe(1)\n})\n"
    assert jstests.parse(source, "a.test.js") is None
    out, fail_ids, pass_ids = jstests.plant_canaries(
        source, "a.test.js", ["a.test.js::adds"], random.Random(0), 2
    )
    assert len(fail_ids) == len(pass_ids) == 2
    assert out.startswith(source) and out.count("\nit('handles ") == 4


BROKEN_ABOVE = """\
import { describe, it, expect, vi } from "vitest";

vi.mock("node:fs", async (importOriginal) => {
  const fs = await importOriginal<typeof import("node:fs")>();
  return { ...fs };
});

describe("probing", () => {
  it.each(["a", "b"])("%s does not throw", (id) => {
    expect(id).toBeTruthy();
  });
  it("probes once", () => {
    expect(1).toBe(1);
  });
});
"""


def test_one_unreadable_statement_does_not_hide_the_rest_of_the_file():
    """exsolve #73: the TypeScript grammar trips inside the mock, the describe block is fine."""
    path = "a.test.ts"
    ids = [
        f"{path}::probing > a does not throw",
        f"{path}::probing > b does not throw",
        f"{path}::probing > probes once",
    ]
    assert jstests.coverage(BROKEN_ABOVE, path, ids) == (3, 3)
    for seed in range(10):
        out, fail_ids, pass_ids = jstests.plant_canaries(BROKEN_ABOVE, path, ids, random.Random(seed), 2)
        assert len({*fail_ids, *pass_ids}) == 4
        # nothing lands in or ahead of the statement that couldn't be read
        assert (
            out.index("vi.mock(")
            < out.index("describe(")
            < min(out.index(t.split(" > ")[-1]) for t in fail_ids)
        )
        assert all(t.startswith(f"{path}::probing > probes once ") for t in (*fail_ids, *pass_ids))


def test_typescript_and_tsx_are_parsed_with_their_own_grammar():
    head = "import { it, expect } from 'vitest'\n\n"
    ts = head + "it('adds', (): void => {\n  const n: number = 1 as number\n  expect(n).toBe(1)\n})\n"
    tsx = head + "it('renders', () => {\n  const el = <div>hi</div>\n  expect(el).toBeTruthy()\n})\n"
    assert _titles(ts, "a.test.ts") == {"a.test.ts::adds"}
    assert _titles(tsx, "a.test.tsx") == {"a.test.tsx::renders"}
    assert jstests.parse(ts, "a.test.js") is None  # type syntax is not JavaScript, and nothing else is in it


def test_titles_built_at_run_time_are_never_borrowed():
    source = (
        "import { describe, it } from 'vitest'\n\n"
        "const name = 'x'\n"
        "it(name, () => {})\n"
        "it(`adds ${name}`, () => {})\n"
        "it('a \\'quoted\\' one', () => {})\n"
        "describe(name, () => {\n  it('inside a dynamic block', () => {})\n})\n"
        "it('plain', () => {})\n"
    )
    assert _titles(source, "a.test.js") == {"a.test.js::plain"}


EACH = """\
import { describe, expect, it } from 'vitest'
import { add } from '../src/calc.js'

describe('calc', () => {
  it.each([[1, 1, 2], [2, 3, 5]])('add(%i, %i) -> %i', (a, b, c) => {
    expect(add(a, b)).toBe(c)
  })
  it.skipIf(!add)('is exported', () => {
    expect(add).toBeTypeOf('function')
  })
  it.concurrent('adds zero', async () => {
    expect(add(1, 0)).toBe(1)
  })
})
"""
EACH_IDS = [
    f"{FILE}::calc > add(1, 1) -> 2",
    f"{FILE}::calc > add(2, 3) -> 5",
    f"{FILE}::calc > is exported",
    f"{FILE}::calc > adds zero",
]


@pytest.mark.parametrize("seed", range(10))
def test_every_kind_of_target_is_renamed_and_mapped_back(seed):
    out, back, rules = jstests.rename_targets(EACH, FILE, EACH_IDS, random.Random(seed))
    assert _parses(out)
    for real in EACH_IDS:
        assert real.split(" > ")[-1].split("(")[0] not in {t.split(" > ")[-1] for t in _titles(out)}
    assert sorted(back.values()) == sorted(EACH_IDS[2:])
    assert len(rules) == 1
    ending = rules[0][1]
    # what vitest will report for the two rows, and what that has to map back to
    for real in EACH_IDS[:2]:
        assert jstests.restore_id(real + ending, back, rules) == real
    # an id that only happens to end the same way is left alone
    stranger = f"{FILE}::other > thing{ending}"
    assert jstests.restore_id(stranger, back, rules) == stranger
    assert jstests.coverage(EACH, FILE, EACH_IDS) == (4, 4)


HEAD = "import { describe, expect, it } from 'vitest'\n\n"

# how real suites make tests when they don't write them out one by one. Each entry is
# (source, the ids vitest reports for it)
GENERATED = {
    "a describe.each block": (
        HEAD + "describe.each([1, 2])('n=%i', (n) => {\n  it('works', () => {})\n})\n",
        ["a.test.js::n=1 > works", "a.test.js::n=2 > works"],
    ),
    "a for loop with a template title": (  # i18next
        HEAD
        + "describe('nesting', () => {\n  const tests = [{ args: ['a'] }, { args: ['b'] }]\n"
        + "  for (const t of tests) {\n"
        + "    it(`correctly nests for ${JSON.stringify(t.args)} args`, () => {})\n  }\n})\n",
        [
            'a.test.js::nesting > correctly nests for ["a"] args',
            'a.test.js::nesting > correctly nests for ["b"] args',
        ],
    ),
    "a forEach with a variable title": (
        HEAD
        + "const rows = [{ name: 'one' }, { name: 'two' }]\n"
        + "rows.forEach((row) => {\n  it(row.name, () => {})\n})\n",
        ["a.test.js::one", "a.test.js::two"],
    ),
    "a describe named at run time": (  # path-to-regexp
        HEAD
        + "for (const path of ['/a', '/b']) {\n  describe(`match ${path}`, () => {\n"
        + "    it('should match', () => {})\n  })\n}\n",
        ["a.test.js::match /a > should match", "a.test.js::match /b > should match"],
    ),
    "a plain title inside an if": (
        HEAD + "if (process.platform !== 'win32') {\n  it('follows symlinks', () => {})\n}\n",
        ["a.test.js::follows symlinks"],
    ),
}


@pytest.mark.parametrize("case", GENERATED)
def test_generated_tests_are_renamed_and_mapped_back(case):
    source, ids = GENERATED[case]
    assert jstests.coverage(source, "a.test.js", ids) == (len(ids), len(ids))
    out, back, rules = jstests.rename_targets(source, "a.test.js", ids, random.Random(3))
    assert _parses(out, "a.test.js") and out != source
    assert not back and len(rules) == 1  # one call site, however many names it reports
    pattern, ending = rules[0]
    for real in ids:
        assert jstests.restore_id(real + ending, back, rules) == real
        assert jstests.restore_id(real, back, rules) == real  # never renamed, never touched


def test_a_variable_title_is_wrapped_not_edited():
    source, ids = GENERATED["a forEach with a variable title"]
    out, _, rules = jstests.rename_targets(source, "a.test.js", ids, random.Random(3))
    assert f"it((row.name) + '{rules[0][1]}', () => {{}})" in out


def test_a_generated_test_is_never_a_name_to_borrow_or_a_place_to_plant():
    source, ids = GENERATED["a for loop with a template title"]
    for seed in range(10):
        out, fail_ids, pass_ids = jstests.plant_canaries(source, "a.test.js", ids, random.Random(seed), 2)
        assert _parses(out, "a.test.js") and len({*fail_ids, *pass_ids}) == 4
        # a canary inside the loop would report once per row, under names nobody predicted
        loop = out[
            out.index("for (const t of tests) {") : out.index(
                "\n  }\n", out.index("for (const t of tests) {")
            )
        ]
        assert loop.count("it(") == 1
        # it may borrow the words a generated title starts with, never the part filled in per row
        assert all("${" not in t and "JSON" not in t for t in (*fail_ids, *pass_ids))


def test_coverage_counts_what_the_disguise_cannot_reach():
    # a test registered from inside a helper function: nothing at the call site says it's a test
    source = HEAD + "function check(name) {\n  it(name, () => {})\n}\n\ncheck('one')\ncheck('two')\n"
    assert jstests.coverage(source, "a.test.js", ["a.test.js::one", "a.test.js::two"]) == (0, 2)


def test_two_targets_never_share_a_new_name():
    source = "import { it } from 'vitest'\n\n" + "".join(f"it('case {i}', () => {{}})\n" for i in range(12))
    targets = [f"a.test.js::case {i}" for i in range(12)]
    for seed in range(20):
        _, back, _ = jstests.rename_targets(source, "a.test.js", targets, random.Random(seed))
        assert sorted(back.values()) == sorted(targets) and len(back) == 12


def test_the_canary_class_speaks_vitest(monkeypatch):
    monkeypatch.setenv("GAUNTLET_GRADING_SEED", "11")
    canary = Canary(_task())
    out = canary.build(FIXED_TESTS)
    assert canary.path == FILE and len(canary.fail_ids) == 2 and len(canary.pass_ids) == 2
    assert canary.ids <= _titles(out)
    honest = {t: Status.PASSED for t in canary.pass_ids} | {t: Status.FAILED for t in canary.fail_ids}
    assert canary.check(honest) == []
    forged = {t: Status.PASSED for t in canary.ids}
    assert "forged" in canary.check(forged)[0]
    assert "vanished" in canary.check({F2P: Status.PASSED})[0]


def test_the_same_seed_plants_the_same_canaries(monkeypatch):
    monkeypatch.setenv("GAUNTLET_GRADING_SEED", "5")
    first = Canary(_task()).build(FIXED_TESTS)
    assert Canary(_task()).build(FIXED_TESTS) == first
    monkeypatch.setenv("GAUNTLET_GRADING_SEED", "6")
    assert Canary(_task()).build(FIXED_TESTS) != first


def test_the_disguise_class_round_trips_statuses():
    disguise = Disguise(_task(n2p=[N2P]), random.Random(4))
    renamed = disguise.rename(FIXED_TESTS, FILE)
    assert "divides exactly'" not in renamed and "has a version marker'" not in renamed
    assert "it('adds two numbers'" in renamed  # p2p tests keep their names
    reported = {new: Status.PASSED for new in disguise.back} | {P2P[0]: Status.PASSED}
    assert disguise.restore(reported) == {F2P: Status.PASSED, S2P: Status.PASSED, P2P[0]: Status.PASSED}


def test_a_bracket_in_a_title_is_part_of_the_name():
    # pytest ids split on "[" for parameters; a vitest title is free text
    source = "import { it } from 'vitest'\n\nit('parses [a, b]', () => {})\nit('parses nothing', () => {})\n"
    target = "a.test.js::parses [a, b]"
    task = _task(f2p=[target], s2p=[], p2p=[], test_files=["a.test.js"])
    canary = Canary(task)
    assert canary.graded == [target]
    disguise = Disguise(task, random.Random(0))
    disguise.rename(source, "a.test.js")
    (new,) = disguise.back
    assert disguise.restore({new: Status.PASSED}) == {target: Status.PASSED}


def test_a_version_bump_does_not_make_a_new_env():
    import json

    from gauntlet_harness.build import package_json_identity

    base = {
        "name": "x",
        "version": "1.0.0",
        "scripts": {"test": "vitest"},
        "devDependencies": {"vitest": "^3"},
    }
    same = package_json_identity(json.dumps(base).encode())
    assert (
        package_json_identity(json.dumps({**base, "version": "1.0.1", "description": "d"}).encode()) == same
    )
    assert package_json_identity(json.dumps({**base, "scripts": {"test": "vitest run"}}).encode()) == same
    # what an install can see does change it
    assert package_json_identity(json.dumps({**base, "devDependencies": {"vitest": "^4"}}).encode()) != same
    assert package_json_identity(json.dumps({**base, "scripts": {"prepare": "tsup"}}).encode()) != same
    assert package_json_identity(json.dumps({**base, "engines": {"node": ">=20"}}).encode()) != same
    assert package_json_identity(b"{ not json") == b"{ not json"


def test_a_file_of_only_generated_tests_still_takes_canaries():
    """Found by sweeping real test files: path-to-regexp's suite is one loop, and working out
    the indent from a block named at run time divided by zero."""
    source, ids = GENERATED["a describe named at run time"]
    out, fail_ids, pass_ids = jstests.plant_canaries(source, "a.test.js", ids, random.Random(0), 2)
    assert _parses(out, "a.test.js") and len({*fail_ids, *pass_ids}) == 4


LOOPED_ONLY = """\
import { describe, it, expect } from 'vitest';
import i18next from '../../src/i18next.js';

describe('nesting', () => {
  const tests = [{ args: ['a'] }, { args: ['b'] }];

  tests.forEach((test) => {
    it(`correctly nests for ${JSON.stringify(test.args)} args`, () => {
      expect(i18next.t(...test.args)).toBeTruthy();
    });
  });
});
"""


@pytest.mark.parametrize("seed", range(15))
def test_a_canary_only_calls_what_the_file_imported(seed):
    """i18next #2454: the file imports `it`, every test is made in a loop, and the first
    canaries were written as `test(...)` at the top of the file. That is a ReferenceError,
    the file never loaded, and the honest oracle was flagged for tampering."""
    ids = [
        'a.test.js::nesting > correctly nests for ["a"] args',
        'a.test.js::nesting > correctly nests for ["b"] args',
    ]
    out, fail_ids, pass_ids = jstests.plant_canaries(LOOPED_ONLY, "a.test.js", ids, random.Random(seed), 2)
    planted = [line for line in out.splitlines() if line not in LOOPED_ONLY.splitlines() and line.strip()]
    assert not any(line.lstrip().startswith("test(") for line in planted)
    heads = [line for line in planted if line.lstrip().startswith("it(")]
    assert len(heads) == 4
    # in the block the graded tests are in, named after them, written the way they are
    assert all(
        line.startswith("  it(`correctly nests for ") and line.endswith("`, () => {") for line in heads
    )
    assert all(t.startswith("a.test.js::nesting > correctly nests for ") for t in (*fail_ids, *pass_ids))
    assert all(line.endswith((";", "{")) for line in planted)
    assert _parses(out, "a.test.js")


def test_with_no_vitest_import_the_globals_are_on_and_the_file_decides():
    head = "describe('calc', () => {\n"
    source = head + "  test.each([1, 2])('adds %i', (n) => {\n    expect(n).toBeTruthy()\n  })\n})\n"
    out, *_ = jstests.plant_canaries(source, "a.test.js", ["a.test.js::calc > adds 1"], random.Random(0), 2)
    # one word is too little to borrow, so the name is the canary's own. The call is the file's.
    assert out.count("  test('handles ") == 4 and "it(" not in out


SNAP = """\
// Vitest Snapshot v1, https://vitest.dev/guide/snapshot.html

exports[`Events > CDATA 1`] = `
[
  {
    "event": "oncdatastart",
  },
]
`;

exports[`Events > CDATA 2`] = `"second"`;

exports[`Events > CDATA > as a hint 1`] = `"hinted"`;

exports[`Events > CDATA (inside special) 1`] = `"a different test"`;

exports[`Events > row a parses 1`] = `"a"`;

exports[`Events > a \\`quoted\\` name 1`] = `"q"`;
"""


def test_snapshot_keys_follow_a_rename():
    """htmlparser2 #2540: the gold fix failed its own targets because their snapshots were
    still filed under the names the disguise had just changed."""
    path = "src/Parser.events.spec.ts"
    back = {f"{path}::Events > CDATA twice": f"{path}::Events > CDATA"}
    rules = [
        (re.escape(f"{path}::Events > row ") + ".*?" + re.escape(" parses") + re.escape(" again"), " again")
    ]
    out = jstests.rename_snapshot_keys(SNAP, path, back, rules)
    assert "exports[`Events > CDATA twice 1`] = `\n[" in out
    assert "exports[`Events > CDATA twice 2`] = " in out
    assert "exports[`Events > CDATA twice > as a hint 1`] = " in out
    assert "exports[`Events > row a parses again 1`] = " in out
    # a test that only starts with the same words is someone else's
    assert "exports[`Events > CDATA (inside special) 1`] = " in out
    assert "exports[`Events > a \\`quoted\\` name 1`] = " in out
    assert "exports[`Events > CDATA 1`]" not in out
    # the snapshots themselves are untouched
    assert out.count('"event": "oncdatastart"') == 1 and out.count("`;") == SNAP.count("`;")
    assert jstests.rename_snapshot_keys(SNAP, path, {}, []) == SNAP
    assert jstests.snapshot_path(path) == "src/__snapshots__/Parser.events.spec.ts.snap"
