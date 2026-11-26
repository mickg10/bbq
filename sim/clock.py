"""Simulated clock (docs/SIMULATOR.md).

Headless runs drive the clock themselves: every simulator step advances it by
exactly ``dt`` seconds, so a run is a pure function of its seed and its inputs
and replays bit-for-bit.

A live server takes the same fixed steps but paces them against the wall clock
at ``speed``×. Only *how many* steps run per loop iteration depends on timing;
their size and order never do, so ``serve --speed 60`` walks exactly the
trajectory a headless run would, just faster.
"""

from __future__ import annotations

import time
from typing import Callable


class SimClock:
    """Simulated time ``t`` in seconds since the run started."""

    def __init__(
        self,
        t: float = 0.0,
        *,
        speed: float = 1.0,
        realtime: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if speed <= 0:
            raise ValueError("speed must be positive")
        if t < 0:
            raise ValueError("t must be non-negative")
        self.speed = float(speed)
        self.realtime = bool(realtime)
        self._t = float(t)
        self._monotonic = monotonic
        self._anchor_t: float | None = None  # sim time at the wall-clock anchor
        self._anchor_wall = 0.0

    @property
    def t(self) -> float:
        return self._t

    def advance(self, dt: float) -> float:
        """Headless mode: move time forward by ``dt`` and return the new ``t``."""
        if dt < 0:
            raise ValueError("dt must be non-negative")
        self._t += dt
        return self._t

    def start(self) -> None:
        """Anchor realtime mode to the wall clock at the current ``t``."""
        self._anchor_wall = self._monotonic()
        self._anchor_t = self._t

    def target(self) -> float:
        """Where the wall clock says ``t`` should be now (realtime mode)."""
        if self._anchor_t is None:
            return self._t
        return self._anchor_t + (self._monotonic() - self._anchor_wall) * self.speed

    def steps_due(self, dt: float, max_backlog: float = 120.0) -> int:
        """Realtime mode: whole ``dt`` steps owed to the wall clock.

        Returns 0 in headless mode. A long stall (a laptop asleep) would ask
        for a huge burst of steps; rather than replaying hours of missed time
        the clock then skips ahead, keeping at most ``max_backlog`` seconds.
        """
        if not self.realtime:
            return 0
        if self._anchor_t is None:
            self.start()
            return 0
        backlog = self.target() - self._t
        if backlog > max_backlog:
            # pull the anchor back so the target lands max_backlog ahead of t
            self._anchor_t -= backlog - max_backlog
            backlog = max_backlog
        return int(backlog / dt + 1e-9) if backlog > 0 else 0
