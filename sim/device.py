"""CyberQ state machine (docs/SIMULATOR.md): settings, timer, modes, statuses.

The device owns what the real box owns — names and setpoints, the control
settings, the cook timer — plus the state it derives from them. Every step it
hands a :class:`sim.controllers.base.Controller` the probe readings and the
settings, and applies the fan demand it gets back. The control law is the
plug-in's business; what follows is everything around it.

Assumptions the firmware phase will confirm or correct:

* Status codes are the stock UI's ``statusValToStr``: 0 OK, 1 HIGH, 2 LOW,
  3 DONE, 4 ERROR, 5 HOLD, 6 ALARM, 7 SHUTDOWN.
* ``ALARMDEV`` either side of the *effective* pit target gives HIGH or LOW.
  A food probe is not LOW for being under its target: the recon capture shows
  a 74.6 °F food probe on a 180 °F target as OK.
* A food probe at or above its target is DONE and stays DONE, until its
  target is raised above it or the probe is unplugged.
* ``TIMEOUT_ACTION`` is control.htm's list — 0 No Action, 1 Hold, 2 Alarm,
  3 Shutdown — and on expiry each does what it says to the *cook* status:
  HOLD drops the pit to COOKHOLD, ALARM flags it, SHUTDOWN stops the fan.
* Cook hold also engages when the last plugged-in food probe becomes DONE.
* An unplugged cook probe is ERROR and stops the fan; an unplugged food probe
  is ERROR. A pit at or above ``safety_cutoff_f`` latches SHUTDOWN.
* HOLD, ALARM and SHUTDOWN are latched. A different cook setpoint, a raised
  food setpoint or a new timer releases them.
* ``TIMER_STATUS`` is 0 idle, 1 counting down, 2 expired (the stock UI only
  tells 1 from everything else).

Temperatures are °F; setpoints are multiples of 0.1, the wire's resolution.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Sequence

from sim.controllers.base import (
    FOOD_PROBES,
    ControlConfig,
    Controller,
    Mode,
    duty_in_step,
)
from sim.controllers.proportional import ProportionalController


class Status(IntEnum):
    """Probe status codes, in the device's order (``statusValToStr``)."""

    OK = 0
    HIGH = 1
    LOW = 2
    DONE = 3
    ERROR = 4
    HOLD = 5
    ALARM = 6
    SHUTDOWN = 7


class TimeoutAction(IntEnum):
    """control.htm's Timeout Action list."""

    NONE = 0
    HOLD = 1
    ALARM = 2
    SHUTDOWN = 3


#: °F limits, as the dashboard's client enforces them (app/device.py).
COOK_SET_LIMITS_F = (32.0, 475.0)
FOOD_SET_LIMITS_F = (32.0, 300.0)
COOKHOLD_LIMITS_F = (32.0, 475.0)
ALARMDEV_LIMITS_F = (0.0, 475.0)
PROPBAND_LIMITS_F = (0.0, 475.0)
CYCTIME_LIMITS_S = (0, 99)

_HHMMSS = re.compile(r"^(\d{2}):(\d{2}):(\d{2})$")


def parse_hhmmss(text: str) -> int:
    """``"01:30:00"`` -> seconds; anything else is a ValueError."""
    match = _HHMMSS.fullmatch(text.strip() if isinstance(text, str) else "")
    if match is None or int(match.group(2)) > 59 or int(match.group(3)) > 59:
        raise ValueError(f"timer must be HH:MM:SS, got {text!r}")
    hours, minutes, seconds = (int(group) for group in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def format_hhmmss(seconds: float) -> str:
    """Seconds left -> ``"HH:MM:SS"``, rounded up (a timer shows 00:00:01
    until the last second has actually passed)."""
    total = max(0, int(math.ceil(seconds - 1e-9)))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _clamp(value: float, limits: tuple[float, float], label: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return min(max(number, limits[0]), limits[1])


def _tenths(value: float) -> float:
    """Snap to the wire's 0.1 °F resolution."""
    return round(value, 1)


@dataclass
class DeviceSettings:
    """The settings the control loop runs on. Defaults are the recon capture."""

    cook_set_f: float = 190.0
    food_set_f: list[float] = field(default_factory=lambda: [180.0] * FOOD_PROBES)
    cookhold_f: float = 200.0
    alarmdev_f: float = 50.0
    propband_f: float = 30.0
    cyctime_s: int = 6
    ramp: int = 0
    opendetect: bool = True
    timeout_action: TimeoutAction = TimeoutAction.NONE

    def __post_init__(self) -> None:
        self.cook_set_f = _tenths(_clamp(self.cook_set_f, COOK_SET_LIMITS_F, "cook set"))
        if len(self.food_set_f) != FOOD_PROBES:
            raise ValueError(f"food_set_f needs {FOOD_PROBES} entries")
        self.food_set_f = [
            _tenths(_clamp(value, FOOD_SET_LIMITS_F, "food set"))
            for value in self.food_set_f
        ]
        self.cookhold_f = _tenths(_clamp(self.cookhold_f, COOKHOLD_LIMITS_F, "cookhold"))
        self.alarmdev_f = _tenths(_clamp(self.alarmdev_f, ALARMDEV_LIMITS_F, "alarmdev"))
        self.propband_f = _tenths(_clamp(self.propband_f, PROPBAND_LIMITS_F, "propband"))
        self.cyctime_s = int(_clamp(self.cyctime_s, CYCTIME_LIMITS_S, "cyctime"))
        if not 0 <= int(self.ramp) <= FOOD_PROBES:
            raise ValueError(f"ramp must be 0..{FOOD_PROBES}")
        self.ramp = int(self.ramp)
        self.timeout_action = TimeoutAction(int(self.timeout_action))


@dataclass
class TimerState:
    """The cook timer, as ``COOK_TIMER`` sets it and ``TIMER_CURR`` reports it."""

    remaining_s: float = 0.0
    running: bool = False
    expired: bool = False

    @property
    def status(self) -> int:
        return 1 if self.running else 2 if self.expired else 0

    @property
    def text(self) -> str:
        return format_hhmmss(self.remaining_s)

    def set(self, seconds: float) -> None:
        """A positive duration starts the timer; zero stops and clears it."""
        if seconds <= 0:
            self.remaining_s, self.running, self.expired = 0.0, False, False
        else:
            self.remaining_s, self.running, self.expired = float(seconds), True, False

    def tick(self, dt: float) -> bool:
        """Count down; True on the step the timer expires on."""
        if not self.running:
            return False
        self.remaining_s -= dt
        if self.remaining_s <= 1e-9:
            self.remaining_s, self.running, self.expired = 0.0, False, True
            return True
        return False


@dataclass(frozen=True)
class DeviceOutput:
    """The device's live state for one step (what status.xml reports)."""

    fan_pct: int                 # OUTPUT_PERCENT
    duty: float                  # fraction of the step the fan is on
    cook_set_f: float            # COOK_SET as reported (COOKHOLD while holding)
    setpoint_f: float            # the target the law drove to
    statuses: tuple[Status, ...]  # cook, food1..3
    timer: str                   # TIMER_CURR
    timer_status: int            # TIMER_STATUS
    lid_hold: bool
    ramp_active: bool

    @property
    def mode(self) -> Mode:
        if self.statuses[0] is Status.SHUTDOWN:
            return Mode.SHUTDOWN
        if self.statuses[0] is Status.HOLD:
            return Mode.HOLD
        return Mode.NORMAL


class CyberQDevice:
    """The box: settings + timer + latched modes, driving a controller."""

    def __init__(
        self,
        settings: DeviceSettings | None = None,
        controller: Controller | None = None,
        *,
        safety_cutoff_f: float = 525.0,
    ) -> None:
        self.settings = settings if settings is not None else DeviceSettings()
        self.controller = controller if controller is not None else ProportionalController()
        self.safety_cutoff_f = float(safety_cutoff_f)
        self.timer = TimerState()
        self.hold = False
        self.alarm = False
        self.shutdown = False
        self.done = [False] * FOOD_PROBES
        self.output = DeviceOutput(
            0, 0.0, self.settings.cook_set_f, self.settings.cook_set_f,
            (Status.OK,) * 4, self.timer.text, self.timer.status, False, False,
        )

    # -- settings -------------------------------------------------------------

    @property
    def mode(self) -> Mode:
        if self.shutdown:
            return Mode.SHUTDOWN
        return Mode.HOLD if self.hold else Mode.NORMAL

    @property
    def reported_cook_set_f(self) -> float:
        """COOK_SET as the device shows it: the hold target while holding."""
        return self.settings.cookhold_f if self.hold else self.settings.cook_set_f

    def release(self) -> None:
        """Let the user take over: forget HOLD, ALARM and SHUTDOWN."""
        self.hold = self.alarm = self.shutdown = False

    def set_cook_set_tenths(self, tenths: float) -> None:
        """COOK_SET as posted (°F × 10), clamped; a change releases the latches."""
        value = _tenths(_clamp(tenths / 10.0, COOK_SET_LIMITS_F, "cook set"))
        if value != self.settings.cook_set_f:
            self.release()
        self.settings.cook_set_f = value

    def set_food_set_tenths(self, index: int, tenths: float) -> None:
        """FOODn_SET as posted. Raising it un-does that probe; a change releases."""
        if not 0 <= index < FOOD_PROBES:
            raise ValueError(f"food probe index must be 0..{FOOD_PROBES - 1}")
        value = _tenths(_clamp(tenths / 10.0, FOOD_SET_LIMITS_F, "food set"))
        if value != self.settings.food_set_f[index]:
            self.release()
        if value > self.settings.food_set_f[index]:
            self.done[index] = False
        self.settings.food_set_f[index] = value

    def set_cookhold_tenths(self, tenths: float) -> None:
        self.settings.cookhold_f = _tenths(
            _clamp(tenths / 10.0, COOKHOLD_LIMITS_F, "cookhold")
        )

    def set_alarmdev_tenths(self, tenths: float) -> None:
        self.settings.alarmdev_f = _tenths(
            _clamp(tenths / 10.0, ALARMDEV_LIMITS_F, "alarmdev")
        )

    def set_propband_tenths(self, tenths: float) -> None:
        self.settings.propband_f = _tenths(
            _clamp(tenths / 10.0, PROPBAND_LIMITS_F, "propband")
        )

    def set_cyctime_s(self, seconds: float) -> None:
        self.settings.cyctime_s = int(_clamp(seconds, CYCTIME_LIMITS_S, "cyctime"))

    def set_ramp(self, probe: int) -> None:
        self.settings.ramp = int(_clamp(probe, (0, FOOD_PROBES), "ramp"))

    def set_opendetect(self, enabled: bool) -> None:
        self.settings.opendetect = bool(enabled)

    def set_timeout_action(self, action: int) -> None:
        self.settings.timeout_action = TimeoutAction(
            int(_clamp(action, (0, 3), "timeout action"))
        )

    def set_timer_text(self, text: str) -> None:
        """``COOK_TIMER`` as posted: HH:MM:SS starts a timer, 00:00:00 stops it."""
        self.set_timer_s(parse_hhmmss(text))

    def set_timer_s(self, seconds: float) -> None:
        if seconds > 0:
            self.release()
        self.timer.set(seconds)

    def reset(self) -> None:
        """Clear everything that is not a setting."""
        self.release()
        self.timer.set(0)
        self.done = [False] * FOOD_PROBES
        self.controller.reset()

    # -- the step -------------------------------------------------------------

    def _config(self) -> ControlConfig:
        s = self.settings
        return ControlConfig(
            setpoint_f=s.cook_set_f,
            hold_setpoint_f=s.cookhold_f,
            propband_f=s.propband_f,
            cyctime_s=s.cyctime_s,
            ramp=s.ramp,
            opendetect=s.opendetect,
            food_set_f=tuple(s.food_set_f),
            food_done=tuple(self.done),
            mode=self.mode,
        )

    def step(self, t: float, dt: float, readings: Sequence[int | None]) -> DeviceOutput:
        """One control step at simulated time ``t``.

        ``readings`` are the four probes in tenths of °F (``None`` = OPEN).
        """
        if len(readings) != FOOD_PROBES + 1:
            raise ValueError(f"need {FOOD_PROBES + 1} readings")
        s = self.settings
        pit = None if readings[0] is None else readings[0] / 10.0
        foods = [None if r is None else r / 10.0 for r in readings[1:]]

        # safety first: an overheating pit stops the fan and stays stopped
        if pit is not None and pit >= self.safety_cutoff_f:
            self.shutdown = True

        # the timer
        if self.timer.tick(dt):
            action = s.timeout_action
            if action is TimeoutAction.HOLD:
                self.hold = True
            elif action is TimeoutAction.ALARM:
                self.alarm = True
            elif action is TimeoutAction.SHUTDOWN:
                self.shutdown = True

        # food reaching its target; the last one to get there starts cook hold
        plugged, all_done, newly_done = 0, True, False
        for i, food in enumerate(foods):
            if food is None:
                continue
            plugged += 1
            if not self.done[i] and food >= s.food_set_f[i]:
                self.done[i] = newly_done = True
            all_done = all_done and self.done[i]
        if newly_done and plugged and all_done:
            self.hold = True

        # the law
        out = self.controller.step(t, pit, foods, self._config())

        # statuses: ERROR > SHUTDOWN > ALARM > HOLD > HIGH/LOW > OK
        if pit is None:
            cook_status = Status.ERROR
        elif self.shutdown:
            cook_status = Status.SHUTDOWN
        elif self.alarm:
            cook_status = Status.ALARM
        elif self.hold:
            cook_status = Status.HOLD
        elif pit > out.setpoint_f + s.alarmdev_f:
            cook_status = Status.HIGH
        elif pit < out.setpoint_f - s.alarmdev_f:
            cook_status = Status.LOW
        else:
            cook_status = Status.OK
        statuses = [cook_status]
        for i, food in enumerate(foods):
            if food is None:
                statuses.append(Status.ERROR)
            elif self.done[i] and food < s.food_set_f[i] - s.alarmdev_f:
                statuses.append(Status.LOW)  # done, then cooled off
            elif self.done[i]:
                statuses.append(Status.DONE)
            else:
                statuses.append(Status.OK)

        # the fan: off without a pit probe or when shut down
        fan_pct = 0 if pit is None or self.shutdown else int(out.fan_pct)
        duty = duty_in_step(t, dt, fan_pct, s.cyctime_s) if fan_pct else 0.0
        self.output = DeviceOutput(
            fan_pct, duty, self.reported_cook_set_f, out.setpoint_f,
            tuple(statuses), self.timer.text, self.timer.status,
            out.lid_hold, out.ramp_active,
        )
        return self.output
