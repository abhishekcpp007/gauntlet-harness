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
    # set when one call reports under many names, or under a name the source doesn't spell out:
    # a regex for the names it can come out as. See `_title` for the cases.
    pattern: str | None = None
    wrap: bool = False  # the title is an expression, renamed by wrapping it and not by editing text


@dataclass
class Container:
    """The top of the file, or the body of one describe block."""

    path: tuple[str, ...]
    indent: str  # what a statement directly inside it starts with
    tests: list[TestCall] = field(default_factory=list)
    # tests whose reported name isn't simply their title: it.each, a template string with a
    # value in it, a variable, or anything inside a loop or a describe block like that.
    # Never a name to borrow, and renamed by an appended ending instead of a new title.
    templates: list[TestCall] = field(default_factory=list)
    spots: list[int] = field(default_factory=list)  # rows a new statement can go in front of
    titles: set[str] = field(default_factory=set)  # every static title here, tests and describes
    # a regex for "outer > inner > ", and whether any part of it was built at run time
    pattern: str = ""
    dynamic: bool = False

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


_EXPRESSIONS = {
    "identifier",
    "member_expression",
    "subscript_expression",
    "binary_expression",
    "call_expression",
}
# What vitest fills into an it.each title: printf tokens and $name (or $a.b) lookups.
_PLACEHOLDER = re.compile(r"%[sdifjoO#%]|\$\w+(?:\.\w+)*")
ANY = ".*?"


def _printf_pattern(template: str) -> str:
    """A regex for the names an it.each format string can come out as."""
    return ANY.join(re.escape(p) for p in _PLACEHOLDER.split(template))


def _title(node, src: bytes) -> tuple[str, str, int, int, str | None, bool] | None:
    """(text, quote, start, end, pattern, wrap) for the first argument of a test or suite call.

    pattern is None for a plain string, where the text is the name. Otherwise it is a regex
    for the names the call can report:

        `adds ${a} and ${b}`   the fixed parts, with anything between them
        name, row.title, 'a' + b   anything at all, and `wrap` is set: there is no text to
                                   edit, so a rename wraps the expression instead

    None when the argument is something a title can't be read from or safely edited.
    """
    if node is None:
        return None
    text = src[node.start_byte + 1 : node.end_byte - 1].decode("utf-8", "replace")
    if node.type == "string":
        if "\\" in text:
            return None  # an escape would need decoding to match the reported name
        return (
            text,
            src[node.start_byte : node.start_byte + 1].decode(),
            node.start_byte + 1,
            node.end_byte - 1,
            None,
            False,
        )
    if node.type == "template_string":
        if "\\" in text or "\n" in text:
            return None
        if not any(c.type == "template_substitution" for c in node.children):
            return text, "`", node.start_byte + 1, node.end_byte - 1, None, False
        parts, at = [], node.start_byte + 1
        for child in node.children:
            if child.type == "template_substitution":
                parts.append(re.escape(src[at : child.start_byte].decode("utf-8", "replace")))
                at = child.end_byte
        parts.append(re.escape(src[at : node.end_byte - 1].decode("utf-8", "replace")))
        return text, "`", node.start_byte + 1, node.end_byte - 1, ANY.join(parts), False
    if node.type in _EXPRESSIONS:
        whole = src[node.start_byte : node.end_byte].decode("utf-8", "replace")
        return whole, "'", node.start_byte, node.end_byte, ANY, True
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


_LOOPS = {"for_statement", "for_in_statement", "while_statement", "do_statement"}
_ITERATORS = {"forEach", "map"}


def _function_body(node):
    """The statement block of an arrow function or a function expression, if it has one."""
    if node is None or node.type not in ("arrow_function", "function_expression", "function"):
        return None
    body = node.child_by_field_name("body")
    return body if body is not None and body.type == "statement_block" else None


_HELPER = "\0helper"  # marks a nested block that is a function body, not a describe block
_DECLARATIONS = {"lexical_declaration", "variable_declaration"}


def _helper_bodies(st) -> list:
    """The bodies of functions `st` declares: `function check(name) {...}`, `const check = (name) => {...}`.

    immer writes whole groups of tests this way, a describe block inside a function that is then
    called once per configuration. Nothing at the call site says a test is being made, so the
    function body is read instead, as a block that can end up anywhere.
    """
    if st.type == "export_statement":
        st = st.child_by_field_name("declaration")
        if st is None:
            return []
    if st.type in ("function_declaration", "generator_function_declaration"):
        body = st.child_by_field_name("body")
        return [body] if body is not None and body.type == "statement_block" else []
    if st.type in _DECLARATIONS:
        bodies = [_function_body(c.child_by_field_name("value")) for c in st.named_children]
        return [b for b in bodies if b is not None]
    return []


def parse(source: str, path: str) -> JsFile | None:
    src = source.encode("utf-8")
    tree = _parser(path).parse(src)
    # A syntax error somewhere doesn't make the rest of the file unreadable. exsolve has a test
    # file where the TypeScript grammar trips on `importOriginal<typeof import("node:fs")>()`
    # inside a mock, twenty lines above a describe block that parses fine. Giving up on the
    # whole file left that task's targets with their real names. So a statement with an error
    # in it is skipped (a describe block is still entered, its own statements are checked one
    # by one), and a block that holds such a statement takes no canary.
    lines = src.split(b"\n")
    out: list[Container] = []

    def scan(statements, box: Container, nested: list, looped: bool) -> None:
        """Collect the tests and describe blocks among `statements` into `box`.

        `looped` is set once we are inside anything that can run its body more than once or
        not at all: a for loop, an if, a forEach callback. A test in there may report several
        times, so it is never a plain test and its line is never a place to plant a canary.
        """
        past_imports = looped
        for st in statements:
            if st.type == "comment":
                continue
            if st.type == "import_statement" and not past_imports:
                continue
            past_imports = True
            bad = st.has_error
            if not looped and not bad and _starts_its_line(st, lines):
                box.spots.append(_comment_run(st.start_point[0], lines))
            if bad and st.type != "expression_statement":
                continue
            helpers = _helper_bodies(st)
            if helpers:
                nested.extend(("", _HELPER, body) for body in helpers)
                continue
            if st.type in _LOOPS or st.type == "statement_block":
                body = st.child_by_field_name("body") if st.type in _LOOPS else st
                if body is not None:
                    scan(body.named_children if body.type == "statement_block" else [body], box, nested, True)
                continue
            if st.type == "if_statement":
                for branch in (st.child_by_field_name("consequence"), st.child_by_field_name("alternative")):
                    if branch is None:
                        continue
                    inner = branch.named_children[0] if branch.type == "else_clause" else branch
                    scan(
                        inner.named_children if inner.type == "statement_block" else [inner],
                        box,
                        nested,
                        True,
                    )
                continue
            if st.type != "expression_statement" or not st.named_children:
                continue
            call = st.named_children[0]
            if call.type != "call_expression":
                continue
            args = call.child_by_field_name("arguments")
            if args is None:
                continue
            named = args.named_children
            who = _callee(call, src)
            if who is None:
                if bad:
                    continue
                # rows.forEach((row) => { it(...) }): the callback's body is a loop body
                fn = call.child_by_field_name("function")
                prop = (
                    fn.child_by_field_name("property")
                    if fn is not None and fn.type == "member_expression"
                    else None
                )
                body = _function_body(named[-1]) if named else None
                if (
                    prop is not None
                    and body is not None
                    and src[prop.start_byte : prop.end_byte].decode() in _ITERATORS
                ):
                    scan(body.named_children, box, nested, True)
                elif body is not None:
                    nested.append(("", _HELPER, body))
                continue
            base, kind = who
            if base not in _TEST_CALLEES and base not in _SUITE_CALLEES:
                # describeFn(name, () => { test(...) }): some other function is handed a
                # callback. immer picks `describe` or a pass-through into a variable and calls
                # that. Whatever it is, tests in the callback may land under any block.
                body = _function_body(named[-1]) if named and not bad else None
                if body is not None:
                    nested.append(("", _HELPER, body))
                continue
            title = _title(named[0] if named else None, src)
            if title is None:
                continue
            text, quote, start, end, pattern, wrap = title
            if kind == "template" and pattern is None:
                pattern = _printf_pattern(text)
            if base in _TEST_CALLEES:
                if bad:
                    continue
                if not wrap:
                    box.titles.add(text)
                if not _starts_its_line(st, lines):
                    continue
                found = TestCall(
                    title=text,
                    callee=base,
                    quote=quote,
                    title_start=start,
                    title_end=end,
                    row=st.start_point[0],
                    semicolon=src[st.start_byte : st.end_byte].rstrip().endswith(b";"),
                    pattern=pattern,
                    wrap=wrap,
                )
                if pattern is None and not looped and not box.dynamic:
                    box.tests.append(found)
                else:
                    found.pattern = pattern if pattern is not None else re.escape(text)
                    box.templates.append(found)
            elif base in _SUITE_CALLEES and len(named) >= 2:
                body = _function_body(named[-1])
                if body is None:
                    continue
                if pattern is None and not looped and not box.dynamic:
                    box.titles.add(text)
                    nested.append((text, None, body))
                else:
                    nested.append((text, pattern if pattern is not None else re.escape(text), body))

    def walk(
        statements, parent: Container | None, name: str, piece: str | None, indent: str, close_row
    ) -> None:
        if parent is None:
            box = Container(path=(), indent=indent)
        elif piece is None:
            box = Container(
                path=(*parent.path, name), indent=indent, pattern=parent.pattern + re.escape(name + SEP)
            )
        elif piece is _HELPER:
            # tests written inside a function, to be registered wherever it is called from:
            # under the top of the file, or under any number of describe blocks
            anywhere = parent.pattern + "(?:.*" + re.escape(SEP) + ")?"
            box = Container(path=parent.path, indent=indent, pattern=anywhere, dynamic=True)
        else:  # a describe block whose name isn't known until it runs
            box = Container(
                path=parent.path, indent=indent, pattern=parent.pattern + piece + re.escape(SEP), dynamic=True
            )
        out.append(box)
        nested: list = []
        scan(statements, box, nested, False)
        clean = not any(st.has_error for st in statements)
        if not clean:
            box.spots.clear()
        if close_row is None:
            box.spots.append(len(lines) - 1 if lines[-1] == b"" else len(lines))  # the end of the file
        elif clean:
            box.spots.append(close_row)
        for child_name, child_piece, body in nested:
            inner = list(body.named_children)
            close = body.end_point[0]
            closes_alone = not lines[close][: body.end_point[1] - 1].strip()
            first = next((c for c in inner if c.type != "comment" and _starts_its_line(c, lines)), None)
            if first is None or not closes_alone or body.start_point[0] == close:
                continue  # a one-line or empty body has nowhere clean to put a test
            pad = lines[first.start_point[0]][: first.start_point[1]].decode()
            walk(inner, box, child_name, child_piece, pad, close)

    walk(list(tree.root_node.named_children), None, "", None, "", None)
    if tree.root_node.has_error and not any(box.tests or box.templates for box in out):
        return None  # nothing readable in it: the caller falls back to the end of the file
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
        # a describe block written at the top of the file: its body is exactly one level in.
        # Blocks named at run time are left out, their depth in the file isn't their depth here.
        if box.indent and len(box.path) == 1 and not box.dynamic:
            return "\t" if box.indent[0] == "\t" else box.indent
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


_VITEST_IMPORT = re.compile(r"import\s*(?:type\s*)?\{([^}]*)\}\s*from\s*['\"](?:vitest|@jest/globals)['\"]")


def _usable_callee(source: str, calls: list[TestCall], fallback: str | None) -> str:
    """`it` or `test`, whichever a new test at the top of this file can actually call.

    Most files import what they use from vitest and nothing else. i18next's import `it` and
    never `test`, and a canary written as `test(...)` was a ReferenceError that took the whole
    file down with it: no canary reported, and the honest oracle was flagged for tampering.
    When nothing is imported the runner's globals are on and both names exist.
    """
    imported = {
        part.split(" as ")[-1].strip()
        for names in _VITEST_IMPORT.findall(source)
        for part in names.split(",")
        if part.strip()
    }
    usable = sorted(_TEST_CALLEES & imported) or sorted(_TEST_CALLEES)
    used = [c.callee for c in calls if c.callee in usable]
    if used:
        return max(usable, key=used.count)
    return fallback if fallback in usable else usable[0]


def _stem(style: TestCall | None, rng) -> str:
    """Words for a canary's name when its block has no plain test to borrow one from."""
    if style is not None and not style.wrap:
        # `correctly nests for ${args} args` -> "correctly nests for", then a word of our own
        head = re.split(r"\$\{|%[sdifjoO#]|\$\w", style.title)[0].strip()
        if len(head.split()) >= 2 and style.quote not in head:
            return f"{head} {rng.choice(_WORDS)}"
    return f"handles {rng.choice(_WORDS)}"


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
        if box.dynamic:
            continue  # a canary in a block that runs per row would report under names we can't predict
        prefix = SEP.join(box.path)
        mine = [n for n in names if (n.startswith(prefix + SEP) if prefix else SEP not in n)]
        if mine and box.spots and (box.tests or box.templates or not prefix):
            homes.append(box)
    if not homes:
        # every graded test has a run-time title, or sits somewhere this parser doesn't follow.
        # The blocks that hold tests are still collected, so they're still the right place.
        homes = [b for b in parsed.containers if (b.tests or b.templates) and b.spots and not b.dynamic] or [
            parsed.root
        ]

    every = [t for b in parsed.containers for t in (*b.tests, *b.templates)]
    default_callee = _usable_callee(source, every, parsed.fallback_callee)
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
        # no written-out test to copy: take the call, quotes and semicolons from a generated
        # one in the same block, and the words its title starts with
        style = model or (rng.choice(box.templates) if box.templates else None)
        while True:
            stem = model.title if model else _stem(style, rng)
            name = f"{stem} {rng.choice(_SUFFIXES)}"
            if name not in box.titles:
                box.titles.add(name)
                break
        callee = style.callee if style else default_callee
        quote = style.quote if style and not style.wrap else "'"
        semi = ";" if (style.semicolon if style else any(t.semicolon for t in every)) else ""
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


def rename_targets(
    source: str, path: str, targets: list[str], rng
) -> tuple[str, dict[str, str], list[tuple[str, str]]]:
    """`source` with each target test given a fresh title, and how to map reported ids back.

    Returns (new source, exact map from new id to real id, rules). A rule is (regex, ending):
    a reported id the regex matches in full is a renamed test, and dropping the ending from it
    gives the real id.

    A plain test is renamed in place when its title matches exactly one call. Everything else
    that can be a target gets an ending added to whatever produces its name: the format string
    of an `it.each`, the template string of a title with a value in it, or the expression
    itself, wrapped as `(expr) + ' ending'`. That covers tests made in loops and under describe
    blocks named at run time, which is how i18next and path-to-regexp write most of theirs.
    """
    parsed = parse(source, path)
    if parsed is None:
        return source, {}, []
    wanted = set(targets)
    edits: list[tuple[int, int, str]] = []
    back: dict[str, str] = {}
    rules: list[tuple[str, str]] = []
    for box in parsed.containers:
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
        for test in box.templates:
            full = re.escape(f"{path}::") + box.pattern + (test.pattern or re.escape(test.title))
            mine = re.compile(full)
            if not any(mine.fullmatch(t) for t in wanted):
                continue
            free = [s for s in _SUFFIXES if not test.title.endswith(s)]
            ending = f" {rng.choice(free)}"
            if test.wrap:
                edits.append((test.title_start, test.title_end, f"({test.title}) + '{ending}'"))
            else:
                edits.append((test.title_start, test.title_end, test.title + ending))
            rules.append((full + re.escape(ending), ending))
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


_SNAP_KEY = re.compile(r"^exports\[`((?:[^`\\\\]|\\\\.)*)`\] = ", re.MULTILINE)


def snapshot_path(path: str) -> str:
    """Where vitest keeps the snapshots of the test file at `path`."""
    p = PurePosixPath(path)
    return str(p.parent / "__snapshots__" / f"{p.name}.snap")


def rename_snapshot_keys(
    snap: str, path: str, back: dict[str, str], rules: list[tuple[str, str]], runner: str = "vitest"
) -> str:
    """A .snap file with the keys of renamed tests renamed to match.

    vitest stores what `toMatchSnapshot()` saw under the test's full name plus a counter:
    ``exports[`Events > CDATA 1`]``. Rename the test and the lookup misses. Under CI a missing
    snapshot is a failure and not something to write, so the gold fix for htmlparser2 #2540
    failed its own two target tests while the canary and every other test passed. A hint
    (`toMatchSnapshot('hint')`) sits between the name and the counter, so the name is matched
    as a prefix.

    jest writes the same file with two differences: the describe titles are joined by a space
    (``exports[`Events CDATA 1`]``) and a hint follows a colon (``Events CDATA: hint 1``).
    """
    jest = runner == "jest"
    forward = {real: new for new, real in back.items()}
    patterns = [(re.compile(p[: -len(re.escape(e))]), e) for p, e in rules]
    if jest:
        forward = {k.replace(SEP, " "): v.replace(SEP, " ") for k, v in forward.items()}
        gap = re.escape(SEP)
        patterns = [(re.compile(p.pattern.replace(gap, re.escape(" "))), e) for p, e in patterns]

    def renamed(test_id: str) -> str | None:
        if test_id in forward:
            return forward[test_id]
        for pattern, ending in patterns:
            if pattern.fullmatch(test_id):
                return test_id + ending
        return None

    def cuts(head: str):
        """(test name, what follows it), longest name first."""
        if jest:
            yield head, ""
            at = head.rfind(": ")
            while at > 0:
                yield head[:at], head[at:]
                at = head.rfind(": ", 0, at)
            return
        pieces = head.split(SEP)
        for cut in range(len(pieces), 0, -1):
            yield SEP.join(pieces[:cut]), "".join(SEP + p for p in pieces[cut:])

    def swap(match: re.Match) -> str:
        key = match.group(1)
        name = key.replace("\\`", "`").replace("\\${", "${").replace("\\\\", "\\")
        head, _, counter = name.rpartition(" ")
        if not counter.isdigit():
            return match.group(0)
        for test, rest in cuts(head):
            new = renamed(f"{path}::{test}")
            if new is None:
                continue
            fresh = new.split("::", 1)[1] + rest + f" {counter}"
            fresh = fresh.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
            return f"exports[`{fresh}`] = "
        return match.group(0)

    return _SNAP_KEY.sub(swap, snap)


def coverage(source: str, path: str, targets: list[str]) -> tuple[int, int]:
    """(targets in this file the disguise can rename, targets in this file)."""
    import random

    mine = [t for t in targets if t.startswith(f"{path}::")]
    _, back, rules = rename_targets(source, path, mine, random.Random(0))
    exact = set(back.values())
    patterns = [(re.compile(p), e) for p, e in rules]
    hit = sum(1 for t in mine if t in exact or any(p.fullmatch(t + e) for p, e in patterns))
    return hit, len(mine)
