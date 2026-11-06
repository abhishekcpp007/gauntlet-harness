"""Ceilings on a run. No Docker, no API calls."""

from __future__ import annotations

import threading

import pytest

from gauntlet_harness.budget import Budget, BudgetExhausted, parse_prices


def test_no_ceilings_never_stops():
    b = Budget()
    for _ in range(100):
        b.check()
        b.record("openai:gpt-5.1", {"input_tokens": 10**6, "output_tokens": 10**6})
    assert b.stopped_because is None


def test_episode_ceiling():
    b = Budget(max_episodes=3)
    for _ in range(3):
        b.check()
        b.record("noop", {})
    with pytest.raises(BudgetExhausted, match="episode ceiling"):
        b.check()
    assert b.episodes == 3


def test_token_ceiling():
    b = Budget(max_tokens=1000)
    b.check()
    b.record("openai:gpt-5.1", {"input_tokens": 600, "output_tokens": 500})
    with pytest.raises(BudgetExhausted, match="token ceiling"):
        b.check()


def test_spend_ceiling_uses_the_price_table():
    # $1.25 per million in, $10 per million out
    b = Budget(max_usd=1.0, prices={"openai:gpt-5.1": (1.25, 10.0)})
    b.record("openai:gpt-5.1", {"input_tokens": 400_000, "output_tokens": 50_000})
    assert round(b.usd, 4) == round(0.4 * 1.25 + 0.05 * 10.0, 4) == 1.0
    with pytest.raises(BudgetExhausted, match="spend ceiling"):
        b.check()


def test_an_unpriced_policy_costs_nothing():
    b = Budget(max_usd=1.0, prices={"openai:": (1.25, 10.0)})
    b.record("oracle", {"input_tokens": 10**7, "output_tokens": 10**7})
    assert b.usd == 0.0
    b.check()


def test_the_longest_matching_prefix_wins():
    b = Budget(prices={"openai:": (1.0, 1.0), "openai:gpt-5.1": (2.0, 2.0)})
    assert b.price("openai:gpt-5.1") == (2.0, 2.0)
    assert b.price("openai:gpt-4o") == (1.0, 1.0)
    assert b.price("claude:claude-opus-5-5") is None


def test_time_ceiling(monkeypatch):
    b = Budget(max_seconds=10)
    b.check()
    monkeypatch.setattr(type(b), "elapsed", property(lambda self: 11.0))
    with pytest.raises(BudgetExhausted, match="time ceiling"):
        b.check()


def test_the_reason_is_kept_so_every_worker_sees_the_same_one():
    b = Budget(max_episodes=1)
    b.record("noop", {})
    first = None
    try:
        b.check()
    except BudgetExhausted as e:
        first = str(e)
    with pytest.raises(BudgetExhausted) as second:
        b.check()
    assert first == str(second.value)


def test_counting_is_thread_safe():
    b = Budget()

    def work():
        for _ in range(500):
            b.record("openai:gpt-5.1", {"input_tokens": 1, "output_tokens": 2})

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert b.episodes == 4000
    assert (b.input_tokens, b.output_tokens) == (4000, 8000)


def test_summary_reports_what_was_spent():
    b = Budget(prices={"openai:": (1.0, 2.0)})
    b.record("openai:gpt-5.1", {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    out = b.summary()
    assert out["episodes"] == 1 and out["usd"] == 3.0 and out["stopped_because"] is None


def test_parse_prices():
    assert parse_prices("openai:gpt-5.1=1.25/10,claude:=3/15") == {
        "openai:gpt-5.1": (1.25, 10.0),
        "claude:": (3.0, 15.0),
    }
    assert parse_prices("") == {}
    assert parse_prices("  ") == {}


@pytest.mark.parametrize("bad", ["gpt-5.1", "=1/2", "openai:=1", "openai:=x/y"])
def test_bad_prices_are_rejected(bad):
    with pytest.raises(ValueError):
        parse_prices(bad)


# ------------------------------------------------------------------ in the runner


def _task():
    from gauntlet_harness.schema import HarnessTask

    return HarnessTask(
        instance_id="a__b-1",
        repo="https://github.com/a/b",
        pr={
            "number": 1,
            "title": "t",
            "url": "https://github.com/a/b/pull/1",
            "merged_at": "2026-01-01T00:00:00Z",
            "base_sha": "0" * 40,
            "merge_sha": "1" * 40,
            "repo": "a/b",
        },
        base_commit="0" * 40,
        solution_commit="1" * 40,
        problem_statement="p",
        statement_source="pr",
        image="python:3.12-slim",
        workdir="/repo",
        env_fingerprint="deadbeef",
        gold_patch="",
        test_patch="",
        test_files=["tests/t.py"],
        f2p=["tests/t.py::test_a"],
    )


def test_an_exhausted_budget_stops_before_any_sandbox_starts(tmp_path):
    """The check runs first, so a ceiling already reached costs no Docker and no API call."""
    from gauntlet_harness.runner import run

    b = Budget(max_episodes=0)
    out = run([_task()], ["noop"], out_dir=tmp_path, budget=b)

    assert out == []
    assert "episode ceiling" in b.stopped_because
    # nothing was graded, so nothing was written
    assert not (tmp_path / "results.jsonl").exists()


def test_no_budget_means_no_gate(tmp_path, monkeypatch):
    """Passing nothing leaves the runner exactly as it was: the episode gets as far as the policy.

    The sentinel propagates rather than becoming a failed episode, because the runner builds the
    policy and the env outside its try block. That is current behaviour, not something this test
    is asking for.
    """
    from gauntlet_harness import runner

    seen = {}

    def fake_make_policy(spec, client=None):
        seen["called"] = True
        raise RuntimeError("sentinel")

    monkeypatch.setattr(runner, "make_policy", fake_make_policy)
    with pytest.raises(RuntimeError, match="sentinel"):
        runner.run([_task()], ["noop"], out_dir=tmp_path, budget=None)
    assert seen.get("called")  # no gate stood between the job and the policy


def test_a_reached_ceiling_gates_before_the_policy(tmp_path, monkeypatch):
    """The mirror of the test above: with the ceiling reached, the policy is never built."""
    from gauntlet_harness import runner

    def fake_make_policy(spec, client=None):
        raise AssertionError("the budget should have stopped this before here")

    monkeypatch.setattr(runner, "make_policy", fake_make_policy)
    assert runner.run([_task()], ["noop"], out_dir=tmp_path, budget=Budget(max_episodes=0)) == []
