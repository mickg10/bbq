"""Cooker model: pit, fire, fuel, lid, wind and the heat balance between them."""

from __future__ import annotations

import math

import pytest

from sim.food import Food, preset
from sim.plant import Cooker, Plant, PlantParams

HOUR = 3600.0


def _preheated(pit_f: float = 225.0, t_amb: float = 70.0, **params) -> Cooker:
    """A pit already at ``pit_f`` with its fire at the level that holds it."""
    plant = Plant(PlantParams(**params) if params else None, t_amb=t_amb)
    plant.pit_f = pit_f
    plant.fire = plant.steady_fire(pit_f, t_amb)
    return Cooker(plant)


def test_pit_settles_where_heat_in_equals_heat_out():
    plant = Plant(PlantParams(tau_fire=1e-3, draft_min=0.0), t_amb=70.0, fuel_btu=1e9)
    cooker = Cooker(plant)
    for _ in range(400):  # 400 h; the pit's own time constant is about 1.1 h
        cooker.step(HOUR, fan_duty=0.5, t_amb=70.0)
    # half a fire is 15,000 BTU/h into 70 BTU/(h·°F) of shell
    assert plant.pit_f == pytest.approx(70.0 + 15_000.0 / 70.0, rel=1e-6)
    assert plant.heat() * HOUR == pytest.approx(15_000.0, rel=1e-6)


def test_the_fire_lags_the_fan_by_tau_fire():
    plant = Plant(PlantParams(tau_fire=100.0), t_amb=70.0)
    Cooker(plant).step(100.0, fan_duty=1.0, t_amb=70.0)
    assert plant.fire == pytest.approx(1.0 - math.exp(-1.0))


def test_fuel_burns_down_by_the_heat_it_released():
    plant = Plant(t_amb=70.0)
    plant.fire = 0.5
    Cooker(plant).step(60.0, fan_duty=0.5, t_amb=70.0)
    assert plant.fuel0_btu - plant.fuel_btu == pytest.approx(plant.q_fuel * 60.0)
    assert plant.q_fuel * HOUR == pytest.approx(30_000.0 * (0.04 + 0.96 * 0.5), rel=1e-3)


def test_the_bed_holds_full_power_until_the_tail_then_falls_off():
    plant = Plant(PlantParams(fuel_tail=0.25), t_amb=70.0, fuel_btu=100.0)
    assert plant.power == 1.0
    plant.fuel_btu = 30.0
    assert plant.power == 1.0
    plant.fuel_btu = 20.0
    assert plant.power == pytest.approx(0.8)
    plant.fuel_btu = 0.0
    assert plant.power == 0.0
    assert plant.heat() == 0.0


def test_a_stopped_fan_still_leaks_draft():
    sealed = Plant(PlantParams(draft_min=0.0), t_amb=70.0)
    sealed.fire = 0.0
    assert sealed.heat() == 0.0
    leaky = Plant(t_amb=70.0)
    leaky.fire = 0.0
    assert leaky.heat() * HOUR == pytest.approx(30_000.0 * 0.04)


def test_a_steady_fire_holds_the_pit_where_it_is():
    cooker = _preheated(225.0)
    duty = cooker.plant.fire
    for _ in range(60):
        cooker.step(60.0, fan_duty=duty, t_amb=70.0)
    assert cooker.pit_f == pytest.approx(225.0, abs=0.05)


def test_lid_open_drops_the_pit_and_stokes_the_fire():
    closed, open_lid = _preheated(), _preheated()
    duty = closed.plant.fire
    for _ in range(3):  # three minutes
        closed.step(60.0, fan_duty=duty, t_amb=70.0)
        open_lid.step(60.0, fan_duty=duty, t_amb=70.0, lid_open=True)
    assert closed.pit_f == pytest.approx(225.0, abs=0.05)
    assert open_lid.pit_f < closed.pit_f - 20.0
    assert open_lid.plant.fire > closed.plant.fire + 0.05


def test_wind_and_cold_draw_more_from_the_fire():
    plant = Plant(t_amb=70.0)
    calm = plant.steady_fire(250.0, 70.0)
    assert 0 < calm < 1
    assert plant.steady_fire(250.0, 70.0, wind_mph=15) > calm
    assert plant.steady_fire(250.0, 30.0) > calm
    assert plant.conductance(lid_open=True) == pytest.approx(
        plant.conductance() * plant.params.lid_ua_factor
    )


def test_food_draws_heat_from_the_pit():
    empty, loaded = _preheated(), _preheated()
    loaded.foods = (Food(preset("brisket"), temp_f=40.0),)
    for cooker in (empty, loaded):
        cooker.step(60.0, fan_duty=empty.plant.fire, t_amb=70.0)
    assert loaded.plant.q_food > 0
    assert 0.05 < empty.pit_f - loaded.pit_f < 0.5


def test_the_heat_balance_books_every_term():
    plant = Plant(t_amb=70.0)
    plant.pit_f, plant.fire = 180.0, 0.5
    cooker = Cooker(plant, [Food(preset("brisket"), temp_f=100.0)])
    before = plant.pit_f
    cooker.step(1.0, fan_duty=0.5, t_amb=70.0)
    stored = plant.params.c_pit * (plant.pit_f - before)  # BTU put into the mass
    assert stored == pytest.approx(plant.q_fuel - plant.q_loss - plant.q_food, rel=1e-3)


def test_a_large_step_is_stable_and_lands_where_small_ones_do():
    def settled(dt: float) -> float:
        cooker = Cooker(
            Plant(PlantParams(tau_fire=60.0, draft_min=0.0), t_amb=70.0, fuel_btu=1e9)
        )
        seconds = 0.0
        while seconds < 30 * HOUR:
            cooker.step(dt, fan_duty=0.4, t_amb=70.0)
            seconds += dt
        return cooker.pit_f

    assert settled(1.0) == pytest.approx(settled(3600.0), rel=1e-6)
    assert settled(3600.0) == pytest.approx(70.0 + 12_000.0 / 70.0, rel=1e-3)


def test_no_fuel_means_no_fire():
    plant = Plant(t_amb=70.0, fuel_btu=0.0, pit_f=225.0)
    cooker = Cooker(plant)
    for _ in range(30):
        cooker.step(HOUR, fan_duty=1.0, t_amb=70.0)
    assert plant.power == 0.0 and plant.heat() == 0.0
    assert cooker.pit_f == pytest.approx(70.0, abs=1e-6)


def test_the_last_of_the_fuel_cannot_give_more_heat_than_it_holds():
    plant = Plant(t_amb=70.0, fuel_btu=5.0, pit_f=225.0)
    plant.fire = 1.0
    Cooker(plant).step(60.0, fan_duty=1.0, t_amb=70.0)
    assert plant.fuel_btu == 0.0
    assert plant.q_fuel * 60.0 == pytest.approx(5.0)


def test_bad_parameters_are_rejected():
    with pytest.raises(ValueError):
        PlantParams(c_pit=0)
    with pytest.raises(ValueError):
        PlantParams(draft_min=2)
    with pytest.raises(ValueError):
        PlantParams(fuel_tail=0)
    with pytest.raises(ValueError):
        Plant(t_amb=70.0, fuel_btu=-1)
