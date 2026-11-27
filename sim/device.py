"""CyberQ state machine (docs/SIMULATOR.md): settings, timer, modes, statuses.

The device owns what the real box owns — names and setpoints, the control
settings, the cook timer — plus the state it derives from them. Every step it
hands a :class:`sim.controllers.base.Controller` the probe readings and the
settings, and applies the fan demand it gets back. The control law is the
plug-in's business; what follows is everything around it.

What the vendor's guide (CyberQ Wi-Fi User Guide, Firmware v 2.3/3.1) and the
stock web pages establish:

* Status codes are the stock UI's ``statusValToStr``: 0 OK, 1 HIGH, 2 LOW,
  3 DONE, 4 ERROR, 5 HOLD, 6 ALARM, 7 SHUTDOWN. Each field uses a subset of
  them (the V1.7 guide's table of status.xml values, and the stock page
  rendering every field through that one table):

  - ``COOK_STATUS``: OK, HIGH, LOW, ERROR;
  - ``FOODn_STATUS``: OK, DONE, ERROR;
  - ``TIMER_STATUS``: OK, and HOLD, ALARM or SHUTDOWN after a timeout with
    that action. There is no "running" code.

* ``ALARMDEV`` either side of the effective pit target gives HIGH or LOW.
  The guide sets it from 10 to 100 °F, and says the alarm *sound* is only
  allowed once the pit has got near its setpoint; the status shows at once
  (the recon capture reads LOW at 75 °F).
* ``TIMEOUT_ACTION`` is control.htm's list — 0 No Action, 1 Hold, 2 Alarm,
  3 Shutdown. On expiry: HOLD resets the cook setpoint to COOKHOLD (and
  COOK_SET shows it), ALARM raises the timer status and nothing else, SHUTDOWN
  turns the fan off.
* Cook hold comes from the timer and nothing else. The automatic version of
  it is the ramp. (That every food probe being DONE also started it was a
  guess here, and the guide does not have it.)
* An open (unplugged) probe reads OPEN and is ERROR, on any channel.

Guesses, for a recorded cook or the firmware to confirm or correct:

* An open cook probe stops the fan.
* A food probe at or above its target is DONE and stays DONE until its target
  is raised or the probe unplugged (the guide does not say whether it
  latches).
* HOLD, ALARM and SHUTDOWN stay raised until the user changes a setpoint or
  sets a timer (the guide says only that a key press clears the message).
* ``safety_cutoff_f`` is not the device's: it is the simulator's own guard,
  which stops the fan while the pit reads that hot. The guide has no
  over-temperature shutdown.

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
    """The cook timer, as ``COOK_TIMER`` sets it and ``TIMER_CURR`` reports it.
    Its *status* on the wire is the device's to raise (see
    :attr:`CyberQDevice.timer_status`): the timer running is not a status."""

    remaining_s: float = 0.0
    running: bool = False
    expired: bool = False

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
    timer_status: Status         # TIMER_STATUS: OK until a timeout raises it
    lid_hold: bool
    ramp_active: bool
    startup_hold: bool = False  # the startup limit has the fan off

    @property
    def mode(self) -> Mode:
        if self.timer_status is Status.SHUTDOWN:
            return Mode.SHUTDOWN
        if self.timer_status is Status.HOLD:
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
        self.timer_status = Status.OK  # raised by a timeout, in its action's name
        self.hold = False
        self.alarm = False
        self.shutdown = False
        self.done = [False] * FOOD_PROBES
        self.output = DeviceOutput(
            0, 0.0, self.settings.cook_set_f, self.settings.cook_set_f,
            (Status.OK,) * 4, self.timer.text, Status.OK, False, False, False,
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
        self.timer_status = Status.OK

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

        # the simulator's own guard, not a device behaviour: nothing feeds a
        # pit this hot (while it reads this hot; it is not latched)
        overheated = pit is not None and pit >= self.safety_cutoff_f

        # the timer, and what its running out does
        if self.timer.tick(dt):
            action = s.timeout_action
            if action is TimeoutAction.HOLD:
                self.hold, self.timer_status = True, Status.HOLD
            elif action is TimeoutAction.ALARM:
                self.alarm, self.timer_status = True, Status.ALARM
            elif action is TimeoutAction.SHUTDOWN:
                self.shutdown, self.timer_status = True, Status.SHUTDOWN

        # food reaching its target
        for i, food in enumerate(foods):
            if food is not None and not self.done[i] and food >= s.food_set_f[i]:
                self.done[i] = True

        # the law
        out = self.controller.step(t, pit, foods, self._config())

        # statuses, each field in its own subset of the codes
        if pit is None:
            cook_status = Status.ERROR
        elif pit > out.setpoint_f + s.alarmdev_f:
            cook_status = Status.HIGH
        elif pit < out.setpoint_f - s.alarmdev_f:
            cook_status = Status.LOW
        else:
            cook_status = Status.OK
        statuses = [cook_status]
        for i, food in enumerate(foods):
            statuses.append(
                Status.ERROR if food is None else Status.DONE if self.done[i] else Status.OK
            )

        # the fan: off without a pit probe, when a timeout shut it down, or
        # when the simulator's guard is holding it
        fan_pct = 0 if pit is None or self.shutdown or overheated else int(out.fan_pct)
        duty = duty_in_step(t, dt, fan_pct, s.cyctime_s) if fan_pct else 0.0
        self.output = DeviceOutput(
            fan_pct, duty, self.reported_cook_set_f, out.setpoint_f,
            tuple(statuses), self.timer.text, self.timer_status,
            out.lid_hold, out.ramp_active, out.startup_hold,
        )
        return self.output
