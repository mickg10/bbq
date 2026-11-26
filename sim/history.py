"""Recorded history (docs/SIMULATOR.md): the dashboard's samples, replayed and scored.

The dashboard stores one row per poll in ``samples`` (app/db.py):
temperatures in °F with NULL for an open probe, setpoints, the fan demand and
the statuses. :func:`load_track` reads a stretch of that; :func:`replay` runs
it back through the model; :func:`score` says how close the model came.

A replay is either closed loop — the recorded setpoints steer the simulated
controller, which is what ``sim compare`` reports — or open loop, with the
recorded fan output driving the plant directly and no controller in the way.
That is the one to fit the plant with: it does not depend on guessing the
device's control law right.

Recordings are messier than simulations, and are treated accordingly:

* a hole longer than ``max_gap_s`` (the device was off or away) splits the
  recording; the longest piece is replayed and the rest reported as skipped;
* a reading that jumps by more than ``jump_f`` between two samples is a
  plug/unplug transient, not a temperature, and is left out of the score;
* a probe that reads OPEN stays unplugged until its first reading, and what
  it shows in the first ``after_plug_s`` after being plugged in is not scored.
"""

from __future__ import annotations

import csv
import dataclasses
import math
import sqlite3
import statistics
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from sim.controllers import get_controller
from sim.device import CyberQDevice, DeviceSettings
from sim.engine import Environment, Event, Simulator
from sim.food import Food, FoodParams
from sim.plant import Cooker, Plant, PlantParams
from sim.probes import Probe
from sim.scenario import format_time

PROBES = ("cook", "food1", "food2", "food3")
FOOD_PROBES = PROBES[1:]

#: a hole longer than this splits the recording (the poller's interval is 5 s)
MAX_GAP_S = 120.0
#: a reading that jumps by more than this between two samples is a plug or
#: an unplug, not a temperature
JUMP_F = 25.0
#: ...and so many samples on either side of it: a probe being pulled does not
#: always go at once, and the steps just before the jump are its ramp
TRANSIENT_MARGIN = 2
#: what a probe shows just after being plugged in is not compared
AFTER_PLUG_S = 120.0
#: fewer successive readings than this say nothing about a probe's noise
MIN_NOISE_SAMPLES = 60


class HistoryError(ValueError):
    """A recording that cannot be read or replayed."""


# -- reading the database ------------------------------------------------------

@dataclasses.dataclass
class Track:
    """One contiguous piece of a recording, times relative to its first sample."""

    start_ts: int
    times: list[float]
    readings: dict[str, list[float | None]]  # probe -> °F, None where OPEN
    sets: dict[str, list[float]]             # probe -> setpoint, °F
    output: list[int]                        # the device's OUTPUT_PERCENT
    skipped: list[tuple[int, int, int]] = dataclasses.field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.times[-1]

    def at(self, series: Sequence, t: float):
        """The value in force at time ``t``: the last sample's at or before it."""
        return series[max(0, bisect_right(self.times, t + 1e-9) - 1)]


def open_history(path: str) -> sqlite3.Connection:
    """Open the dashboard's database read-only, and leave nothing behind.

    The dashboard keeps its database in WAL mode, and a plain read-only open
    of a WAL database creates ``-shm`` and ``-wal`` files beside it. So a
    database that nobody is writing — no WAL or rollback journal beside it —
    is opened immutable, which touches nothing; one with a writer is opened
    the ordinary read-only way.
    """
    file = Path(path)
    if not file.is_file():
        raise HistoryError(f"no database at {path}")
    sidecars = any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-journal"))
    conn = sqlite3.connect(f"file:{path}?mode=ro" + ("" if sidecars else "&immutable=1"), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_track(
    conn: sqlite3.Connection, t_from: int | None = None, t_to: int | None = None,
    *, max_gap_s: float = MAX_GAP_S,
) -> Track:
    """The longest contiguous piece of the samples between two timestamps
    (unix seconds; either may be None for unbounded)."""
    rows = conn.execute(
        "SELECT * FROM samples WHERE (? IS NULL OR ts >= ?) AND (? IS NULL OR ts <= ?)"
        " ORDER BY ts",
        (t_from, t_from, t_to, t_to),
    ).fetchall()
    if len(rows) < 2:
        raise HistoryError("fewer than two samples in that range")
    pieces, current = [], [rows[0]]
    for row in rows[1:]:
        if row["ts"] - current[-1]["ts"] > max_gap_s:
            pieces.append(current)
            current = []
        current.append(row)
    pieces.append(current)
    pieces.sort(key=len, reverse=True)
    kept, skipped = pieces[0], pieces[1:]
    start = kept[0]["ts"]
    return Track(
        start_ts=start,
        times=[row["ts"] - start for row in kept],
        readings={probe: [row[probe] for row in kept] for probe in PROBES},
        sets={probe: [row[f"{probe}_set"] for row in kept] for probe in PROBES},
        output=[row["output_pct"] for row in kept],
        skipped=[(piece[0]["ts"], piece[-1]["ts"], len(piece)) for piece in skipped],
    )


# -- replaying -------------------------------------------------------------------

@dataclasses.dataclass
class Replay:
    track: Track
    sim: Simulator
    simulated: dict[str, list[float | None]]  # true temperature per probe, per sample
    fan: list[int | None]                     # the simulated demand, per sample

    @property
    def open_loop(self) -> bool:
        return self.sim.open_loop is not None


def _or_air(reading: float | None, ambient_f: float) -> float:
    """A probe's first reading, or the air temperature if it was open."""
    return ambient_f if reading is None else reading


def replay(
    track: Track,
    *,
    ambient_f: float,
    params: PlantParams | None = None,
    fuel_btu: float = 200_000.0,
    foods: dict[str, FoodParams] | None = None,
    controller: str = "proportional",
    controller_options: dict | None = None,
    open_loop: bool = False,
    dt: float = 5.0,
) -> Replay:
    """Run the recording through the model.

    The model starts where the recording does — pit and food at their first
    readings, the fire at the level that holds the pit there (zero, at a cold
    start) — and then follows it: setpoints as the device had them, probes
    plugged and unplugged as they were. In open loop the recorded fan demand
    drives the fire.

    What a recording cannot say, the model cannot know: when a lid came off,
    or how far the coals had already come up by the first sample. Both show
    up as error — around such an event, and over the first hour.
    """
    foods = foods or {}
    if track.readings["cook"][0] is None:
        raise HistoryError("the recording starts with the pit probe open: nothing to start from")
    pit0 = track.readings["cook"][0]
    plant = Plant(params, t_amb=ambient_f, fuel_btu=fuel_btu)
    plant.pit_f = pit0
    plant.fire = min(max(plant.steady_fire(pit0, ambient_f), 0.0), 1.0)
    # only the food that was named goes in the cooker — a food probe with
    # nothing on it reads the pit, and draws nothing from it
    cooker = Cooker(
        plant,
        tuple(
            Food(foods[probe], temp_f=_or_air(track.readings[probe][0], ambient_f))
            if probe in foods else None
            for probe in FOOD_PROBES
        ),
    )
    probes = [Probe(plugged=True)] + [Probe(plugged=track.readings[p][0] is not None) for p in FOOD_PROBES]
    device = CyberQDevice(
        DeviceSettings(
            cook_set_f=track.sets["cook"][0],
            food_set_f=[track.sets[p][0] for p in FOOD_PROBES],
        ),
        get_controller(controller, **(controller_options or {})),
    )
    sim = Simulator(
        cooker, probes, device, env=Environment(ambient_f=ambient_f), events=_events(track, foods),
        dt=dt, sample_every=1, seed=0,
    )
    for i, probe in enumerate(sim.probes):  # start from what the device showed
        probe.settle(track.readings[PROBES[i]][0] if track.readings[PROBES[i]][0] is not None else ambient_f)
    if open_loop:
        sim.open_loop = lambda t: float(track.at(track.output, t))  # noqa: E731

    simulated: dict[str, list[float | None]] = {probe: [] for probe in PROBES}  # lagged, as a reading
    fan: list[int | None] = []

    def take(first: bool = False) -> None:
        # the probe's lagged value, not the truth it sits in: a real reading
        # lags a moving pit, and the model's does too
        for i, name in enumerate(PROBES):
            simulated[name].append(sim.probes[i].tracked if sim.probes[i].plugged else None)
        # no step has run yet at the first sample: the simulated demand is
        # not known there
        fan.append(None if first else sim.device.output.fan_pct)

    take(first=True)  # sample 0, before anything steps
    next_sample = 1
    while next_sample < len(track.times):
        sim.step()
        # what became due at the moment just reached shows in the samples
        # taken for it (step() only fires events as it starts)
        sim.catch_up()
        while next_sample < len(track.times) and track.times[next_sample] <= sim.clock.t + 1e-9:
            take()
            next_sample += 1
    return Replay(track, sim, simulated, fan)


def _events(track: Track, foods: dict[str, FoodParams]) -> list[Event]:
    """Setpoint changes and plug/unplug moments, as events at the samples
    that first show them (the replay fires what is due before it takes each
    sample, so no step-size fudge is needed)."""
    events: list[Event] = []

    def setpoint(index: int) -> None:
        def action(sim: Simulator, i=index) -> None:
            sim.device.set_cook_set_tenths(round(track.sets["cook"][i] * 10))
            for n, probe in enumerate(FOOD_PROBES):
                sim.device.set_food_set_tenths(n, round(track.sets[probe][i] * 10))
        events.append(Event(track.times[index], action, f"setpoints@{index}"))

    setpoint(0)
    for i in range(1, len(track.times)):
        if any(track.sets[p][i] != track.sets[p][i - 1] for p in PROBES):
            setpoint(i)

    for index, probe in enumerate(PROBES):
        was = track.readings[probe][0] is not None
        for i in range(1, len(track.times)):
            reading = track.readings[probe][i]
            if (reading is not None) == was:
                continue
            was = reading is not None

            def plug(sim: Simulator, index=index, food=probe in foods, now_plugged=was, temp=reading) -> None:
                sim.probes[index].plugged = now_plugged
                if now_plugged:
                    if food:  # the food appears at its first reading
                        sim.cooker.foods[index - 1].temp = temp
                        sim.cooker.foods[index - 1].moisture = 1.0
                    sim.probes[index].settle(sim.true_temp(index))
            events.append(Event(track.times[i], plug, f"{'plug' if was else 'unplug'} {probe}@{i}"))
    return events


# -- scoring --------------------------------------------------------------------

def transients(
    values: Sequence[float | None], jump_f: float = JUMP_F, margin: int = TRANSIENT_MARGIN
) -> list[bool]:
    """Samples that are a plug or unplug rather than a temperature: every
    sample ``margin`` or fewer places from a jump bigger than ``jump_f``
    between two consecutive readings."""
    flags = [False] * len(values)
    for i in range(1, len(values)):
        before, after = values[i - 1], values[i]
        if before is not None and after is not None and abs(after - before) > jump_f:
            for j in range(max(0, i - 1 - margin), min(len(values), i + margin + 1)):
                flags[j] = True
    return flags


def score(
    track: Track, simulated: dict[str, list[float | None]], fan: Sequence[int | None] | None = None,
    *, jump_f: float = JUMP_F, after_plug_s: float = AFTER_PLUG_S,
    exclude: Sequence[tuple[float, float]] = (),
) -> dict:
    """Measured against modelled, per probe (only where both have a reading).

    ``exclude`` holds (start, end) windows in seconds from the recording's
    first sample that are left out altogether — for a lid that came off,
    which the recording cannot tell the model about.
    """
    out: dict = {"samples": len(track.times), "probes": {}, "transients": 0}
    skipped = [any(start <= t < end for start, end in exclude) for t in track.times]
    out["excluded"] = sum(skipped)
    for probe in PROBES:
        measured = track.readings[probe]
        modelled = simulated[probe]
        transient = transients(measured, jump_f)
        out["transients"] += sum(transient)
        errors = []
        plugged_at = 0.0 if measured[0] is not None else None
        for i, value in enumerate(measured):
            if value is None:
                plugged_at = None
                continue
            if plugged_at is None:
                plugged_at = track.times[i]
            if skipped[i] or transient[i] or modelled[i] is None:
                continue
            if track.times[i] - plugged_at < after_plug_s:
                continue
            errors.append(modelled[i] - value)
        if not errors:
            out["probes"][probe] = {"n": 0}
            continue
        out["probes"][probe] = {
            "n": len(errors),
            "rmse": math.sqrt(statistics.mean(e * e for e in errors)),
            "mae": statistics.mean(abs(e) for e in errors),
            "bias": statistics.mean(errors),
            "max_abs": max(abs(e) for e in errors),
        }
    if fan is not None:
        compared = [i for i in range(len(track.output)) if not skipped[i] and fan[i] is not None]
        fan_errors = sum(1 for i in compared if fan[i] != track.output[i])
        squares = sum((fan[i] - track.output[i]) ** 2 for i in compared if fan[i] != track.output[i])
        out["fan_rmse"] = math.sqrt(squares / len(compared)) if compared else 0.0
        out["fan_exact"] = 1.0 - fan_errors / len(compared) if compared else 1.0
    return out


def mse(scored: dict, probes: Sequence[str] = ("cook",)) -> float:
    """Mean squared error over ``probes``, sample-weighted: the fit's objective."""
    total, count = 0.0, 0
    for probe in probes:
        info = scored["probes"][probe]
        total += info["rmse"] ** 2 * info["n"]
        count += info["n"]
    return total / count if count else math.inf


# -- as text and CSV ---------------------------------------------------------------

def format_score(track: Track, scored: dict, *, model: str) -> str:
    """A comparison, as a few lines of text (errors are modelled minus measured)."""
    started = datetime.fromtimestamp(track.start_ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    lines = [
        f"recorded from {started} UTC, {format_time(track.duration)}, {scored['samples']} samples"
        + (f"; {len(track.skipped)} shorter piece{'s' if len(track.skipped) != 1 else ''} skipped" if track.skipped else "")
    ]
    lines.append(f"model: {model}")
    lines.append(f"  {'probe':<6}{'n':>7}{'RMSE':>8}{'MAE':>8}{'bias':>8}{'max':>7}   °F")
    for probe, info in scored["probes"].items():
        if info["n"]:
            lines.append(
                f"  {probe:<6}{info['n']:>7}{info['rmse']:>8.2f}{info['mae']:>8.2f}"
                f"{info['bias']:>+8.2f}{info['max_abs']:>7.2f}"
            )
        else:
            lines.append(f"  {probe:<6}{'–':>7}   nothing to compare")
    if scored["transients"]:
        lines.append(f"  ({scored['transients']} readings left out as plug/unplug transients)")
    if scored.get("excluded"):
        lines.append(f"  ({scored['excluded']} samples left out, as asked)")
    if "fan_rmse" in scored:
        lines.append(
            f"  fan demand: {scored['fan_exact']:.0%} of samples exactly as the device had it, "
            f"RMSE {scored['fan_rmse']:.1f} points"
        )
    return "\n".join(lines)


def write_comparison_csv(path: Path | str, played: Replay) -> Path:
    """The recording and the model side by side: per sample, each probe's
    measured and modelled temperature, and both fan demands."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["ts", "t"]
            + [f"{probe}{suffix}" for probe in PROBES for suffix in ("", "_model")]
            + ["output_pct", "output_model"]
        )
        for i, t in enumerate(played.track.times):
            writer.writerow(
                [played.track.start_ts + round(t), f"{t:.1f}"]
                + [value for probe in PROBES for value in (
                    played.track.readings[probe][i], played.simulated[probe][i])]
                + [played.track.output[i], played.fan[i]]
            )
    return path
