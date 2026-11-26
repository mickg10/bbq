"""The CyberQ "as documented" — the baseline control law (docs/SIMULATOR.md).

This implements what the manual and the web UI imply. It is deliberately
plain: a proportional band with no integral action, so the pit settles below
the setpoint by whatever offset the band allows. The firmware phase will
confirm or correct each piece, and :class:`sim.controllers.firmware` replaces
it then.

* **Output**: ``fan% = clamp((set − pit) / PROPBAND · 100, 0, 100)`` — the fan
  is at full speed once the pit is a whole band below the setpoint and stops
  at the setpoint. PROPBAND 0 is plain on/off. The device reports this demand
  as ``OUTPUT_PERCENT``; the fan itself runs ``fan% · CYCTIME`` seconds of
  each cycle (:func:`sim.controllers.base.duty_in_step`).
* **Open-lid detect** (``OPENDETECT``): a pit drop faster than
  ``lid_drop_f_per_min`` sustained over ``lid_window_s`` is taken to be the
  lid coming off. The demand is forced to 0 for ``lid_hold_s`` after the drop
  ends, so the fire — stoked by the open lid — is not fed straight back up to
  the setpoint on closing.
* **Ramp** (``COOK_RAMP``): with a food probe chosen, as that food closes the
  last ``ramp_span_f`` degrees to its setpoint the pit target slides linearly
  from the cook setpoint down to the food setpoint. An unplugged probe, or a
  food setpoint at or above the cook setpoint, leaves the target alone.
* **Cook hold**: in :attr:`Mode.HOLD` the target is COOKHOLD and ramp does not
  apply; in :attr:`Mode.SHUTDOWN` the demand is 0.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Sequence

from sim.controllers import register
from sim.controllers.base import ControlConfig, ControlOutput, Mode

#: Where the ramp starts: this many °F short of its target.
DEFAULT_RAMP_SPAN_F = 30.0


def round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def proportional_demand(error_f: float, propband_f: float) -> int:
    """Fan % for being ``error_f`` below the target (negative: above it)."""
    if propband_f <= 0:
        return 100 if error_f > 0 else 0
    return min(max(round_half_up(error_f / propband_f * 100.0), 0), 100)


@register
class ProportionalController:
    name = "proportional"

    def __init__(
        self,
        *,
        lid_drop_f_per_min: float = 5.0,
        lid_window_s: float = 60.0,
        lid_hold_s: float = 180.0,
        ramp_span_f: float = DEFAULT_RAMP_SPAN_F,
    ) -> None:
        self.lid_drop_f_per_min = float(lid_drop_f_per_min)
        self.lid_window_s = float(lid_window_s)
        self.lid_hold_s = float(lid_hold_s)
        self.ramp_span_f = float(ramp_span_f)
        self._hist: deque[tuple[float, float]] = deque()
        self._lid_until: float | None = None
        self.last_rate_f_per_min = 0.0

    def reset(self) -> None:
        self._hist.clear()
        self._lid_until = None
        self.last_rate_f_per_min = 0.0

    # -- the pieces, each on its own so tests (and the firmware phase) can compare --

    def target(self, food_f: Sequence[float | None], cfg: ControlConfig) -> tuple[float, bool]:
        """The pit target for this step and whether the ramp is bending it."""
        if cfg.mode is Mode.HOLD:
            return cfg.hold_setpoint_f, False
        if cfg.ramp and cfg.mode is Mode.NORMAL:
            index = cfg.ramp - 1
            food, food_set = food_f[index], cfg.food_set_f[index]
            if (
                food is not None
                and food_set is not None
                and cfg.setpoint_f > food_set
            ):
                if cfg.food_done[index]:
                    progress = 0.0
                else:
                    # 1 a whole span short of the target, 0 at it
                    progress = (food_set - food) / self.ramp_span_f
                    progress = min(max(progress, 0.0), 1.0)
                if progress < 1.0:
                    return food_set + (cfg.setpoint_f - food_set) * progress, True
        return cfg.setpoint_f, False

    def lid_open(self, t: float, pit_f: float | None, cfg: ControlConfig) -> bool:
        """True while an open-lid recovery window is running."""
        if not cfg.opendetect or pit_f is None:
            self._hist.clear()
            self._lid_until = None
            self.last_rate_f_per_min = 0.0
            return False
        self._hist.append((t, pit_f))
        # keep the newest entry at least a window old, plus the current one
        while len(self._hist) > 2 and t - self._hist[1][0] >= self.lid_window_s:
            self._hist.popleft()
        t0, pit0 = self._hist[0]
        rate = 0.0
        # no verdict until a whole window of history exists: over a shorter
        # span a tenth of a degree of probe noise reads as degrees per minute
        if t - t0 >= self.lid_window_s:
            rate = (pit_f - pit0) / (t - t0) * 60.0
        self.last_rate_f_per_min = rate
        if rate <= -self.lid_drop_f_per_min:
            self._lid_until = t + self.lid_hold_s
        return self._lid_until is not None and t < self._lid_until

    def step(
        self,
        t: float,
        pit_f: float | None,
        food_f: Sequence[float | None],
        cfg: ControlConfig,
    ) -> ControlOutput:
        setpoint, ramping = self.target(food_f, cfg)
        lid_hold = self.lid_open(t, pit_f, cfg)
        if pit_f is None or cfg.mode is Mode.SHUTDOWN or lid_hold:
            fan_pct = 0
        else:
            fan_pct = proportional_demand(setpoint - pit_f, cfg.propband_f)
        return ControlOutput(fan_pct, setpoint, lid_hold, ramping)
