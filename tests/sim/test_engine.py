"""The simulator end to end: deterministic, and behaving like a cooker."""

from __future__ import annotations

import pytest

from sim.controllers import get_controller
from sim.device import Status
from sim.engine import Event, Simulator
from sim.food import Food, preset

HOUR = 3600.0
STATUS = Status


# -- determinism --------------------------------------------------------------

def test_the_same_seed_replays_bit_for_bit(make_sim):
    def run():
        sim = make_sim(seed=3, sample_every=7)
        sim.run(3 * HOUR)
        return sim.rows

    assert run() == run()


def test_a_different_seed_changes_only_the_noise(make_sim):
    a, b = make_sim(seed=0, sample_every=13), make_sim(seed=1, sample_every=13)
    a.run(2 * HOUR)
    b.run(2 * HOUR)
    assert [r["cook"] for r in a.rows] != [r["cook"] for r in b.rows]
    for ra, rb in zip(a.rows, b.rows):
        assert ra["pit_f"] == pytest.approx(rb["pit_f"], abs=0.05)


def test_a_row_mirrors_the_dashboard_sample_table(make_sim):
    row = make_sim().step()
    for column in (
        "cook", "food1", "food2", "food3", "cook_set", "food1_set", "food2_set",
        "food3_set", "output_pct", "cook_status", "food1_status", "food2_status",
        "food3_status", "timer", "timer_status",
    ):
        assert column in row, column
    assert row["t"] == 1.0
    assert isinstance(row["cook"], float) and isinstance(row["output_pct"], int)
    # nothing plugged into food2/3: OPEN, and the device says so
    assert row["food2"] is None and row["food2_status"] == STATUS.ERROR
    assert row["cook_status"] == STATUS.LOW and row["output_pct"] == 100


# -- a cook -------------------------------------------------------------------

def test_a_cold_start_reaches_the_band_and_settles(make_sim):
    sim = make_sim(cook_set_f=225.0, sample_every=60)
    sim.run(HOUR)
    cook = [r["cook"] for r in sim.rows]
    assert cook[0] < 75.0
    assert cook[40] > 195.0                     # 40 minutes in
    assert max(cook) < 232.0                   # no big overshoot on the way
    assert 205.0 < cook[-1] < 225.0            # and it settles just under target
    assert 20 <= sim.rows[-1]["output_pct"] <= 45  # on a third of the fan


def test_preheated_the_pit_holds_steady_for_a_while(make_sim):
    sim = make_sim(preheat=215.0, cook_set_f=225.0, sample_every=60)
    sim.run(HOUR)
    pit = [r["pit_f"] for r in sim.rows]
    assert all(abs(p - 215.0) < 1.5 for p in pit[:10])
    assert 212.0 < pit[-1] < 218.0             # a P-only law creeps down, slowly


def test_fuel_burns_down_with_the_cook(make_sim):
    sim = make_sim(preheat=215.0, cook_set_f=225.0, sample_every=600)
    sim.run(2 * HOUR)
    assert 0.85 < sim.rows[-1]["fuel"] < 0.95
    assert sim.cooker.plant.fuel_btu >= 0.0


def test_food_stalls_then_finishes_and_the_pit_goes_on_hold(make_sim):
    sim = make_sim(
        food=Food(preset("ribs"), temp_f=40.0), cook_set_f=250.0,
        food_set_f=195.0, cookhold_f=180.0, sample_every=60,
    )
    sim.run(6 * HOUR)
    rows = sim.rows
    done = [i for i, r in enumerate(rows) if r["food1_status"] == STATUS.DONE]
    assert done and 2 * 60 < done[0] < 5 * 60  # ribs take hours, not minutes
    # with one food probe, done is also "all done": the pit drops to COOKHOLD
    assert rows[done[0]]["cook_status"] == STATUS.HOLD
    assert rows[-1]["cook_status"] == STATUS.HOLD
    assert rows[-1]["cook_set"] == 180.0 and rows[-1]["setpoint_f"] == 180.0
    assert rows[-1]["pit_f"] < 185.0
    # the stall: flat in the low 150s between warming up and climbing out
    stall = [r["food1"] for r in rows if 90 * 60 <= r["t"] < 135 * 60]
    assert stall and all(145.0 < temp < 160.0 for temp in stall)
    assert max(stall) - min(stall) < 3.0
    assert min(r["food1"] for r in rows) < 45.0   # it started cold


def test_lid_open_drops_the_pit_then_it_recovers(make_sim):
    sim = make_sim(
        preheat=215.0, cook_set_f=225.0, lid_events=[(600.0, 780.0)], sample_every=1,
    )
    rows = sim.run(45 * 60)
    before = [r for r in rows if 500 <= r["t"] < 600]
    assert all(abs(r["pit_f"] - 215.0) < 2.0 for r in before)
    during = [r for r in rows if 600 <= r["t"] < 1200]
    lowest = min(r["pit_f"] for r in during)
    assert 165.0 < lowest < 192.0   # three minutes off: 25–50 °F gone, a fast drop
    held = [r for r in rows if r["lid_hold"]]
    assert held and all(r["output_pct"] == 0 and r["fan_duty"] == 0.0 for r in held)
    half_hour = [r for r in rows if r["t"] >= 2400]    # half an hour after shutting it
    assert half_hour and all(r["pit_f"] > 205.0 for r in half_hour)
    assert abs(half_hour[-1]["pit_f"] - 215.0) < 3.0


def test_fuel_out_lets_the_pit_fall(make_sim):
    sim = make_sim(preheat=239.0, cook_set_f=250.0, fuel_btu=100_000.0, sample_every=600)
    sim.run(9 * HOUR)
    by_hour = {round(r["t"] / HOUR, 3): r for r in sim.rows}
    assert by_hour[6.0]["pit_f"] > 235.0        # held fine while the bed was deep
    assert by_hour[7.0]["pit_f"] < by_hour[6.0]["pit_f"] - 2.0
    assert by_hour[9.0]["pit_f"] < 190.0        # then it could not keep up
    assert sim.cooker.plant.fuel_frac < 0.05


def test_wind_makes_the_fan_work_harder(make_sim):
    calm = make_sim(preheat=239.0, cook_set_f=250.0, sample_every=600)
    windy = make_sim(preheat=239.0, cook_set_f=250.0, wind_mph=15.0, sample_every=600)
    calm.run(2 * HOUR)
    windy.run(2 * HOUR)
    assert windy.rows[-1]["output_pct"] > calm.rows[-1]["output_pct"] + 10


def test_ambient_can_follow_a_schedule(make_sim):
    sim = make_sim(
        preheat=215.0, ambient_f=[(0.0, 70.0), (HOUR, 40.0)], sample_every=600,
    )
    sim.run(HOUR)
    assert [round(r["t_amb"], 1) for r in sim.rows] == [65.0, 60.0, 55.0, 50.0, 45.0, 40.0]


# -- controller variants in the loop ------------------------------------------

def test_the_pi_variant_closes_the_offset_the_documented_law_leaves(make_sim):
    documented = make_sim(preheat=215.0, cook_set_f=225.0, sample_every=600)
    integral = make_sim(
        preheat=215.0, cook_set_f=225.0, sample_every=600, controller=get_controller("pi"),
    )
    documented.run(3 * HOUR)
    integral.run(3 * HOUR)
    assert abs(documented.rows[-1]["pit_f"] - 215.0) < 1.0  # a third of a band under
    assert abs(integral.rows[-1]["pit_f"] - 225.0) < 0.5
    assert max(r["pit_f"] for r in integral.rows) < 227.0    # and no overshoot getting there


def test_pi_from_cold_overshoots_a_little_and_settles_on_target(make_sim):
    sim = make_sim(cook_set_f=225.0, sample_every=300, controller=get_controller("pi"))
    sim.run(4 * HOUR)
    assert abs(sim.rows[-1]["pit_f"] - 225.0) < 0.5
    assert 225.0 < max(r["pit_f"] for r in sim.rows) < 232.0


# -- events -------------------------------------------------------------------

def test_events_fire_in_time_order(make_sim):
    seen: list[str] = []
    events = [
        Event(30.0, lambda sim: seen.append("c"), "c"),
        Event(10.0, lambda sim: seen.append("a"), "a"),
        Event(20.0, lambda sim: seen.append("b"), "b"),
    ]
    make_sim(events=events).run(100)
    assert seen == ["a", "b", "c"]


def test_a_setpoint_change_steers_the_pit(make_sim):
    def raise_setpoint(sim: Simulator) -> None:
        sim.device.set_cook_set_tenths(2750)

    sim = make_sim(
        preheat=215.0, cook_set_f=225.0, sample_every=60,
        events=[Event(60.0, raise_setpoint, "set 275")],
    )
    sim.run(30 * 60)
    assert sim.rows[0]["cook_set"] == 225.0
    assert sim.rows[1]["cook_set"] == 275.0 and sim.rows[1]["output_pct"] == 100
    assert sim.rows[-1]["pit_f"] > 240.0


def test_a_food_probe_can_be_unplugged_and_plugged_back_in(make_sim):
    def unplug(sim: Simulator) -> None:
        sim.probe("food1").plugged = False

    def replug(sim: Simulator) -> None:
        sim.probe("food1").plugged = True

    sim = make_sim(
        food=Food(preset("ribs"), temp_f=40.0), preheat=225.0, sample_every=1,
        events=[Event(600.0, unplug, "unplug"), Event(1200.0, replug, "replug")],
    )
    sim.run(1500)
    by_t = {r["t"]: r for r in sim.rows}
    assert by_t[600.0]["food1"] is not None
    for t in range(601, 1201):
        assert by_t[float(t)]["food1"] is None
        assert by_t[float(t)]["food1_status"] == STATUS.ERROR
    first_back = by_t[1201.0]["food1"]
    assert first_back is not None and 68.0 <= first_back <= 72.0  # it lay in the air
    assert by_t[1300.0]["food1"] > first_back + 5.0              # and warms back in


def test_an_unplugged_cook_probe_stops_the_fan(make_sim):
    def unplug(sim: Simulator) -> None:
        sim.probe("cook").plugged = False

    sim = make_sim(preheat=150.0, cook_set_f=225.0, sample_every=1, events=[Event(60.0, unplug)])
    sim.run(200)
    gone = [r for r in sim.rows if r["t"] > 61.0]
    assert gone and all(
        r["cook"] is None and r["cook_status"] == STATUS.ERROR and r["output_pct"] == 0
        and r["fan_duty"] == 0.0
        for r in gone
    )


def test_run_stops_when_told_to(make_sim):
    sim = make_sim()
    sim.run(HOUR, until=lambda r: r["t"] >= 10.0)
    assert sim.steps == 10


# -- construction -------------------------------------------------------------

def test_it_needs_one_probe_per_channel_and_a_positive_step(make_sim):
    with pytest.raises(ValueError):
        make_sim(dt=0)
    with pytest.raises(ValueError):
        make_sim(sample_every=0)
    sim = make_sim()
    with pytest.raises(ValueError):
        Simulator(sim.cooker, sim.probes[:3], sim.device)
