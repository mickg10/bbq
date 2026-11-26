"""Cooker thermal model (docs/SIMULATOR.md): pit, fuel, airflow, ambient, lid.

    C_pit dT/dt = Q_fuel − UA (T − T_amb) − Q_food − Q_lid

* **Pit**: one thermal mass — ceramic or steel plus grates — at ``pit_f``.
* **Fuel/airflow**: a charcoal bed holding ``fuel_btu``. The heat it gives is

      Q_fuel = q_max · power · (draft_min + (1 − draft_min) · fire)

  where ``fire`` is the coals' response to the fan: a first-order lag of the
  fan duty with time constant ``tau_fire`` (coals take minutes to react), and
  ``power`` is what the load can still give. A bed keeps full power until only
  ``fuel_tail`` of it is left and then falls off linearly to nothing; with
  ``fuel_tail = 1`` the power is simply the fraction of fuel left. The load
  burns down in proportion to the heat released. ``draft_min`` is the draft
  that leaks past the fan with it off.
* **Lid open**: the shell's conductance is multiplied by ``lid_ua_factor``
  for the event (a fast pit drop), and the fire sees ``lid_fire_boost`` of
  air whatever the fan does. It carries that for ``tau_fire`` after the lid
  closes — the overshoot open-lid detect exists to damp.
* **Ambient/wind**: losses are ``UA · (1 + wind_coef · mph) · (T − T_amb)``.
* **Food**: each :class:`sim.food.Food` draws ``h (T_pit − T_food)``.

Units are °F, seconds and BTU, with rates per hour where that is the natural
unit (``ua_pit``, ``q_max``). The step integrates the linear part exactly for
a constant heat input, so it is stable at any ``dt``; the food temperatures
are frozen at the step's start for the pit's balance, then advanced.

The defaults are a large ceramic kamado with a good load of lump — plausible,
not fitted. :mod:`sim.fit` refines them against recorded history.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from sim.food import Food

BTU_PER_H = 1.0 / 3600.0  # BTU/h -> BTU/s

#: Energy in a pound of lump charcoal, for scenarios that size a load in pounds.
BTU_PER_LB_LUMP = 13_000.0


@dataclass(frozen=True)
class PlantParams:
    c_pit: float = 80.0          # BTU/°F: thermal mass of the pit
    ua_pit: float = 70.0         # BTU/(h·°F): shell losses in still air
    wind_coef: float = 0.03      # loss multiplier per mph of wind
    q_max: float = 30_000.0      # BTU/h: full fire, full draft, full load
    fuel_tail: float = 0.25      # power falls off only below this fraction of the load
    draft_min: float = 0.04      # fraction of q_max that leaks past a stopped fan
    tau_fire: float = 420.0      # s: the coals' response to airflow
    lid_ua_factor: float = 6.0   # UA multiplier while the lid is open
    lid_fire_boost: float = 0.6  # airflow the coals see while the lid is open

    def __post_init__(self) -> None:
        if self.c_pit <= 0 or self.ua_pit <= 0 or self.q_max <= 0:
            raise ValueError("c_pit, ua_pit and q_max must be positive")
        if self.tau_fire <= 0:
            raise ValueError("tau_fire must be positive")
        if not 0 <= self.draft_min <= 1:
            raise ValueError("draft_min must be in [0, 1]")
        if not 0 < self.fuel_tail <= 1:
            raise ValueError("fuel_tail must be in (0, 1]")
        if self.wind_coef < 0 or self.lid_ua_factor < 1 or self.lid_fire_boost < 0:
            raise ValueError("wind_coef >= 0, lid_ua_factor >= 1, lid_fire_boost >= 0")


class Plant:
    """The pit without its food: shell, fire and fuel."""

    def __init__(
        self,
        params: PlantParams | None = None,
        *,
        t_amb: float,
        fuel_btu: float = 200_000.0,
        pit_f: float | None = None,
        fire: float = 0.0,
    ) -> None:
        """``fire`` is the coals' initial state (0 = waiting for the fan)."""
        if fuel_btu < 0:
            raise ValueError("fuel_btu must be non-negative")
        self.params = params if params is not None else PlantParams()
        self.pit_f = float(t_amb if pit_f is None else pit_f)
        self.fire = float(fire)
        self.fuel_btu = float(fuel_btu)
        self.fuel0_btu = float(fuel_btu)
        self.q_fuel = 0.0   # heat released over the last step, BTU/s
        self.q_loss = 0.0   # shell losses, lid included, BTU/s
        self.q_food = 0.0   # heat into the food, BTU/s

    @property
    def fuel_frac(self) -> float:
        """Fraction of the original load left (0 if there was none)."""
        return self.fuel_btu / self.fuel0_btu if self.fuel0_btu > 0 else 0.0

    @property
    def power(self) -> float:
        """What fraction of ``q_max`` the bed can still give at full draft."""
        return min(1.0, self.fuel_frac / self.params.fuel_tail)

    def airflow(self, fan_duty: float, lid_open: bool = False) -> float:
        """The air the coals see: the fan, or the open lid if that is more."""
        return max(fan_duty, self.params.lid_fire_boost) if lid_open else fan_duty

    def heat(self, fire: float | None = None) -> float:
        """Heat the fire releases per second at coal state ``fire``, BTU/s."""
        p = self.params
        level = self.fire if fire is None else fire
        draft = p.draft_min + (1.0 - p.draft_min) * level
        return p.q_max * self.power * draft * BTU_PER_H

    def conductance(self, wind_mph: float = 0.0, lid_open: bool = False) -> float:
        """Shell conductance UA, BTU/(s·°F), as wind and the lid leave it."""
        p = self.params
        ua = p.ua_pit * (1.0 + p.wind_coef * max(wind_mph, 0.0))
        return ua * (p.lid_ua_factor if lid_open else 1.0) * BTU_PER_H

    def steady_fire(
        self, pit_f: float, t_amb: float, *, q_food_btus: float = 0.0,
        wind_mph: float = 0.0,
    ) -> float:
        """Coal state that holds the pit at ``pit_f``: 0..1, or above 1 if
        the fire cannot reach it (not enough load left). 0 with no fuel."""
        capacity = self.params.q_max * self.power * BTU_PER_H
        if capacity <= 0 or self.params.draft_min >= 1:
            return 0.0  # nothing to burn, or a fan that makes no difference
        need = self.conductance(wind_mph) * (pit_f - t_amb) + q_food_btus
        draft = need / capacity
        return (draft - self.params.draft_min) / (1.0 - self.params.draft_min)


class Cooker:
    """The physical cooker: a :class:`Plant` plus the food sitting in it,
    stepped together (the food is part of the pit's heat balance)."""

    def __init__(self, plant: Plant, foods: Sequence[Food | None] = ()) -> None:
        self.plant = plant
        self.foods: tuple[Food | None, ...] = tuple(foods)

    @property
    def pit_f(self) -> float:
        return self.plant.pit_f

    def step(
        self,
        dt: float,
        *,
        fan_duty: float,
        t_amb: float,
        wind_mph: float = 0.0,
        lid_open: bool = False,
    ) -> None:
        """Hold the fan at ``fan_duty`` (0..1) for ``dt`` seconds."""
        if dt <= 0:
            return
        plant, p = self.plant, self.plant.params

        # 1. the coals answer the air they are given
        air = plant.airflow(fan_duty, lid_open)
        fire0 = plant.fire
        decay = math.exp(-dt / p.tau_fire)
        plant.fire = min(max(air - (air - fire0) * decay, 0.0), 1.0)
        # what the pit felt is the coals' time-average over the step, not
        # their end value: that overstates the heat while the fire climbs
        # (and understates it while it dies), by more the longer the step
        mean_fire = air - (air - fire0) * (p.tau_fire / dt) * (1.0 - decay)

        # 2. heat released, capped by what is left to burn
        q_fuel = min(plant.heat(mean_fire), plant.fuel_btu / dt)
        plant.q_fuel = q_fuel

        # 3. the pit: exact solution for a constant input through a total
        #    conductance of shell + food, with the food held at its current temperature
        ua_shell = plant.conductance(wind_mph, lid_open)
        h_food = sum(food.h for food in self.foods if food is not None)
        ua = ua_shell + h_food
        if ua > 0:
            rest = (ua_shell * t_amb + sum(
                food.h * food.temp for food in self.foods if food is not None
            )) / ua
            steady = rest + q_fuel / ua
            plant.pit_f = steady + (plant.pit_f - steady) * math.exp(-dt * ua / p.c_pit)
        else:  # neither losses nor food: all the heat goes into the mass
            plant.pit_f += q_fuel * dt / p.c_pit
        plant.q_loss = ua_shell * (plant.pit_f - t_amb)

        # 4. the food follows the new pit temperature
        q_food = 0.0
        for food in self.foods:
            if food is None:
                continue
            food.step(dt, plant.pit_f)
            q_food += food.h * (plant.pit_f - food.temp)
        plant.q_food = q_food

        # 5. the load burns down with the heat it gave
        plant.fuel_btu = max(0.0, plant.fuel_btu - q_fuel * dt)
