"""Headless runs (docs/SIMULATOR.md, ``sim run``).

:func:`run_scenario` plays a scenario as fast as the machine can go and
sums it up; :func:`write_csv` writes its time series. The CSV's first block
of columns is the dashboard's ``samples`` table (readings in °F, an open
probe empty, as NULL is in the database), so a run and a recorded cook can be
looked at with the same tools; the rest is simulator internals.
"""

from __future__ import annotations

import csv
import dataclasses
import pathlib
from typing import Sequence

from sim.engine import PROBE_NAMES, Simulator
from sim.scenario import Scenario
from sim.summary import summarize

#: as the dashboard stores a sample, then what only the simulator knows
CSV_COLUMNS = (
    "t", "t_h",
    *PROBE_NAMES, "cook_set", *(f"{name}_set" for name in PROBE_NAMES[1:]),
    "output_pct", *(f"{name}_status" for name in PROBE_NAMES), "timer", "timer_status",
    "pit_f", *(f"{name}_f" for name in PROBE_NAMES[1:]),
    "setpoint_f", "fan_duty", "fire", "fuel", "fuel_btu", "t_amb", "wind_mph",
    "lid_open", "lid_hold", "startup_hold",
)
DEFAULT_SAMPLE_S = 60


@dataclasses.dataclass(frozen=True)
class RunResult:
    scenario: Scenario
    controller: str
    sim: Simulator
    rows: list[dict]
    summary: dict


def run_scenario(scenario: Scenario, *, sample_every: int = DEFAULT_SAMPLE_S, dt: float = 1.0) -> RunResult:
    """Play ``scenario`` to its end. ``sample_every`` is in steps of ``dt``:
    the step the control loop runs on is always ``dt``, and every
    ``sample_every``-th row is kept (for the summary and the CSV)."""
    sim = scenario.build(sample_every=sample_every, dt=dt)
    sim.run(scenario.duration)
    summary = {
        "scenario": scenario.name, "controller": scenario.controller,
        **summarize(sim.rows, fuel0_btu=sim.cooker.plant.fuel0_btu),
    }
    return RunResult(scenario, scenario.controller, sim, list(sim.rows), summary)


def _cell(value) -> str:
    if value is None:
        return ""  # an open probe: NULL in the dashboard's database
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return value


def write_csv(path: pathlib.Path | str, rows: Sequence[dict], *, fuel0_btu: float) -> pathlib.Path:
    """Write ``rows`` (one per sample) as CSV; returns the path written."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            cooked = {**row, "t_h": row["t"] / 3600.0, "fuel_btu": row["fuel"] * fuel0_btu}
            writer.writerow([_cell(cooked.get(column)) for column in CSV_COLUMNS])
    return path
