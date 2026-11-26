"""What a run adds up to (docs/SIMULATOR.md, ``sim run``).

:func:`summarize` reads a run's rows and reports how the control did:

* **to setpoint** — when the pit reading first reached the setpoint;
* **overshoot** — how far past it the reading went;
* **settled at / held for** — the longest stretch (at least half an hour)
  over which the reading stayed inside a 10 °F span: when it began, and how
  long it lasted. This is *where the control settled*, which for the
  documented law is a fraction of a band under the setpoint, not at it;
* **steady error** — the mean reading-minus-setpoint over that stretch;
* **fan** — the mean demand over the run, and over that stretch;
* **fuel** — what burned, and what is left.

These are measured against the *effective* setpoint in force for the longest
time (after ramp and cook hold), and on the reading rather than the true
temperature: this is what the device would have shown.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from typing import Sequence

from sim.device import Status
from sim.plant import BTU_PER_LB_LUMP
from sim.scenario import format_time

PROBES = ("cook", "food1", "food2", "food3")
FOOD_PROBES = PROBES[1:]

SETTLE_BAND_F = 5.0
#: a food reading between these counts as stalled
STALL_LOW_F, STALL_HIGH_F = 145.0, 172.0
#: a settled stretch has to last this long to count (a pass-through on the
#: way up the curve does not make a cook "settled")
MIN_SETTLED_S = 1800.0
#: and so does a stall
MIN_STALL_S = 600.0


def _longest(flags: Sequence[bool]) -> tuple[int, int]:
    """(index, length) of the longest run of True."""
    best_start, best_length, start = 0, 0, None
    for i, flag in enumerate(flags):
        if flag:
            start = i if start is None else start
            if i - start + 1 > best_length:
                best_start, best_length = start, i - start + 1
        else:
            start = None
    return best_start, best_length


def _plateau(values: Sequence[float | None], counted: Sequence[bool], span: float) -> tuple[int, int]:
    """(index, length) of the longest run of counted values that stays inside
    a ``span``-wide band.

    Two overlapping grids of bins that wide catch any run whose own range is
    at most half the span (and most slightly wider ones).
    """
    best_start, best_length = 0, 0
    for offset in (0.0, span / 2.0):
        run_start, run_band = None, None
        for i, value in enumerate(values):
            if not counted[i] or value is None:  # not in force, or an open probe: either ends a run
                run_start = None
                continue
            band = math.floor((value - offset) / span)
            if run_start is None or band != run_band:
                run_start, run_band = i, band
            if i - run_start + 1 > best_length:
                best_start, best_length = run_start, i - run_start + 1
    return best_start, best_length


def _first(rows, predicate):
    for row in rows:
        if predicate(row):
            return row["t"]
    return None


def summarize(
    rows: Sequence[dict], *, fuel0_btu: float, settle_band_f: float = SETTLE_BAND_F
) -> dict:
    """The run's rows (uniformly sampled) as a dict of plain numbers."""
    if not rows:
        raise ValueError("no rows to summarize")
    step = rows[1]["t"] - rows[0]["t"] if len(rows) > 1 else rows[0]["t"]

    cook = [row["cook"] for row in rows]
    # the setpoint in force for the longest time, to the wire's resolution
    reference = Counter(round(row["setpoint_f"], 1) for row in rows).most_common(1)[0][0]
    in_force = [abs(row["setpoint_f"] - reference) < 0.05 for row in rows]
    seen = [in_force[i] and cook[i] is not None for i in range(len(rows))]

    settled_start, settled_length = _plateau(cook, seen, 2.0 * settle_band_f)
    settled = settled_length * step >= MIN_SETTLED_S
    window = range(settled_start, settled_start + settled_length)

    burned = fuel0_btu * (1.0 - rows[-1]["fuel"]) if fuel0_btu > 0 else 0.0
    status_time = Counter()
    for row in rows:
        status_time[Status(row["cook_status"]).name] += step

    lid_low = [
        row["cook"] for row in rows
        if (row["lid_hold"] or row["lid_open"]) and row["cook"] is not None
    ]
    holds = sum(
        1 for before, row in zip(rows, rows[1:]) if row["lid_hold"] and not before["lid_hold"]
    )

    # what the lid cost: from it coming off until the reading is back within
    # the band of where it had been for the ten minutes before — counted from
    # when it had clearly left that band, so the first sample or two after
    # the lid lifts do not count as "back"
    lid_rows = [i for i, row in enumerate(rows) if row["lid_open"] or row["lid_hold"]]
    lid_back = None
    if lid_rows:
        first = lid_rows[0]
        before = [cook[i] for i in range(max(0, first - int(600 / step)), first) if cook[i] is not None]
        level = statistics.mean(before) if before else cook[first]
        left = next(
            (i for i in range(first, len(rows)) if cook[i] is not None and abs(cook[i] - level) > settle_band_f),
            None,
        )
        if left is not None:
            lid_back = next((
                rows[i]["t"] - rows[first]["t"] for i in range(left + 1, len(rows))
                if cook[i] is not None and abs(cook[i] - level) <= settle_band_f
            ), None)

    food = {}
    for probe in FOOD_PROBES:
        readings = [row[probe] for row in rows]
        if all(value is None for value in readings):
            food[probe] = {"plugged": False}
            continue
        stall_start, stall_length = _longest([
            value is not None and STALL_LOW_F <= value <= STALL_HIGH_F for value in readings
        ])
        stalled = [readings[i] for i in range(stall_start, stall_start + stall_length)]
        food[probe] = {
            "plugged": True,
            "done_at_s": _first(rows, lambda row, p=probe: row[f"{p}_status"] == Status.DONE),
            "stall": (
                [rows[stall_start]["t"], rows[stall_start + stall_length - 1]["t"], statistics.mean(stalled)]
                if stall_length * step >= MIN_STALL_S else None
            ),
            "final_f": next((value for value in reversed(readings) if value is not None), None),
        }

    last = rows[-1]
    return {
        "duration_s": last["t"],
        "setpoint_f": reference,
        "to_setpoint_s": _first(rows, lambda row: abs(row["setpoint_f"] - reference) < 0.05 and row["cook"] is not None and row["cook"] >= reference),
        "overshoot_f": max([0.0] + [cook[i] - reference for i in range(len(rows)) if seen[i]]),
        "settled_at_s": rows[settled_start]["t"] if settled else None,
        "held_s": settled_length * step if settled else 0.0,
        "steady_error_f": statistics.mean(cook[i] - reference for i in window) if settled else None,
        "steady_sd_f": statistics.pstdev(cook[i] for i in window) if settled else None,
        "fan_mean_pct": statistics.mean(row["output_pct"] for row in rows),
        "fan_hold_pct": statistics.mean(rows[i]["output_pct"] for i in window) if settled else None,
        "fan_peak_pct": max(row["output_pct"] for row in rows),
        "fuel_btu": burned,
        "fuel_lb": burned / BTU_PER_LB_LUMP,
        "fuel_left": last["fuel"] if fuel0_btu > 0 else None,
        "cook_status_s": dict(status_time),
        "lid_holds": holds,
        "lid_low_f": min(lid_low) if lid_low else None,
        "lid_back_s": lid_back,
        "food": food,
        "final": {
            "cook_f": last["cook"],
            "setpoint_f": last["setpoint_f"],
            "cook_status": last["cook_status"],
            "timer": last["timer"],
        },
    }


# -- as text -------------------------------------------------------------------

def _num(value: float | None, digits: int = 1, unit: str = "") -> str:
    return "–" if value is None else f"{value:.{digits}f}{unit}"


def _signed(value: float | None, digits: int = 1) -> str:
    return "–" if value is None else f"{value:+.{digits}f}"


def _status_spans(status_time: dict) -> str:
    """Time in each cook status, longest first."""
    return ", ".join(
        f"{name} {format_time(seconds)}"
        for name, seconds in sorted(status_time.items(), key=lambda item: -item[1])
    )


def _notes(summary: dict) -> str:
    notes = []
    if summary["lid_low_f"] is not None:
        hold = (
            f", {summary['lid_holds']} hold, back in {format_time(summary['lid_back_s'])}"
            if summary["lid_holds"] else ", no hold"
        )
        notes.append(f"lid, low {summary['lid_low_f']:.0f} °F{hold}")
    statuses = summary["cook_status_s"]
    for name in ("HOLD", "ALARM", "SHUTDOWN"):
        if name in statuses:
            notes.append({"HOLD": "cook hold", "ALARM": "alarm", "SHUTDOWN": "shutdown"}[name])
    return ", ".join(notes)


def format_summary(summary: dict) -> str:
    """One run, as a few lines of text."""
    final = summary["final"]
    lines = [
        f"{summary['scenario']} · {summary['controller']} · {format_time(summary['duration_s'])} run, "
        f"setpoint {summary['setpoint_f']:g} °F",
        f"  reached setpoint {format_time(summary['to_setpoint_s'])}; overshot "
        f"{_num(summary['overshoot_f'])} °F; settled at {format_time(summary['settled_at_s'])} "
        f"and held for {format_time(summary['held_s'] or None)}",
        f"  steady error {_signed(summary['steady_error_f'])} °F (sd {_num(summary['steady_sd_f'])}); "
        f"fan mean {_num(summary['fan_mean_pct'], 0, ' %')}, while held "
        f"{_num(summary['fan_hold_pct'], 0, ' %')}, peak {summary['fan_peak_pct']} %",
        (
            f"  fuel burned {_num(summary['fuel_lb'], 2)} lb, "
            f"{_num(summary['fuel_left'] * 100 if summary['fuel_left'] is not None else None, 0, ' %')} of the load left"
        ),
    ]
    for probe, info in summary["food"].items():
        if not info["plugged"]:
            continue
        stall = info["stall"]
        in_stall = (
            f"stall {stall[2]:.0f} °F from {format_time(stall[0])} to {format_time(stall[1])}; "
            if stall else "no stall; "
        )
        lines.append(
            f"  {probe}: done at {format_time(info['done_at_s'])}; {in_stall}"
            f"final {_num(info['final_f'])} °F"
        )
    if summary["lid_low_f"] is not None:
        detector = (
            f"open-lid detect held the fan {summary['lid_holds']} time"
            + ("" if summary["lid_holds"] == 1 else "s")
            + f"; the reading was back within {5:g} °F of where it had been after "
            f"{format_time(summary['lid_back_s'])}"
            if summary["lid_holds"] else "open-lid detect was off"
        )
        lines.append(
            f"  lid came off: pit reading down to {_num(summary['lid_low_f'])} °F; {detector}"
        )
    lines.append(
        f"  cook status: {_status_spans(summary['cook_status_s'])}; ends at "
        f"{_num(final['cook_f'])} °F on a {final['setpoint_f']:g} °F target, "
        f"{Status(final['cook_status']).name}"
    )
    return "\n".join(lines)


_TABLE_HEAD = (
    "scenario", "controller", "to set", "overshoot °F", "settled at", "held for",
    "steady err °F", "fan mean %", "fan held %", "fuel lb", "food1 done", "notes",
)


def _cells(summary: dict) -> tuple:
    food1 = summary["food"]["food1"]
    return (
        summary["scenario"], summary["controller"],
        format_time(summary["to_setpoint_s"]), _num(summary["overshoot_f"]),
        format_time(summary["settled_at_s"]), format_time(summary["held_s"] or None),
        _signed(summary["steady_error_f"]), _num(summary["fan_mean_pct"], 0),
        _num(summary["fan_hold_pct"], 0), _num(summary["fuel_lb"], 2),
        format_time(food1["done_at_s"]) if food1["plugged"] else "no probe",
        _notes(summary),
    )


def format_table(summaries: Sequence[dict]) -> str:
    """Runs side by side, one line each, for comparing controllers on a
    scenario — or scenarios under a controller."""
    if not summaries:
        return "(nothing to compare)"
    rows = [_cells(summary) for summary in summaries]
    widths = [
        max(len(_TABLE_HEAD[i]), *(len(row[i]) for row in rows))
        for i in range(len(_TABLE_HEAD))
    ]

    def line(cells: Sequence[str]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths)).rstrip()

    rule = line(tuple("-" * width for width in widths))
    return "\n".join([line(_TABLE_HEAD), rule, *(line(row) for row in rows)])
