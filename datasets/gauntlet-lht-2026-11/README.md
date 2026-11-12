# Gauntlet LHT, November 2026

16 long-horizon tasks from 8 repos. Each one is a real release range: the agent starts at the older release and the hidden tests are the newer one's. Every target test belongs to a milestone (one PR or direct commit in the range), and the reward is the mean pass rate over milestones.

| Task | PRs | Direct commits | Milestones | Targets | P2P | Gold lines |
|---|---|---|---|---|---|---|
| `prettytable__prettytable-3.15.1_to_3.16.0` | 2 | 0 | 1 | 4 | 33 | 20 |
| `prettytable__prettytable-3.14.0_to_3.15.0` | 14 | 0 | 7 | 190 | 69 | 366 |
| `prettytable__prettytable-3.16.0_to_3.17.0` | 24 | 0 | 1 | 7 | 171 | 356 |
| `tkem__cachetools-v7.2.8_to_v7.3.0` | 2 | 4 | 1 | 2 | 230 | 270 |
| `tkem__cachetools-v7.2.5_to_v7.2.6` | 0 | 5 | 1 | 15 | 111 | 374 |
| `jazzband__tablib-v3.10.0_to_v3.11.0` | 7 | 0 | 2 | 2 | 180 | 72 |
| `jazzband__tablib-v3.8.0_to_v3.9.0` | 7 | 5 | 4 | 11 | 142 | 452 |
| `jazzband__tablib-v3.9.0_to_v3.10.0` | 9 | 7 | 9 | 11 | 152 | 216 |
| `jazzband__tablib-v3.6.1_to_v3.7.0` | 6 | 4 | 3 | 4 | 136 | 153 |
| `more-itertools__more-itertools-v11.0.2_to_v11.1.0` | 8 | 0 | 4 | 6 | 576 | 97 |
| `python-attrs__cattrs-v25.3.0_to_v26.1.0` | 16 | 1 | 7 | 27 | 242 | 1205 |
| `python-attrs__cattrs-v26.1.0_to_v26.2.0` | 8 | 13 | 9 | 16 | 354 | 1493 |
| `python-poetry__tomlkit-0.14.0_to_0.15.0` | 14 | 0 | 1 | 1 | 286 | 1659 |
| `mahmoud__boltons-26.1.0_to_26.2.0` | 27 | 6 | 27 | 95 | 293 | 656 |
| `mahmoud__boltons-25.0.0_to_26.0.0` | 0 | 21 | 9 | 14 | 216 | 592 |
| `pytest-dev__pluggy-1.4.0_to_1.5.0` | 15 | 0 | 2 | 2 | 63 | 311 |

`validation.jsonl` is the calibrate run that vouches for them, 176 episodes with 11 policies on each task. The oracle resolved 16 of 16 (lowest reward 0.97). Noop got -1 on all 16. Across 144 episodes from 9 cheat policies, none resolved, none earned above 0, and none finished a single milestone. The three cattrs and tomlkit tasks were re-run after the big-patch fix described in the harness README, so their 33 episodes come from that second run.

The pluggy task came last, after a repo2env fix (see the harness README), so its 11 episodes are from a run on 13 Nov.

Cheat Catcher used to call the cattrs `v26.1.0_to_v26.2.0` *gold* patch "suspicious". The release moved its `addopts` from `[tool.pytest.ini_options]` to pytest 9's `[tool.pytest]` table with the same options, and any `addopts` edit counted. A move with an identical set of options is a low finding now, and that task's oracle episode was re-run to show it clean. Rescanning every patch in both datasets with the new rule changed no other verdict.

Milestone counts vary a lot. Some ranges have a single change that owns every target test, and boltons 26.1.0 to 26.2.0 has 27. If you want tasks where partial progress has real shape, filter on milestones.

```bash
gauntlet run --tasks datasets/gauntlet-lht-2026-11/tasks.jsonl --category lht --policies oracle,noop
```

The env image is rebuilt on first use if it isn't on your machine.
