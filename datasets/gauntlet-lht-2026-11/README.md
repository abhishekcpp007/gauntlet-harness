# Gauntlet LHT, November 2026

Three long-horizon tasks, each one a real release range. The agent starts at the older release and the hidden tests are the newer one's.

| Task | PRs | Direct commits | Targets | P2P | Gold lines |
|---|---|---|---|---|---|
| `prettytable__prettytable-3.15.1_to_3.16.0` | 2 | 0 | 4 | 33 | 20 |
| `tkem__cachetools-v7.2.8_to_v7.3.0` | 2 | 4 | 2 | 230 | 270 |
| `tkem__cachetools-v7.2.5_to_v7.2.6` | 0 | 5 | 15 | 111 | 374 |

`validation.jsonl` is the calibrate run that vouches for them: 33 episodes, 0 violations. The oracle resolved all 3 and no cheat earned above 0.

This is a small set on purpose. It's the first proof that the LHT path works end to end on real repos, not a benchmark yet. Each task has one milestone, because in these ranges only one change owned the target tests. Ranges with many PRs, where milestones actually spread the reward, need repos with bigger releases and richer tests.

```bash
gauntlet run --tasks datasets/gauntlet-lht-2026-11/tasks.jsonl --category lht --policies oracle,noop
```

The env image is rebuilt on first use if it isn't on your machine.
