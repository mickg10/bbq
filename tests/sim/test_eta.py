"""When the food will be done (`sim/eta.py`, and `GET /api/eta` in the API tests).

The quick tests use windows made by the food model itself, so the answers are
known. The slow ones run the brisket and ribs scenarios, store their histories
as the dashboard would, and read the estimate back at known distances from the
finish.
"""

from __future__ import annotations

import contextlib
import math
import random
from typing import Callable

import pytest

from app import db as appdb
from app import eta as app_eta
from sim import eta
from sim.food import Food, FoodParams
from sim.run import run_scenario
from sim.scenario import load_scenario, scenario_path

from .conftest import record

PIT = 225.0
SET = 203.0
#: the piece of the stall tests: its timeline is the model's own (a 2.5 h piece
#: from 100 °F stalls at about 1.8 h, climbs again from 4.5 h, is done at 8 h)
STALLED = dict(tau_h=2.5, start=100.0, evaporating=True)


def relax_minutes(temp_f: float, tau_h: float = 3.0) -> float:
    """How long a lump with nothing evaporating takes from ``temp_f`` to the setpoint."""
    return tau_h * 60.0 * math.log((PIT - temp_f) / (PIT - SET))


def cooked(tau_h: float, start: float, minutes: int | None = None, *, every: int = 1, evaporating: bool = False,
           pit: float = PIT, noise: float = 0.0, seed: int = 0, until: Callable[[Food], bool] | None = None
           ) -> tuple[eta.Window, Food]:
    """A window made by the food model itself, and the model at its end: for
    ``minutes``, or until ``until(food)`` holds. Readings every ``every`` minutes."""
    food = Food(FoodParams(mass_lb=1.0, c_food=1.0, h_food=1.0 / tau_h,
                           moisture_btu=eta.reserve() if evaporating else 0.0), temp_f=start)
    food.moisture = 1.0 if evaporating else 0.0
    rng = random.Random(seed)
    times, temps = [0.0], [start]
    minute = 0
    while until is not None or minute < (minutes or 0):
        minute += 1
        food.step(60.0, pit)
        if minute % every == 0:
            times.append(minute * 60.0)
            temps.append(food.temp + (rng.gauss(0.0, noise) if noise else 0.0))
        if until is not None and until(food):
            break
    return eta.Window(tuple(times), tuple(temps), tuple([pit] * len(times))), food


def tail(window: eta.Window, minutes: float) -> eta.Window:
    """The last ``minutes`` of a window."""
    kept = [k for k, t in enumerate(window.times) if t >= window.times[-1] - minutes * 60.0]
    first = window.times[kept[0]]
    return eta.Window(tuple(window.times[k] - first for k in kept), tuple(window.food[k] for k in kept),
                      tuple(window.pit[k] for k in kept))


def minutes_to(food: Food, set_f: float, pit: float = PIT) -> float:
    """How long the model itself takes to reach ``set_f``, the pit held."""
    minutes = 0
    while food.temp < set_f:
        food.step(60.0, pit)
        minutes += 1
    return minutes


# -- where the food is -----------------------------------------------------------------

def test_past_the_stall_the_time_constant_is_read_and_the_finish_placed():
    window, now = cooked(3.0, 190.0, 45)
    est = eta.estimate(window, SET)
    assert (est.state, est.stall_h) == ("eta", 0.0)
    assert est.tau_h == pytest.approx(3.0, rel=0.02)
    assert est.minutes == pytest.approx(relax_minutes(now.temp), abs=1.0)
    assert eta.MIN_BAND_MIN <= est.band_min < 45.0


def test_before_the_stall_the_time_constant_is_read_and_a_stall_allowed_for():
    window, _ = cooked(2.0, 100.0, 45, evaporating=True)
    assert window.food[-1] < eta.PLATEAU_MAX_F
    est = eta.estimate(window, SET)
    assert est.state == "eta"
    assert est.tau_h == pytest.approx(2.0, rel=0.1)
    assert est.stall_h == pytest.approx((1.0 - 0.25) * eta.STALL_PER_TAU * est.tau_h)   # the flat three quarters of it
    assert est.minutes > 3.0 * 60.0         # the stall alone is 1.47 × 2 h
    assert est.band_min > 2.0 * 60.0        # and how long it lasts is not known yet


def test_a_flat_piece_in_the_zone_is_stalled_and_says_so():
    window, _ = cooked(2.5, 150.0, 45, evaporating=True)
    assert max(window.food) - min(window.food) < 0.5
    est = eta.estimate(window, SET)
    assert est.state == "stalled"
    assert (est.tau_h, est.stall_h) == (eta.STALL_TAU_H, eta.STALL_PRIOR_H)
    assert est.minutes > 2.0 * 60.0
    assert est.band_min >= (eta.STALL_MAX_H - eta.STALL_PRIOR_H) * 60.0


def test_with_the_rise_before_the_stall_on_record_its_time_constant_is_used():
    """Three hours in: the model stalled it at 1.8 h and has 2.6 h of flat stall to go."""
    history, _ = cooked(minutes=180, every=5, **STALLED)
    window, now = cooked(minutes=180, **STALLED)
    window = tail(window, 45)
    truth = minutes_to(now, SET)
    assert eta.STALL_ZONE_F[0] <= window.food[-1] < eta.PLATEAU_MAX_F

    est = eta.estimate(window, SET, history=history)
    assert est.state == "stalled"
    assert est.stalled_for_h == pytest.approx(1.2, abs=0.3)
    assert est.tau_h == pytest.approx(2.5, rel=0.15)
    assert abs(est.minutes - truth) < 60.0
    assert abs(est.minutes - truth) <= est.band_min
    # with nothing but the window, the same piece gets the assumed time constant
    assert eta.estimate(window, SET).tau_h == eta.STALL_TAU_H


def test_leaving_the_stall_the_model_carries_on_from_where_the_stall_began():
    """At 160 °F and climbing, 5.3 h in."""
    climbing = lambda food: food.temp >= 160.0  # noqa: E731
    history, _ = cooked(every=5, until=climbing, **STALLED)
    window, now = cooked(until=climbing, **STALLED)
    window = tail(window, 45)
    truth = minutes_to(now, SET)
    assert eta.PLATEAU_MAX_F <= window.food[-1] < eta.STALL_ZONE_F[1]
    assert 4.0 * 60.0 < history.times[-1] / 60.0 < 6.0 * 60.0

    est = eta.estimate(window, SET, history=history)
    assert est.state == "eta"
    assert est.tau_h == pytest.approx(2.5, rel=0.15)
    assert abs(est.minutes - truth) <= est.band_min
    # and with no history of the stall: the assumed time constant, the band wide
    blind = eta.estimate(window, SET)
    assert (blind.state, blind.tau_h, blind.stalled_for_h) == ("eta", eta.STALL_TAU_H, None)
    assert abs(blind.minutes - truth) <= blind.band_min
    assert blind.band_min > est.band_min


def test_the_stall_is_dated_from_where_the_food_went_flat_in_the_zone():
    history, _ = cooked(minutes=240, every=5, **STALLED)
    onset = eta.stall_onset(history)
    assert onset is not None
    assert eta.STALL_ZONE_F[0] <= history.food[onset] < eta.PLATEAU_MAX_F
    assert history.times[onset] / 3600.0 == pytest.approx(1.8, abs=0.3)
    rising, _ = cooked(3.0, 100.0, 75, every=5)
    assert rising.food[-1] < eta.STALL_ZONE_F[0]
    assert eta.stall_onset(rising) is None          # never levelled off


def test_a_rise_too_short_to_read_says_so_and_the_assumed_time_constant_is_used():
    """A dashboard that started while the food was already easing onto its
    plateau: the rise on record is 20 minutes and a few degrees, which shows
    nothing about a time constant."""
    full, _ = cooked(minutes=240, every=5, **STALLED)
    onset = eta.stall_onset(full)
    short = eta.Window(tuple(t - full.times[onset - 4] for t in full.times[onset - 4:]),
                       full.food[onset - 4:], full.pit[onset - 4:])
    assert short.times[4] - short.times[0] == 20 * 60.0
    assert short.food[4] - short.food[0] < 5.0
    short_onset = eta.stall_onset(short)
    assert short_onset is not None                          # the stall can still be dated
    assert eta.tau_before_stall(short, short_onset) is None  # but the rise before it shows nothing
    assert eta.tau_before_stall(full, onset) == pytest.approx(2.5, rel=0.15)


def test_noisy_readings_still_place_the_finish_inside_the_band():
    for seed in range(6):
        window, now = cooked(3.0, 190.0, 45, noise=0.4, seed=seed)
        est = eta.estimate(window, SET)
        assert est.state == "eta"
        assert est.tau_h == pytest.approx(3.0, rel=0.2)
        assert abs(est.minutes - relax_minutes(now.temp)) <= est.band_min


def test_the_band_adds_up_the_uncertainties_and_is_not_just_its_floor():
    """Past the stall with two hours to go, the finish is sensitive: a fifth
    off the time constant moves it by half an hour, and the band is the four
    movements added in quadrature, not the largest and not the floor."""
    window, _ = cooked(3.0, 167.2, 45)
    assert window.food[-1] == pytest.approx(180.0, abs=0.1)
    est = eta.estimate(window, SET)
    assert est.state == "eta" and est.minutes == pytest.approx(relax_minutes(window.food[-1]), abs=1.0)
    moves = [eta.forward(eta.piece(180.0, est.tau_h * factor, 0.0), PIT, SET) - est.minutes for factor in (0.8, 1.25)]
    moves += [eta.forward(eta.piece(180.0, est.tau_h, 0.0), PIT + delta, SET) - est.minutes for delta in (-2.0, 2.0)]
    assert max(abs(move) for move in moves) > 20.0                      # past the floor on its own
    assert est.band_min == pytest.approx(math.sqrt(sum(move * move for move in moves)), rel=0.05)


def test_how_far_out_of_the_stall_a_piece_is_can_be_read_from_the_window_alone():
    """The model, stopped once its moisture has fallen to a known point in the
    climb out of the stall and run 45 minutes on: that is the window, and the
    moisture at its start is what is read."""
    def climb_from(moisture: float) -> tuple[eta.Window, float]:
        food = Food(FoodParams(mass_lb=1.0, c_food=1.0, h_food=1.0 / STALLED["tau_h"], moisture_btu=eta.reserve()),
                    temp_f=STALLED["start"])
        food.moisture = 1.0
        while food.moisture > moisture:
            food.step(60.0, PIT)
        started, times, temps = food.moisture, [0.0], [food.temp]
        for minute in range(1, 46):
            food.step(60.0, PIT)
            times.append(minute * 60.0)
            temps.append(food.temp)
        return eta.Window(tuple(times), tuple(temps), tuple([PIT] * len(times))), started

    early, started = climb_from(0.2)                  # just into the climb
    late, _ = climb_from(0.1)
    assert eta.estimate(early, SET).state == "eta"     # climbing out of the stall
    read = eta.moisture_left(early, STALLED["tau_h"])
    assert read == pytest.approx(started, abs=0.06)    # to the grid's 0.05
    assert eta.moisture_left(late, STALLED["tau_h"]) < read - 0.04


def test_the_stall_is_judged_on_the_last_half_hour():
    """Flat at first and climbing at the end is a piece leaving its stall,
    although its average rise over the 45 minutes is under the stall rate."""
    times = tuple(float(k) * 60.0 for k in range(46))
    climbing = tuple(150.0 if k <= 15 else 150.0 + 8.0 / 60.0 * (k - 15) for k in range(46))
    assert (climbing[-1] - climbing[0]) / 45.0 * 60.0 < eta.STALL_RATE_F_PER_H
    assert eta.estimate(eta.Window(times, climbing, tuple([PIT] * 46)), SET).state == "eta"
    settling = tuple(150.0 - 8.0 / 60.0 * (15 - k) if k < 15 else 150.0 for k in range(46))
    assert eta.estimate(eta.Window(times, settling, tuple([PIT] * 46)), SET).state == "stalled"


# -- what is assumed ----------------------------------------------------------------------

def test_the_pit_is_assumed_to_stay_where_it_was_in_the_last_quarter_hour():
    steady, _ = cooked(3.0, 190.0, 45)
    heating = eta.Window(steady.times, steady.food, tuple(200.0 if t < 30 * 60.0 else PIT for t in steady.times))
    assert eta.pit_ahead(heating) == PIT
    assert eta.pit_ahead(eta.Window((0.0, 60.0, 120.0), (1.0, 1.0, 1.0), (220.0, None, 230.0))) == 225.0


def test_gaps_in_the_pit_readings_are_closed():
    assert eta.filled([225.0, None, None, 231.0, None]) == [225.0, 227.0, 229.0, 231.0, 231.0]
    assert eta.filled([None, None, 220.0]) == [220.0, 220.0, 220.0]
    assert eta.filled([None, None]) == []


def flat_cold() -> eta.Window:
    return eta.Window(tuple(float(k) * 60.0 for k in range(46)), tuple(100.0 + k / 90.0 for k in range(46)),
                      tuple([102.0] * 46))


@pytest.mark.parametrize("window, reason", [
    (cooked(3.0, 190.0, 10)[0], "under 15 min of data"),
    (cooked(3.0, 205.0, 45)[0], "done"),
    (eta.Window(tuple(float(k) * 60.0 for k in range(46)), tuple(190.0 + k / 10.0 for k in range(46)),
                tuple([None] * 46)), "no pit reading"),
    (cooked(3.0, 190.0, 45, pit=200.0)[0], "the pit is not above the food setpoint"),
    # in a pit 2 °F above it, the food rises 0.7 °F/h: under a third of the stall rate
    (flat_cold(), "not rising yet"),
])
def test_when_there_is_no_estimate_it_says_why(window, reason):
    est = eta.estimate(window, SET)
    assert (est.state, est.minutes, est.band_min) == ("none", None, None)
    assert est.reason == reason


# -- along a `sim run`, stored as the dashboard would store it (slow) ---------------------

#: minutes before the food is done, whether the piece is stalled there, and
#: how far off the estimate may be: only where nothing is left to stall can it
#: be held to a number, elsewhere it is held to its own band
LEADS = ((180, "ribs-6h", None), (60, None, 45), (15, None, 45))


@pytest.mark.slow
@pytest.mark.parametrize("scenario", ["brisket-12h", "ribs-6h"])
def test_the_finish_is_placed_along_a_stored_run(scenario, tmp_path):
    """`sim run`'s own history, a row a minute, written to the dashboard's
    table; the estimate read back 3 h, 1 h and 15 min before the food is done,
    then every half hour from 1.5 h in to the end."""
    result = run_scenario(load_scenario(scenario_path(scenario)), sample_every=6, dt=10)
    record(tmp_path / "history.db", result.sim)
    start = 1_790_000_000
    done = start + round(next(row["t"] for row in result.rows if row["food1_status"] == 3))
    with contextlib.closing(appdb.connect(str(tmp_path / "history.db"))) as conn:
        at = lambda ts: app_eta.eta_report(conn, ts)["probes"]["food1"]  # noqa: E731

        for lead, stalled, tolerance in LEADS:
            est = at(done - lead * 60)
            error = est["minutes"] - lead
            print(f"\n{scenario}, {lead} min out: {est['state']}, error {error:+d} min, band ±{est['band_min']} min")
            assert est["state"] == ("stalled" if scenario == stalled else "eta")
            assert abs(error) <= est["band_min"]
            assert tolerance is None or abs(error) <= tolerance
            assert abs(est["eta_ts"] - done) <= est["band_min"] * 60

        ts = start + round(1.5 * 3600)
        while ts < done:
            est = at(ts)
            assert est["state"] != "none", f"no estimate {(ts - start) / 3600:.1f} h in: {est.get('reason')}"
            error = est["minutes"] - (done - ts) / 60
            assert abs(error) <= est["band_min"], f"{(ts - start) / 3600:.1f} h in: {error:+.0f} min, band ±{est['band_min']}"
            ts += 1800
