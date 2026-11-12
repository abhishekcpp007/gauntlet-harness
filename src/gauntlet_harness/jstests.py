"""Read and rewrite JavaScript and TypeScript test files: the canary and the disguise for vitest.

The strategy is the one grading.py uses for pytest and it lives there. This module is only the
part that has to know the language: where the tests in a file are, what an id looks like, and
how to write a test that reads like its neighbours.

A vitest id is `path::outer describe > inner describe > title`. That is what its JUnit reporter
writes, and it's the only shape handled here. A title built at run time (`it.each`, a template
string with a value in it, a variable) can't be read off the source, so those tests are never
renamed and never used as a name to borrow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

SEP = " > "

_TEST_CALLEES = {"it", "test"}
_SUITE_CALLEES = {"describe", "suite"}
# it.skip(...), describe.only(...): same call, one modifier deep. `each` and `for` are left
# out on purpose, they return a function and the title is a format string.
_MODIFIERS = {"only", "skip", "concurrent", "sequential", "fails", "todo", "shuffle"}
_CURRIED_PLAIN = {"skipIf", "runIf"}  # it.skipIf(cond)('title', fn): called twice, title still literal
_CURRIED_TEMPLATE = {"each", "for"}  # it.each(rows)('adds %i', fn): one call, many reported names

_SUFFIXES = (
    "roundtrip", "regression", "edge case", "when empty", "with unicode", "twice", "again", "basic",
    "simple", "by default", "when nested", "single", "large input", "small input", "reversed",
    "in order", "copy", "with null",
)  # fmt: skip
_WORDS = ("alpha", "beta", "delta", "omega", "kappa", "sigma", "theta", "gamma", "zeta", "lambda")


class JsSupportMissing(RuntimeError):
    pass


def _parser(path: str):
    try:
        import tree_sitter
        import tree_sitter_javascript
        import tree_sitter_typescript
    except ImportError as e:  # pragma: no cover - the wheels are a core dependency
        raise JsSupportMissing(
            "grading a JavaScript or TypeScript task needs tree-sitter, tree-sitter-javascript "
            "and tree-sitter-typescript installed"
        ) from e
    suffix = PurePosixPath(path).suffix
    if suffix in (".ts", ".mts", ".cts"):
        language = tree_sitter_typescript.language_typescript()
    elif suffix == ".tsx":
        language = tree_sitter_typescript.language_tsx()
    else:
        language = tree_sitter_javascript.language()  # handles JSX too
    return tree_sitter.Parser(tree_sitter.Language(language))


@dataclass
class TestCall:
    __test__ = False

    title: str
    callee: str  # "it" or "test", whichever this call used
    quote: str  # the quote character around the title
    title_start: int  # byte offsets of the title's text, inside the quotes
    title_end: int
    row: int  # first line of the statement, 0-based
    semicolon: bool
    template: bool = False  # the title is a format string, see _callee


@dataclass
class Container:
    """The top of the file, or the body of one describe block."""

    path: tuple[str, ...]
    indent: str  # what a statement directly inside it starts with
    tests: list[TestCall] = field(default_factory=list)
    templates: list[TestCall] = field(default_factory=list)  # it.each and friends, never a name to borrow
    spots: list[int] = field(default_factory=list)  # rows a new statement can go in front of
    titles: set[str] = field(default_factory=set)  # every static title here, tests and describes

    def test_id(self, file: str, title: str) -> str:
        return f"{file}::{SEP.join((*self.path, title))}"


@dataclass
class JsFile:
    containers: list[Container]
    expect: bool
    assert_: bool
    fallback_callee: str | None = None

    @property
    def root(self) -> Container:
        return self.containers[0]


def _static_title(node, src: bytes) -> tuple[str, str, int, int] | None:
    """(text, quote, start, end) when the node is a plain string, else None."""
    if node is None:
        return None
    if node.type == "string":
        quote = src[node.start_byte : node.start_byte + 1].decode()
        text = src[node.start_byte + 1 : node.end_byte - 1].decode("utf-8", "replace")
        if "\\" in text:
            return None  # an escape would need decoding to match the reported name
        return text, quote, node.start_byte + 1, node.end_byte - 1
    if node.type == "template_string" and not any(c.type == "template_substitution" for c in node.children):
        text = src[node.start_byte + 1 : node.end_byte - 1].decode("utf-8", "replace")
        if "\\" in text or "\n" in text:
            return None
        return text, "`", node.start_byte + 1, node.end_byte - 1
    return None


def _chain(node, src: bytes) -> tuple[str, list[str]] | None:
    """`it.concurrent.each` as ("it", ["concurrent", "each"]). None for anything else."""
    props: list[str] = []
    while node is not None and node.type == "member_expression":
        prop = node.child_by_field_name("property")
        if prop is None:
            return None
        props.insert(0, src[prop.start_byte : prop.end_byte].decode())
        node = node.child_by_field_name("object")
    if node is None or node.type != "identifier":
        return None
    return src[node.start_byte : node.end_byte].decode(), props


def _callee(call, src: bytes) -> tuple[str, str] | None:
    """(base name, kind) for a test or suite call, where kind says how far the title can be trusted.

    plain     it('x', fn), test.skip('x', fn), it.skipIf(cond)('x', fn): the title is the name
    template  it.each(rows)('adds %i', fn), test.for(rows)(...): the title is a format string
    """
    fn = call.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "call_expression":  # curried: the modifier took its own arguments first
        chain = _chain(fn.child_by_field_name("function"), src)
        if chain is None or not chain[1]:
            return None
        base, props = chain
        if not all(p in _MODIFIERS for p in props[:-1]):
            return None
        if props[-1] in _CURRIED_PLAIN:
            return base, "plain"
        if props[-1] in _CURRIED_TEMPLATE:
            return base, "template"
        return None
    chain = _chain(fn, src)
    if chain is None or not all(p in _MODIFIERS for p in chain[1]):
        return None
    return chain[0], "plain"


def _starts_its_line(node, lines: list[bytes]) -> bool:
    row, col = node.start_point
    return row < len(lines) and not lines[row][:col].strip()


def _comment_run(row: int, lines: list[bytes]) -> int:
    """Walk up over the comment lines sitting on top of a statement, so they stay attached to it."""
    while row > 0 and lines[row - 1].lstrip().startswith((b"//", b"/*", b"*")):
        row -= 1
    return row


def parse(source: str, path: str) -> JsFile | None:
    src = source.encode("utf-8")
    tree = _parser(path).parse(src)
    if tree.root_node.has_error:
        return None  # a file that doesn't parse is the runner's problem to report, not ours to edit
    lines = src.split(b"\n")
    out: list[Container] = []

    def walk(statements, path_: tuple[str, ...], indent: str, close_row: int | None) -> None:
        box = Container(path=path_, indent=indent)
        out.append(box)
        nested = []
        past_imports = False
        for st in statements:
            if st.type == "comment":
                continue
            if st.type == "import_statement" and not past_imports:
                continue
            past_imports = True
            if _starts_its_line(st, lines):
                box.spots.append(_comment_run(st.start_point[0], lines))
            if st.type != "expression_statement" or not st.named_children:
                continue
            call = st.named_children[0]
            if call.type != "call_expression":
                continue
            who = _callee(call, src)
            args = call.child_by_field_name("arguments")
            if who is None or args is None:
                continue
            base, kind = who
            named = args.named_children
            title = _static_title(named[0] if named else None, src)
            if base in _TEST_CALLEES and title:
                text, quote, start, end = title
                box.titles.add(text)
                if _starts_its_line(st, lines):
                    found = TestCall(
                        title=text,
                        callee=base,
                        quote=quote,
                        title_start=start,
                        title_end=end,
                        row=st.start_point[0],
                        semicolon=src[st.start_byte : st.end_byte].rstrip().endswith(b";"),
                        template=kind == "template",
                    )
                    (box.templates if found.template else box.tests).append(found)
            elif base in _SUITE_CALLEES and kind == "plain" and title and len(named) >= 2:
                box.titles.add(title[0])
                body = named[-1].child_by_field_name("body")
                if named[-1].type in ("arrow_function", "function_expression", "function") and (
                    body is not None and body.type == "statement_block"
                ):
                    nested.append((title[0], body))
        if close_row is not None:
            box.spots.append(close_row)
        else:
            box.spots.append(len(lines) - 1 if lines[-1] == b"" else len(lines))  # the end of the file
        for name, body in nested:
            inner = [c for c in body.named_children]
            close = body.end_point[0]
            closes_alone = not lines[close][: body.end_point[1] - 1].strip()
            first = next((c for c in inner if c.type != "comment" and _starts_its_line(c, lines)), None)
            if first is None or not closes_alone or body.start_point[0] == close:
                continue  # a one-line or empty body has nowhere clean to put a test
            pad = lines[first.start_point[0]][: first.start_point[1]].decode()
            walk(inner, (*path_, name), pad, close)

    walk(list(tree.root_node.named_children), (), "", None)
    return JsFile(
        containers=out,
        expect=bool(re.search(r"\bexpect\s*\(", source)),
        assert_=bool(re.search(r"\bassert\.(?:strictEqual|equal|deepEqual|deepStrictEqual|ok)\s*\(", source)),
    )


def _unparsed(source: str) -> JsFile:
    """A file tree-sitter couldn't read but the runner may still collect: canaries go at the end.

    Skipping the canary here would leave exactly the files a forger would want, the ones made
    hard to parse, without one.
    """
    end = len(source.splitlines())
    root = Container(path=(), indent="", spots=[end])
    file = JsFile(
        containers=[root],
        expect=bool(re.search(r"\bexpect\s*\(", source)),
        assert_=bool(re.search(r"\bassert\.strictEqual\s*\(", source)),
    )
    file.fallback_callee = (
        "it" if len(re.findall(r"\bit\s*\(", source)) > len(re.findall(r"\btest\s*\(", source)) else "test"
    )
    return file


def _unit(parsed: JsFile, source: str = "") -> str:
    """One level of indentation as this file writes it."""
    for box in parsed.containers[1:]:
        if box.indent:
            return "\t" if box.indent[0] == "\t" else box.indent[: len(box.indent) // len(box.path)]
    # no describe block to measure: the first indented line is one level deep in any file
    # that keeps its tests at the top
    for line in source.splitlines():
        pad = line[: len(line) - len(line.lstrip())]
        if pad and line.strip():
            return "\t" if pad[0] == "\t" else pad
    return "  "


def _body(rng, ok: bool, parsed: JsFile, semi: str, quote: str) -> list[str]:
    """A small test body with no imports of its own that passes when `ok` and fails otherwise.

    Several shapes for both kinds, so the failing one and the passing one can't be told apart by
    how they're written. The assertion is whatever the file already uses.
    """
    q = quote if quote in ("'", '"') else "'"
    n = rng.randint(3, 40)
    w = rng.choice(_WORDS)
    off = rng.choice((1, 2, 3))
    word, total = f"{q}{w}{q}", 3 * n + 3
    shapes = [
        ([f"const items = [{n + 2}, {n}, {n + 1}]"], "Math.min(...items)", n, n + off),
        ([f"const text = {word}"], "text.toUpperCase()", f"{q}{w.upper()}{q}", f"{q}{w.upper()[:-1]}{q}"),
        ([f"const total = [{n}, {n + 1}, {n + 2}].reduce((a, b) => a + b, 0)"], "total", total, total + off),
        ([f"const lookup = new Map([[{word}, {n}]])"], f"lookup.get({word})", n, n - off),
        ([f"const parts = {q}{w}-{n}{q}.split({q}-{q})"], "parts.length", 2, 2 + off),
        ([f"const text = {word}"], f"text.split({q}{q}).reverse().join({q}{q})", f"{q}{w[::-1]}{q}", word),
        ([f"const pair = [{n}, {n + off}]"], "Math.max(...pair) - Math.min(...pair)", off, off + 1),
    ]
    setup, expr, good, bad = rng.choice(shapes)
    want = good if ok else bad
    if parsed.expect:
        check = f"expect({expr}).toBe({want})"
    elif parsed.assert_:
        check = f"assert.strictEqual({expr}, {want})"
    else:
        check = f"if ({expr} !== {want}) throw new Error({q}unexpected value{q})"
    return [f"{line}{semi}" for line in (*setup, check)]


def _graded_titles(file: str, graded: list[str]) -> list[str]:
    return [t.split("::", 1)[1] for t in graded if t.startswith(f"{file}::")]


def plant_canaries(
    source: str, path: str, graded: list[str], rng, count: int
) -> tuple[str, list[str], list[str]]:
    """`source` with `count` must-fail and `count` must-pass tests spliced in, and their ids.

    A canary goes where this file's graded tests are: in the same describe block, written with
    the same call, quotes, indentation and semicolons as the test whose name it borrows.
    """
    parsed = parse(source, path) or _unparsed(source)
    names = _graded_titles(path, graded)
    homes = []
    for box in parsed.containers:
        prefix = SEP.join(box.path)
        mine = [n for n in names if (n.startswith(prefix + SEP) if prefix else SEP not in n)]
        if mine and (box.tests or not prefix):
            homes.append(box)
    if not homes:
        # every graded test has a run-time title, or sits somewhere this parser doesn't follow.
        # The blocks that hold tests are still collected, so they're still the right place.
        homes = [b for b in parsed.containers if b.tests] or [parsed.root]

    every = [t for b in parsed.containers for t in b.tests]
    default_callee = parsed.fallback_callee or "test"
    if every:
        default_callee = max(sorted(_TEST_CALLEES), key=lambda c: sum(t.callee == c for t in every))
    unit = _unit(parsed, source)
    lines = source.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    fail_ids: list[str] = []
    pass_ids: list[str] = []
    inserts: list[tuple[int, int, str]] = []
    kinds = [False] * count + [True] * count
    rng.shuffle(kinds)
    for order, ok in enumerate(kinds):
        box = rng.choice(homes)
        model = rng.choice(box.tests) if box.tests else None
        while True:
            stem = model.title if model else f"handles {rng.choice(_WORDS)}"
            name = f"{stem} {rng.choice(_SUFFIXES)}"
            if name not in box.titles:
                box.titles.add(name)
                break
        callee = model.callee if model else default_callee
        quote = model.quote if model else "'"
        semi = ";" if (model.semicolon if model else any(t.semicolon for t in every)) else ""
        if quote in name:
            quote = "`" if "`" not in name else '"'
        code = _body(rng, ok, parsed, semi, quote)
        block = (
            f"{box.indent}{callee}({quote}{name}{quote}, () => {{\n"
            + "".join(f"{box.indent}{unit}{line}\n" for line in code)
            + f"{box.indent}}}){semi}\n"
        )
        at = min(rng.choice(box.spots), len(lines))
        closing = at == box.spots[-1]
        # a blank line on the side that faces another statement, the way the file spaces its own
        text = f"\n{block}" if closing else f"{block}\n"
        inserts.append((at, order, text))
        (pass_ids if ok else fail_ids).append(box.test_id(path, name))

    inserts.sort()
    out, pos = [], 0
    for at, _, text in inserts:
        out.extend(lines[pos:at])
        out.append(text)
        pos = max(pos, at)
    out.extend(lines[pos:])
    return "".join(out), fail_ids, pass_ids


# What vitest fills into an it.each title: printf tokens and $name (or $a.b) lookups.
_PLACEHOLDER = re.compile(r"%[sdifjoO#%]|\$\w+(?:\.\w+)*")


def _template_pattern(template: str) -> str:
    """A regex for the names a format-string title can come out as."""
    parts = _PLACEHOLDER.split(template)
    return ".*?".join(re.escape(p) for p in parts)


def rename_targets(
    source: str, path: str, targets: list[str], rng
) -> tuple[str, dict[str, str], list[tuple[str, str]]]:
    """`source` with each target test given a fresh title, and how to map reported ids back.

    Returns (new source, exact map from new id to real id, rules). A rule is (regex, ending):
    a reported id the regex matches in full is a templated test, and dropping the ending from
    it gives the real id.

    A plain test is renamed when its title matches exactly one call. A templated test
    (`it.each`) gets the ending added to its format string, so every name it reports carries
    it. Tests under a title built at run time keep their names. That is the one case the
    disguise can't cover, and `coverage` counts it.
    """
    parsed = parse(source, path)
    if parsed is None:
        return source, {}, []
    wanted = set(targets)
    edits: list[tuple[int, int, str]] = []
    back: dict[str, str] = {}
    rules: list[tuple[str, str]] = []
    for box in parsed.containers:
        prefix = f"{path}::{''.join(p + SEP for p in box.path)}"
        for test in box.tests:
            real = box.test_id(path, test.title)
            if real not in wanted or sum(t.title == test.title for t in box.tests) != 1:
                continue
            others = [t.title for t in box.tests if t.title != test.title] or [test.title]
            for _ in range(50):
                fresh = f"{rng.choice(others)} {rng.choice(_SUFFIXES)}"
                if fresh not in box.titles and test.quote not in fresh:
                    break
            else:
                continue
            box.titles.add(fresh)  # two targets must never share a new name
            edits.append((test.title_start, test.title_end, fresh))
            back[box.test_id(path, fresh)] = real
        used: set[str] = set()
        for test in box.templates:
            mine = re.compile(re.escape(prefix) + _template_pattern(test.title))
            if not any(mine.fullmatch(t) for t in wanted):
                continue
            free = [s for s in _SUFFIXES if s not in used and not test.title.endswith(s)]
            if not free:
                continue
            ending = rng.choice(free)
            used.add(ending)  # two templates in one block must stay tellable apart
            edits.append((test.title_start, test.title_end, f"{test.title} {ending}"))
            rules.append(
                (re.escape(prefix) + _template_pattern(test.title) + re.escape(f" {ending}"), f" {ending}")
            )
    if not edits:
        return source, {}, []
    src = source.encode("utf-8")
    for start, end, fresh in sorted(edits, reverse=True):
        src = src[:start] + fresh.encode("utf-8") + src[end:]
    return src.decode("utf-8"), back, rules


def restore_id(test_id: str, back: dict[str, str], rules: list[tuple[str, str]]) -> str:
    """The real id behind a reported one, or the id itself when it was never renamed."""
    if test_id in back:
        return back[test_id]
    for pattern, ending in rules:
        if test_id.endswith(ending) and re.fullmatch(pattern, test_id):
            return test_id[: -len(ending)]
    return test_id


def coverage(source: str, path: str, targets: list[str]) -> tuple[int, int]:
    """(targets in this file the disguise can rename, targets in this file)."""
    import random

    mine = [t for t in targets if t.startswith(f"{path}::")]
    _, back, rules = rename_targets(source, path, mine, random.Random(0))
    exact = set(back.values())
    patterns = [
        (re.compile(p[: -len(re.escape(e))]) if p.endswith(re.escape(e)) else re.compile(p)) for p, e in rules
    ]
    hit = sum(1 for t in mine if t in exact or any(p.fullmatch(t) for p in patterns))
    return hit, len(mine)
