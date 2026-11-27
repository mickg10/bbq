"""Controller contract (docs/SIMULATOR.md).

A controller is the CyberQ's control law and nothing else. Every simulator
step the device state machine (:mod:`sim.device`) hands it the probe readings
and the settings they depend on, and gets back the fan demand for that step.
Which law runs is a plug-in choice: the documented baseline
(:mod:`sim.controllers.proportional`) now, the one recovered from the
firmware (:mod:`sim.controllers.firmware`) later.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol, Sequence, runtime_checkable

FOOD_PROBES = 3


class Mode(IntEnum):
    """Why the pit is being driven to the target it is."""

    NORMAL = 0
    HOLD = 1        # cook-hold has taken over the setpoint (timer, or food done)
    SHUTDOWN = 2    # the device switched the output off (timer action, safety)


@dataclass(frozen=True)
class ControlConfig:
    """What the law needs to know for one step (°F, seconds).

    ``setpoint_f`` is the pit setpoint as the user set it and
    ``hold_setpoint_f`` is COOKHOLD, which replaces it in :attr:`Mode.HOLD`.
    Food entries are ``None`` while that probe is unplugged.
    """

    setpoint_f: float
    hold_setpoint_f: float
    propband_f: float
    cyctime_s: float
    ramp: int = 0                            # 0 off; 1..3 = that food probe
    opendetect: bool = True
    food_set_f: Sequence[float | None] = (None,) * FOOD_PROBES
    food_done: Sequence[bool] = (False,) * FOOD_PROBES
    mode: Mode = Mode.NORMAL

    def __post_init__(self) -> None:
        if len(self.food_set_f) != FOOD_PROBES or len(self.food_done) != FOOD_PROBES:
            raise ValueError(f"food_set_f/food_done need {FOOD_PROBES} entries")
        if not 0 <= self.ramp <= FOOD_PROBES:
            raise ValueError(f"ramp must be 0..{FOOD_PROBES}")


@dataclass(frozen=True)
class ControlOutput:
    """The law's answer for one step."""

    fan_pct: int              # 0..100 demand — the device's OUTPUT_PERCENT
    setpoint_f: float         # the target actually used (after ramp / hold)
    lid_hold: bool = False    # open-lid recovery window: demand forced to 0
    ramp_active: bool = False
    startup_hold: bool = False  # climbing faster than the startup limit: demand 0


@runtime_checkable
class Controller(Protocol):
    """``step`` is called once per simulator step, in time order."""

    name: str

    def step(
        self,
        t: float,
        pit_f: float | None,
        food_f: Sequence[float | None],
        cfg: ControlConfig,
    ) -> ControlOutput: ...

    def reset(self) -> None: ...


def _on_before(x: float, on: float, cyctime: float) -> float:
    """Seconds the fan has been on in [0, x)."""
    cycles, phase = divmod(x, cyctime)
    return cycles * on + min(phase, on)


def duty_in_step(t: float, dt: float, fan_pct: float, cyctime_s: float) -> float:
    """Fraction of the step [t, t + dt) the fan is on, for a demand in %.

    The fan runs ``fan% × CYCTIME`` seconds from the start of each CYCTIME
    window, with windows aligned on t = 0. With no cycle time (CYCTIME 0) the
    output is treated as continuous: the fan is on for ``fan%`` of the step.
    """
    frac = min(max(fan_pct / 100.0, 0.0), 1.0)
    if cyctime_s <= 0.0 or frac <= 0.0:
        return frac
    if frac >= 1.0:
        return 1.0
    on = frac * cyctime_s
    duty = (_on_before(t + dt, on, cyctime_s) - _on_before(t, on, cyctime_s)) / dt
    return min(max(duty, 0.0), 1.0)  # the subtraction can leave ±1e-16 of dust
