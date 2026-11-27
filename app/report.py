"""A cook as it went: what ``GET /api/report`` answers.

Two things are computed on the stored history, and both are the simulator's,
run on a recording instead of a scenario:

* **the scorecard** (sim/summary.py): when the pit reached its setpoint, how
  far past it went, where it settled and for how long, the steady error, the
  fan, the lid, and when each food probe was done;
* **the control law** (sim/identify.py): which law the fan output shows, or
  its refusal when the recording cannot tell.

A recording is messier than a run, and is read as ``sim compare`` reads one:

* the longest unbroken stretch is scored, and the shorter ones are listed;
* where a probe read OPEN the reading is missing, which is not an error: it
  ends a plateau, and is all there is to say about a stretch;
* the device does not report the lid. An event is inferred from the pit
  reading falling faster than the simulator's open-lid detector reacts to.

Read-only: the database is all this reads.
"""

from __future__ import annotations

import sqlite3
import statistics
import time
from typing import Any, Sequence

from app import db
from sim.identify import IdentifyError, format_identification, identify, pseudo_code, segments_from_samples
from sim.summary import FOOD_PROBES, summarize

#: a hole longer than this in the recording splits it (sim.history.MAX_GAP_S)
MAX_GAP_S = 120.0
#: the stretch is scored on this grid, seconds
STEP_S = 60
#: the control law is fitted on this one: finer than the score needs, coarse
#: enough to stay quick (the fitter thins to 4,000 samples anyway)
IDENTIFY_STEP_S = 30
#: without a cook or a span named: the latest cook, or else this many hours
DEFAULT_HOURS = 12.0
MAX_HOURS = 24.0 * 7.0
#: an open lid, as the simulator's detector sees it: the pit reading falling
#: at least this fast (sim/controllers/proportional.py, lid_drop_f_per_min)
LID_DROP_F_PER_MIN = 5.0
LID_WINDOW_S = 60.0
#: ... and back when it is within this of where it had been for the ten minutes before
LID_BACK_F = 5.0
LID_BEFORE_S = 600.0
#: a lid comes off a hot cooker. A fall from below this is the fire going out
#: or the probe coming out, which a recording shows the same way
LID_MIN_LEVEL_F = 140.0
#: where the control settled is within this of the setpoint (the documented
#: law's is a share of its band under it). A plateau further off is the cooker
#: idle, or failing to get to temperature: said so, not called "settled".
OFF_TARGET_F = 35.0

#: what is averaged over a bucket: the temperatures, setpoints and fan
_MEAN_KEYS = ("cook", *FOOD_PROBES, "cook_set", *(f"{probe}_set" for probe in FOOD_PROBES), "output_pct")
#: what is taken as the last value in it
_LAST_KEYS = ("cook_status", *(f"{probe}_status" for probe in FOOD_PROBES), "timer_status", "timer")
#: what the device gives whether or not a probe is in, so that a gap in it
#: is taken to be what was given last. A temperature is never carried: an
#: open probe is a missing reading, and inventing one for it would draw a
#: flat line through the outage and a cliff where the probe came back.
_CARRIED_KEYS = ("cook_set", *(f"{probe}_set" for probe in FOOD_PROBES), "output_pct", "cook_status",
                 "food1_status", "food2_status", "food3_status", "timer_status", "timer")


# -- the report -----------------------------------------------------------------------

def cook_report(conn: sqlite3.Connection, *, cook: int | None = None, hours: float | None = None,
                now: int | None = None) -> dict[str, Any]:
    """The report for a cook (``cook``), for the last ``hours``, or, with
    neither, for the latest cook or else the last :data:`DEFAULT_HOURS`.
    Raises ``LookupError`` if there is no such cook."""
    now = int(time.time()) if now is None else int(now)
    report: dict[str, Any] = {"range": _span(conn, cook, hours, now)}
    samples = db.samples_between(conn, report["range"]["from"], report["range"]["to"])
    pieces = _split(samples, MAX_GAP_S)
    longest = max(pieces, key=_duration) if pieces else []
    report["history"] = {
        "samples": len(samples),
        "stretch": _piece(longest),
        "skipped": [_piece(piece) for piece in pieces if piece is not longest],
    }
    grid = _even(_bucket(longest, STEP_S))
    # the lid is looked for over all that was recorded, not only the stretch
    # that is scored: the law is fitted to the whole span, and must not be
    # fitted through a lid coming off
    events = _lid(_bucket(samples, STEP_S))
    report["scorecard"] = _scorecard(grid, events) if len(grid) >= 2 else None
    report["verdict"] = _verdict(samples, events)
    return report


def _span(conn: sqlite3.Connection, cook: int | None, hours: float | None, now: int) -> dict[str, Any]:
    """From when to when, and why."""
    if cook is not None:
        row = db.get_cook(conn, cook)
        if row is None:
            raise LookupError(f"no cook {cook}")
        return _cook_span(row, now, "cook")
    if hours is not None:
        return {"from": now - int(hours * 3600.0), "to": now, "cook": None, "hours": float(hours), "basis": "hours"}
    latest = db.list_cooks(conn, limit=1)
    if latest:
        return _cook_span(latest[0], now, "latest cook")
    return {"from": now - int(DEFAULT_HOURS * 3600.0), "to": now, "cook": None, "hours": DEFAULT_HOURS, "basis": "default"}


def _cook_span(row: dict[str, Any], now: int, basis: str) -> dict[str, Any]:
    return {
        "from": int(row["started_ts"]),
        "to": int(row["ended_ts"]) if row["ended_ts"] is not None else now,
        "cook": row, "hours": None, "basis": basis,
    }


# -- reading the recording ---------------------------------------------------------------

def _split(samples: Sequence[dict], max_gap_s: float) -> list[list[dict]]:
    """The samples in unbroken pieces: a hole longer than ``max_gap_s`` splits."""
    pieces: list[list[dict]] = []
    for sample in samples:
        if pieces and sample["ts"] - pieces[-1][-1]["ts"] <= max_gap_s:
            pieces[-1].append(sample)
        else:
            pieces.append([sample])
    return pieces


def _duration(piece: Sequence[dict]) -> float:
    return piece[-1]["ts"] - piece[0]["ts"] if piece else 0.0


def _piece(piece: Sequence[dict]) -> dict[str, Any] | None:
    if not piece:
        return None
    return {"from": int(piece[0]["ts"]), "to": int(piece[-1]["ts"]), "samples": len(piece)}


def _bucket(samples: Sequence[dict], step: int) -> list[dict]:
    """The samples averaged onto a grid of ``step`` seconds: temperatures,
    setpoints and fan as means over each bucket, the rest as the last value in
    it. A bucket with no sample gets no row. What the device did not give at
    a sample (a setpoint, a status) is taken to be what it gave last, so that
    a reading is never lost to a missing one. A temperature is never carried
    forward: where a probe read OPEN, the reading is missing."""
    members: dict[int, list[dict]] = {}
    for sample in samples:
        members.setdefault(int(sample["ts"]) // step * step, []).append(sample)
    grid: list[dict] = []
    carried: dict[str, Any] = {}
    for first in sorted(members):
        group = members[first]
        row: dict[str, Any] = {"ts": first}
        for key in _MEAN_KEYS:
            values = [sample[key] for sample in group if sample[key] is not None]
            row[key] = statistics.fmean(values) if values else None
        for key in _LAST_KEYS:
            row[key] = next((sample[key] for sample in reversed(group) if sample[key] is not None), None)
        for key in _CARRIED_KEYS:
            if row[key] is None and key in carried:
                row[key] = carried[key]
            elif row[key] is not None:
                carried[key] = row[key]
        grid.append(row)
    return grid


def _even(grid: Sequence[dict]) -> list[dict]:
    """The longest part of ``grid`` with every minute there and readable: the
    scorecard counts time as samples times their spacing, so a missing minute
    ends it, and so does one with no setpoint, fan or status to go by."""
    pieces: list[list[dict]] = []
    for row in grid:
        if not _readable(row):
            continue
        if pieces and row["ts"] - pieces[-1][-1]["ts"] == STEP_S:
            pieces[-1].append(row)
        else:
            pieces.append([row])
    return max(pieces, key=len) if pieces else []


def _readable(row: dict) -> bool:
    return all(row[key] is not None for key in ("cook_set", "output_pct", "cook_status", "timer_status"))


# -- the lid, inferred --------------------------------------------------------------------

def lid_events(times: Sequence[float], cook: Sequence[float | None]) -> list[dict[str, Any]]:
    """Where the lid came off, inferred from the pit reading: it fell at least
    :data:`LID_DROP_F_PER_MIN` over :data:`LID_WINDOW_S`, from a cooking
    temperature, and it came back. Each event says where the fall began (the
    last reading before it: ``start``), where the reading bottomed out
    (``low``, ``low_f``) and where it was back within :data:`LID_BACK_F` of
    where it had been for the ten minutes before (``back``). All indices into
    the series. A fall the reading never came back from is the cooker going
    cold, and is not one."""
    if len(times) < 2:
        return []
    window = max(1, round(LID_WINDOW_S / (times[1] - times[0])))
    events: list[dict[str, Any]] = []
    for i in range(window, len(times)):
        before, here = cook[i - window], cook[i]
        if before is None or here is None:
            continue
        minutes = (times[i] - times[i - window]) / 60.0
        if minutes <= 0 or (before - here) / minutes < LID_DROP_F_PER_MIN:
            continue
        if events and i <= events[-1]["low"]:
            continue                                    # still the fall already counted
        start = i - window
        low = i
        while low + 1 < len(times) and cook[low + 1] is not None and cook[low + 1] < cook[low]:
            low += 1
        level = _level(times, cook, start)
        if level is None or level < LID_MIN_LEVEL_F:
            continue
        back = next((k for k in range(low + 1, len(times))
                     if cook[k] is not None and abs(cook[k] - level) <= LID_BACK_F), None)
        if back is None:
            continue                                    # never came back: the cooker going cold
        events.append({"start": start, "low": low, "low_f": cook[low], "back": back})
    return events


def _level(times: Sequence[float], cook: Sequence[float | None], start: int) -> float | None:
    """The mean pit reading over the ten minutes up to ``start``."""
    before = [value for k, value in enumerate(cook[:start + 1])
              if value is not None and times[k] >= times[start] - LID_BEFORE_S]
    return statistics.fmean(before) if before else None


def _lid(grid: Sequence[dict]) -> list[dict[str, Any]]:
    """The lid events of a grid, by clock time: when the pit began to fall,
    where it bottomed out, and when it was back."""
    times = [float(row["ts"]) for row in grid]
    return [
        {
            "at_ts": int(times[event["start"]]), "low_f": event["low_f"],
            "low_ts": int(times[event["low"]]),
            "back_ts": int(times[event["back"]]) if event["back"] is not None else None,
        }
        for event in lid_events(times, [row["cook"] for row in grid])
    ]


# -- the scorecard --------------------------------------------------------------------------

def _scorecard(grid: Sequence[dict], events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """``sim.summary.summarize`` over the stretch, its times given as clock
    times too, without the fuel (which a recording cannot know), and with
    every lid event in the stretch."""
    here = [event for event in events if grid[0]["ts"] <= event["at_ts"] <= grid[-1]["ts"]]
    lid = [any(event["at_ts"] < row["ts"] <= event["low_ts"] for event in here) for row in grid]
    started = int(grid[0]["ts"])
    rows = [
        {
            "t": row["ts"] - started, "cook": row["cook"], **{probe: row[probe] for probe in FOOD_PROBES},
            "setpoint_f": row["cook_set"], "output_pct": row["output_pct"],
            "cook_status": row["cook_status"], **{f"{probe}_status": row[f"{probe}_status"] for probe in FOOD_PROBES},
            "timer_status": row["timer_status"], "timer": row["timer"],
            "lid_open": was_open, "lid_hold": was_open,
        }
        for row, was_open in zip(grid, lid)
    ]
    summary = summarize(rows, fuel0_btu=0.0)
    for key in ("fuel_btu", "fuel_lb", "fuel_left"):
        del summary[key]
    # a plateau far from the setpoint is the cooker idle, or not up to temperature yet
    summary["off_target"] = summary["steady_error_f"] is not None and abs(summary["steady_error_f"]) > OFF_TARGET_F
    if summary["to_setpoint_s"] is not None:
        summary["to_setpoint_ts"] = started + summary["to_setpoint_s"]
    if summary["settled_at_s"] is not None:
        summary["settled_at_ts"] = started + summary["settled_at_s"]
    for info in summary["food"].values():
        if info.get("done_at_s") is not None:
            info["done_ts"] = started + info["done_at_s"]
        if info.get("stall") is not None:
            info["stall_from_ts"] = started + info["stall"][0]
            info["stall_to_ts"] = started + info["stall"][1]
    summary["lid"] = here
    return summary


# -- the control law --------------------------------------------------------------------------

#: how long before the lid lifts, and after the pit is back, is left out of
#: the fit with it: the device holds the fan off around a lid event, whatever
#: the law would ask for
LID_MARGIN_S = 60.0

def _verdict(samples: Sequence[dict], events: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """`sim identify` over the whole span, leaving the lid events out of it
    (as its ``--skip`` does), or its refusal."""
    if not samples:
        return None
    grid = _bucket(samples, IDENTIFY_STEP_S)
    started = grid[0]["ts"]
    skipped = [
        (event["at_ts"] - started - LID_MARGIN_S,
         (event["back_ts"] if event["back_ts"] is not None else event["low_ts"] + 600.0) - started + LID_MARGIN_S)
        for event in events
    ]
    segments = segments_from_samples(
        [row["ts"] - started for row in grid], [row["cook_set"] for row in grid],
        [row["cook"] for row in grid], [row["output_pct"] for row in grid], skip=skipped,
    )
    plugged = sum(1 for sample in samples if sample["cook"] is not None and sample["output_pct"] is not None)
    try:
        result = identify(segments, samples_total=len(samples), good_samples=plugged)
    except IdentifyError as exc:
        return {"state": "refused", "message": str(exc)}
    chosen = result.selected
    return {
        "state": "identified",
        "law": chosen.label,
        "line": pseudo_code(chosen.law, chosen.params, chosen.starts[0] if len(chosen.starts) == 1 else None),
        "rmse_pp": chosen.rmse,
        "params": chosen.params,
        "certainty": chosen.certainty,
        "samples": result.samples,
        "pinned_pct": result.pinned,
        "lid_windows_left_out": len(events),
        "fits": [
            {"law": fit.label, "params": fit.params, "rmse_pp": fit.rmse, "within_2pp": fit.within_2pp,
             "selected": fit.selected}
            for fit in result.fits
        ],
        "text": format_identification(result),
    }
