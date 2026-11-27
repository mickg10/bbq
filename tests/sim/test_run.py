"""`sim run`: what a run adds up to, its CSV, and the scenarios that ship."""

from __future__ import annotations

import csv
import dataclasses
import json

import pytest

from sim.run import CSV_COLUMNS, run_scenario, write_csv
from sim.scenario import SCENARIOS_DIR, load_scenario, scenario_path
from sim.summary import (
    MIN_SETTLED_S,
    _plateau,
    format_summary,
    format_table,
    summarize,
)

STEP = 60.0  # one row a minute, as `sim run` keeps them


def _row(t, cook, setpoint=225.0, fan=35, food1=None, **extra) -> dict:
    row = {
        "t": t, "cook": cook, "food1": food1, "food2": None, "food3": None,
        "cook_set": setpoint, "food1_set": 203.0, "food2_set": 180.0, "food3_set": 180.0,
        "output_pct": fan, "cook_status": 0, "food1_status": 0, "food2_status": 4,
        "food3_status": 4, "timer": "00:00:00", "timer_status": 0, "pit_f": cook,
        "food1_f": food1, "food2_f": None, "food3_f": None, "setpoint_f": setpoint,
        "fan_duty": fan / 100.0, "fire": 0.3, "fuel": 1.0, "t_amb": 70.0, "wind_mph": 0.0,
        "lid_open": 0, "lid_hold": 0,
    }
    row.update(extra)
    return row


def _minutes(count, cook, start=0, **extra) -> list[dict]:
    """``count`` rows a minute apart from minute ``start``, ``cook`` a value or
    a function of the row index."""
    return [
        _row((start + i) * STEP, cook(i) if callable(cook) else cook, **extra)
        for i in range(count)
    ]


def _heat_up(minutes: int = 40, top: float = 225.0) -> list[dict]:
    """A kettle warming from 70 °F to just short of ``top`` over ``minutes``,
    full fan all the way, and arriving on the last row (so that where the
    heat-up ends and the plateau begins is not a matter of degree)."""
    return _minutes(
        minutes,
        lambda i: top if i == minutes - 1 else 70.0 + (top - 6.0 - 70.0) * i / max(minutes - 1, 1),
        fan=100,
    )


# -- the plateau behind "settled" ------------------------------------------------

def test_plateau_is_the_longest_run_inside_a_narrow_band():
    values = [100.0] * 5 + [200.0, 200.4, 200.9, 201.0, 205.0, 204.0] + [300.0] * 3
    assert _plateau(values, [True] * len(values), 10.0) == (5, 6)  # 6 in a 5 °F span beats 5 flat
    assert _plateau(values, [True] * len(values), 3.0) == (0, 5)   # too narrow to bridge 200->205
    assert _plateau(values, [True] * len(values), 400.0) == (0, 14)  # everything is one plateau then
    assert _plateau([1.0, None, 1.0, 1.0], [True] * 4, 10.0) == (2, 2)  # a gap ends a run
    assert _plateau([1.0, None, 1.0], [True] * 3, 10.0) == (0, 1)  # equal runs: the earlier is reported
    assert _plateau([1.0, 1.0], [False, False], 10.0) == (0, 0)   # nothing in force
    assert _plateau([], [], 10.0) == (0, 0)


def test_plateau_spans_any_band_whatever_its_offset():
    """A stretch with a range of 4 °F is found wherever it sits relative to
    the 10 °F bins — and one with a range of 9 °F nearly always is."""
    for base in (0.0, 1.3, 2.5, 3.7, 4.9, 5.5, 6.1, 7.7, 8.8, 9.9):
        values = [base + 4.0 * i / 199 for i in range(200)]
        start, length = _plateau(values, [True] * 200, 10.0)
        assert (start, length) == (0, 200), base


# -- summarize ------------------------------------------------------------------

def test_summary_of_a_cook_that_reaches_and_holds_its_setpoint():
    rows = _heat_up(40) + _minutes(20, 228.0, 40, fan=0) + _minutes(270, 225.0, 60, fan=34)
    s = summarize(rows, fuel0_btu=100_000.0)
    assert (s["setpoint_f"], s["duration_s"]) == (225.0, 329 * STEP)  # the last row's time
    assert s["to_setpoint_s"] == pytest.approx(39 * STEP)
    assert s["overshoot_f"] == pytest.approx(3.0)
    # the plateau is the overshoot and the hold together: 3 °F apart at most
    assert s["settled_at_s"] == pytest.approx(39 * STEP) and s["held_s"] == pytest.approx(291 * STEP)
    assert s["steady_error_f"] == pytest.approx(20 * 3.0 / 291, abs=0.01) and s["steady_sd_f"] == pytest.approx(0.76, abs=0.01)  # 20 rows at 228 among 291
    assert s["fan_mean_pct"] == pytest.approx((40 * 100 + 20 * 0 + 270 * 34) / 330)
    assert s["fan_hold_pct"] == pytest.approx((100 + 270 * 34) / 291) and s["fan_peak_pct"] == 100
    assert s["food"] == {probe: {"plugged": False} for probe in ("food1", "food2", "food3")}


def test_summary_measures_against_the_setpoint_in_force_the_longest():
    """An hour of cook hold at the end does not count against a 225 °F hold."""
    rows = _heat_up(40) + _minutes(300, 225.0, 40, fan=34) + _minutes(60, 180.0, 340, setpoint=180.0, fan=0)
    s = summarize(rows, fuel0_btu=1.0)
    assert s["setpoint_f"] == 225.0
    assert s["overshoot_f"] == 0.0 and s["steady_error_f"] == pytest.approx(0.0, abs=1e-6)
    assert s["settled_at_s"] == pytest.approx(39 * STEP) and s["held_s"] == pytest.approx(301 * STEP)
    assert s["final"]["setpoint_f"] == 180.0 and s["final"]["cook_f"] == 180.0


def test_cook_hold_is_not_the_setpoint_even_when_it_lasts_longest():
    """Regression: the hold is a different target on purpose. In a run where
    it outlasts the cook, the cook's temperature was still what 'the setpoint'
    meant — otherwise 70 °F of perfectly good hold read as overshoot."""
    rows = _heat_up(40, top=250.0) + _minutes(120, 250.0, 40, fan=40) + _minutes(300, 180.0, 160, setpoint=180.0, fan=0)
    for row in rows[:160]:
        row.update(setpoint_f=250.0, cook_set=250.0)
    for row in rows[160:]:
        row["timer_status"] = 5  # HOLD is a timer status (the cook probe stays OK)
    s = summarize(rows, fuel0_btu=1.0)
    assert s["setpoint_f"] == 250.0  # 160 rows of cook against 300 of hold
    assert s["overshoot_f"] == 0.0 and s["to_setpoint_s"] == pytest.approx(39 * STEP)
    assert s["steady_error_f"] == pytest.approx(0.0, abs=1e-6) and s["held_s"] == pytest.approx(121 * STEP)
    assert s["final"]["setpoint_f"] == 180.0 and "HOLD" in s["timer_status_s"]
    assert "HOLD" not in s["cook_status_s"]
    # a run that is nothing but hold still gets a reference
    only = _minutes(60, 180.0, fan=0, setpoint=180.0)
    for row in only:
        row["timer_status"] = 5
    assert summarize(only, fuel0_btu=1.0)["setpoint_f"] == 180.0


def test_summary_says_where_the_documented_law_settled_short():
    """The baseline parks a fraction of a band under the setpoint. That is
    'settled' too — and the steady error says how far short."""
    rows = _heat_up(40, top=215.0) + _minutes(320, 215.0, 40, fan=32)
    s = summarize(rows, fuel0_btu=1.0)
    assert s["to_setpoint_s"] is None and s["overshoot_f"] == 0.0
    assert s["settled_at_s"] == pytest.approx(39 * STEP) and s["held_s"] == pytest.approx(321 * STEP)
    assert s["steady_error_f"] == pytest.approx(-10.0)
    assert s["fan_hold_pct"] == pytest.approx((100 + 320 * 32) / 321)  # the plateau opens on the last heat-up row


def test_a_pass_through_on_the_way_up_is_not_settling():
    """Ten minutes inside the band while climbing past it, then a plateau
    below it: only the plateau counts."""
    rows = (
        _minutes(30, lambda i: 70.0 + 150.0 * i / 29)
        + _minutes(10, 226.0, 30)
        + _minutes(240, 215.0, 40)
    )
    s = summarize(rows, fuel0_btu=1.0)
    assert s["to_setpoint_s"] == pytest.approx(30 * STEP) and s["overshoot_f"] == pytest.approx(1.0)
    assert s["settled_at_s"] == pytest.approx(40 * STEP) and s["steady_error_f"] == pytest.approx(-10.0)


def test_a_run_that_never_settles_says_so():
    rows = _minutes(300, lambda i: 250.0 - 0.5 * i)  # falling the whole time
    s = summarize(rows, fuel0_btu=1.0)
    assert s["settled_at_s"] is None and s["held_s"] == 0.0
    assert s["steady_error_f"] is None and s["fan_hold_pct"] is None
    assert s["to_setpoint_s"] == 0.0 and s["overshoot_f"] == 25.0  # it began 25 °F above
    short = summarize(_heat_up(40) + _minutes(20, 225.0), fuel0_btu=1.0)  # 20 min is under the minimum
    assert short["settled_at_s"] is None
    assert MIN_SETTLED_S > 20 * STEP


def test_summary_of_food_fuel_lid_and_statuses():
    food = [40.0 + i for i in range(60)] + [151.0] * 120 + [203.0] * 120
    rows = _heat_up(60, top=225.0)[:60] + _minutes(240, 225.0, 60, fan=34)
    assert len(rows) == 300
    for i, row in enumerate(rows):
        row.update(food1=food[i], food1_f=food[i], food1_status=3 if food[i] >= 203.0 else 0)
        row["fuel"] = 1.0 - i / 1000.0
        row["cook_status"] = 2 if i < 30 else 0
    for i in range(100, 104):  # the lid comes off at 100 min, the detector holds the fan
        rows[i].update(lid_open=1, lid_hold=1, cook=200.0 + i - 100, output_pct=0)
    for i in range(104, 130):
        rows[i].update(lid_hold=1, cook=201.0 + (i - 104) * 0.9, output_pct=0)
    s = summarize(rows, fuel0_btu=50_000.0)
    brisket = s["food"]["food1"]
    assert brisket["plugged"] and brisket["done_at_s"] == pytest.approx(180 * STEP)
    assert brisket["stall"][:2] == [pytest.approx(60 * STEP), pytest.approx(179 * STEP)]
    assert brisket["stall"][2] == pytest.approx(151.0) and brisket["final_f"] == 203.0
    assert s["food"]["food2"] == {"plugged": False}
    assert s["fuel_lb"] == pytest.approx(299 * 50_000.0 / 1000.0 / 13_000.0)
    assert s["fuel_left"] == pytest.approx(0.701)
    assert s["cook_status_s"] == {"OK": 270 * STEP, "LOW": 30 * STEP}
    assert (s["lid_holds"], s["lid_low_f"]) == (1, 200.0)
    assert s["lid_back_s"] == pytest.approx(26 * STEP)  # within 5 °F of 225 again at minute 126
    assert s["final"]["cook_status"] == 0 and s["final"]["timer"] == "00:00:00"


def test_a_lid_with_the_detector_off_is_reported_too():
    rows = _minutes(120, 225.0)
    for i in range(60, 64):
        rows[i].update(lid_open=1, cook=200.0)
    s = summarize(rows, fuel0_btu=0.0)
    assert (s["lid_holds"], s["lid_low_f"], s["lid_back_s"]) == (0, 200.0, STEP * 4)
    assert s["fuel_btu"] == 0.0 and s["fuel_lb"] == 0.0 and s["fuel_left"] is None
    assert "lid, low 200 °F, no hold" in format_table([{**s, "scenario": "s", "controller": "c"}])


def test_summary_needs_rows():
    with pytest.raises(ValueError):
        summarize([], fuel0_btu=1.0)


# -- as text --------------------------------------------------------------------

def _run(scenario_name, controller, duration_h=2.0, dt=1.0, sample_s=60):
    """A shipped scenario, shortened, a row every ``sample_s`` seconds."""
    scenario = load_scenario(SCENARIOS_DIR / f"{scenario_name}.yaml", controller=controller)
    return run_scenario(
        dataclasses.replace(scenario, duration=duration_h * 3600.0),
        sample_every=max(1, round(sample_s / dt)), dt=dt,
    )


def test_format_summary_and_table():
    result = _run("ribs-6h", "proportional", dt=5.0)
    text = format_summary(result.summary)
    assert text.startswith("ribs-6h · proportional · 2h run, setpoint 250 °F")
    assert "reached setpoint –" in text and "steady error " in text
    assert "fan mean " in text and "fuel burned " in text and "cook status:" in text
    summaries = [r.summary for r in (_run("ribs-6h", c, dt=5.0) for c in ("proportional", "pi"))]
    lines = format_table(summaries, legend=False).splitlines()
    assert lines[0].split()[:5] == ["scenario", "controller", "reached", "set", "overshoot"]
    assert set(lines[1]) <= {"-", " "}
    assert len(lines) == 4 and all(line.startswith("ribs-6h ") for line in lines[2:])
    assert format_table([], legend=False) == "(nothing to compare)"
    assert all(len(line) < 240 for line in lines)


def test_the_table_explains_itself():
    """A plateau can precede the first touch of the setpoint; without the
    legend that reads as a bug."""
    summaries = [_run("ribs-6h", c, dt=5.0).summary for c in ("proportional", "pi")]
    bare = format_table(summaries, legend=False).splitlines()
    with_legend = format_table(summaries).splitlines()
    assert with_legend[:len(bare)] == bare and len(with_legend) > len(bare)
    legend = " ".join(with_legend[len(bare):])
    assert "first reached the setpoint (–: never)" in legend
    assert "inside a 10 °F span" in legend and 'so it can precede "reached set"' in legend
    assert "steady err: mean reading minus setpoint" in legend
    assert all(len(line) <= max(100, len(bare[0])) for line in with_legend)


# -- run_scenario and its CSV -----------------------------------------------------

def test_a_run_is_deterministic(tmp_path):
    first = _run("lid-open", "proportional", 0.5)
    second = _run("lid-open", "proportional", 0.5)
    assert first.rows == second.rows and first.summary == second.summary
    assert len(first.rows) == 30 and first.rows[-1]["t"] == 1800.0
    assert first.summary["scenario"] == "lid-open" and first.summary["controller"] == "proportional"


def test_csv_mirrors_the_dashboard_sample_table(tmp_path):
    result = _run("brisket-12h", "proportional", 0.25)
    path = write_csv(tmp_path / "runs" / "cook.csv", result.rows, fuel0_btu=result.sim.cooker.plant.fuel0_btu)
    assert path == tmp_path / "runs" / "cook.csv"
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        assert tuple(reader.fieldnames) == CSV_COLUMNS
        rows = list(reader)
    assert len(rows) == 15
    first, last = rows[0], rows[-1]
    assert CSV_COLUMNS[2:10] == ("cook", "food1", "food2", "food3", "cook_set", "food1_set", "food2_set", "food3_set")
    assert first["t"] == "60" and first["t_h"] == "0.0167"
    assert first["food2"] == "" and first["food3"] == ""         # OPEN: empty, as NULL in the database
    assert first["food2_status"] == "4" and first["cook_status"] == "2"
    assert abs(float(first["cook"]) - float(first["pit_f"])) < 1.0    # reading against truth
    # the fraction is written to four places; fuel_btu is the precise column
    assert abs(float(last["fuel_btu"]) - float(last["fuel"]) * 195_000.0) < 195_000.0 * 0.5e-4
    assert float(last["t"]) == 3600.0 * 0.25 and last["t_amb"] == "70"
    assert first["timer"] == "00:00:00" and first["timer_status"] == "0"


# -- the scenarios that ship, each as its own controller runs it -------------------

def test_brisket_12h():
    s = run_scenario(load_scenario(SCENARIOS_DIR / "brisket-12h.yaml"), sample_every=60, dt=5.0).summary
    assert 12.5 * 3600 < s["food"]["food1"]["done_at_s"] < 14 * 3600
    stall = s["food"]["food1"]["stall"]
    assert 145 < stall[2] < 155 and stall[1] - stall[0] > 2.5 * 3600   # hours in the stall
    assert s["steady_error_f"] == pytest.approx(-9.6, abs=1.0)          # the documented law's offset
    # the food being done is not cook hold: that is the timer's doing
    assert s["final"]["cook_status"] == 0 and "HOLD" not in s["timer_status_s"]
    assert s["final"]["setpoint_f"] == 225.0
    assert s["to_setpoint_s"] is None


def test_ribs_6h():
    s = run_scenario(load_scenario(SCENARIOS_DIR / "ribs-6h.yaml"), sample_every=60, dt=5.0).summary
    assert 5 * 3600 < s["food"]["food1"]["done_at_s"] < 6 * 3600       # a six-hour rack
    assert s["food"]["food1"]["stall"][2] == pytest.approx(153.0, abs=2.0)
    # the seven-hour timer drops the pit to COOKHOLD, and the timer says so
    assert "HOLD" in s["timer_status_s"] and s["final"]["cook_status"] == 0
    assert (s["final"]["setpoint_f"], s["final"]["timer_status"]) == (180.0, 5)
    assert s["final"]["cook_f"] < 185.0


def test_lid_open():
    s = run_scenario(load_scenario(SCENARIOS_DIR / "lid-open.yaml"), sample_every=60, dt=5.0).summary
    assert s["lid_holds"] == 1                                        # the detector held the fan
    assert 180.0 < s["lid_low_f"] < 200.0                             # a 25–45 °F drop
    assert 10 * 60 < s["lid_back_s"] < 40 * 60                        # and back within the half hour
    assert s["food"]["food1"]["stall"] is not None and s["food"]["food1"]["done_at_s"] is None
    assert s["held_s"] > MIN_SETTLED_S


def test_fuel_out():
    s = run_scenario(load_scenario(SCENARIOS_DIR / "fuel-out.yaml"), sample_every=60, dt=5.0).summary
    assert 5 * 3600 < s["held_s"] < 8 * 3600                          # six good hours
    assert s["final"]["cook_f"] < 150.0 and "LOW" in s["cook_status_s"]  # then it falls away
    assert s["fan_peak_pct"] == 100 and s["fan_mean_pct"] > 50
    assert s["fuel_left"] == pytest.approx(0.0, abs=0.02)


def test_cold_windy():
    s = run_scenario(load_scenario(SCENARIOS_DIR / "cold-windy.yaml"), sample_every=60, dt=5.0).summary
    assert s["setpoint_f"] == 250.0 and s["to_setpoint_s"] is None
    assert s["fan_mean_pct"] > 65 and s["fan_hold_pct"] > 60           # the fan works for it
    assert s["steady_error_f"] == pytest.approx(-20.4, abs=2.5)        # more fan, more offset
    assert s["settled_at_s"] == pytest.approx(69 * 60, abs=10 * 60) and s["held_s"] > 5 * 3600


def test_the_controller_scorecard_separates_the_three_laws():
    """The lead's scorecard: the documented law, a third of the fan up front,
    and integral action, on one scenario."""
    results = [
        run_scenario(
            dataclasses.replace(load_scenario(SCENARIOS_DIR / "brisket-12h.yaml", controller=law), duration=4 * 3600.0),
            sample_every=60, dt=5.0,
        ).summary for law in ("proportional", "bias", "pi")
    ]
    by_law = {s["controller"]: s for s in results}
    assert by_law["proportional"]["to_setpoint_s"] is None
    assert by_law["proportional"]["steady_error_f"] == pytest.approx(-9.6, abs=0.5)
    assert 5.0 < by_law["bias"]["overshoot_f"] < 11.0 and abs(by_law["bias"]["steady_error_f"]) < 1.5
    assert by_law["pi"]["overshoot_f"] < 3.5 and abs(by_law["pi"]["steady_error_f"]) < 1.0
    table = format_table(results, legend=False)
    assert [line.split()[1] for line in table.splitlines()[2:]] == ["proportional", "bias", "pi"]


def test_scenario_path_finds_a_shipped_scenario_by_name():
    assert scenario_path("ribs-6h") == SCENARIOS_DIR / "ribs-6h.yaml"
    assert scenario_path("brisket-12h.yaml") == SCENARIOS_DIR / "brisket-12h.yaml"


def test_json_of_a_summary_is_plain_data():
    result = _run("fuel-out", "proportional", 0.2, dt=5.0)
    assert json.loads(json.dumps(result.summary))["scenario"] == "fuel-out"
