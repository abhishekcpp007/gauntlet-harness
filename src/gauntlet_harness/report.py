"""Turn results.jsonl into a leaderboard.

pass@k uses the unbiased estimator from Chen et al. (2021): per task, with n attempts and c
resolved, pass@k = 1 - C(n - c, k) / C(n, k), averaged over tasks. A resolved episode only
counts when Cheat Catcher didn't call it cheating.

With a cutoff date, every metric is also split into tasks merged before and after it, the
SWE-rebench way of spotting contamination: a model that does much better on older PRs has
probably seen them.
"""

from __future__ import annotations

import html
from collections import defaultdict
from datetime import date, datetime
from math import comb
from statistics import mean
from typing import Any

from .schema import ALL_KINDS, EpisodeResult


def solved(r: EpisodeResult) -> bool:
    return bool(r.grade and r.grade.resolved and r.cheat_verdict != "cheating")


def pass_at_k(results: list[EpisodeResult], k: int) -> float | None:
    by_task: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        by_task[r.instance_id].append(solved(r))
    scores = []
    for outcomes in by_task.values():
        n, c = len(outcomes), sum(outcomes)
        if n < k:
            continue
        scores.append(1.0 if n - c < k else 1.0 - comb(n - c, k) / comb(n, k))
    return round(mean(scores), 4) if scores else None


def _avg(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(mean(vals), 4) if vals else None


def _block(results: list[EpisodeResult]) -> dict[str, Any]:
    grades = [r.grade for r in results if r.grade is not None]
    out: dict[str, Any] = {
        "episodes": len(results),
        "tasks": len({r.instance_id for r in results}),
        "resolved_rate": round(sum(solved(r) for r in results) / len(results), 4) if results else 0.0,
        "pass@1": pass_at_k(results, 1),
        "mean_reward": _avg([r.reward for r in results]),
        "mean_rubric": _avg([r.rubric.score if r.rubric else None for r in results]),
        "cheating_rate": round(sum(r.cheat_verdict == "cheating" for r in results) / len(results), 4)
        if results
        else 0.0,
        "errors": sum(1 for r in results if r.error),
        "mean_steps": _avg([float(r.steps) for r in results]),
        "mean_seconds": _avg([r.seconds for r in results]),
        "tokens": {
            key: sum(r.usage.get(key, 0) for r in results) for key in ("input_tokens", "output_tokens")
        },
    }
    for kind in ALL_KINDS:
        out[f"{kind}_rate"] = _avg([g.rate(kind) for g in grades])
    attempts = max((r.attempt + 1 for r in results), default=1)
    for k in (2, 5):
        if attempts >= k:
            out[f"pass@{k}"] = pass_at_k(results, k)
    return out


def _merged(r: EpisodeResult) -> date | None:
    if not r.merged_at:
        return None
    try:
        return datetime.fromisoformat(r.merged_at.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def summarize(results: list[EpisodeResult], *, cutoff: date | None = None) -> dict[str, Any]:
    by_policy: dict[str, list[EpisodeResult]] = defaultdict(list)
    for r in results:
        by_policy[r.policy].append(r)
    table = []
    for policy, rs in by_policy.items():
        row = {"policy": policy, **_block(rs)}
        if cutoff:
            before = [r for r in rs if (d := _merged(r)) and d < cutoff]
            after = [r for r in rs if (d := _merged(r)) and d >= cutoff]
            row["before_cutoff"] = _block(before) if before else None
            row["after_cutoff"] = _block(after) if after else None
        table.append(row)
    table.sort(key=lambda r: (-r["resolved_rate"], -(r["mean_reward"] or 0)))
    return {"cutoff": cutoff.isoformat() if cutoff else None, "policies": table}


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


COLUMNS = [
    ("policy", "Policy"),
    ("resolved_rate", "Resolved"),
    ("pass@1", "pass@1"),
    ("f2p_rate", "F2P"),
    ("n2p_rate", "N2P"),
    ("s2p_rate", "S2P"),
    ("p2p_rate", "P2P"),
    ("mean_rubric", "Rubric"),
    ("mean_reward", "Reward"),
    ("cheating_rate", "Cheating"),
    ("episodes", "Episodes"),
]


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "| " + " | ".join(h for _, h in COLUMNS) + " |",
        "|" + "---|" * len(COLUMNS),
    ]
    for row in summary["policies"]:
        lines.append("| " + " | ".join(_fmt(row.get(k)) for k, _ in COLUMNS) + " |")
    if summary.get("cutoff"):
        lines += ["", f"Resolved rate before / after {summary['cutoff']} (contamination check):", ""]
        for row in summary["policies"]:
            b, a = row.get("before_cutoff"), row.get("after_cutoff")
            before = _fmt(b and b["resolved_rate"])
            after = _fmt(a and a["resolved_rate"])
            lines.append(f"- {row['policy']}: {before} before, {after} after")
    return "\n".join(lines) + "\n"


def render_html(summary: dict[str, Any], title: str = "Gauntlet harness results") -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for _, h in COLUMNS)
    rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(_fmt(row.get(k)))}</td>" for k, _ in COLUMNS) + "</tr>"
        for row in summary["policies"]
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font:15px system-ui;margin:32px;background:#0d0f13;color:#e8ebf1}}table{{border-collapse:collapse}}
th,td{{padding:8px 14px;border-bottom:1px solid #2a2f3a;text-align:left}}th{{color:#98a1b2;font-size:12px;text-transform:uppercase}}
@media (prefers-color-scheme: light){{body{{background:#f6f7f9;color:#16181d}}th,td{{border-color:#dde1e7}}}}</style></head>
<body><h1>{html.escape(title)}</h1><table><tr>{head}</tr>{rows}</table>
<pre>{html.escape(render_markdown(summary))}</pre></body></html>"""
