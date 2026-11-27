"""The CyberQ "as documented" — the baseline control law (docs/SIMULATOR.md).

This implements what the vendor's guide (CyberQ Wi-Fi User Guide, Firmware
v 2.3/3.1) and the web UI say, and no more. One thing the guide says it cannot
implement: the box runs a "full-time adaptive control algorithm [that] learns
your pit". So the baseline is deliberately plain — a proportional band with no
memory, which settles below the setpoint by whatever offset the band allows —
and is not what the device does. A recorded cook (`sim identify`) or the
firmware will say what it does; :class:`ProportionalIntegralController` is the
nearest stand-in until then.

* **Output**: ``fan% = clamp((set − pit) / PROPBAND · 100, 0, 100)``. The
  guide: "Proportional Band is the range of temperatures over which the fan
  will pulse" (30 °F recommended; larger if the pit oscillates), and "Cycle
  Time is the number of seconds between fan pulses" (6 s recommended). Where
  the band sits relative to the setpoint is not said: here it is the whole
  band below it, full speed at the bottom and stopped at the setpoint. PROPBAND
  0 is plain on/off. The device reports this demand as ``OUTPUT_PERCENT`` ("a
  display of the output percentage of the control"); the fan itself runs
  ``fan% · CYCTIME`` seconds of each cycle (:func:`duty_in_step`).
* **Open-lid detect** (``OPENDETECT``, default on). The guide says the
  control detects that the lid has been opened and "minimizes the blower
  running during that time", and — less obviously — that with it on "the rate
  of temperature rise of your pit will be limited to preventing over-firing",
  so that a startup to 250 °F takes at least about 20–30 minutes. In both
  cases *how* is not said. Here:
  - a pit drop faster than ``lid_drop_f_per_min`` sustained over
    ``lid_window_s`` is taken to be the lid coming off, and the demand is
    forced to 0 for ``lid_hold_s`` after the drop ends;
  - a pit still below its target and rising faster than
    ``startup_slew_f_per_min`` is not fed until it slows to that.
* **Ramp** (``COOK_RAMP``): with a food probe chosen, "this feature will
  gradually lower the pit temperature to the food set point temperature when
  the food is within approximately 30° of being done", and then "hold the pit
  temperature slightly above your food set point". Here the target slides
  linearly over the last ``ramp_span_f`` degrees and ends ``ramp_above_f``
  above the food setpoint. An unplugged probe, or a pit already at or below
  that, leaves the target alone.
* **Cook hold**: in :attr:`Mode.HOLD` the target is COOKHOLD and ramp does not
  apply; in :attr:`Mode.SHUTDOWN` the demand is 0.

Two options are off by default and exist for what the device turns out to do:
``bias_pct`` adds a fixed share of fan to the proportional term, and
:class:`ProportionalIntegralController` (``pi``) adds integral action. The
``bias`` preset is the baseline with that share set, for comparisons.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Sequence

from sim.controllers import register
from sim.controllers.base import ControlConfig, ControlOutput, Mode

#: Where the ramp starts: this many °F short of its target (the guide:
#: "within approximately 30° of being done").
DEFAULT_RAMP_SPAN_F = 30.0
#: Where the ramp ends: this far above the food setpoint (the guide:
#: "slightly above your food set point"; how far is not said).
DEFAULT_RAMP_ABOVE_F = 5.0
#: The fastest climb the startup limit allows, °F per minute. The guide gives
#: a startup to 250 °F "a minimum of about 20-30 minutes": from room
#: temperature that is 6–9 °F/min on average, and a rate limit a little above
#: an average. The default kamado climbs at most 4.8 °F/min and never meets it.
DEFAULT_STARTUP_SLEW_F_PER_MIN = 8.0

#: The fan share the ``bias`` preset puts up front.
DEFAULT_BIAS_PCT = 33.0


def round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def clamp_pct(value: float) -> int:
    return min(max(round_half_up(value), 0), 100)


def proportional_demand(error_f: float, propband_f: float, bias_pct: float = 0.0) -> int:
    """Fan % for being ``error_f`` below the target (negative: above it).

    ``bias_pct`` is added to the proportional term: a fixed feed-forward share
    of the fan, which leaves the error less to make up for. It does not apply
    to on/off control (PROPBAND 0).
    """
    if propband_f <= 0:
        return 100 if error_f > 0 else 0
    return clamp_pct(bias_pct + error_f / propband_f * 100.0)


@register
class ProportionalController:
    name = "proportional"

    def __init__(
        self,
        *,
        bias_pct: float = 0.0,
        lid_drop_f_per_min: float = 5.0,
        lid_window_s: float = 60.0,
        lid_hold_s: float = 180.0,
        startup_slew_f_per_min: float = DEFAULT_STARTUP_SLEW_F_PER_MIN,
        ramp_span_f: float = DEFAULT_RAMP_SPAN_F,
        ramp_above_f: float = DEFAULT_RAMP_ABOVE_F,
    ) -> None:
        self.bias_pct = float(bias_pct)
        self.lid_drop_f_per_min = float(lid_drop_f_per_min)
        self.lid_window_s = float(lid_window_s)
        self.lid_hold_s = float(lid_hold_s)
        self.startup_slew_f_per_min = float(startup_slew_f_per_min)
        self.ramp_span_f = float(ramp_span_f)
        self.ramp_above_f = float(ramp_above_f)
        self._hist: deque[tuple[float, float]] = deque()
        self._lid_until: float | None = None
        self.last_rate_f_per_min = 0.0

    def reset(self) -> None:
        self._hist.clear()
        self._lid_until = None
        self.last_rate_f_per_min = 0.0

    # -- the pieces, each on its own so tests (and a recorded cook) can compare --

    def target(self, food_f: Sequence[float | None], cfg: ControlConfig) -> tuple[float, bool]:
        """The pit target for this step and whether the ramp is bending it."""
        if cfg.mode is Mode.HOLD:
            return cfg.hold_setpoint_f, False
        if cfg.ramp and cfg.mode is Mode.NORMAL:
            index = cfg.ramp - 1
            food, food_set = food_f[index], cfg.food_set_f[index]
            if food is not None and food_set is not None:
                floor = food_set + self.ramp_above_f
                if cfg.setpoint_f > floor:
                    if cfg.food_done[index]:
                        progress = 0.0
                    else:
                        # 1 a whole span short of the target, 0 at it
                        progress = (food_set - food) / self.ramp_span_f
                        progress = min(max(progress, 0.0), 1.0)
                    if progress < 1.0:
                        return floor + (cfg.setpoint_f - floor) * progress, True
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

    def climbing_too_fast(
        self, pit_f: float | None, setpoint_f: float, cfg: ControlConfig
    ) -> bool:
        """True while the pit is below its target and rising faster than the
        startup limit allows (which needs open-lid detect on, as the guide
        has it: the same setting gates both)."""
        return (
            cfg.opendetect
            and pit_f is not None
            and pit_f < setpoint_f
            and self.last_rate_f_per_min > self.startup_slew_f_per_min
        )

    def regulating(self, cfg: ControlConfig, hold: bool, pit_f: float | None) -> bool:
        """Whether the law is free to drive the fan this step."""
        return pit_f is not None and cfg.mode is not Mode.SHUTDOWN and not hold

    def _demand(
        self,
        t: float,
        setpoint_f: float,
        pit_f: float | None,
        cfg: ControlConfig,
        hold: bool,
    ) -> int:
        if not self.regulating(cfg, hold, pit_f):
            return 0
        assert pit_f is not None
        return proportional_demand(setpoint_f - pit_f, cfg.propband_f, self.bias_pct)

    def step(
        self,
        t: float,
        pit_f: float | None,
        food_f: Sequence[float | None],
        cfg: ControlConfig,
    ) -> ControlOutput:
        setpoint, ramping = self.target(food_f, cfg)
        lid_hold = self.lid_open(t, pit_f, cfg)
        startup_hold = self.climbing_too_fast(pit_f, setpoint, cfg)
        # either detector holding the fan off leaves the law nothing to say
        fan_pct = self._demand(t, setpoint, pit_f, cfg, lid_hold or startup_hold)
        return ControlOutput(fan_pct, setpoint, lid_hold, ramping, startup_hold)


@register
class BiasedProportionalController(ProportionalController):
    """The documented law with a fixed share of the fan up front (``bias``).

    Not a documented CyberQ mode. It sits between the baseline and ``pi`` in
    the comparison table as the simplest thing that closes most of the
    baseline's steady offset without any memory. The default share is about
    what a 225 °F hold needs on the default cooker.
    """

    name = "bias"

    def __init__(self, *, bias_pct: float = DEFAULT_BIAS_PCT, **baseline) -> None:
        super().__init__(bias_pct=bias_pct, **baseline)


@register
class ProportionalIntegralController(ProportionalController):
    """The same law plus integral action: ``fan% = bias + err / band · 100 + I``.

    The documented law settles a fraction of a band under the setpoint,
    because nothing in it closes a steady error; ``I`` does. ``I`` grows by
    ``ki`` percent per °F below target per minute.

    Anti-windup, in two parts — either alone misbehaves:

    * ``I`` is held in [0, 100];
    * and it stops accumulating while the output is already pinned at the
      limit the error is pushing it towards. Clamping alone lets a cold start
      charge ``I`` to 100 % and keep the fan flat out long after the pit had
      passed the setpoint: on the default kamado that is a 26 °F overshoot
      against 3 °F with both parts.

    ``I`` is also frozen whenever the law is not driving the fan (an open-lid
    recovery window, the startup limit, an open pit probe, shutdown). Ramp,
    hold, lid detect and the bias are the baseline's; PROPBAND 0 is on/off
    here too, with no integral action.

    ``ki`` defaults to 0.1, the best-damped value on the default kamado (0.2
    rings slightly, 0.4 and above oscillate). A real device's value is the
    device's to tell.
    """

    name = "pi"

    def __init__(self, *, ki: float = 0.1, **baseline) -> None:
        super().__init__(**baseline)
        if ki < 0:
            raise ValueError("ki must be non-negative")
        self.ki = float(ki)  # % per °F below target, per minute
        self.integral = 0.0  # %, held in [0, 100]
        self._last_t: float | None = None

    def reset(self) -> None:
        super().reset()
        self.integral = 0.0
        self._last_t = None

    def _demand(
        self,
        t: float,
        setpoint_f: float,
        pit_f: float | None,
        cfg: ControlConfig,
        hold: bool,
    ) -> int:
        dt = 0.0 if self._last_t is None else max(0.0, t - self._last_t)
        self._last_t = t
        if not self.regulating(cfg, hold, pit_f):
            return 0  # frozen: neither accumulating nor forgotten
        assert pit_f is not None
        if cfg.propband_f <= 0:
            self.integral = 0.0
            return proportional_demand(setpoint_f - pit_f, cfg.propband_f)
        error = setpoint_f - pit_f
        proportional = self.bias_pct + error / cfg.propband_f * 100.0
        output = proportional + self.integral
        saturated = (output >= 100.0 and error > 0) or (output <= 0.0 and error < 0)
        if dt > 0 and not saturated:
            self.integral = min(
                max(self.integral + self.ki * error * dt / 60.0, 0.0), 100.0
            )
        return clamp_pct(proportional + self.integral)
