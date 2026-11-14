"""`gauntlet`: collect PRs, build leak-free tasks, run models on them, report.

gauntlet collect  --repo pallets/click --merged-after 2026-06-01 --limit 30 -o prs.jsonl
gauntlet build    --prs prs.jsonl -o tasks.jsonl
gauntlet leakcheck --tasks tasks.jsonl
gauntlet run      --tasks tasks.jsonl --policies oracle,noop,claude:claude-opus-5-5 --attempts 3 -o runs/
gauntlet report   --results runs/results.jsonl --cutoff 2026-06-01
gauntlet export   --format sft --results runs/results.jsonl -o sft.jsonl
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


def _out(path: str) -> Path:
    """An output path whose folder exists, so `-o out/prs.jsonl` works on a fresh checkout."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


# ---------------------------------------------------------------------- commands


def cmd_collect(args: argparse.Namespace) -> int:
    from .github import GitHubClient, RateLimited, prs_from_git

    out = _out(args.output)
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

    prs = _read_models(args.prs, PRInfo)
    out, rej = _out(args.output), _out(args.rejected)
    out.write_text("")
    rej.write_text("")
    client = _claude_client() if args.rubric else None
    kept = 0
    for pr in prs:
        _say(f"{pr.repo}#{pr.number}: {pr.title[:70]}")
        try:
            result = build_task(pr, python=args.python, scope=args.scope, fix_runs=args.fix_runs, log=_say)
        except Exception as e:  # one broken PR shouldn't sink the batch
            iid = f"{pr.repo.replace('/', '__')}-{pr.number}"
            result = Rejected(instance_id=iid, stage="crash", reason=f"{type(e).__name__}: {e}"[:500])
        if isinstance(result, Rejected):
            _say(f"  rejected at {result.stage}: {result.reason}")
            _append(rej, result)
            continue
        if _keep(result, args, out, rej, client):
            kept += 1
    print(f"built {kept} task(s) from {len(prs)} PRs into {out} (rejections in {rej})")
    return 0 if kept else 1


def _keep(task: HarnessTask, args: argparse.Namespace, out: Path, rej: Path, client: Any) -> bool:
    """Leak check, optional LLM rubric, then write the task. Shared by build and build-lht."""
    from .leakcheck import leakcheck

    if not args.no_leakcheck:
        report = leakcheck(task)
        task.validation["leakcheck"] = report.model_dump()
        if not report.ok:
            bad = [name for name, c in report.checks.items() if not c.ok]
            _say(f"  rejected at leak: {', '.join(bad)}")
            info = {name: report.checks[name].detail for name in bad}
            _append(
                rej, Rejected(instance_id=task.instance_id, stage="leak", reason=", ".join(bad), detail=info)
            )
            return False
    if client is not None:
        from .judge import generate_rubric
        from .rubric import default_rubric

        task.rubric = default_rubric(task) + generate_rubric(task, client, model=args.judge_model)
    counts = ", ".join(f"{k}={len(getattr(task, k))}" for k in ("f2p", "n2p", "s2p", "p2p"))
    if task.horizon is not None:
        counts += f", milestones={len(task.horizon.milestones)}, prs={len(task.horizon.prs)}"
    _say(f"  TASK {task.instance_id} [{task.category}]: {counts}")
    _append(out, task)
    return True


def cmd_ranges(args: argparse.Namespace) -> int:
    """List the release ranges a repo offers, newest last, as `from to` pairs."""
    from gauntlet_repo2env.repo import clone, git

    from .lht import prs_in_range, release_ranges

    repo = clone(args.repo_url or f"https://github.com/{args.repo}.git")
    for a, b in release_ranges(repo, last=args.last, prereleases=args.prereleases):
        base = git(repo.path, "rev-parse", f"{a}^{{commit}}").strip()
        head = git(repo.path, "rev-parse", f"{b}^{{commit}}").strip()
        prs, direct = prs_in_range(repo, args.repo, base, head)
        print(f"{a}\t{b}\t{len(prs)} PRs\t{len(direct)} direct commits")
    return 0


def cmd_build_lht(args: argparse.Namespace) -> int:
    from gauntlet_repo2env.repo import clone

    from .lht import build_range_task, release_ranges

    if args.range:
        pairs = [tuple(r.split("..", 1)) for r in args.range]
        if any(len(p) != 2 for p in pairs):
            raise SystemExit("--range takes FROM..TO, for example v1.2.0..v1.3.0")
    else:
        pairs = release_ranges(clone(args.repo_url or f"https://github.com/{args.repo}.git"), last=args.last)
    out, rej = _out(args.output), _out(args.rejected)
    if not args.append:
        out.write_text("")
        rej.write_text("")
    client = _claude_client() if args.rubric else None
    kept = 0
    for a, b in pairs:
        _say(f"{args.repo} {a} -> {b}")
        try:
            result = build_range_task(
                args.repo,
                a,
                b,
                python=args.python,
                fix_runs=args.fix_runs,
                repo_url=args.repo_url,
                max_prs=args.max_prs,
                max_gold_lines=args.max_gold_lines,
                scope=args.scope,
                log=_say,
            )
        except Exception as e:
            from .lht import range_id

            result = Rejected(
                instance_id=range_id(args.repo, a, b), stage="crash", reason=f"{type(e).__name__}: {e}"[:500]
            )
        if isinstance(result, Rejected):
            _say(f"  rejected at {result.stage}: {result.reason}")
            _append(rej, result)
            continue
        if _keep(result, args, out, rej, client):
            kept += 1
    print(f"built {kept} LHT task(s) from {len(pairs)} release range(s) into {out} (rejections in {rej})")
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


def cmd_check_envs(args: argparse.Namespace) -> int:
    """Recompute each task's env fingerprint the way a fresh machine would before rebuilding it.

    A task file outlives the code that built it. If repo2env's install script for a repo moves,
    `ensure_image` rebuilds a different env and refuses to grade on it, but only on the first
    machine without the image, long after the change that broke it.
    """
    from gauntlet_repo2env.repo import clone

    from .build import env_fingerprint, fetch_commit

    repos: dict[str, Any] = {}
    drifted = 0
    for path in args.tasks:
        for task in _read_models(path, HarnessTask):
            if task.repo not in repos:
                repos[task.repo] = clone(task.repo)
            fetch_commit(repos[task.repo], task.base_commit)
            python = str(task.validation.get("python", "3.12"))
            now, _ = env_fingerprint(repos[task.repo], task.base_commit, python)
            ok = now == task.env_fingerprint
            drifted += not ok
            shipped = task.env_fingerprint[:16]
            print(f"{'ok   ' if ok else 'DRIFT'} {task.instance_id} {shipped} now {now[:16]}")
    return 1 if drifted else 0


def _budget(args: argparse.Namespace):
    """A Budget when any ceiling was asked for, otherwise None."""
    from .budget import Budget, parse_prices

    names = ("max_episodes", "max_tokens", "max_seconds", "max_usd")
    if not any(getattr(args, n, None) for n in names) and not getattr(args, "prices", None):
        return None
    return Budget(
        max_episodes=getattr(args, "max_episodes", None),
        max_tokens=getattr(args, "max_tokens", None),
        max_seconds=getattr(args, "max_seconds", None),
        max_usd=getattr(args, "max_usd", None),
        prices=parse_prices(getattr(args, "prices", None) or ""),
    )


def cmd_run(args: argparse.Namespace) -> int:
    from .report import render_markdown, summarize
    from .runner import run

    tasks = _read_models(args.tasks, HarnessTask)
    if args.category:
        tasks = [t for t in tasks if t.category == args.category]
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

    budget = _budget(args)
    results = run(
        tasks,
        policies,
        out_dir=Path(args.output),
        attempts=args.attempts,
        workers=args.workers,
        max_steps=args.max_steps,
        budget=budget,
        judge_client=client if args.judge else None,
        judge_model=args.judge_model,
        policy_client=client,
        on_result=show,
    )
    from .runner import load_results

    print(render_markdown(summarize(load_results(Path(args.output) / "results.jsonl"))))
    if budget is not None and budget.stopped_because:
        _say(f"\nstopped early: {budget.stopped_because}")
        _say(f"spent: {budget.summary()}")
        _say("raise the ceiling and run the same command again to carry on where it stopped")
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


def cmd_serve(args: argparse.Namespace) -> int:
    from .serve import EnvServer, serve

    tasks = {t.instance_id: t for t in _read_models(args.tasks, HarnessTask)}
    task = tasks.get(args.task) if args.task else next(iter(tasks.values()), None)
    if task is None:
        print(f"error: no task {args.task!r} in {args.tasks}", file=sys.stderr)
        return 2
    env_server = EnvServer(task, policy=args.policy, out_dir=Path(args.output), max_steps=args.max_steps)
    httpd = serve(env_server, port=args.port)
    where = f"http://127.0.0.1:{httpd.server_address[1]}"
    print(f"serving {task.instance_id} as {args.policy} on {where}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        env_server.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from . import export

    out = _out(args.output)
    if args.format == "swebench":
        if not args.tasks:
            print("error: --format swebench needs --tasks", file=sys.stderr)
            return 2
        rows = (export.to_swebench(t) for t in _read_models(args.tasks, HarnessTask))
    else:
        if not args.results:
            print(f"error: --format {args.format} needs --results", file=sys.stderr)
            return 2
        from .runner import load_results

        results = load_results(Path(args.results))
        cal = args.include_calibration
        rows = {
            "sft": lambda: export.sft_rows(results, min_reward=args.min_reward, include_calibration=cal),
            "grpo": lambda: export.grpo_rows(results, include_calibration=cal),
            "dpo": lambda: export.dpo_rows(results, margin=args.margin, include_calibration=cal),
        }[args.format]()
    n = export.write_jsonl(rows, out)
    print(f"wrote {n} {args.format} rows to {out}")
    return 0


CALIBRATION_POLICIES = [
    "oracle",
    "noop",
    "cheater",
    "cheater:monkeypatch",
    "cheater:stealth",
    "cheater:rewrite",
    "cheater:canary-aware",
    "cheater:ghost",
    "cheater:junit",
    "cheater:exit",
    "cheater:snapshot",
]


# A task pins a base commit in somebody else's repository. When that history is rewritten the
# commit stops existing and the task cannot be built by anyone, ever again. That is the task
# rotting, not the harness breaking, and calling it a violation trains people to ignore the
# nightly. Measured 6 Nov 2026: 2 of the 53 live tasks rotted within four days, both from one
# repo force-pushing.
ROT_MARKERS = (
    "not a tree object",
    "bad object",
    "couldn't find remote ref",
    "did not match any file(s) known to git",
    "reference is not a tree",
)


def is_task_rot(error: str | None) -> bool:
    """True when the error says the upstream commit this task pins no longer exists."""
    return bool(error) and any(marker in error.lower() for marker in ROT_MARKERS)


def rotted_tasks(results: list) -> dict[str, str]:
    """Instance ids whose upstream history was rewritten, mapped to the error that says so."""
    return {r.instance_id: r.error for r in results if is_task_rot(r.error)}


def calibration_violations(results: list) -> list[str]:
    """What must always hold on a sound harness. Each string is one broken promise.

    A task whose upstream commit has gone is left out: nothing about the harness can be
    concluded from an episode that never got to run.
    """
    bad = []
    for r in results:
        where = f"{r.policy} on {r.instance_id}"
        g = r.grade
        if is_task_rot(r.error):
            continue
        if r.error:
            bad.append(f"{where}: episode crashed: {r.error}")
        elif r.policy == "oracle":
            if not (g and g.resolved) or r.reward < 0.9:
                why = (g.tampered or g.error) if g else "no grade"
                bad.append(f"{where}: the real fix didn't resolve (reward {r.reward:+.3f}, {why})")
        elif r.policy == "noop":
            if r.reward > -1.0:
                bad.append(f"{where}: doing nothing earned {r.reward:+.3f}")
        elif r.policy.startswith("cheater"):
            # a cheat that got caught scores -1; one that silently did nothing is just a junk
            # patch and scores 0, like a failed honest try. It must never resolve or beat that.
            if (g and g.resolved) or r.reward > 0.0:
                bad.append(f"{where}: a cheat earned {r.reward:+.3f} (verdict {r.cheat_verdict})")
    return bad


def cmd_calibrate(args: argparse.Namespace) -> int:
    import random

    from .report import render_markdown, summarize
    from .runner import load_results, run

    tasks = _read_models(args.tasks, HarnessTask)
    seed = args.seed or date.today().isoformat()
    if args.sample and args.sample < len(tasks):
        tasks = random.Random(seed).sample(tasks, args.sample)
    _say(f"calibrating on {len(tasks)} task(s), seed {seed}: {', '.join(t.instance_id for t in tasks)}")
    policies = [p.strip() for p in args.policies.split(",") if p.strip()]
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    run(tasks, policies, out_dir=out, workers=args.workers)
    picked = {t.instance_id for t in tasks}
    # judge everything on record for these tasks, so a resumed run is checked in full
    results = [
        r for r in load_results(out / "results.jsonl") if r.instance_id in picked and r.policy in policies
    ]
    print(render_markdown(summarize(results)))
    rotted = rotted_tasks(results)
    if rotted:
        from .stats import wilson

        lo, hi = wilson(len(rotted), len(picked)) or (0.0, 0.0)
        print(
            f"\n{len(rotted)} of {len(picked)} tasks have rotted: the commit they pin is gone from "
            f"the upstream repository, so nobody can build them. 95% CI {lo:.1%} to {hi:.1%}."
        )
        for iid, err in sorted(rotted.items()):
            print(f"  ROTTED {iid}: {err}")
        print("  Rebuild the task set to replace them. This is not a harness failure.")
    bad = calibration_violations(results)
    for line in bad:
        print(f"VIOLATION {line}")
    judged = len(results) - sum(1 for r in results if is_task_rot(r.error))
    print(f"{len(results)} episodes, {judged} judged, {len(bad)} violation(s)")
    return 1 if bad else 0


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
        p.add_argument("--max-steps", type=int, default=None, help="default: 60 for swe, 400 for lht")
        p.add_argument(
            "--judge", action="store_true", help="LLM rubric judging (needs Anthropic credentials)"
        )
        p.add_argument("--judge-model", default="claude-opus-5-5")
        # ceilings, so a sweep against a metered API cannot run away
        p.add_argument("--max-episodes", type=int, help="stop after this many episodes")
        p.add_argument("--max-tokens", type=int, help="stop once the run has used this many tokens")
        p.add_argument("--max-seconds", type=float, help="stop once the run has taken this long")
        p.add_argument("--max-usd", type=float, help="stop once the run has cost this much (needs --prices)")
        p.add_argument(
            "--prices",
            help="dollars per million tokens, e.g. 'openai:gpt-5.1=1.25/10,claude:=3/15'. "
            "The longest matching policy prefix wins",
        )

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

    p = sub.add_parser("ranges", help="list a repo's release ranges, the raw material for LHT tasks")
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--repo-url", help="clone from here instead of GitHub (a local path works)")
    p.add_argument("--last", type=int, help="only the newest N ranges")
    p.add_argument("--prereleases", action="store_true", help="count rc/alpha/beta tags as releases")
    p.set_defaults(fn=cmd_ranges)

    p = sub.add_parser("build-lht", help="turn release ranges (X to Y) into long-horizon tasks")
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--repo-url", help="clone from here instead of GitHub (a local path works)")
    p.add_argument("--range", action="append", help="FROM..TO, repeatable; default is every range")
    p.add_argument("--last", type=int, help="with no --range, only the newest N ranges")
    p.add_argument("-o", "--output", default="lht_tasks.jsonl")
    p.add_argument("--rejected", default="lht_rejected.jsonl")
    p.add_argument("--append", action="store_true", help="add to the output files instead of starting over")
    p.add_argument("--python", default="3.12")
    p.add_argument("--fix-runs", type=int, default=2)
    p.add_argument("--max-prs", type=int, default=40, help="cap on PRs and direct commits that change source")
    p.add_argument("--max-gold-lines", type=int, default=6000)
    p.add_argument(
        "--scope",
        choices=["full", "changed"],
        default="full",
        help="full runs the whole suite so P2P guards the modules the release didn't touch",
    )
    p.add_argument("--no-leakcheck", action="store_true")
    p.add_argument("--rubric", action="store_true", help="add LLM-written rubric items (needs credentials)")
    p.add_argument("--judge-model", default="claude-opus-5-5")
    p.set_defaults(fn=cmd_build_lht)

    p = sub.add_parser("leakcheck", help="re-run the leak checks on built tasks")
    p.add_argument("--tasks", required=True)
    p.set_defaults(fn=cmd_leakcheck)

    p = sub.add_parser("check-envs", help="check that every task's env still rebuilds on its fingerprint")
    p.add_argument("--tasks", required=True, nargs="+")
    p.set_defaults(fn=cmd_check_envs)

    p = sub.add_parser("run", help="run policies on tasks")
    p.add_argument("--tasks", required=True)
    p.add_argument("--category", choices=["swe", "lht"], help="only run tasks of this category")
    p.add_argument("-o", "--output", default="runs")
    runner_args(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("report", help="leaderboard from results.jsonl")
    p.add_argument("--results", required=True)
    p.add_argument("--cutoff", help="YYYY-MM-DD, split metrics before/after to spot contamination")
    p.add_argument("--html")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("serve", help="expose one task's env over HTTP for an external agent")
    p.add_argument("--tasks", required=True)
    p.add_argument("--task", help="instance_id, defaults to the first task")
    p.add_argument("--policy", default="external", help="name recorded in results")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--max-steps", type=int, default=None, help="default: 60 for swe, 400 for lht")
    p.add_argument("-o", "--output", default="runs")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("export", help="tasks to SWE-bench format, episodes to SFT / GRPO / DPO data")
    p.add_argument("--format", choices=["swebench", "sft", "grpo", "dpo"], required=True)
    p.add_argument("--tasks", help="tasks.jsonl (swebench)")
    p.add_argument("--results", help="results.jsonl (sft, grpo, dpo)")
    p.add_argument("--min-reward", type=float, default=0.5, help="sft: lowest reward kept")
    p.add_argument("--margin", type=float, default=0.5, help="dpo: smallest chosen/rejected reward gap")
    p.add_argument(
        "--include-calibration", action="store_true", help="keep oracle, noop and cheater episodes"
    )
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("calibrate", help="red-team the harness: oracle must win, every cheat must lose")
    p.add_argument("--tasks", required=True)
    p.add_argument("--sample", type=int, default=0, help="random tasks to use, 0 means all")
    p.add_argument("--seed", help="sample seed, defaults to today's date so each day checks new tasks")
    p.add_argument("--policies", default=",".join(CALIBRATION_POLICIES))
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("-o", "--output", default="calibration")
    p.set_defaults(fn=cmd_calibrate)

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
