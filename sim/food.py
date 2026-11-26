"""Food model (docs/SIMULATOR.md): one lumped mass per probe, with the stall.

    C_food dT/dt = h_food (T_pit − T) − Q_evap(T)

* The lump is the whole cut: ``C_food = mass_lb · c_food``. It is heated by
  the pit through ``h_food`` and nothing else happens to it above ambient —
  a cold brisket warms toward the pit temperature with a time constant of
  ``C_food / h_food`` (hours, for a roast).
* The stall (150–170 °F, hours long) is evaporative cooling. The meat's
  surface is wet, and how fast water leaves it rises steeply with
  temperature — steeply enough to act like a thermostat:

      Q_evap(T) = stall_rate · exp((T − stall_temp) / evap_tscale) · avail

  ``stall_rate`` is fixed by the reference point: a fully wet piece in a
  ``stall_pit_ref`` pit settles at ``stall_temp``, whatever ``h_food`` is
  (``stall_rate = h_food · (stall_pit_ref − stall_temp)``).
* ``avail`` is the moisture left. Evaporation draws the moisture down in
  proportion to the heat it takes (``moisture_btu`` is the whole reserve), and
  stays at full rate until only ``moisture_tail`` of it remains. So the
  temperature sits at the stall while the reserve drains, then leaves it — the
  stall lasts ``moisture_btu / stall_rate`` hours, give or take the tail.

Units: °F, seconds, BTU; ``h_food`` and ``stall_rate`` are BTU/h.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

BTU_PER_H = 1.0 / 3600.0  # BTU/h -> BTU/s


@dataclass(frozen=True)
class FoodParams:
    mass_lb: float = 12.0
    c_food: float = 1.0            # BTU/(lb·°F): meat is mostly water
    h_food: float = 4.0            # BTU/(h·°F), pit -> food
    stall_temp: float = 150.0      # °F: where a wet piece sits in a reference pit
    stall_pit_ref: float = 225.0   # °F: that reference pit
    evap_tscale: float = 6.0       # °F: e-folding scale of the evaporation demand
    moisture_btu: float = 1400.0   # energy that can leave as evaporated moisture
    moisture_tail: float = 0.25    # fraction of the reserve where drying takes over

    def __post_init__(self) -> None:
        if self.mass_lb < 0 or self.c_food <= 0 or self.h_food < 0:
            raise ValueError("food mass/heat capacity/coupling must be non-negative")
        if self.evap_tscale <= 0:
            raise ValueError("evap_tscale must be positive")
        if self.moisture_btu < 0:
            raise ValueError("moisture_btu must be non-negative")
        if not 0 < self.moisture_tail <= 1:
            raise ValueError("moisture_tail must be in (0, 1]")


#: Cuts a scenario can name. The roasts all use the 4 BTU/(h·°F) coupling of a
#: football-sized piece; mass sets the warm-up time, the moisture reserve the
#: stall's length. ``chicken``/``no-stall`` never stall (a chicken dries out
#: before it matters). ``air`` is no food at all: a probe plugged in with
#: nothing on it, reading the pit.
PRESETS: dict[str, FoodParams] = {
    "brisket": FoodParams(mass_lb=12.0, moisture_btu=1400.0),
    "pork-butt": FoodParams(mass_lb=8.0, moisture_btu=1000.0),
    "ribs": FoodParams(mass_lb=4.5, moisture_btu=420.0),
    "chicken": FoodParams(mass_lb=4.0, moisture_btu=0.0),
    "no-stall": FoodParams(mass_lb=4.0, moisture_btu=0.0),
    "air": FoodParams(mass_lb=0.0, moisture_btu=0.0),
}


def preset(name: str, **overrides: float) -> FoodParams:
    """A named cut (see :data:`PRESETS`), with any parameter overridden."""
    try:
        params = PRESETS[name]
    except KeyError as exc:
        raise ValueError(f"unknown food preset {name!r}") from exc
    return replace(params, **overrides)


class Food:
    """One piece of food on one probe."""

    def __init__(self, params: FoodParams | None = None, *, temp_f: float) -> None:
        self.params = params if params is not None else FoodParams()
        self.temp = float(temp_f)
        self.moisture = 1.0        # fraction of the reserve left
        self.q_evap = 0.0          # evaporative cooling over the last step, BTU/s

    @property
    def h(self) -> float:
        """Coupling to the pit, BTU/(s·°F)."""
        return self.params.h_food * BTU_PER_H

    @property
    def capacity(self) -> float:
        """Heat capacity, BTU/°F."""
        return self.params.mass_lb * self.params.c_food

    @property
    def time_constant_s(self) -> float:
        """How fast the lump follows the pit with no evaporation."""
        return self.capacity / self.h if self.h > 0 else math.inf

    @property
    def stall_rate(self) -> float:
        """Evaporative demand at ``stall_temp`` with full moisture, BTU/h."""
        p = self.params
        return p.h_food * (p.stall_pit_ref - p.stall_temp)

    def demand(self, temp_f: float) -> float:
        """Evaporative cooling at ``temp_f`` with the moisture left, BTU/h."""
        p = self.params
        if p.moisture_btu <= 0 or self.moisture <= 0:
            return 0.0
        exponent = min((temp_f - p.stall_temp) / p.evap_tscale, 30.0)
        avail = min(1.0, self.moisture / p.moisture_tail)
        return self.stall_rate * math.exp(exponent) * avail

    def step(self, dt: float, t_pit: float) -> None:
        """Advance the food by ``dt`` seconds in a pit held at ``t_pit``.

        Each sub-step holds the evaporative demand at its start-of-sub-step
        value and solves the lump's relaxation towards its resulting
        equilibrium exactly, so a step is accurate to the demand's change
        within a sub-step. The demand rises steeply with temperature, which
        makes the evaporation feedback stiff near the stall (its time
        constant is minutes against hours for the heating); the step is
        subdivided to keep that explicit part stable for any ``dt``.
        """
        if dt <= 0:
            return
        capacity = self.capacity
        if capacity <= 0:  # a probe measuring the pit air: no lump to heat
            self.temp = t_pit
            self.q_evap = 0.0
            return
        p = self.params
        h = self.h
        evap = self.demand(self.temp) * BTU_PER_H
        slope = h + (evap / p.evap_tscale if evap > 0 else 0.0)
        tau = capacity / slope if slope > 0 else math.inf
        # ≥ 20 sub-steps per time constant, bounded so a huge dt cannot explode
        sub = min(max(1, math.ceil(dt / (tau / 20.0))), 100_000)
        sub_dt = dt / sub
        relax = math.exp(-h * sub_dt / capacity)
        q_evap = 0.0
        for _ in range(sub):
            q_evap = self.demand(self.temp) * BTU_PER_H
            if q_evap > 0:
                self.moisture = max(0.0, self.moisture - q_evap * sub_dt / p.moisture_btu)
            if h > 0:
                equilibrium = t_pit - q_evap / h
                self.temp = equilibrium + (self.temp - equilibrium) * relax
            else:
                self.temp -= q_evap * sub_dt / capacity
        self.q_evap = q_evap
