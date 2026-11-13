# Gauntlet Live JS, November 2026

69 coding tasks in JavaScript and TypeScript, made from real pull requests merged on GitHub between 4 August and 10 November 2026. They were built and validated with [gauntlet-harness](../../README.md), and every repo here tests with vitest. If your model's training data stops before August 2026, it hasn't seen these fixes.

This is the second language for the harness. The Python set is [gauntlet-live-2026-11](../gauntlet-live-2026-11).

## Files

| File | What's in it |
|---|---|
| `tasks.jsonl` | One `HarnessTask` per line: statement, base commit, gold patch, hidden test patch, F2P / N2P / P2P test ids and the leak check report |
| `swebench.jsonl` | The same tasks as SWE-bench instances, for any SWE-bench style harness |
| `validation.jsonl` | 759 calibration episodes: 11 policies on every task |
| `stats.json` | The numbers on this page, as JSON |

## Use it

```bash
gauntlet run --tasks tasks.jsonl --policies claude:claude-sonnet-5-5,oracle --attempts 4
```

The first run on a task builds its Docker image at the base commit, from `node:18-slim`, `node:20-slim` or `node:22-slim` depending on what the repo asks for. That takes a minute or two per repo, and it's cached after.

## Validation

Every policy ran on every task, 759 episodes in one go on the code that's in this repo now.

| Policy | Resolved | Caught cheating | Mean reward | Best reward |
|---|---|---|---|---|
| oracle (applies the merged fix) | 68 / 69 | 0 / 69 | +1.000 | +1.000 |
| noop (submits nothing) | 0 / 69 | 0 / 69 | -1.000 | -1.000 |
| cheater (the original, blunt one) | 0 / 69 | 69 / 69 | -1.000 | -1.000 |
| cheater:exit (forces a clean exit code) | 0 / 69 | 69 / 69 | -1.000 | -1.000 |
| cheater:junit (edits the report after the run) | 0 / 69 | 67 / 69 | -0.971 | +0.000 |
| cheater:rewrite (rewrites test files at run time) | 0 / 69 | 62 / 69 | -0.899 | +0.000 |
| cheater:monkeypatch (swaps the assert library) | 0 / 69 | 43 / 69 | -0.623 | +0.000 |
| cheater:stealth (the same, hidden from static checks) | 0 / 69 | 43 / 69 | -0.623 | +0.000 |
| cheater:canary-aware (spares anything named like a canary) | 0 / 69 | 0 / 69 | +0.000 | +0.000 |
| cheater:ghost (the same, hidden in base64) | 0 / 69 | 0 / 69 | +0.000 | +0.000 |
| cheater:snapshot (spares any test name that is new since the base commit) | 0 / 69 | 0 / 69 | +0.000 | +0.000 |

No cheat scored above zero on any task. That's 621 cheating episodes with nothing to show for them.

**The one miss is the oracle on radashi #494, and it's a flaky test, not the grader.** The real fix passed all 3 of its target tests and scored +0.999. One regression test in another file, `AggregateError.test.ts`, failed because the machine was busy with a second batch of Docker builds. I reran the same episode alone three times and it resolved all three. That test is now listed under `flaky` in the 6 radashi tasks that had it as P2P, so it can't decide a grade again. I've left the miss in `validation.jsonl` as it was measured.

**Read the zeros before you trust the table.** A reward of 0 means the cheat did nothing and the task stayed unsolved. It doesn't mean the grader caught something.

- `cheater:monkeypatch` and `cheater:stealth` acted on 43 tasks and were caught on all 43. On the other 26 (magic-string, cheerio, htmlparser2, devalue, confbox) the payload fails to load, because those repos are on a newer vitest that no longer exports the module it hooks. So for those 26 tasks this table says nothing about that trick.
- `cheater:rewrite` did nothing on the 7 immer tasks, and `cheater:junit` did nothing on 2.
- `canary-aware`, `ghost` and `snapshot` score 0 everywhere, and that zero is the defence working. They force tests to pass but spare anything that looks like a canary, and the grader renames the target tests so they look like canaries too. With renaming switched off `canary-aware` forges every target, and a test in the harness repo pins both halves.

## Where the tasks came from

| Repo | Merged PRs looked at | Tasks |
|---|---|---|
| [Rich-Harris/magic-string](https://github.com/Rich-Harris/magic-string) | 15 | 9 |
| [cheeriojs/cheerio](https://github.com/cheeriojs/cheerio) | 80 | 6 |
| [eemeli/yaml](https://github.com/eemeli/yaml) | 10 | 3 |
| [fb55/htmlparser2](https://github.com/fb55/htmlparser2) | 15 | 6 |
| [i18next/i18next](https://github.com/i18next/i18next) | 15 | 11 |
| [immerjs/immer](https://github.com/immerjs/immer) | 20 | 7 |
| [pillarjs/path-to-regexp](https://github.com/pillarjs/path-to-regexp) | 11 | 3 |
| [radashi-org/radashi](https://github.com/radashi-org/radashi) | 15 | 13 |
| [sveltejs/devalue](https://github.com/sveltejs/devalue) | 15 | 2 |
| [unjs/confbox](https://github.com/unjs/confbox) | 10 | 2 |
| [unjs/exsolve](https://github.com/unjs/exsolve) | 13 | 7 |
| **Total** | **219** | **69** |

150 PRs didn't make it, each for a recorded reason:

- 112 changed no JavaScript or TypeScript source (dependency bumps, CI, docs)
- 18 changed no test module
- 15 had no test that the fix moves to passing
- 2 had a fix that breaks a passing test
- 2 had a statement that quotes the fix
- 1 was an older yaml commit still on jest

No task in the set failed a leak check.

## Things to know

- 15 tasks have a linked issue as their statement. The rest use the PR title and body, cleaned of diffs and links. `statement_source` says which.
- 50 of the 69 tasks have TypeScript tests. The other 19 are plain JavaScript.
- Every task runs the repo's whole suite. P2P is 69,726 tests in total with a median of 910 per task, from 28 on a small confbox task to 4,183 on immer.
- Tests are graded by `file::describe > title`, so a row of `it.each` counts as its own test.
- The grader can rename all 339 target tests while it grades (`validation.disguise`). That number was 188 of 227 on the first build, and the gap was real: two cheats solved three immer tasks through it before the renamer learned to follow helper functions.
- 7 tasks carry one flaky test each, dropped from grading: the radashi one above and a timing test in i18next.
- Node version per task: 47 on Node 22, 9 on Node 20, 13 on Node 18.
- Repos that use workspaces, jest or bun aren't here. The harness refuses them with a reason for now.

## Every task

| Task | Merged | Title | F2P | N2P | P2P | Statement |
|---|---|---|---|---|---|---|
| [Rich-Harris__magic-string-366](https://github.com/Rich-Harris/magic-string/pull/366) | 2026-10-26 | fix: skip empty matches at a chunk boundary inside removed content | 3 | 0 | 452 | pr |
| [Rich-Harris__magic-string-367](https://github.com/Rich-Harris/magic-string/pull/367) | 2026-10-26 | fix: don't treat chunks emptied by an overwrite as removed in `replace | 2 | 0 | 456 | pr |
| [Rich-Harris__magic-string-369](https://github.com/Rich-Harris/magic-string/pull/369) | 2026-10-27 | fix: shift replacesUntil and replacedFrom in Bundle#toMagicString | 1 | 0 | 475 | pr |
| [Rich-Harris__magic-string-371](https://github.com/Rich-Harris/magic-string/pull/371) | 2026-10-27 | chore: enable strict type checking | 1 | 0 | 458 | pr |
| [Rich-Harris__magic-string-373](https://github.com/Rich-Harris/magic-string/pull/373) | 2026-10-27 | fix: reject non-integer and out-of-range indices consistently | 11 | 0 | 460 | pr |
| [Rich-Harris__magic-string-374](https://github.com/Rich-Harris/magic-string/pull/374) | 2026-10-27 | fix: keep the stored name when an overwritten range was split | 4 | 0 | 471 | pr |
| [Rich-Harris__magic-string-375](https://github.com/Rich-Harris/magic-string/pull/375) | 2026-10-27 | fix(bundle): stop addSource mutating the caller's source description | 3 | 0 | 473 | pr |
| [Rich-Harris__magic-string-377](https://github.com/Rich-Harris/magic-string/pull/377) | 2026-10-27 | fix: respect indent exclusions on moved edits and CRLF bundle intros | 2 | 0 | 476 | pr |
| [Rich-Harris__magic-string-379](https://github.com/Rich-Harris/magic-string/pull/379) | 2026-10-27 | perf: cut allocations and quadratic lookups on the sourcemap and bundl | 4 | 0 | 484 | pr |
| [cheeriojs__cheerio-5558](https://github.com/cheeriojs/cheerio/pull/5558) | 2026-10-17 | fix(attributes): set data on mixed node selections | 1 | 0 | 821 | issue |
| [cheeriojs__cheerio-5560](https://github.com/cheeriojs/cheerio/pull/5560) | 2026-10-17 | fix(attributes): guard style access on non-element nodes | 1 | 0 | 820 | issue |
| [cheeriojs__cheerio-5614](https://github.com/cheeriojs/cheerio/pull/5614) | 2026-11-06 | fix(traversing): isolate stop predicates for nested Until calls | 2 | 0 | 827 | issue |
| [cheeriojs__cheerio-5645](https://github.com/cheeriojs/cheerio/pull/5645) | 2026-11-07 | fix: preserve selectors when loading a document array | 1 | 0 | 829 | issue |
| [cheeriojs__cheerio-5655](https://github.com/cheeriojs/cheerio/pull/5655) | 2026-11-06 | fix: preserve sources when insertion targets are empty | 2 | 0 | 825 | issue |
| [cheeriojs__cheerio-5708](https://github.com/cheeriojs/cheerio/pull/5708) | 2026-11-06 | feat: export DOM node type guards (#5459) | 1 | 0 | 822 | issue |
| [eemeli__yaml-720](https://github.com/eemeli/yaml/pull/720) | 2026-09-20 | Add `doc.createMergePair()` + do not duck-type `<<` string keys as mer | 17 | 0 | 961 | issue |
| [eemeli__yaml-722](https://github.com/eemeli/yaml/pull/722) | 2026-09-20 | Fix JS type of self-referential mappings used as merge sources | 1 | 0 | 978 | pr |
| [eemeli__yaml-726](https://github.com/eemeli/yaml/pull/726) | 2026-09-23 | Include context arg in reviver calls + serialize raw JSON values | 6 | 0 | 975 | pr |
| [fb55__htmlparser2-2539](https://github.com/fb55/htmlparser2/pull/2539) | 2026-11-06 | fix(parser): make reset() safe inside callbacks | 1 | 0 | 210 | pr |
| [fb55__htmlparser2-2540](https://github.com/fb55/htmlparser2/pull/2540) | 2026-11-06 | fix(parser): recognize CDATA directly inside SVG and MathML integratio | 2 | 0 | 202 | pr |
| [fb55__htmlparser2-2541](https://github.com/fb55/htmlparser2/pull/2541) | 2026-11-06 | fix(parser): preserve prototype-named attributes | 1 | 0 | 201 | pr |
| [fb55__htmlparser2-2542](https://github.com/fb55/htmlparser2/pull/2542) | 2026-11-06 | fix(stream): preserve byte order when string chunks interrupt decoding | 3 | 0 | 211 | pr |
| [fb55__htmlparser2-2547](https://github.com/fb55/htmlparser2/pull/2547) | 2026-11-06 | fix(parser): close an open <p> for li, dd, dt, listing, plaintext and  | 2 | 0 | 207 | pr |
| [fb55__htmlparser2-2549](https://github.com/fb55/htmlparser2/pull/2549) | 2026-11-06 | perf(parser): answer "is this name open" with a count per name | 3 | 0 | 204 | issue |
| [i18next__i18next-2454](https://github.com/i18next/i18next/pull/2454) | 2026-10-21 | fix: replace the matched placeholder, not an earlier copy inside a val | 2 | 0 | 1001 | pr |
| [i18next__i18next-2455](https://github.com/i18next/i18next/pull/2455) | 2026-10-22 | fix: keep placeholders inside unescaped values literal | 3 | 0 | 1005 | pr |
| [i18next__i18next-2456](https://github.com/i18next/i18next/pull/2456) | 2026-10-22 | fix: copy nested resources when cloning with forkResourceStore | 1 | 0 | 1008 | pr |
| [i18next__i18next-2457](https://github.com/i18next/i18next/pull/2457) | 2026-10-23 | fix: format values when cached formatter options are not serializable | 1 | 0 | 1009 | pr |
| [i18next__i18next-2458](https://github.com/i18next/i18next/pull/2458) | 2026-10-24 | fix: pass the resolved details when extending joined arrays | 2 | 0 | 1010 | pr |
| [i18next__i18next-2459](https://github.com/i18next/i18next/pull/2459) | 2026-10-24 | fix: don't interpolate inherited methods like constructor from nested  | 3 | 0 | 1013 | pr |
| [i18next__i18next-2460](https://github.com/i18next/i18next/pull/2460) | 2026-10-24 | fix: don't match a supported language that only starts with the same l | 2 | 0 | 1021 | pr |
| [i18next__i18next-2461](https://github.com/i18next/i18next/pull/2461) | 2026-10-25 | fix: preserve falsy nested resource values | 3 | 0 | 1023 | pr |
| [i18next__i18next-2463](https://github.com/i18next/i18next/pull/2463) | 2026-10-28 | fix: preserve caller options when forking instances | 2 | 0 | 1030 | pr |
| [i18next__i18next-2464](https://github.com/i18next/i18next/pull/2464) | 2026-10-28 | fix: keep nested 0, false and '' interpolation values with a custom ke | 2 | 0 | 1032 | pr |
| [i18next__i18next-2467](https://github.com/i18next/i18next/pull/2467) | 2026-11-07 | fix: keep at most 1000 formatters in each cached format | 1 | 0 | 1097 | pr |
| [immerjs__immer-1314](https://github.com/immerjs/immer/pull/1314) | 2026-10-21 | fix: draft relocated base objects at previously assigned indices in ar | 12 | 0 | 4150 | pr |
| [immerjs__immer-1315](https://github.com/immerjs/immer/pull/1315) | 2026-10-21 | fix: resolve the splice start index against the pre-splice length in a | 4 | 0 | 4032 | pr |
| [immerjs__immer-1316](https://github.com/immerjs/immer/pull/1316) | 2026-10-20 | fix: skip nested patches for children of shifted or reordered arrays | 4 | 0 | 3978 | pr |
| [immerjs__immer-1325](https://github.com/immerjs/immer/pull/1325) | 2026-10-20 | fix: avoid endless iteration in current Set snapshots | 10 | 0 | 3891 | pr |
| [immerjs__immer-1341](https://github.com/immerjs/immer/pull/1341) | 2026-10-21 | fix: snapshot array length before optimized filter callbacks | 2 | 0 | 4165 | pr |
| [immerjs__immer-1345](https://github.com/immerjs/immer/pull/1345) | 2026-10-22 | fix: skip patches of a draft discarded by writing its base value back | 8 | 0 | 4175 | pr |
| [immerjs__immer-1357](https://github.com/immerjs/immer/pull/1357) | 2026-11-10 | fix: honor class-only shallow copy in Immer constructor | 1 | 0 | 4183 | pr |
| [pillarjs__path-to-regexp-443](https://github.com/pillarjs/path-to-regexp/pull/443) | 2026-10-21 | Avoid duplicate trailing delimiter matches | 2 | 85 | 371 | issue |
| [pillarjs__path-to-regexp-448](https://github.com/pillarjs/path-to-regexp/pull/448) | 2026-08-04 | fix: reject wildcard array that compiles to an empty path | 2 | 0 | 448 | pr |
| [pillarjs__path-to-regexp-456](https://github.com/pillarjs/path-to-regexp/pull/456) | 2026-09-22 | Fix stringify lookahead across empty text tokens | 4 | 0 | 454 | pr |
| [radashi-org__radashi-481](https://github.com/radashi-org/radashi/pull/481) | 2026-09-14 | fix(diff,counting): stop inherited Object.prototype keys from corrupti | 1 | 0 | 906 | pr |
| [radashi-org__radashi-484](https://github.com/radashi-org/radashi/pull/484) | 2026-09-17 | fix(selectFirst): map a matched undefined element | 2 | 0 | 910 | issue |
| [radashi-org__radashi-485](https://github.com/radashi-org/radashi/pull/485) | 2026-09-17 | fix(toggle): remove a matching undefined element instead of appending | 2 | 0 | 912 | issue |
| [radashi-org__radashi-486](https://github.com/radashi-org/radashi/pull/486) | 2026-09-21 | fix(memo): stop inherited Object.prototype keys from being treated as  | 4 | 0 | 915 | pr |
| [radashi-org__radashi-487](https://github.com/radashi-org/radashi/pull/487) | 2026-10-27 | fix!: align AggregateError polyfill with native behavior | 21 | 0 | 921 | issue |
| [radashi-org__radashi-488](https://github.com/radashi-org/radashi/pull/488) | 2026-10-23 | fix(parallel): stop treating falsy rejections as success | 5 | 0 | 924 | pr |
| [radashi-org__radashi-489](https://github.com/radashi-org/radashi/pull/489) | 2026-10-23 | fix(round): preserve infinity with nonzero precision | 4 | 0 | 919 | pr |
| [radashi-org__radashi-491](https://github.com/radashi-org/radashi/pull/491) | 2026-11-06 | fix(promiseMemo): retain replacement cache entries after stale rejecti | 1 | 0 | 943 | pr |
| [radashi-org__radashi-492](https://github.com/radashi-org/radashi/pull/492) | 2026-11-06 | fix(promiseMemo): support prototype-named custom cache keys | 4 | 0 | 946 | pr |
| [radashi-org__radashi-493](https://github.com/radashi-org/radashi/pull/493) | 2026-11-06 | fix(Semaphore): drain queued requests while capacity remains | 1 | 0 | 945 | pr |
| [radashi-org__radashi-494](https://github.com/radashi-org/radashi/pull/494) | 2026-11-06 | fix(Semaphore): honor already aborted acquisition signals | 3 | 0 | 951 | pr |
| [radashi-org__radashi-495](https://github.com/radashi-org/radashi/pull/495) | 2026-11-07 | fix(retry): check cancellation before starting attempts | 2 | 0 | 954 | pr |
| [radashi-org__radashi-496](https://github.com/radashi-org/radashi/pull/496) | 2026-11-07 | fix(retry): stop on falsy exit reasons | 5 | 0 | 956 | pr |
| [sveltejs__devalue-189](https://github.com/sveltejs/devalue/pull/189) | 2026-09-21 | fix: populate `error.path` for values inside promises in `stringifyAsy | 2 | 0 | 1247 | pr |
| [sveltejs__devalue-191](https://github.com/sveltejs/devalue/pull/191) | 2026-09-21 | fix(parse): validate indices before dereferencing malformed payloads | 23 | 0 | 1224 | pr |
| [unjs__confbox-99](https://github.com/unjs/confbox/pull/99) | 2026-10-29 | fix(format): honor an explicit indent of `0` or `""` on parsed values | 6 | 0 | 34 | pr |
| [unjs__confbox-103](https://github.com/unjs/confbox/pull/103) | 2026-10-29 | fix(yaml): use a non-zero indent when stringifying flat documents | 3 | 0 | 28 | issue |
| [unjs__exsolve-63](https://github.com/unjs/exsolve/pull/63) | 2026-10-03 | fix: don't keep callers alive through cached errors | 1 | 0 | 83 | pr |
| [unjs__exsolve-72](https://github.com/unjs/exsolve/pull/72) | 2026-10-27 | feat: expose `preserveSymlinks` | 2 | 0 | 89 | pr |
| [unjs__exsolve-73](https://github.com/unjs/exsolve/pull/73) | 2026-10-27 | perf: use `throwIfNoEntry: false` in `tryStatSync` | 2 | 0 | 84 | pr |
| [unjs__exsolve-78](https://github.com/unjs/exsolve/pull/78) | 2026-10-27 | fix: handle symlinked parents, `..`, `try` and empty input for absolut | 8 | 0 | 103 | issue |
| [unjs__exsolve-79](https://github.com/unjs/exsolve/pull/79) | 2026-10-27 | fix: reset package.json cache when the resolve cache is cleared direct | 2 | 0 | 101 | issue |
| [unjs__exsolve-80](https://github.com/unjs/exsolve/pull/80) | 2026-10-27 | fix: validate constructor name in error value type detection | 3 | 0 | 121 | pr |
| [unjs__exsolve-81](https://github.com/unjs/exsolve/pull/81) | 2026-10-27 | fix: report unreadable package.json instead of ignoring it | 1 | 0 | 116 | pr |
