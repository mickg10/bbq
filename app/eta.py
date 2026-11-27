"""When each food probe will be done: what ``GET /api/eta`` answers.

The estimating is :mod:`sim.eta`'s (the food model of ``sim/food.py`, run
forward). This module feeds it from the stored history and nothing else: the
endpoint never asks the device for anything.

For each food probe the answer is ``state`` ``eta``, ``stalled`` or ``none``.
There is no estimate (``none``, with a ``reason``) for a probe that is open or
in error, one with no setpoint, food that is done, under 15 minutes of data,
or a history that has gone stale because the device is unreachable.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app import db
from sim import eta

FOOD_PROBES = ("food1", "food2", "food3")

#: status codes (the device's): 3 DONE, 4 ERROR
DONE, ERROR = 3, 4
#: with no sample for this long, the history is stale and nothing is estimated
STALE_S = 15 * 60


def eta_report(conn: sqlite3.Connection, now: int) -> dict[str, Any]:
    """The estimate for every food probe as of the latest stored sample."""
    latest = db.latest_sample(conn, ts_to=now)
    return {"ts": int(now), "probes": {probe: _probe(conn, probe, latest, now) for probe in FOOD_PROBES}}


def _probe(conn: sqlite3.Connection, probe: str, latest: dict[str, Any] | None, now: int) -> dict[str, Any]:
    none = {"state": "none", "reason": ""}
    if latest is None:
        return {**none, "reason": "no samples yet"}
    age = int(now) - int(latest["ts"])
    temp, set_f, status = latest[probe], latest[f"{probe}_set"], latest[f"{probe}_status"]
    if temp is None or status == ERROR:
        return {**none, "reason": "the probe is open or in error"}
    if age > STALE_S:
        return {**none, "reason": f"the last sample is {age // 60} min old"}
    if set_f is None:
        return {**none, "reason": "no setpoint"}
    if status == DONE or temp >= set_f:
        return {**none, "reason": "done"}

    window = _readings(conn, probe, int(latest["ts"]), eta.WINDOW_MIN, step=60)
    history = _readings(conn, probe, int(latest["ts"]), eta.HISTORY_H * 60.0, step=300)
    estimate = eta.estimate(window, set_f, history=history if history.span_min > window.span_min + 1 else None)
    if estimate.state == "none":
        return {**none, "reason": estimate.reason}
    sample_ts = int(latest["ts"])
    minutes = round(estimate.minutes)
    return {
        "state": estimate.state,
        "reason": "",
        "temp_f": temp,
        "set_f": set_f,
        "minutes": minutes,
        "band_min": round(estimate.band_min),
        "eta_ts": sample_ts + minutes * 60,      # the same minutes, so the two agree to the second
        "sample_ts": sample_ts,
        "age_s": age,
        "tau_h": estimate.tau_h,
        "stall_h": estimate.stall_h,
        "stalled_for_h": estimate.stalled_for_h,
        "rate_f_per_h": estimate.rate_f_per_h,
    }


def _readings(conn: sqlite3.Connection, probe: str, now: int, minutes: float, *, step: int) -> eta.Window:
    """The probe's food and the pit's readings over the last ``minutes`` up to
    ``now``, averaged per ``step`` seconds as the history endpoint does, where
    both were read. Times are seconds from the first of them."""
    rows = [
        row for row in db.history(conn, int(now - minutes * 60) - step, now, step)
        if row[probe] is not None and row["cook"] is not None and row["ts"] >= now - minutes * 60
    ]
    first = rows[0]["ts"] if rows else 0
    return eta.Window(
        tuple(row["ts"] - first for row in rows),
        tuple(row[probe] for row in rows),
        tuple(row["cook"] for row in rows),
    )
