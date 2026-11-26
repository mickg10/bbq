"""Food model: lumped warm-up and the evaporative stall."""

from __future__ import annotations

import math

import pytest

from sim.food import Food, FoodParams, preset

HOUR = 3600.0


def _run(food: Food, pit_f: float, seconds: float, dt: float = 30.0) -> Food:
    for _ in range(int(round(seconds / dt))):
        food.step(dt, pit_f)
    return food


def _hours_to(food: Food, pit_f: float, target_f: float, limit_h: float = 24.0) -> float:
    """Simulated hours until the food reaches ``target_f`` (else ``limit_h``)."""
    dt = 60.0
    for step in range(1, int(limit_h * HOUR / dt) + 1):
        food.step(dt, pit_f)
        if food.temp >= target_f:
            return step * dt / HOUR
    return limit_h


def test_without_a_stall_the_lump_warms_toward_the_pit_exponentially():
    food = Food(FoodParams(moisture_btu=0.0), temp_f=40.0)
    # 12 lb × 1 BTU/(lb·°F) through 4 BTU/(h·°F): three hours
    assert food.time_constant_s == pytest.approx(3 * HOUR)
    _run(food, 140.0, food.time_constant_s)
    assert food.temp == pytest.approx(40.0 + 100.0 * (1 - math.exp(-1.0)), rel=1e-6)
    assert food.demand(200.0) == 0.0


def test_the_lump_never_passes_the_pit_and_never_cools_below_it_warms():
    food = Food(FoodParams(moisture_btu=0.0), temp_f=40.0)
    previous = food.temp
    for _ in range(200):
        food.step(60.0, 140.0)
        assert previous <= food.temp < 140.0
        previous = food.temp


def test_food_cools_in_a_cooler_pit():
    food = Food(FoodParams(moisture_btu=0.0), temp_f=180.0)
    _run(food, 140.0, HOUR)
    assert 160.0 < food.temp < 180.0


def test_brisket_stalls_in_the_150s_for_hours_then_pushes_through():
    """A 12 lb brisket in a 225 °F pit: flat around 150 °F from about 3 h to
    about 7 h, 203 °F (done) around 11 h."""
    food = Food(preset("brisket"), temp_f=40.0)
    samples = {}
    for i in range(16 * 60):            # 16 h at one-minute steps
        food.step(60.0, 225.0)
        samples[(i + 1) * 60] = food.temp
    assert 100.0 < samples[2 * int(HOUR)] < 140.0            # warming up
    for hour in (3, 4, 5, 6, 7):
        assert 145.0 <= samples[hour * int(HOUR)] <= 160.0, hour
    assert samples[7 * int(HOUR)] - samples[3 * int(HOUR)] < 6.0  # flat
    assert 160.0 < samples[9 * int(HOUR)] < 190.0            # climbing out
    done = _hours_to(Food(preset("brisket"), temp_f=40.0), 225.0, 203.0)
    assert 10.0 < done < 12.5


def test_a_hotter_pit_finishes_the_food_sooner():
    at_225 = _hours_to(Food(preset("brisket"), temp_f=40.0), 225.0, 203.0)
    at_250 = _hours_to(Food(preset("brisket"), temp_f=40.0), 250.0, 203.0)
    assert at_250 < at_225 - 2.0


def test_the_stall_ends_because_the_moisture_runs_out():
    food = Food(preset("brisket"), temp_f=40.0)
    assert food.moisture == 1.0
    _run(food, 225.0, 6 * HOUR)
    assert 0.05 < food.moisture < 0.4          # still drying in the stall
    _run(food, 225.0, 8 * HOUR)
    assert food.moisture == 0.0
    assert food.demand(food.temp) == 0.0
    assert food.temp > 203.0


def test_moisture_leaves_with_exactly_the_heat_it_takes():
    food = Food(preset("brisket"), temp_f=40.0)
    evaporated = 0.0
    for _ in range(int(10 * HOUR / 30.0)):
        food.step(30.0, 225.0)
        evaporated += food.q_evap * 30.0        # q_evap is the last sub-step's
    assert evaporated == pytest.approx(
        preset("brisket").moisture_btu * (1.0 - food.moisture), rel=0.01,
    )


def test_a_longer_moisture_reserve_makes_a_longer_stall():
    def hours(moisture_btu: float) -> float:
        return _hours_to(
            Food(preset("brisket", moisture_btu=moisture_btu), temp_f=40.0), 225.0, 203.0
        )

    short, normal, long = hours(700.0), hours(1400.0), hours(2800.0)
    assert short < normal < long
    assert long - short > 3.0


def test_presets_that_never_stall():
    for name in ("chicken", "no-stall"):
        food = Food(preset(name), temp_f=40.0)
        previous = food.temp
        for _ in range(6 * 60):
            food.step(60.0, 225.0)
            assert food.temp > previous, name
            assert food.demand(food.temp) == 0.0
            previous = food.temp


def test_a_probe_in_air_follows_the_pit_immediately():
    food = Food(FoodParams(mass_lb=0.0), temp_f=40.0)
    food.step(1.0, 225.0)
    assert food.temp == 225.0


def test_bad_parameters_are_rejected():
    with pytest.raises(ValueError):
        FoodParams(mass_lb=-1)
    with pytest.raises(ValueError):
        FoodParams(evap_tscale=0)
    with pytest.raises(ValueError):
        FoodParams(moisture_tail=0)
    with pytest.raises(ValueError):
        FoodParams(moisture_tail=1.5)


def test_preset_lookups():
    assert preset("ribs").mass_lb == pytest.approx(4.5)
    assert preset("ribs", mass_lb=6.0).mass_lb == pytest.approx(6.0)
    with pytest.raises(ValueError):
        preset("turban")
