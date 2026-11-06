"""A ceiling on what a run is allowed to spend before it stops.

A full sweep is tasks by policies by attempts. Fifty-three tasks, four policies and four
attempts is 848 episodes, and with a metered API behind one of those policies there was
nothing between a typo and a large bill. `Budget` is checked before each episode starts and
again as each one finishes, so a run stops at the ceiling instead of discovering it afterwards.

It stops rather than raises. Episodes already written to results.jsonl stay there, and because
the runner skips what is already done, raising the ceiling and running the same command again
carries on from where it stopped.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


class BudgetExhausted(Exception):
    """Raised inside a worker so the episode stops; the run itself ends cleanly."""


@dataclass
class Budget:
    """Ceilings for one run. Any of them may be None, which means no ceiling."""

    max_episodes: int | None = None
    max_tokens: int | None = None
    max_seconds: float | None = None
    max_usd: float | None = None
    # dollars per million tokens, by policy name prefix, e.g. {"openai:gpt-5.1": (1.25, 10.0)}
    prices: dict[str, tuple[float, float]] = field(default_factory=dict)

    started: float = field(default_factory=time.monotonic)
    episodes: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    stopped_because: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def price(self, policy: str) -> tuple[float, float] | None:
        """Longest matching prefix wins, so `openai:` can price a whole family."""
        matches = [k for k in self.prices if policy.startswith(k)]
        return self.prices[max(matches, key=len)] if matches else None

    def _exceeded(self) -> str | None:
        if self.max_episodes is not None and self.episodes >= self.max_episodes:
            return f"episode ceiling reached: {self.episodes}/{self.max_episodes}"
        if self.max_tokens is not None and self.input_tokens + self.output_tokens >= self.max_tokens:
            return f"token ceiling reached: {self.input_tokens + self.output_tokens}/{self.max_tokens}"
        if self.max_usd is not None and self.usd >= self.max_usd:
            return f"spend ceiling reached: ${self.usd:.2f}/${self.max_usd:.2f}"
        if self.max_seconds is not None and self.elapsed >= self.max_seconds:
            return f"time ceiling reached: {self.elapsed:.0f}s/{self.max_seconds:.0f}s"
        return None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def check(self) -> None:
        """Called before an episode starts. Raises once any ceiling is reached."""
        with self._lock:
            if self.stopped_because is None:
                self.stopped_because = self._exceeded()
            reason = self.stopped_because
        if reason:
            raise BudgetExhausted(reason)

    def record(self, policy: str, usage: dict[str, int]) -> None:
        """Called as an episode finishes, with whatever the policy reported."""
        inp = int(usage.get("input_tokens", 0) or 0)
        out = int(usage.get("output_tokens", 0) or 0)
        with self._lock:
            self.episodes += 1
            self.input_tokens += inp
            self.output_tokens += out
            if (rate := self.price(policy)) is not None:
                self.usd += inp / 1_000_000 * rate[0] + out / 1_000_000 * rate[1]
            if self.stopped_because is None:
                self.stopped_because = self._exceeded()

    def summary(self) -> dict[str, object]:
        return {
            "episodes": self.episodes,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "usd": round(self.usd, 4),
            "seconds": round(self.elapsed, 1),
            "stopped_because": self.stopped_because,
        }


def parse_prices(spec: str) -> dict[str, tuple[float, float]]:
    """`openai:gpt-5.1=1.25/10,claude:=3/15` into a price table, in dollars per million tokens."""
    prices: dict[str, tuple[float, float]] = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        name, _, rates = part.rpartition("=")
        if not name or "/" not in rates:
            raise ValueError(f"bad price {part!r}, expected policy=INPUT/OUTPUT per million tokens")
        inp, _, out = rates.partition("/")
        prices[name] = (float(inp), float(out))
    return prices
