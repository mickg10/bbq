"""The simulator: clock + cooker + probes + device, stepped deterministically.

:class:`Simulator` wires the pieces together and owns the step order. One step
is ``dt`` seconds (1 s by default):

1. events scheduled for this moment fire (scenario writes, probe plug/unplug);
2. the device reads the probes as they are *now* and decides the fan demand
   for the coming step — as the real controller does, on what it can see;
3. the cooker runs with that fan, under this step's ambient, wind and lid;
4. the probes follow the new true temperatures;
5. the clock advances.

Everything random comes from per-channel streams seeded from ``seed``, and
time only moves in whole ``dt`` steps, so the same seed and inputs replay
bit-for-bit regardless of how fast the steps are taken.

``open_loop`` (a callable of time giving a fan demand in %) takes the fan out
of the controller's hands and drives it from a recording instead — how
:mod:`sim.history` replays a real cook's own output through the plant.
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, replace
from typing import Callable, Sequence

from sim.clock import SimClock
from sim.controllers.base import FOOD_PROBES, duty_in_step
from sim.device import CyberQDevice, DeviceOutput, DeviceSettings
from sim.food import Food
from sim.plant import Cooker, Plant
from sim.probes import Probe

PROBE_NAMES = ("cook", "food1", "food2", "food3")


def _schedule(value, label: str) -> Callable[[float], float]:
    """A constant, a callable, or [(t_seconds, value), ...] piecewise-linear."""
    if callable(value):
        return value
    if isinstance(value, (int, float)):
        constant = float(value)
        return lambda t: constant
    points = sorted((float(t), float(v)) for t, v in value)
    if not points:
        raise ValueError(f"{label} schedule needs at least one point")
    if len(points) == 1:
        constant = points[0][1]
        return lambda t: constant

    def at(t: float) -> float:
        if t <= points[0][0]:
            return points[0][1]
        for (t0, v0), (t1, v1) in zip(points, points[1:]):
            if t <= t1:
                return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
        return points[-1][1]

    return at


@dataclass
class Environment:
    """The cooker's surroundings over time: ambient, wind, lid."""

    ambient_f: float | Sequence | Callable = 70.0
    wind_mph: float | Sequence | Callable = 0.0
    #: (open_at, close_at) intervals in seconds; the fire flares while open
    lid_events: Sequence[tuple[float, float]] = ()

    def __post_init__(self) -> None:
        self._ambient = _schedule(self.ambient_f, "ambient")
        self._wind = _schedule(self.wind_mph, "wind")
        self._lid = sorted((float(a), float(b)) for a, b in self.lid_events)
        for opened, closed in self._lid:
            if closed <= opened:
                raise ValueError("lid events are (open_at, close_at) with close > open")

    def ambient_at(self, t: float) -> float:
        return self._ambient(t)

    def wind_at(self, t: float) -> float:
        return self._wind(t)

    def lid_open_at(self, t: float) -> bool:
        return any(opened <= t < closed for opened, closed in self._lid)


@dataclass(frozen=True)
class Event:
    """Something that happens to the simulator at a given moment."""

    t: float
    action: Callable[["Simulator"], None]
    label: str = ""


class Simulator:
    def __init__(
        self,
        cooker: Cooker,
        probes: Sequence[Probe],
        device: CyberQDevice,
        *,
        clock: SimClock | None = None,
        env: Environment | None = None,
        dt: float = 1.0,
        sample_every: int = 1,
        events: Sequence[Event] = (),
        seed: int = 0,
        max_rows: int | None = None,
    ) -> None:
        """``max_rows`` caps the sampled history (a server that runs for days
        keeps only its most recent rows); None keeps everything."""
        if len(probes) != len(PROBE_NAMES):
            raise ValueError(f"need one probe per channel: {', '.join(PROBE_NAMES)}")
        if dt <= 0:
            raise ValueError("dt must be positive")
        if sample_every < 1:
            raise ValueError("sample_every must be at least 1")
        if max_rows is not None and max_rows < 1:
            raise ValueError("max_rows must be at least 1")
        self.cooker = cooker
        self.probes = list(probes)
        self.device = device
        self.clock = clock if clock is not None else SimClock()
        self.env = env if env is not None else Environment()
        self.dt = float(dt)
        self.sample_every = int(sample_every)
        self.events = sorted(events, key=lambda event: event.t)  # stable: ties keep file order
        self.seed = int(seed)
        for i, probe in enumerate(self.probes):
            probe.rng = random.Random(self.seed * 1_000_003 + i)
        self.rows: list[dict] | deque[dict] = (
            deque(maxlen=max_rows) if max_rows is not None else []
        )
        #: set to a callable ``t -> fan %`` to drive the fan from a recording
        self.open_loop: Callable[[float], float] | None = None
        self.steps = 0
        self._next_event = 0
        # a reading for t = 0, so the first control step sees the probes
        for i, probe in enumerate(self.probes):
            probe.step(0.0, self.true_temp(i), self.env.ambient_at(self.clock.t))

    # -- state ----------------------------------------------------------------

    def food(self, index: int) -> Food | None:
        """The food on food probe ``index`` (0..2), or None if none is modelled."""
        foods = self.cooker.foods
        return foods[index] if index < len(foods) else None

    def true_temp(self, index: int) -> float:
        """What probe ``index`` really sits in: the pit, or that piece of food.
        A plugged food probe with no food modelled measures the pit air."""
        food = None if index == 0 else self.food(index - 1)
        return self.cooker.pit_f if food is None else food.temp

    def probe(self, name: str) -> Probe:
        return self.probes[PROBE_NAMES.index(name)]

    def preheat(self, pit_f: float) -> None:
        """Start mid-cook: the pit at ``pit_f`` with the fire holding it there,
        and the probes settled (food goes in at whatever temperature it has)."""
        plant = self.cooker.plant
        plant.pit_f = float(pit_f)
        plant.fire = min(max(plant.steady_fire(pit_f, self.env.ambient_at(self.clock.t)), 0.0), 1.0)
        for i, probe in enumerate(self.probes):
            probe.settle(self.true_temp(i))

    # -- stepping ---------------------------------------------------------------

    def _fire_events(self, t: float) -> None:
        while self._next_event < len(self.events) and self.events[self._next_event].t <= t + 1e-9:
            self.events[self._next_event].action(self)
            self._next_event += 1

    def catch_up(self, t: float | None = None) -> int:
        """Fire the events due by time ``t`` (default: now) without stepping.

        :meth:`step` fires events as it starts; this is for a caller that
        wants the state at a moment to already include what became due at
        that very moment — as :mod:`sim.history` does when it lines a replay
        up with a recording. Returns how many fired."""
        due = self._next_event
        self._fire_events(self.clock.t if t is None else t)
        return self._next_event - due

    def step(self) -> dict:
        """Advance one ``dt`` and return the row for the new moment."""
        t = self.clock.t
        self._fire_events(t)
        out = self.device.step(t, self.dt, [probe.reading for probe in self.probes])
        if self.open_loop is not None:  # a recording drives the fan, not the law
            pct = int(min(max(round(self.open_loop(t)), 0), 100))
            out = replace(
                out, fan_pct=pct,
                duty=duty_in_step(t, self.dt, pct, self.device.settings.cyctime_s) if pct else 0.0,
            )
        t_amb = self.env.ambient_at(t)
        self.cooker.step(
            self.dt,
            fan_duty=out.duty,
            t_amb=t_amb,
            wind_mph=self.env.wind_at(t),
            lid_open=self.env.lid_open_at(t),
        )
        for i, probe in enumerate(self.probes):
            probe.step(self.dt, self.true_temp(i), t_amb)
        self.clock.advance(self.dt)
        self.steps += 1
        row = self.row(out, t_amb)
        if self.steps % self.sample_every == 0:
            self.rows.append(row)
        return row

    def run(self, seconds: float, until: Callable[[dict], bool] | None = None) -> list[dict]:
        """Headless: step for ``seconds`` of simulated time (or until ``until``
        says stop) as fast as possible. Returns the sampled rows, oldest first."""
        for _ in range(int(round(seconds / self.dt))):
            row = self.step()
            if until is not None and until(row):
                break
        return list(self.rows)

    # -- reporting --------------------------------------------------------------

    def row(self, out: DeviceOutput | None = None, t_amb: float | None = None) -> dict:
        """A flat snapshot. The first block mirrors the dashboard's ``samples``
        table (readings in °F, OPEN as None); the rest is simulator internals."""
        out = out if out is not None else self.device.output
        s = self.device.settings
        plant = self.cooker.plant
        row: dict = {"t": self.clock.t}
        for i, name in enumerate(PROBE_NAMES):
            row[name] = self.probes[i].reading_f
        row["cook_set"] = out.cook_set_f
        for i in range(3):
            row[f"food{i + 1}_set"] = s.food_set_f[i]
        row["output_pct"] = out.fan_pct
        for i, name in enumerate(PROBE_NAMES):
            row[f"{name}_status"] = int(out.statuses[i])
        row["timer"] = out.timer
        row["timer_status"] = out.timer_status
        row["pit_f"] = self.cooker.pit_f
        for i in range(FOOD_PROBES):
            food = self.food(i)
            row[f"food{i + 1}_f"] = None if food is None else food.temp
        row["setpoint_f"] = out.setpoint_f
        row["fan_duty"] = out.duty
        row["fire"] = plant.fire
        row["fuel"] = plant.fuel_frac
        row["t_amb"] = self.env.ambient_at(self.clock.t) if t_amb is None else t_amb
        row["wind_mph"] = self.env.wind_at(self.clock.t)
        row["lid_open"] = int(self.env.lid_open_at(self.clock.t))
        row["lid_hold"] = int(out.lid_hold)
        return row


def default_simulator(
    *,
    cook_set_f: float | None = None,
    food: Food | None = None,
    food_set_f: float | None = None,
    ambient_f: float = 70.0,
    fuel_btu: float = 200_000.0,
    wind_mph: float = 0.0,
    seed: int = 0,
    controller=None,
    max_rows: int | None = None,
    **settings,
) -> Simulator:
    """The default cooker with nothing else specified: the default kamado
    (:class:`sim.plant.PlantParams`) in still ``ambient_f`` air with a pit
    probe, and a food probe plugged if — and only if — there is ``food`` on
    it. Setpoints left out are the device's defaults; ``settings`` go to
    :class:`sim.device.DeviceSettings`."""
    env = Environment(ambient_f=ambient_f, wind_mph=wind_mph)
    cooker = Cooker(Plant(t_amb=ambient_f, fuel_btu=fuel_btu), [food] if food else [])
    probes = [Probe(), Probe(plugged=food is not None), Probe(plugged=False), Probe(plugged=False)]
    if cook_set_f is not None:
        settings["cook_set_f"] = cook_set_f
    if food_set_f is not None:
        settings["food_set_f"] = [food_set_f, 180.0, 180.0]
    device = CyberQDevice(DeviceSettings(**settings), controller)
    return Simulator(cooker, probes, device, env=env, seed=seed, max_rows=max_rows)
