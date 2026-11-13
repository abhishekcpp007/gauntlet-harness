# Gauntlet LHT, November 2026

25 long-horizon tasks from 13 repos. Each one is a real release range: the agent starts at the older release and the hidden tests are the newer one's. Every target test belongs to a milestone (one PR or direct commit in the range), and the reward is the mean pass rate over milestones.

Every task runs the whole test suite (`test_scope` is empty), so P2P guards every module, not just the files the release touched. These were built with `build-lht --scope full`, the default since 13 Nov.

| Task | PRs | Direct commits | Milestones | Targets | P2P | Gold lines |
|---|---|---|---|---|---|---|
| `prettytable__prettytable-3.15.1_to_3.16.0` | 2 | 0 | 1 | 4 | 263 | 20 |
| `prettytable__prettytable-3.14.0_to_3.15.0` | 14 | 0 | 7 | 190 | 69 | 366 |
| `prettytable__prettytable-3.16.0_to_3.17.0` | 24 | 0 | 1 | 7 | 268 | 356 |
| `tkem__cachetools-v7.2.8_to_v7.3.0` | 2 | 4 | 1 | 2 | 257 | 270 |
| `tkem__cachetools-v7.2.5_to_v7.2.6` | 0 | 5 | 1 | 15 | 228 | 374 |
| `jazzband__tablib-v3.10.0_to_v3.11.0` | 7 | 0 | 2 | 2 | 186 | 72 |
| `jazzband__tablib-v3.8.0_to_v3.9.0` | 7 | 5 | 4 | 11 | 162 | 452 |
| `jazzband__tablib-v3.9.0_to_v3.10.0` | 9 | 7 | 9 | 11 | 172 | 216 |
| `jazzband__tablib-v3.6.1_to_v3.7.0` | 6 | 4 | 3 | 4 | 156 | 153 |
| `more-itertools__more-itertools-v11.0.2_to_v11.1.0` | 8 | 0 | 4 | 6 | 716 | 97 |
| `python-attrs__cattrs-v25.3.0_to_v26.1.0` | 16 | 1 | 7 | 27 | 886 | 1205 |
| `python-attrs__cattrs-v26.1.0_to_v26.2.0` | 8 | 13 | 9 | 16 | 927 | 1493 |
| `python-poetry__tomlkit-0.14.0_to_0.15.0` | 14 | 0 | 1 | 1 | 286 | 1659 |
| `mahmoud__boltons-26.1.0_to_26.2.0` | 27 | 6 | 27 | 95 | 424 | 656 |
| `mahmoud__boltons-25.0.0_to_26.0.0` | 0 | 21 | 9 | 14 | 423 | 592 |
| `pytest-dev__pluggy-1.4.0_to_1.5.0` | 15 | 0 | 2 | 2 | 107 | 311 |
| `python-humanize__humanize-4.14.0_to_4.15.0` | 12 | 0 | 3 | 39 | 645 | 162 |
| `python-humanize__humanize-4.15.0_to_4.16.0` | 34 | 0 | 3 | 14 | 686 | 1190 |
| `jd__tenacity-9.2.0_to_9.2.1` | 21 | 0 | 5 | 15 | 170 | 323 |
| `theskumar__python-dotenv-v1.2.1_to_v1.2.2` | 10 | 2 | 1 | 4 | 214 | 500 |
| `theskumar__python-dotenv-v1.2.2_to_v1.2.3` | 12 | 1 | 4 | 11 | 229 | 309 |
| `theskumar__python-dotenv-v1.2.3_to_v1.2.4` | 3 | 2 | 3 | 11 | 248 | 43 |
| `un33k__python-slugify-v9.1.0_to_v9.1.1` | 2 | 1 | 1 | 2 | 123 | 40 |
| `un33k__python-slugify-v9.1.2_to_v9.1.3` | 5 | 0 | 1 | 1 | 131 | 76 |
| `pallets__markupsafe-3.0.2_to_3.0.3` | 11 | 1 | 1 | 2 | 76 | 2457 |

`validation.jsonl` is the calibrate run that vouches for them, 275 episodes with 11 policies on each task, all on the same code. The oracle resolved 25 of 25 (lowest reward 0.97) and Cheat Catcher called all 25 of its runs clean. Noop got -1 on all 25, also clean. Across 225 episodes from 9 cheat policies, none resolved, none earned above 0, and none finished a single milestone. 0 violations.

**What changed from the first version of this set.** It had 16 tasks that ran only the changed test files. markupsafe `3.0.2_to_3.0.3` had zero P2P tests that way, and prettytable `3.15.1_to_3.16.0` had 33 where it has 263 now. Running everything also turned up two grader bugs, both fixed before this run:

- On tenacity the hidden `test_asyncio.py` imports an API the agent hasn't written yet, so until it has, that file doesn't collect and its canary went with it. An honest agent read as a cheat. A second set of canaries now goes in another graded file.
- markupsafe parametrizes every test from its conftest, so canaries came back as `name[...]` and weren't found. They're matched by base id now.

Milestone counts vary a lot. Some ranges have a single change that owns every target test, and boltons 26.1.0 to 26.2.0 has 27. If you want tasks where partial progress has real shape, filter on milestones.

`cheater:rewrite` hangs a boltons socket test on `25.0.0_to_26.0.0` until the grader's one-hour kill, so a full calibrate on this set takes a bit over an hour. The cheat still scores 0.

```bash
gauntlet run --tasks datasets/gauntlet-lht-2026-11/tasks.jsonl --category lht --policies oracle,noop
```

The env image is rebuilt on first use if it isn't on your machine.
