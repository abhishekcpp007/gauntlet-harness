"""What changes from one test runner to the next, kept in one place.

The pipeline, the grading order, the canary strategy and the cheat checks are the same for every
language. What differs is small and listed here: which files decide what a run collects, what
has to be redone when a sandbox boots at a new commit, how to ask whether the network is open,
and how much room the runner needs.

The pytest values are the ones the harness has always used, moved here unchanged. Every Python
task validated before this file existed is graded by exactly the commands it was validated with.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Runner:
    name: str
    language: str
    label: str  # how to name the toolchain to an agent
    # `find` tests for the files the integrity check hashes before and after a run: anything
    # that decides what gets collected or how it is reported
    watched: str
    prune: str  # `find` clause for folders the hash never walks into
    # run in the repo root after the base commit is unpacked into the sandbox
    reinstall: str
    network_probe: str  # prints OPEN or BLOCKED
    source_suffixes: tuple[str, ...]
    # the env image and every task sandbox for this runner
    memory: str = "512m"
    pids_limit: int = 256
    cpus: float = 1.0

    def compile_check(self, path: str) -> str | None:
        """A command that fails when `path` doesn't even parse, or None when there's no cheap one."""
        q = shlex.quote(path)
        if self.name == "pytest":
            return f"test ! -f {q} || python -m py_compile {q}" if path.endswith(".py") else None
        if path.endswith((".js", ".mjs", ".cjs")):
            return f"test ! -f {q} || node --check {q}"
        return None  # TypeScript needs the project's own compiler settings to say anything true


PYTEST = Runner(
    name="pytest",
    language="python",
    label="pytest",
    watched=(
        "-name 'test*.py' -o -name '*_test.py' -o -name conftest.py -o -name pytest.ini "
        "-o -name .pytest.ini -o -name tox.ini -o -name setup.cfg -o -name pyproject.toml"
    ),
    prune="-path ./.git -prune -o",
    # The env image may have been built at a different commit with the same dependencies, and
    # the workdir gets replaced, so anything the build generated (setuptools-scm / hatch-vcs
    # _version.py files, editable install metadata) has to be regenerated for THIS commit.
    # Offline and dependency-free: the env already has every dependency and the build backend.
    reinstall=(
        "if [ -f pyproject.toml ] || [ -f setup.py ]; then "
        'SETUPTOOLS_SCM_PRETEND_VERSION="${SETUPTOOLS_SCM_PRETEND_VERSION:-0.0.0}" '
        "python -m pip install -q --disable-pip-version-check --no-cache-dir --no-index --no-deps "
        "--no-build-isolation -e . >/tmp/.reinstall.log 2>&1 || echo REINSTALL_FAILED; "
        "rm -f /tmp/.reinstall.log; fi"
    ),
    network_probe=(
        'python -c "import socket; socket.setdefaulttimeout(3); '
        "socket.create_connection(('1.1.1.1', 443))\" 2>/dev/null && echo OPEN || echo BLOCKED"
    ),
    source_suffixes=(".py",),
)

VITEST = Runner(
    name="vitest",
    language="node",
    label="vitest",
    watched=(
        "-name '*.test.*' -o -name '*.spec.*' -o -path '*/__tests__/*' -o -path './test/*' "
        "-o -path './tests/*' -o -name 'vitest.config.*' -o -name 'vitest.workspace.*' "
        "-o -name 'vite.config.*' -o -name 'vitest.setup.*' -o -name package.json"
    ),
    prune="\\( -path ./.git -o -name node_modules \\) -prune -o",
    # node_modules is kept one level above the repo so that emptying the workdir doesn't take
    # it. The link is for the agent, whose `npx vitest` and `npm test` expect it in the usual
    # place. Grading never goes through it: the suite command names the runner by path.
    #
    # The hosts line is there because a sandbox with networking off comes up with an empty
    # /etc/hosts, so `localhost` doesn't resolve. vite looks it up on startup and dies with
    # EAI_AGAIN before it collects a single test, which read as "the suite produced no results"
    # for every run of every task. Loopback is not the network: nothing leaves the container.
    reinstall=(
        "grep -q localhost /etc/hosts 2>/dev/null || "
        "printf '127.0.0.1\\tlocalhost\\n::1\\tlocalhost\\n' >> /etc/hosts; "
        "if [ -f package.json ] && [ -d ../node_modules ]; then "
        "ln -sfn ../node_modules node_modules || echo REINSTALL_FAILED; fi"
    ),
    network_probe=(
        "node -e \"const s=require('net').connect({host:'1.1.1.1',port:443,timeout:3000});"
        "s.on('connect',()=>{console.log('OPEN');process.exit(0)});"
        "const no=()=>{console.log('BLOCKED');process.exit(0)};s.on('error',no);s.on('timeout',no)\" "
        "2>/dev/null || echo BLOCKED"
    ),
    source_suffixes=(".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"),
    # vite transforms every module it loads and keeps the results in memory; 512m is not
    # enough for a mid-sized TypeScript suite and the failure looks like a hung run
    memory="2g",
    pids_limit=512,
    # vitest runs each test file in its own process and starts as many as there are cores.
    # On one core radashi's 960 tests took 100 seconds a run, and validating one PR is four
    # runs plus the leak check.
    cpus=4.0,
)

# jest runs in the same sandbox as vitest: same folders, same reinstall, same room. What differs is
# the config file it reads, which has to be watched, and the command and report, which live with
# the other suite commands in repo2env.
JEST = replace(
    VITEST,
    name="jest",
    label="jest",
    watched=VITEST.watched + " -o -name 'jest.config.*' -o -name 'jest.setup.*' -o -name 'babel.config.*'",
)

JS_RUNNERS = frozenset({VITEST.name, JEST.name})

RUNNERS: dict[str, Runner] = {r.name: r for r in (PYTEST, VITEST, JEST)}


def runner_for(task) -> Runner:
    """The runner a task was validated with. Tasks written before the field existed are pytest."""
    name = getattr(task, "runner", None) or "pytest"
    try:
        return RUNNERS[name]
    except KeyError:
        raise ValueError(
            f"task {getattr(task, 'instance_id', '?')} names test runner {name!r}, known: {sorted(RUNNERS)}"
        ) from None
