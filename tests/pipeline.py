"""Build the fake repo's tasks once per test session and share them between test modules."""

from __future__ import annotations

import atexit
import functools
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import docker

from gauntlet_harness.build import build_task
from gauntlet_harness.schema import HarnessTask

from .fakerepo import GOLD_LINE, ISSUE_10, make_pr_repo


@functools.cache
def pipeline() -> SimpleNamespace:
    root = Path(tempfile.mkdtemp(prefix="gauntlet-pipeline-"))
    repo, prs = make_pr_repo(root)
    cache = root / "cache"
    by_number = {pr.number: pr for pr in prs}

    def build(pr, **kw):
        return build_task(pr, cache_dir=cache, repo_url=str(repo), **kw)

    # newest first, like a real collection run: PR 2's env is built after PR 1 merged, and
    # PR 1 then reuses that image, which is exactly the case leakcheck has to vouch for
    results = {
        3: build(by_number[3]),
        2: build(by_number[2], scope="full"),
        1: build(by_number[1]),
    }
    leaky_pr = by_number[1].model_copy(deep=True)
    leaky_pr.issue_texts[10] = f"{ISSUE_10}\n\nJust write `{GOLD_LINE}` in div."
    strict = build(leaky_pr)
    leaky = build(leaky_pr, strict_statement=False)

    images = {r.image for r in (*results.values(), leaky) if isinstance(r, HarnessTask)}

    def cleanup() -> None:
        client = docker.from_env()
        for image in images:
            try:
                client.images.remove(image, force=True)
            except docker.errors.APIError:
                pass
        shutil.rmtree(root, ignore_errors=True)

    atexit.register(cleanup)
    return SimpleNamespace(
        repo=repo, prs=by_number, cache=cache, results=results, leaky=leaky, leaky_strict=strict
    )
