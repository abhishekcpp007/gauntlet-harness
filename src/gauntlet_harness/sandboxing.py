"""Boot a fresh sandbox for a task at its base commit. Used for building, grading and agents."""

from __future__ import annotations

import contextlib
import fcntl
import shlex
from collections.abc import Iterator
from pathlib import Path

import docker
from docker.errors import ImageNotFound
from gauntlet_repo2env.repo import Repo, archive, clone, default_cache, slug_for
from gauntlet_sandbox import Sandbox, SandboxConfig

from .schema import HarnessTask
from .testing import apply_patch

SCRATCH_GIT = "git -c user.name=workspace -c user.email=workspace@localhost"

# The env image may have been built at a different commit with the same dependencies, and
# the workdir gets replaced, so anything the build generated (setuptools-scm / hatch-vcs
# _version.py files, editable install metadata) has to be regenerated for THIS commit.
# Offline and dependency-free: the env already has every dependency and the build backend.
REINSTALL = (
    "if [ -f pyproject.toml ] || [ -f setup.py ]; then "
    'SETUPTOOLS_SCM_PRETEND_VERSION="${SETUPTOOLS_SCM_PRETEND_VERSION:-0.0.0}" '
    "python -m pip install -q --disable-pip-version-check --no-cache-dir --no-index --no-deps "
    "--no-build-isolation -e . >/tmp/.reinstall.log 2>&1 || echo REINSTALL_FAILED; "
    "rm -f /tmp/.reinstall.log; fi"
)


@contextlib.contextmanager
def _locked(cache_dir: Path | None, name: str) -> Iterator[None]:
    """An exclusive file lock, so parallel workers (threads or processes) don't race on a clone
    or an image build. flock holds per open file, so two opens in one process exclude too."""
    lock_dir = (cache_dir or default_cache()) / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / f"{name.replace('/', '_').replace(':', '_')}.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def cached_clone(url: str, cache_dir: Path | None = None) -> Repo:
    """The cached clone, without a network fetch if it already exists."""
    dest = (cache_dir or default_cache()) / "repos" / slug_for(url)
    if (dest / ".git").exists():
        return Repo(dest, url, slug_for(url))
    with _locked(cache_dir, f"clone-{slug_for(url)}"):
        if (dest / ".git").exists():  # another worker finished it while we waited
            return Repo(dest, url, slug_for(url))
        return clone(url, cache_dir)


def ensure_image(task: HarnessTask, cache_dir: Path | None = None, log=lambda _: None) -> None:
    """Make sure the task's env image exists, rebuilding it at the base commit if it doesn't.

    Images live on the machine that built them, so a task file shared with anyone else (the
    Gauntlet Live set, a CI runner) would otherwise point at an image nobody has. The rebuild
    has to land on the same dependency fingerprint, or the env has drifted and grading on it
    wouldn't be the task that was validated.
    """
    client = docker.from_env()
    try:
        client.images.get(task.image)
        return
    except ImageNotFound:
        pass
    with _locked(cache_dir, f"image-{task.image}"):
        try:
            client.images.get(task.image)  # built by another worker while we waited
            return
        except ImageNotFound:
            pass
        from .build import ensure_env  # build imports this module

        repo = cached_clone(task.repo, cache_dir)
        python = str(task.validation.get("python", "3.12"))
        log(f"{task.instance_id}: no local image {task.image}, rebuilding it at {task.base_commit[:12]}")
        # lock order is always image -> clone, so this can't deadlock with cached_clone
        with _locked(cache_dir, f"clone-{slug_for(task.repo)}"):
            tag, fingerprint, _ = ensure_env(
                task.repo, repo, task.base_commit, python=python, cache_dir=cache_dir, log=log
            )
        if fingerprint != task.env_fingerprint:
            raise RuntimeError(
                f"rebuilt env for {task.instance_id} has fingerprint {fingerprint[:16]}, the task expects "
                f"{task.env_fingerprint[:16]}: its dependencies drifted, so it can't be graded as validated"
            )
        if tag != task.image:
            repo_name, _, tag_name = task.image.partition(":")
            client.images.get(tag).tag(repo_name, tag_name or "latest")


def boot(
    task: HarnessTask,
    *,
    apply_tests: bool,
    baseline_git: bool,
    timeout: float = 120,
    trajectory_path: str | Path | None = None,
    record: bool = True,
    cache_dir: Path | None = None,
) -> Sandbox:
    """A started sandbox with the repo at the task's base commit. The caller closes it.

    No network. The workdir is emptied first so nothing from the env image's build commit
    survives. `apply_tests` adds the hidden tests (grading and validation only, never for an
    agent). `baseline_git` commits the tree to a throwaway one-commit repo so whatever the
    agent changes can be diffed later.
    """
    repo = cached_clone(task.repo, cache_dir)
    ensure_image(task, cache_dir)
    cfg = SandboxConfig(
        image=task.image,
        workdir=task.workdir,
        default_timeout=timeout,
        max_output_bytes=200_000,
    )
    sb = Sandbox(
        cfg,
        trajectory_path=trajectory_path,
        record=record,
        metadata={"instance_id": task.instance_id},
    ).start()
    wd = shlex.quote(task.workdir)
    try:
        sb.exec(f"find {wd} -mindepth 1 -delete")
        sb.put_archive(task.workdir, archive(repo, task.base_commit))
        res = sb.exec(REINSTALL, workdir=task.workdir, timeout=max(timeout, 300))
        sb.metadata["reinstall_ok"] = "REINSTALL_FAILED" not in res.stdout
        if apply_tests:
            apply_patch(sb, task.workdir, task.test_patch, "test")
        if baseline_git:
            res = sb.exec(
                f"git init -q && git add -A && {SCRATCH_GIT} commit -q -m base", workdir=task.workdir
            )
            if not res.ok:
                raise RuntimeError(f"couldn't create the scratch git repo: {res.stderr.strip()}")
    except Exception:
        sb.close()
        raise
    return sb
