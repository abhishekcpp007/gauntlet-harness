"""Long-horizon (release range) tasks: finding ranges, attributing tests, rewarding progress."""

from __future__ import annotations

import difflib
import functools
import shutil
import tempfile
from pathlib import Path

import pytest
from gauntlet_repo2env.repo import Repo, clone

from gauntlet_harness.grading import milestone_rates
from gauntlet_harness.lht import (
    attribute,
    prs_in_range,
    range_id,
    range_statement,
    release_ranges,
    release_tags,
    units_in_order,
    unmask,
)
from gauntlet_harness.profiles import LHT, SWE, profile_for
from gauntlet_harness.report import render_html, render_markdown, summarize
from gauntlet_harness.reward import compute_reward
from gauntlet_harness.schema import (
    EpisodeResult,
    GradeResult,
    HarnessTask,
    Horizon,
    Milestone,
    PRInfo,
    RubricScore,
)

from .fakerelease import BASE_CALC, NEG, REPO, make_release_repo


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Repo:
    path = make_release_repo(tmp_path_factory.mktemp("rel"))
    return clone(str(path), tmp_path_factory.mktemp("cache"))


def _sha(repo: Repo, ref: str) -> str:
    from gauntlet_repo2env.repo import git

    return git(repo.path, "rev-parse", f"{ref}^{{commit}}").strip()


# ---------------------------------------------------------------------- ranges


def test_release_tags_skip_prereleases_unless_asked(repo):
    # v0.1.1 was cut after v0.2.0, the way a maintenance release usually is
    assert release_tags(repo) == ["v0.1.0", "v0.2.0", "v0.1.1"]
    assert "v0.2.0rc1" in release_tags(repo, prereleases=True)


def test_each_release_pairs_with_its_newest_ancestor_release(repo):
    # v0.1.1 sits on a side branch: it is its own patch release off v0.1.0, and it must not
    # become the start of v0.2.0's range, since nothing on main descends from it
    assert release_ranges(repo) == [("v0.1.0", "v0.2.0"), ("v0.1.0", "v0.1.1")]
    assert release_ranges(repo, last=1) == [("v0.1.0", "v0.1.1")]  # newest by date


def test_prs_in_range_reads_merges_squashes_and_direct_commits(repo):
    prs, direct = prs_in_range(repo, REPO, _sha(repo, "v0.1.0"), _sha(repo, "v0.2.0"))
    assert [(p.number, p.title) for p in prs] == [(1, "Fix division and add mul"), (4, "Add neg")]
    assert all(p.merge_commit_sha and p.base_sha for p in prs)
    assert len(direct) == 1 and direct[0].endswith("Describe calc at the top of the module")


def test_direct_commits_are_units_in_merge_order(repo):
    # a maintainer who pushes straight to main still ships releases, so a direct commit can
    # own tests like a PR can
    base, head = _sha(repo, "v0.1.0"), _sha(repo, "v0.2.0")
    prs, direct = prs_in_range(repo, REPO, base, head)
    units = units_in_order(repo, base, head, prs, direct)
    ids = [u[0] for u in units]
    assert ids[0] == "pr-1" and ids[1].startswith("commit-") and ids[2] == "pr-4"
    assert units[2][3] == ({"tests/test_neg.py"}, {"test_neg_flips_sign"})
    assert units[1][2] == []  # no PR number behind a direct commit


def test_range_id_is_filesystem_safe():
    assert range_id("acme/calc", "v0.1.0", "v0.2.0") == "acme__calc-v0.1.0_to_v0.2.0"
    assert range_id("a/b", "release/1.0", "release/1.1") == "a__b-release-1.0_to_release-1.1"


# ---------------------------------------------------------------------- milestones


def test_attribution_prefers_names_then_files_then_gives_up_honestly():
    touched = {
        "pr-1": ({"tests/test_a.py", "tests/test_shared.py"}, {"test_alpha"}),
        "pr-2": ({"tests/test_shared.py", "tests/test_b.py"}, {"test_alpha", "test_beta"}),
    }
    targets = [
        "tests/test_a.py::test_alpha",  # both PRs define the name, only #1 touched this file
        "tests/test_shared.py::test_alpha[x]",  # both PRs define test_alpha in shared: last wins
        "tests/test_b.py::TestB::test_beta",  # name match inside a class
        "tests/test_b.py::test_other",  # no name match, file touched only by #2
        "tests/test_shared.py::test_gamma",  # file touched by both, no name: nobody owns it
    ]
    ms = {m.id: m for m in attribute(targets, touched, {"pr-1": "one"}, {"pr-1": [1], "pr-2": [2]})}
    assert ms["pr-1"].prs == [1] and ms["pr-1"].title == "one"
    assert ms["pr-1"].tests == ["tests/test_a.py::test_alpha"]
    assert ms["pr-1"].attribution == "name"
    assert ms["pr-2"].tests == [
        "tests/test_b.py::TestB::test_beta",
        "tests/test_b.py::test_other",
        "tests/test_shared.py::test_alpha[x]",
    ]
    assert ms["pr-2"].attribution == "file"  # mixed evidence reports the weaker kind
    assert ms["unattributed"].tests == ["tests/test_shared.py::test_gamma"]
    assert sum(len(m.tests) for m in ms.values()) == len(targets)  # every target counted once


def test_statement_numbers_the_changes_and_keeps_items_short():
    long_body = "word " * 800
    prs = [
        PRInfo(repo="a/b", number=1, title="Fix div", url="u",
               body="It rounds. See https://github.com/a/b/pull/1"),
        PRInfo(repo="a/b", number=2, title="Add neg", body=long_body, url="u", linked_issues=[9],
               issue_texts={9: "neg is missing"}),
    ]  # fmt: skip
    text = range_statement("a/b", "v1", "v2", prs, ["abc123 Tidy the module"])
    assert text.startswith("Changes between v1 and v2:")
    assert "1. Fix div" in text and "2. Add neg" in text
    assert "github.com/a/b/pull" not in text  # sanitized like an SWE statement
    assert "neg is missing" in text and "word word" not in text  # issue text beats the PR body
    assert "- Tidy the module" in text


def test_tests_hidden_by_a_collection_crash_go_back_to_p2p():
    before = (
        "def test_same():\n    assert 1\n\n\n"
        "def test_body_changed():\n    assert 1\n\n\n"
        "class TestK:\n    def test_m(self):\n        assert 2\n"
    )
    after = (
        "import new_helper  # the import that broke collection\n\n\n"
        "def test_same():\n    assert   1  # formatting doesn't count\n\n\n"
        "def test_body_changed():\n    assert 2\n\n\n"
        "class TestK:\n    def test_m(self):\n        assert 2\n\n\n"
        "def test_brand_new():\n    assert 3\n"
    )
    files = {"before": {"t.py": before}, "after": {"t.py": after}}
    run = {
        "t.py::test_same": "PASSED",
        "t.py::test_body_changed": "PASSED",
        "t.py::TestK::test_m": "PASSED",
    }
    n2p = [*run, "t.py::test_brand_new", "gone.py::test_x"]
    kept, masked = unmask(n2p, run, files["before"].get, files["after"].get)
    assert masked == ["t.py::test_same", "t.py::TestK::test_m"]
    assert kept == ["t.py::test_body_changed", "t.py::test_brand_new", "gone.py::test_x"]


def _lht_task(milestones: list[Milestone], p2p: list[str] = ()) -> HarnessTask:
    targets = [t for m in milestones for t in m.tests]
    return HarnessTask(
        category="lht",
        instance_id="a__b-v1_to_v2",
        repo="x",
        pr=PRInfo(repo="a/b", number=0, title="v1 to v2", url="u"),
        base_commit="b",
        solution_commit="s",
        problem_statement="p",
        statement_source="pr",
        image="i",
        env_fingerprint="f",
        gold_patch="",
        test_patch="",
        test_files=[],
        n2p=targets,
        p2p=list(p2p),
        horizon=Horizon(from_ref="v1", to_ref="v2", milestones=milestones),
    )


def test_milestone_rates_weigh_each_milestone_on_its_own():
    big = Milestone(id="pr-1", tests=[f"t.py::test_{i}" for i in range(9)])
    small = Milestone(id="pr-2", tests=["t.py::test_small"])
    task = _lht_task([big, small])
    statuses = {"t.py::test_small": "PASSED"}
    assert milestone_rates(task, statuses) == {"pr-1": 0.0, "pr-2": 1.0}
    swe = task.model_copy(update={"horizon": None, "category": "swe"})
    assert milestone_rates(swe, statuses) == {}


# ---------------------------------------------------------------------- reward and profiles


def _grade(milestones: dict[str, float], *, resolved=False, p2p=True, targets=0.1) -> GradeResult:
    return GradeResult(
        instance_id="x",
        patch_applied=True,
        resolved=resolved,
        passed={"f2p": {"a": targets > 0}, "n2p": {}, "s2p": {}, "p2p": {"p": p2p}},
        milestones=milestones,
    )


def test_lht_reward_pays_for_milestones_not_for_test_count():
    # one of two milestones done: half the test term, whatever the test counts were
    g = _grade({"pr-1": 0.0, "pr-2": 1.0})
    r, b = compute_reward(g, RubricScore(score=0.0), "clean", "+x", "+y", LHT.reward)
    assert b["tests"] == pytest.approx(0.5) and b["milestones_done"] == 1.0
    assert r == pytest.approx(0.8 * 0.5)
    assert b["similarity"] == 0.0 or LHT.reward.w_similarity == 0.0


def test_lht_reward_still_zeroes_regressions_and_cheats():
    broke_p2p = _grade({"pr-1": 1.0, "pr-2": 1.0}, p2p=False)
    _, b = compute_reward(broke_p2p, None, "clean", "+x", "+y", LHT.reward)
    assert b["tests"] == 0.0
    r, _ = compute_reward(_grade({"pr-1": 1.0}), None, "cheating", "+x", "+y", LHT.reward)
    assert r == LHT.reward.cheat_penalty
    nothing = _grade({"pr-1": 0.0, "pr-2": 0.0}, targets=0)
    r, _ = compute_reward(nothing, RubricScore(score=1.0), "clean", "+x", "+y", LHT.reward)
    assert r == 0.0  # the no-progress cap applies to lht too


def test_swe_reward_ignores_milestones():
    g = _grade({"pr-1": 1.0})
    _, b = compute_reward(g, None, "clean", "+x", "+y", SWE.reward)
    assert "milestones_done" not in b


def test_profiles_follow_the_category():
    task = _lht_task([Milestone(id="pr-1", tests=["t.py::test_x"])])
    assert profile_for(task) is LHT and LHT.max_steps > SWE.max_steps
    assert profile_for(task.model_copy(update={"category": "swe"})) is SWE


def test_old_task_files_load_as_swe():
    old = _lht_task([]).model_dump()
    old.pop("category"), old.pop("horizon")
    task = HarnessTask.model_validate(old)
    assert task.category == "swe" and task.horizon is None


# ---------------------------------------------------------------------- report


def _ep(iid, category, resolved, milestones=None):
    g = _grade(milestones or {}, resolved=resolved)
    return EpisodeResult(
        episode_id=f"{iid}-m", instance_id=iid, category=category, policy="m", grade=g, reward=float(resolved)
    )


def test_report_never_pools_swe_and_lht():
    rs = [
        _ep("swe-1", "swe", True),
        _ep("swe-2", "swe", True),
        _ep("lht-1", "lht", False, {"pr-1": 1.0, "pr-2": 0.0}),
    ]
    s = summarize(rs)
    assert s["category"] == "mixed" and list(s["categories"]) == ["swe", "lht"]
    assert s["categories"]["swe"]["policies"][0]["resolved_rate"] == 1.0
    lht = s["categories"]["lht"]["policies"][0]
    assert lht["resolved_rate"] == 0.0 and lht["milestone_rate"] == 0.5 and lht["milestones_done"] == 1.0
    md = render_markdown(s)
    assert "## SWE" in md and "## LHT" in md and "Milestones" in md
    assert "<h2>" in render_html(s)


def test_single_category_report_keeps_its_old_shape():
    s = summarize([_ep("swe-1", "swe", True)])
    assert s["category"] == "swe" and s["policies"][0]["resolved_rate"] == 1.0


# ---------------------------------------------------------------------- end to end, Docker


@functools.cache
def _built():
    from gauntlet_harness.lht import build_range_task

    root = Path(tempfile.mkdtemp(prefix="gauntlet-lht-"))
    path = make_release_repo(root)
    task = build_range_task(REPO, "v0.1.0", "v0.2.0", cache_dir=root / "cache", repo_url=str(path))
    return root, task


@pytest.fixture(scope="module")
def built():
    import docker

    root, task = _built()
    yield root, task
    if isinstance(task, HarnessTask):
        try:
            docker.from_env().images.remove(task.image, force=True)
        except Exception:
            pass
    shutil.rmtree(root, ignore_errors=True)


@pytest.mark.docker
def test_release_range_becomes_an_lht_task(built):
    _, task = built
    assert isinstance(task, HarnessTask), task
    assert task.category == "lht" and task.instance_id == "acme__calc-v0.1.0_to_v0.2.0"
    h = task.horizon
    assert [p.number for p in h.prs] == [1, 4] and len(h.direct_commits) == 1
    ms = {m.id: m.tests for m in h.milestones}
    assert ms == {
        "pr-1": [
            "tests/test_calc.py::test_div_gives_exact_quotient",
            "tests/test_calc.py::test_version_marker",
            "tests/test_mul.py::test_mul_multiplies_numbers",
        ],
        "pr-4": ["tests/test_neg.py::test_neg_flips_sign"],
    }
    assert task.p2p == ["tests/test_calc.py::test_add"]
    assert h.dropped_regressions == []
    assert "1. Fix division and add mul" in task.problem_statement
    assert "def neg" in task.gold_patch and "tests/" not in task.gold_patch


@pytest.mark.docker
def test_lht_task_passes_the_leak_checks(built):
    from gauntlet_harness.leakcheck import leakcheck

    root, task = built
    report = leakcheck(task, cache_dir=root / "cache")
    assert report.ok, {k: c.detail for k, c in report.checks.items() if not c.ok}


@pytest.mark.docker
def test_gold_resolves_and_half_a_release_earns_half(built):
    from gauntlet_harness.grading import grade_patch

    root, task = built
    gold = grade_patch(task, task.gold_patch, cache_dir=root / "cache")
    assert gold.resolved and gold.milestones == {"pr-1": 1.0, "pr-4": 1.0}

    only_neg = "".join(
        difflib.unified_diff(
            BASE_CALC.splitlines(True),
            (BASE_CALC + NEG).splitlines(True),
            "a/calc/__init__.py",
            "b/calc/__init__.py",
        )
    )
    half = grade_patch(task, only_neg, cache_dir=root / "cache")
    assert not half.resolved and not half.tampered
    assert half.milestones == {"pr-1": 0.0, "pr-4": 1.0}
    r, b = compute_reward(half, None, "clean", only_neg, task.gold_patch, LHT.reward, task=task)
    assert b["tests"] == pytest.approx(0.5) and 0.0 < r < 1.0


def test_shipped_lht_dataset_is_whole():
    path = Path(__file__).parent.parent / "datasets" / "gauntlet-lht-2026-11" / "tasks.jsonl"
    tasks = [HarnessTask.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(tasks) == 16
    for task in tasks:
        assert task.category == "lht" and task.horizon is not None
        owned = sorted(t for m in task.horizon.milestones for t in m.tests)
        assert owned == sorted(task.targets), task.instance_id  # every target in exactly one milestone
        assert task.validation["leakcheck"]["ok"] and task.validation["python"]
