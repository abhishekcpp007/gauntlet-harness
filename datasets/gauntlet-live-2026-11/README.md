# Gauntlet Live, November 2026

53 coding tasks made from real pull requests merged on GitHub between 7 September and 30 October 2026, built and validated with [gauntlet-harness](../../README.md). If your model's training data stops before September 2026, it hasn't seen these fixes.

## Files

| File | What's in it |
|---|---|
| `tasks.jsonl` | One `HarnessTask` per line: statement, base commit, gold patch, hidden test patch, F2P / N2P / S2P / P2P test ids, rubric, and the leak check report |
| `swebench.jsonl` | The same tasks as SWE-bench instances, for any SWE-bench style harness |
| `validation.jsonl` | 159 calibration episodes: oracle, noop and cheater on every task |
| `stats.json` | The numbers on this page, as JSON |

## Use it

```bash
gauntlet run --tasks tasks.jsonl --policies claude:claude-sonnet-5-5,oracle --attempts 4
```

The first run on a task builds its Docker image at the base commit, which takes a minute or two per repo. After that it's cached.

## Validation

| Policy | Resolved | Caught cheating | Mean reward |
|---|---|---|---|
| oracle (applies the merged fix) | 53 / 53 | 0 | +0.999 |
| noop (submits nothing) | 0 / 53 | 0 | -1.000 |
| cheater (forges pytest reports, skips target tests) | 0 / 53 | 53 / 53 | -1.000 |

The oracle's mean is 0.999 and not 1.0 because of one task. The merged fix for python-slugify #198 also edits `tox.ini`, which belongs to the grader, so that edit gets dropped and the rubric's test-tampering pitfall fires. The fix still resolves the task.

## Where the tasks came from

| Repo | Merged PRs looked at | Tasks |
|---|---|---|
| [hukkin/tomli](https://github.com/hukkin/tomli) | 12 | 1 |
| [jazzband/tablib](https://github.com/jazzband/tablib) | 15 | 0 |
| [mahmoud/boltons](https://github.com/mahmoud/boltons) | 15 | 15 |
| [more-itertools/more-itertools](https://github.com/more-itertools/more-itertools) | 15 | 8 |
| [pallets/itsdangerous](https://github.com/pallets/itsdangerous) | 0 | 0 |
| [pallets/markupsafe](https://github.com/pallets/markupsafe) | 9 | 0 |
| [prettytable/prettytable](https://github.com/prettytable/prettytable) | 15 | 1 |
| [pypa/packaging](https://github.com/pypa/packaging) | 15 | 6 |
| [pytest-dev/pluggy](https://github.com/pytest-dev/pluggy) | 15 | 5 |
| [python-attrs/cattrs](https://github.com/python-attrs/cattrs) | 15 | 3 |
| [python-humanize/humanize](https://github.com/python-humanize/humanize) | 15 | 0 |
| [python-poetry/tomlkit](https://github.com/python-poetry/tomlkit) | 15 | 4 |
| [tkem/cachetools](https://github.com/tkem/cachetools) | 15 | 4 |
| [un33k/python-slugify](https://github.com/un33k/python-slugify) | 15 | 6 |
| **Total** | **186** | **53** |

133 PRs didn't make it, each for a recorded reason: 115 changed no Python source or no tests (dependency bumps, CI, docs), 16 had no test that the fix moves to passing, and 2 had a statement that quotes the fix. No task in the set failed a leak check.

## Things to know

- 14 tasks have a linked issue as their statement. The rest use the PR title and body, cleaned of diffs and links. `statement_source` says which.
- 6 packaging tasks run only the PR's own test modules, because the full suite (63,350 tests) doesn't fit in the time budget. `validation.scope` is `changed` for those.
- boltons gives 15 tasks because its maintainers merged a sweep of small bug fixes in September 2026, each with its own tests.
- Tests are graded by their pytest node id, so the counts below are test cases, parametrized ones included.

## Every task

| Task | Merged | Title | F2P | N2P | P2P | Statement |
|---|---|---|---|---|---|---|
| [hukkin__tomli-318](https://github.com/hukkin/tomli/pull/318) | 2026-10-06 | Set smaller MAX_INLINE_NESTING to avoid python:3.13-alpine worker thre | 1 | 0 | 16 | pr |
| [mahmoud__boltons-434](https://github.com/mahmoud/boltons/pull/434) | 2026-09-07 | Classify Pearson types IV/V/VI instead of raising 'missed a spot' | 0 | 511 | 0 | pr |
| [mahmoud__boltons-440](https://github.com/mahmoud/boltons/pull/440) | 2026-09-07 | Make IndexedSet bulk removal linear via one-pass rebuild | 3 | 0 | 493 | issue |
| [mahmoud__boltons-461](https://github.com/mahmoud/boltons/pull/461) | 2026-09-07 | `ecoutils`: skip the lookups that the `scrub` parameter removes | 2 | 0 | 474 | pr |
| [mahmoud__boltons-464](https://github.com/mahmoud/boltons/pull/464) | 2026-09-07 | Support relative seeking in binary JSONL streams | 1 | 0 | 504 | pr |
| [mahmoud__boltons-465](https://github.com/mahmoud/boltons/pull/465) | 2026-09-07 | Accept the documented end position in JSONLIterator | 1 | 0 | 503 | pr |
| [mahmoud__boltons-467](https://github.com/mahmoud/boltons/pull/467) | 2026-09-07 | Handle zero interquartile range in automatic histograms | 1 | 0 | 505 | pr |
| [mahmoud__boltons-468](https://github.com/mahmoud/boltons/pull/468) | 2026-09-07 | Preserve empty Bits across binary and byte conversions | 1 | 0 | 488 | pr |
| [mahmoud__boltons-471](https://github.com/mahmoud/boltons/pull/471) | 2026-09-07 | Normalize BarrelList slice bounds and reverse steps | 1 | 0 | 500 | pr |
| [mahmoud__boltons-472](https://github.com/mahmoud/boltons/pull/472) | 2026-09-07 | Keep the stop barrel valid during cross-barrel deletion | 2 | 0 | 501 | pr |
| [mahmoud__boltons-474](https://github.com/mahmoud/boltons/pull/474) | 2026-09-07 | Fix IndexedSet.update with multiple iterables | 4 | 0 | 496 | pr |
| [mahmoud__boltons-475](https://github.com/mahmoud/boltons/pull/475) | 2026-09-07 | Fix Unicode line separators in iter_splitlines | 4 | 0 | 506 | pr |
| [mahmoud__boltons-481](https://github.com/mahmoud/boltons/pull/481) | 2026-09-07 | Complete September PR sweep for 26.2.0 | 11 | 0 | 508 | pr |
| [mahmoud__boltons-513](https://github.com/mahmoud/boltons/pull/513) | 2026-09-23 | Fix spooled stream write compatibility and backwards text seeks | 6 | 0 | 519 | pr |
| [mahmoud__boltons-609](https://github.com/mahmoud/boltons/pull/609) | 2026-10-25 | October PR sweep for 26.3.0 | 90 | 0 | 527 | pr |
| [mahmoud__boltons-610](https://github.com/mahmoud/boltons/pull/610) | 2026-10-25 | October PR sweep follow-up: #482, #483, #605, #607, #608 | 6 | 0 | 615 | issue |
| [more-itertools__more-itertools-1260](https://github.com/more-itertools/more-itertools/pull/1260) | 2026-09-18 | Validate running window sizes through the index protocol | 3 | 0 | 760 | pr |
| [more-itertools__more-itertools-1269](https://github.com/more-itertools/more-itertools/pull/1269) | 2026-09-21 | Issue 1268: seekable.peek/bool no longer drop items when maxlen is 0 | 2 | 0 | 763 | pr |
| [more-itertools__more-itertools-1270](https://github.com/more-itertools/more-itertools/pull/1270) | 2026-09-10 | fix nonpositive item limits in constrained_batches | 1 | 0 | 748 | pr |
| [more-itertools__more-itertools-1294](https://github.com/more-itertools/more-itertools/pull/1294) | 2026-09-25 | Bound lookahead storage for zero-cache seekable iterators | 3 | 0 | 763 | issue |
| [more-itertools__more-itertools-1305](https://github.com/more-itertools/more-itertools/pull/1305) | 2026-09-29 | Issue 1304: ichunked matches chunked for n=0 and negative n | 2 | 0 | 765 | pr |
| [more-itertools__more-itertools-1309](https://github.com/more-itertools/more-itertools/pull/1309) | 2026-10-07 | fix: honour a falsey validator in bucket (#1308) | 1 | 0 | 767 | pr |
| [more-itertools__more-itertools-1322](https://github.com/more-itertools/more-itertools/pull/1322) | 2026-10-30 | peekable: avoid caching entire iterable on negative slice with omitted | 2 | 0 | 765 | pr |
| [more-itertools__more-itertools-1342](https://github.com/more-itertools/more-itertools/pull/1342) | 2026-10-30 | fix: validate the chunk size in chunked_even | 2 | 0 | 767 | pr |
| [prettytable__prettytable-475](https://github.com/prettytable/prettytable/pull/475) | 2026-10-30 | Handle rows with fewer cells than header in `from_html()` | 1 | 0 | 337 | issue |
| [pypa__packaging-1372](https://github.com/pypa/packaging/pull/1372) | 2026-10-24 | perf(specifiers): cache a Specifier's canonical form | 13 | 0 | 2076 | pr |
| [pypa__packaging-1377](https://github.com/pypa/packaging/pull/1377) | 2026-10-20 | feat(requirements): add Requirement.__replace__ | 6 | 0 | 5350 | pr |
| [pypa__packaging-1457](https://github.com/pypa/packaging/pull/1457) | 2026-10-20 | feat(specifiers): add `__replace__` to Specifier and SpecifierSet | 7 | 0 | 2038 | pr |
| [pypa__packaging-1458](https://github.com/pypa/packaging/pull/1458) | 2026-10-20 | perf(ranges): compare cached version keys | 1 | 0 | 3092 | pr |
| [pypa__packaging-1459](https://github.com/pypa/packaging/pull/1459) | 2026-10-20 | perf(markers): cache formatted marker strings | 4 | 0 | 7682 | pr |
| [pypa__packaging-1460](https://github.com/pypa/packaging/pull/1460) | 2026-10-20 | perf(ranges): subtract version intervals directly | 6 | 0 | 3190 | pr |
| [pytest-dev__pluggy-752](https://github.com/pytest-dev/pluggy/pull/752) | 2026-10-14 | manager: ignore `register(None)` instead of raising `TypeError` | 1 | 0 | 218 | issue |
| [pytest-dev__pluggy-759](https://github.com/pytest-dev/pluggy/pull/759) | 2026-10-09 | Raise ValueError when unregister's plugin and name disagree | 2 | 0 | 215 | pr |
| [pytest-dev__pluggy-767](https://github.com/pytest-dev/pluggy/pull/767) | 2026-10-17 | Drop entry-point distribution info when unregistering a plugin | 1 | 0 | 219 | issue |
| [pytest-dev__pluggy-799](https://github.com/pytest-dev/pluggy/pull/799) | 2026-10-18 | Keep hook tracing from crashing on impostor values | 5 | 0 | 220 | issue |
| [pytest-dev__pluggy-809](https://github.com/pytest-dev/pluggy/pull/809) | 2026-10-21 | Fix distribution tracking for declined entry points | 1 | 0 | 225 | issue |
| [python-attrs__cattrs-776](https://github.com/python-attrs/cattrs/pull/776) | 2026-09-07 | Sort extra keys when formatting ForbiddenExtraKeysError | 1 | 0 | 943 | pr |
| [python-attrs__cattrs-777](https://github.com/python-attrs/cattrs/pull/777) | 2026-09-14 | Escape the index note when structuring heterogeneous tuples | 1 | 0 | 944 | pr |
| [python-attrs__cattrs-788](https://github.com/python-attrs/cattrs/pull/788) | 2026-10-02 | Inherit omit_if_default when generating dict unstructure hooks | 8 | 0 | 1016 | issue |
| [python-poetry__tomlkit-660](https://github.com/python-poetry/tomlkit/pull/660) | 2026-10-16 | Compare Bool items by value | 1 | 0 | 522 | pr |
| [python-poetry__tomlkit-666](https://github.com/python-poetry/tomlkit/pull/666) | 2026-10-29 | Speed up array appends | 1 | 0 | 543 | issue |
| [python-poetry__tomlkit-673](https://github.com/python-poetry/tomlkit/pull/673) | 2026-10-27 | Reject datetime strings in the date factory | 3 | 0 | 529 | pr |
| [python-poetry__tomlkit-683](https://github.com/python-poetry/tomlkit/pull/683) | 2026-10-27 | Copy trivia in _new so arithmetic results cannot mutate the original i | 6 | 0 | 523 | pr |
| [tkem__cachetools-424](https://github.com/tkem/cachetools/pull/424) | 2026-09-27 | Fix #423: Detect an empty RRCache before calling choice | 1 | 0 | 213 | issue |
| [tkem__cachetools-436](https://github.com/tkem/cachetools/pull/436) | 2026-10-11 | Fix #435: Restore class access to cached instance methods | 2 | 0 | 203 | issue |
| [tkem__cachetools-441](https://github.com/tkem/cachetools/pull/441) | 2026-10-12 | Fix #440: Compute TTL expiration before changing cache state | 1 | 0 | 208 | issue |
| [tkem__cachetools-445](https://github.com/tkem/cachetools/pull/445) | 2026-10-19 | Restore autospec calls to @cachedmethod decorated methods | 1 | 0 | 223 | issue |
| [un33k__python-slugify-193](https://github.com/un33k/python-slugify/pull/193) | 2026-09-18 | Preserve fitting post-replacement output during modern truncation | 1 | 0 | 110 | pr |
| [un33k__python-slugify-195](https://github.com/un33k/python-slugify/pull/195) | 2026-09-18 | Support uppercase hexadecimal references in modern slugs | 3 | 0 | 107 | pr |
| [un33k__python-slugify-198](https://github.com/un33k/python-slugify/pull/198) | 2026-09-18 | Freeze legacy into _legacy.py, reorganize tests, ship 9.1.0 | 0 | 123 | 0 | pr |
| [un33k__python-slugify-200](https://github.com/un33k/python-slugify/pull/200) | 2026-09-22 | Avoid trailing post-replacement delimiters in modern hard cuts | 2 | 0 | 123 | pr |
| [un33k__python-slugify-201](https://github.com/un33k/python-slugify/pull/201) | 2026-09-27 | Skip the transliteration backend for ASCII text | 1 | 0 | 125 | pr |
| [un33k__python-slugify-204](https://github.com/un33k/python-slugify/pull/204) | 2026-10-07 | Preserve replacement delimiters in modern word-boundary truncation | 1 | 0 | 131 | pr |

Each task keeps its upstream project's license. The task metadata here is MIT.
