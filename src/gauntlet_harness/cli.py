"""`gauntlet`: collect PRs, build leak-free tasks, run models on them, report.

gauntlet collect  --repo pallets/click --merged-after 2026-06-01 --limit 30 -o prs.jsonl
gauntlet build    --prs prs.jsonl -o tasks.jsonl
gauntlet leakcheck --tasks tasks.jsonl
gauntlet run      --tasks tasks.jsonl --policies oracle,noop,claude:claude-opus-5-5 --attempts 3 -o runs/
gauntlet report   --results runs/results.jsonl --cutoff 2026-06-01
gauntlet pipeline --repo pallets/click --merged-after 2026-06-01 --policies ... -o runs/
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .schema import HarnessTask, PRInfo, Rejected


def _say(msg: str) -> None:
    print(f"  {msg}", file=sys.stderr, flush=True)


def _claude_client() -> Any:
    """An Anthropic client when credentials are configured, otherwise None (rule-only judging)."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    return anthropic.Anthropic()


def _read_models(path: str, model: type) -> list:
    lines = Path(path).read_text().splitlines()
    return [model.model_validate_json(line) for line in lines if line.strip()]


def _append(path: Path, obj: Any) -> None:
    with path.open("a") as f:
        f.write(obj.model_dump_json() + "\n")


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


# ---------------------------------------------------------------------- commands


def cmd_collect(args: argparse.Namespace) -> int:
    from .github import GitHubClient, RateLimited, prs_from_git

    out = Path(args.output)
    out.write_text("")
    total = 0
    client = GitHubClient()
    for repo in args.repo:
        try:
            prs = client.list_merged_prs(
                repo, merged_after=_date(args.merged_after) if args.merged_after else None, limit=args.limit
            )
        except RateLimited as e:
            if not args.git_fallback:
                print(f"error: GitHub rate limit, resets at {e.reset_at}.")
                print("Set GITHUB_TOKEN or pass --git-fallback.")
                return 1
            from gauntlet_repo2env.repo import clone

            _say(f"rate limited, falling back to git history for {repo}")
            path = clone(f"https://github.com/{repo}").path
            prs = prs_from_git(
                path,
                repo,
                limit=args.limit,
                merged_after=_date(args.merged_after) if args.merged_after else None,
            )
        for pr in prs:
            _append(out, pr)
        total += len(prs)
        _say(f"{repo}: {len(prs)} merged PRs")
    print(f"collected {total} PRs into {out}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    from .build import build_task
    from .leakcheck import leakcheck

    prs = _read_models(args.prs, PRInfo)
    out, rej = Path(args.output), Path(args.rejected)
    out.write_text("")
    rej.write_text("")
    client = _claude_client() if args.rubric else None
    kept = 0
    for pr in prs:
        _say(f"{pr.repo}#{pr.number}: {pr.title[:70]}")
        result = build_task(pr, python=args.python, scope=args.scope, fix_runs=args.fix_runs, log=_say)
        if isinstance(result, Rejected):
            _say(f"  rejected at {result.stage}: {result.reason}")
            _append(rej, result)
            continue
        task: HarnessTask = result
        if not args.no_leakcheck:
            report = leakcheck(task)
            task.validation["leakcheck"] = report.model_dump()
            if not report.ok:
                bad = [name for name, c in report.checks.items() if not c.ok]
                _say(f"  rejected at leak: {', '.join(bad)}")
                _append(rej, Rejected(instance_id=task.instance_id, stage="leak", reason=", ".join(bad)))
                continue
        if client is not None:
            from .judge import generate_rubric
            from .rubric import default_rubric

            task.rubric = default_rubric(task) + generate_rubric(task, client, model=args.judge_model)
        kept += 1
        counts = ", ".join(f"{k}={len(getattr(task, k))}" for k in ("f2p", "n2p", "s2p", "p2p"))
        _say(f"  TASK {task.instance_id}: {counts}")
        _append(out, task)
    print(f"built {kept} task(s) from {len(prs)} PRs into {out} (rejections in {rej})")
    return 0 if kept else 1


def cmd_leakcheck(args: argparse.Namespace) -> int:
    from .leakcheck import leakcheck

    bad = 0
    for task in _read_models(args.tasks, HarnessTask):
        report = leakcheck(task)
        failing = [name for name, c in report.checks.items() if not c.ok]
        bad += not report.ok
        print(f"{'ok  ' if report.ok else 'LEAK'} {task.instance_id} {', '.join(failing)}")
    return 1 if bad else 0


def cmd_run(args: argparse.Namespace) -> int:
    from .report import render_markdown, summarize
    from .runner import run

    tasks = _read_models(args.tasks, HarnessTask)
    policies = [p.strip() for p in args.policies.split(",") if p.strip()]
    needs_claude = any(p.startswith("claude") for p in policies)
    client = _claude_client()
    if needs_claude and client is None:
        print("error: claude policies need ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN)", file=sys.stderr)
        return 2

    def show(r) -> None:
        g = r.grade
        mark = "SOLVED" if g and g.resolved and r.cheat_verdict != "cheating" else "-"
        _say(f"{r.policy:28} {r.instance_id:36} {mark:6} reward={r.reward:+.3f} cheat={r.cheat_verdict}")

    results = run(
        tasks,
        policies,
        out_dir=Path(args.output),
        attempts=args.attempts,
        workers=args.workers,
        max_steps=args.max_steps,
        judge_client=client if args.judge else None,
        judge_model=args.judge_model,
        policy_client=client,
        on_result=show,
    )
    from .runner import load_results

    print(render_markdown(summarize(load_results(Path(args.output) / "results.jsonl"))))
    return 0 if all(r.error is None for r in results) else 1


def cmd_report(args: argparse.Namespace) -> int:
    from .report import render_html, render_markdown, summarize
    from .runner import load_results

    summary = summarize(
        load_results(Path(args.results)), cutoff=date.fromisoformat(args.cutoff) if args.cutoff else None
    )
    print(render_markdown(summary))
    if args.html:
        Path(args.html).write_text(render_html(summary))
        print(f"wrote {args.html}")
    return 0


def cmd_pipeline(args: argparse.Namespace) -> int:
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    prs, tasks = out / "prs.jsonl", out / "tasks.jsonl"
    steps = [
        (cmd_collect, dict(repo=args.repo, merged_after=args.merged_after, limit=args.limit,
                           git_fallback=True, output=str(prs))),
        (cmd_build, dict(prs=str(prs), output=str(tasks), rejected=str(out / "rejected.jsonl"),
                         python=args.python, scope="full", fix_runs=2, no_leakcheck=False,
                         rubric=args.judge, judge_model=args.judge_model)),
        (cmd_run, dict(tasks=str(tasks), policies=args.policies, output=str(out), attempts=args.attempts,
                       workers=args.workers, max_steps=args.max_steps, judge=args.judge,
                       judge_model=args.judge_model)),
    ]  # fmt: skip
    for fn, kw in steps:
        code = fn(argparse.Namespace(**kw))
        if code:
            return code
    return cmd_report(argparse.Namespace(results=str(out / "results.jsonl"), cutoff=args.merged_after,
                                         html=str(out / "report.html")))  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gauntlet", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def runner_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--policies", default="oracle,noop,cheater",
                       help="comma separated: oracle, noop, cheater, claude:<model id>")  # fmt: skip
        p.add_argument("--attempts", type=int, default=1, help="episodes per task per policy (for pass@k)")
        p.add_argument("--workers", type=int, default=2)
        p.add_argument("--max-steps", type=int, default=60)
        p.add_argument(
            "--judge", action="store_true", help="LLM rubric judging (needs Anthropic credentials)"
        )
        p.add_argument("--judge-model", default="claude-opus-5-5")

    p = sub.add_parser("collect", help="fetch recently merged PRs")
    p.add_argument("--repo", action="append", required=True, help="owner/name, repeatable")
    p.add_argument("--merged-after", help="YYYY-MM-DD")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--git-fallback", action="store_true", help="use git history if the API rate-limits")
    p.add_argument("-o", "--output", default="prs.jsonl")
    p.set_defaults(fn=cmd_collect)

    p = sub.add_parser("build", help="turn PRs into validated, leak-checked tasks")
    p.add_argument("--prs", required=True)
    p.add_argument("-o", "--output", default="tasks.jsonl")
    p.add_argument("--rejected", default="rejected.jsonl")
    p.add_argument("--python", default="3.12")
    p.add_argument(
        "--scope",
        choices=["full", "changed"],
        default="full",
        help="full runs the whole suite (catches regressions anywhere), changed only touched test files",
    )
    p.add_argument("--fix-runs", type=int, default=2, help="repeat the fix run to catch flaky tests")
    p.add_argument("--no-leakcheck", action="store_true")
    p.add_argument("--rubric", action="store_true", help="add LLM-written rubric items (needs credentials)")
    p.add_argument("--judge-model", default="claude-opus-5-5")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("leakcheck", help="re-run the leak checks on built tasks")
    p.add_argument("--tasks", required=True)
    p.set_defaults(fn=cmd_leakcheck)

    p = sub.add_parser("run", help="run policies on tasks")
    p.add_argument("--tasks", required=True)
    p.add_argument("-o", "--output", default="runs")
    runner_args(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("report", help="leaderboard from results.jsonl")
    p.add_argument("--results", required=True)
    p.add_argument("--cutoff", help="YYYY-MM-DD, split metrics before/after to spot contamination")
    p.add_argument("--html")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("pipeline", help="collect, build, run and report in one go")
    p.add_argument("--repo", action="append", required=True)
    p.add_argument("--merged-after")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--python", default="3.12")
    p.add_argument("-o", "--output", default="runs")
    runner_args(p)
    p.set_defaults(fn=cmd_pipeline)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
