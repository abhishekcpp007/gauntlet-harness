import docker
import pytest


def _docker_ok() -> bool:
    try:
        docker.from_env().ping()
        return True
    except Exception:
        return False


DOCKER_OK = _docker_ok()


def pytest_collection_modifyitems(config, items):
    import os

    live = os.environ.get("GAUNTLET_LIVE") == "1"
    for item in items:
        if "docker" in item.keywords and not DOCKER_OK:
            item.add_marker(pytest.mark.skip(reason="Docker daemon not reachable"))
        if "live" in item.keywords and not live:
            item.add_marker(pytest.mark.skip(reason="set GAUNTLET_LIVE=1 to hit the real GitHub API"))
