"""When the food will be done: an estimate from the food's history (docs/SIMULATOR.md).

The lumped model of :mod:`sim.food` is run forward, the pit held where it is,
until the food reads its setpoint (the device's DONE). What the model needs is
the piece's time constant and how much moisture it has left: the first sets
how fast it warms, the second how long the stall lasts. Where each comes from
depends on where the food is:

* **Before the stall** (below :data:`PLATEAU_MAX_F`) it heads for the plateau
  with all its moisture. The time constant is fitted to the window, and the
  stall ahead is given the length the model's presets have for a piece of
  that time constant.
* **Stalled** (flat, inside :data:`STALL_ZONE_F`) the window shows nothing.
  If the history shows when the stall began, and holds the rise before it,
  the time constant comes from that rise and the model is run from the
  stall's beginning through the pit that was read, carrying on from the
  state it reaches now. Otherwise the estimate rests on a prior
  (:data:`STALL_TAU_H`, :data:`STALL_PRIOR_H`).
* **Leaving the stall** (rising, above the plateau, below the top of the
  zone) the same, or, without such a history, the assumed time constant and
  how far out of the stall the piece is, from the window alone.
* **Past the stall** nothing evaporates any more: the lump relaxes towards
  the pit, and the time constant is the one number fitted to the window.

``state`` is ``stalled`` only for the flat phase; an estimate that depends on
how long a stall lasts is only as good as its band, and those bands are wide.
The band is how far the estimate moves under other readings of the same data
(the time constant a fifth either way, the pit a couple of degrees either way)
and, wherever a stall is not over, under the stall lasting anything from
nothing to :data:`STALL_MAX_H` in all.

Only ratios are fitted. The model is given unit heat capacity, so that its
time constant is ``tau`` and its moisture reserve is a number of
°F·(BTU/°F): the temperature does not depend on the scale (docs/SIMULATOR.md,
"Fitting a recording"). What is estimated is the food's *reading* reaching the
setpoint, which is how the device judges DONE.
"""

from __future__ import annotations

import dataclasses
import math
import statistics
from typing import Callable, Sequence

from sim.food import Food, FoodParams

#: the window that says where the food is now and how it is moving
WINDOW_MIN = 45.0
#: less history than this says nothing
MIN_DATA_MIN = 15.0
#: how far back to look for the time constant and the start of the stall
HISTORY_H = 8.0
#: rising slower than this is a stall: 1 °F per 10 minutes
STALL_RATE_F_PER_H = 6.0
#: a stall is flat *and* between these. Stalls are quoted from 150 °F; the
#: model's plateau sits at 149–151 °F, so the floor is a little lower. Above
#: the ceiling the food is past any stall, and nothing evaporates any more.
STALL_ZONE_F = (145.0, 175.0)
#: below this a rising piece is still heading for its stall; above it, rising,
#: it is coming out of one (the model's plateau is at 149–152 °F for any pit)
PLATEAU_MAX_F = 153.0
#: the stall is judged on the last this many minutes: a longer average would
#: still say "stalled" for most of an hour after the food had started to climb
RECENT_MIN = 30.0
#: the rise before the stall, this long, shows the time constant. It has to be
#: hours: a piece eases onto its plateau, and climbs almost nothing in the
#: last of it.
RISE_MIN = 240.0
#: ... of which at least this much, with 10 °F climbed in it, or there is nothing to read
RISE_MIN_MIN = 45.0
#: where the pit is assumed to stay: its mean over the last this many minutes
PIT_MIN = 15.0
#: hours of flat stall per hour of time constant, from the model's presets
#: (a 12 lb brisket: 3 h and 4.7 h; spare ribs: 1.1 h and 1.4 h)
STALL_PER_TAU = 1.47
#: without a history of the stall: what is assumed for its remaining length,
#: the time constant assumed, and the longest stall allowed for in the band
STALL_PRIOR_H = 2.0
STALL_TAU_H = 2.5
STALL_MAX_H = 6.0
#: the time constants searched for: from a small chicken to an outsized roast
#: (the presets have an hour per four pounds)
TAU_MIN_H, TAU_MAX_H = 0.5, 12.0
#: how far ahead the model is run before giving up, and in what steps
HORIZON_H = 18.0
STEP_S = 300.0
#: the smallest band reported, minutes: a finish is not placed closer than
#: this, however tidy the readings
MIN_BAND_MIN = 15.0

#: the fraction of the moisture reserve through which the temperature climbs
#: out of the stall (FoodParams.moisture_tail)
_TAIL = 0.25


@dataclasses.dataclass(frozen=True)
class Window:
    """Readings over a stretch: times in seconds from the first, food and pit
    in °F. The pit may be missing (``None``) where the cook probe read OPEN."""

    times: tuple[float, ...]
    food: tuple[float, ...]
    pit: tuple[float | None, ...]

    def __len__(self) -> int:
        return len(self.times)

    @property
    def span_min(self) -> float:
        return (self.times[-1] - self.times[0]) / 60.0


@dataclasses.dataclass(frozen=True)
class Estimate:
    """``state`` is ``eta``, ``stalled`` or ``none`` (then with ``reason``).
    ``minutes`` is to the setpoint and ``band_min`` is plus and minus it."""

    state: str
    temp_f: float
    set_f: float
    minutes: float | None = None
    band_min: float | None = None
    tau_h: float | None = None          # the time constant, fitted or assumed
    stall_h: float | None = None        # the flat stall assumed still to come
    rate_f_per_h: float | None = None   # over the last half hour
    stalled_for_h: float | None = None  # how long ago the stall began, if the history shows it
    reason: str = ""                    # for ``none``: why there is no estimate


def estimate(window: Window, set_f: float, *, history: Window | None = None) -> Estimate:
    """When the food of ``window`` reaches ``set_f``: the window ends now.

    ``history`` is a longer look-back whose last sample is the window's last
    (coarser readings will do); without it the window is all there is."""
    history = history if history is not None else window
    food = window.food[-1]
    if len(window) < 3 or window.span_min < MIN_DATA_MIN:
        return Estimate("none", food, set_f, reason=f"under {MIN_DATA_MIN:g} min of data")
    if food >= set_f:
        return Estimate("none", food, set_f, reason="done")
    if all(value is None for value in window.pit):
        return Estimate("none", food, set_f, reason="no pit reading")
    pit = pit_ahead(window)
    recent = _rate(window, len(window) - 1)
    base = Estimate("eta", food, set_f, rate_f_per_h=recent)

    onset = stall_onset(history)
    in_zone = STALL_ZONE_F[0] <= food <= STALL_ZONE_F[1]
    if in_zone and recent < STALL_RATE_F_PER_H:
        phase = "stalled"
    elif food < PLATEAU_MAX_F:
        phase = "before"
    else:
        phase = "after" if food > STALL_ZONE_F[1] else "leaving"
    if phase == "before" and recent < STALL_RATE_F_PER_H / 3:
        return dataclasses.replace(base, state="none", reason="not rising yet")
    if phase == "leaving" and recent <= 0:
        return dataclasses.replace(base, state="none", reason="not rising")
    if pit <= set_f:
        return dataclasses.replace(base, state="none", reason="the pit is not above the food setpoint")

    if phase == "before":
        # heading for the plateau with all its moisture; the stall is still all ahead
        tau = fit(window, evaporating=True)
        run: Callable[[float], Food] = lambda t: piece(food, t, STALL_PER_TAU * t)
    elif phase == "after":
        tau = fit(window, evaporating=False)            # nothing evaporating any more
        run = lambda t: piece(food, t, 0.0)
    else:
        rise = tau_before_stall(history, onset) if onset is not None else None
        if rise is not None:
            # the stall began within the history: the model carries its own state from then
            tau = rise
            run = lambda t: _as_of(history, onset, t, food)
        elif phase == "stalled":
            tau = STALL_TAU_H
            run = lambda t: piece(food, t, STALL_PRIOR_H)
        else:
            # leaving the stall with no history of it: the time constant assumed,
            # and how far out of the stall it is, from the window
            tau = STALL_TAU_H
            left = moisture_left(window, tau)
            run = lambda t: piece(food, t, max(0.0, left - _TAIL) * STALL_PER_TAU * t)
            onset = None

    minutes = forward(run(tau), pit, set_f)
    if minutes is None:
        return dataclasses.replace(base, state="none", reason="the model never reaches the setpoint",
                                    tau_h=tau, stalled_for_h=_elapsed(history, onset))
    stalled_for = _elapsed(history, onset)
    stall_left = max(0.0, run(tau).moisture - _TAIL) * STALL_PER_TAU * tau
    return dataclasses.replace(
        base, state="stalled" if phase == "stalled" else "eta", minutes=minutes, tau_h=tau, stall_h=stall_left,
        stalled_for_h=stalled_for,
        band_min=_band(run, tau, pit, set_f, minutes, _wobble(window), stalled_for, phase, _reading_minutes(window)),
    )


# -- reading the history ------------------------------------------------------------

def stall_onset(history: Window) -> int | None:
    """Where in ``history`` its latest stall began, as an index: the food in
    the stall zone, rising slower than the stall rate over the previous half
    hour, and staying so for a quarter of an hour. None if it never did."""
    onset: int | None = None
    flat_since: int | None = None
    for k in range(len(history)):
        rate = _rate(history, k)
        if rate is None:
            continue
        if STALL_ZONE_F[0] <= history.food[k] <= STALL_ZONE_F[1] and rate < STALL_RATE_F_PER_H:
            if flat_since is None:
                flat_since = k
            elif history.times[k] - history.times[flat_since] >= RECENT_MIN * 60.0 / 2:
                onset = flat_since
        else:
            flat_since = None
    return onset


def tau_before_stall(history: Window, onset: int) -> float | None:
    """The time constant (hours) read from the rise before the stall began at
    ``onset``. None unless the history holds at least ``RISE_MIN_MIN`` of that
    rise, with 10 °F climbed in it: less says nothing about a time constant."""
    end = onset + 1
    first = next((k for k, t in enumerate(history.times) if t >= history.times[onset] - RISE_MIN * 60.0), 0)
    if (history.times[end - 1] - history.times[first] < RISE_MIN_MIN * 60.0
            or history.food[end - 1] - history.food[first] < 10.0):
        return None
    return fit(_slice(history, first, end), evaporating=True)


def _rate(window: Window, k: int) -> float | None:
    """The food's rate into sample ``k`` over the previous ``RECENT_MIN`` of
    it, °F/h; None with less than ten minutes to go on."""
    first = next((j for j, t in enumerate(window.times) if t >= window.times[k] - RECENT_MIN * 60.0), k)
    if window.times[k] - window.times[first] < RECENT_MIN * 60.0 / 3:
        return None
    return _slope(window.times[first:k + 1], window.food[first:k + 1]) * 3600.0


def _elapsed(history: Window, onset: int | None) -> float | None:
    """Hours between the stall's beginning and the last sample."""
    return None if onset is None else (history.times[-1] - history.times[onset]) / 3600.0


def _slice(window: Window, start: int, end: int) -> Window:
    return Window(window.times[start:end], window.food[start:end], window.pit[start:end])


def _slope(times: Sequence[float], values: Sequence[float]) -> float:
    """Least-squares slope, units per second."""
    mt, mv = statistics.fmean(times), statistics.fmean(values)
    sxx = sum((t - mt) ** 2 for t in times)
    return sum((t - mt) * (v - mv) for t, v in zip(times, values)) / sxx if sxx > 0 else 0.0


# -- the model, as fitted ------------------------------------------------------------

def reserve() -> float:
    """Moisture reserve in the units of :func:`piece`: with unit heat capacity
    the coupling is ``1 / tau``, so this does not depend on ``tau``, and the
    stall lasts ``STALL_PER_TAU · tau`` hours at the full rate."""
    return STALL_PER_TAU * (FoodParams().stall_pit_ref - FoodParams().stall_temp)


def piece(temp_f: float, tau_h: float, stall_h: float) -> Food:
    """The lumped model with unit heat capacity: time constant ``tau_h`` and
    ``stall_h`` hours of flat stall still to come (0: nothing left to evaporate).
    A piece of this time constant has, by the presets, ``STALL_PER_TAU · tau_h``
    hours of stall in all, three quarters of it flat. A longer stall than that
    takes a bigger reserve, as a bigger or wetter cut would have."""
    total = STALL_PER_TAU * tau_h
    flat = (1.0 - _TAIL) * total
    scale = 1.0
    if stall_h > flat:
        scale, stall_h = stall_h / flat, flat
    params = FoodParams(mass_lb=1.0, c_food=1.0, h_food=1.0 / tau_h, moisture_btu=reserve() * scale)
    food = Food(params, temp_f=temp_f)
    food.moisture = _TAIL + stall_h / total if stall_h > 0.0 else 0.0
    return food


def _params(tau_h: float, *, evaporating: bool) -> FoodParams:
    return FoodParams(mass_lb=1.0, c_food=1.0, h_food=1.0 / tau_h, moisture_btu=reserve() if evaporating else 0.0)


def _track(window: Window, params: FoodParams, moisture: float) -> Food:
    """Run the model from the window's first reading, with ``moisture``,
    through the pit that was read; the model as of the window's last sample."""
    food = Food(params, temp_f=window.food[0])
    food.moisture = moisture
    pits = filled(window.pit)
    for k in range(1, len(window)):
        food.step(window.times[k] - window.times[k - 1], 0.5 * (pits[k - 1] + pits[k]))
    return food


def _as_of(history: Window, onset: int, tau_h: float, food_now: float) -> Food:
    """The model at the last sample, run from the moment the stall began with
    all its moisture, through the pit that was read since; its temperature set
    to the food that was read now."""
    food = _track(_slice(history, onset, len(history)), _params(tau_h, evaporating=True), 1.0)
    food.temp = food_now
    return food


def filled(pit: Sequence[float | None]) -> list[float]:
    """The pit readings with gaps closed by straight lines, or held at the
    nearest reading at the ends."""
    out = list(pit)
    known = [k for k, value in enumerate(out) if value is not None]
    for k, value in enumerate(out):
        if value is not None:
            continue
        if not known:
            return []
        before = max((j for j in known if j < k), default=known[0])
        after = min((j for j in known if j > k), default=known[-1])
        first, last = out[before], out[after]
        out[k] = first if after == before else first + (last - first) * (k - before) / (after - before)
    return out


def pit_ahead(window: Window) -> float:
    """Where the pit is assumed to stay: its mean over the last ``PIT_MIN``."""
    first = next((k for k, t in enumerate(window.times) if t >= window.times[-1] - PIT_MIN * 60.0), 0)
    pits = filled(window.pit[first:])
    return statistics.fmean(pits) if pits else float("nan")


# -- fitting the time constant --------------------------------------------------------

def fit(window: Window, *, evaporating: bool) -> float:
    """The time constant (hours) whose track through the recorded pit comes
    closest to the food that was read: with all of the moisture still there,
    or with nothing evaporating. A coarse grid, then a golden-section search
    around the best of it."""
    def sse(tau: float) -> float:
        model = _temperatures(window, _params(tau, evaporating=evaporating), 1.0 if evaporating else 0.0)
        return sum((m - f) ** 2 for m, f in zip(model, window.food))

    grid = [TAU_MIN_H * (TAU_MAX_H / TAU_MIN_H) ** (i / 24) for i in range(25)]
    best = min(range(len(grid)), key=lambda i: sse(grid[i]))
    a, b = math.log(grid[max(best - 1, 0)]), math.log(grid[min(best + 1, len(grid) - 1)])
    phi = (math.sqrt(5.0) - 1.0) / 2.0
    c, d = b - phi * (b - a), a + phi * (b - a)
    fc, fd = sse(math.exp(c)), sse(math.exp(d))
    for _ in range(24):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - phi * (b - a)
            fc = sse(math.exp(c))
        else:
            a, c, fc = c, d, fd
            d = a + phi * (b - a)
            fd = sse(math.exp(d))
    return math.exp(0.5 * (a + b))


def moisture_left(window: Window, tau_h: float) -> float:
    """For a piece leaving its stall with no history of it: how much moisture
    it has left (a fraction of the reserve), with the time constant assumed.
    The window alone cannot show both: a slow piece whose evaporation has
    stopped looks like a quick one whose evaporation is fading."""
    def sse(left: float) -> float:
        model = _temperatures(window, _params(tau_h, evaporating=True), left)
        return sum((m - f) ** 2 for m, f in zip(model, window.food))

    grid = [k / 20.0 for k in range(21)]
    return min(grid, key=sse)


def _temperatures(window: Window, params: FoodParams, moisture: float) -> list[float]:
    food = Food(params, temp_f=window.food[0])
    food.moisture = moisture
    pits = filled(window.pit)
    out = [food.temp]
    for k in range(1, len(window)):
        food.step(window.times[k] - window.times[k - 1], 0.5 * (pits[k - 1] + pits[k]))
        out.append(food.temp)
    return out


# -- forward, and the band -------------------------------------------------------------

def forward(food: Food, pit: float, set_f: float) -> float | None:
    """Minutes until the food reads ``set_f``, the pit held at ``pit``. None
    if it does not get there within the horizon."""
    minutes = 0.0
    while food.temp < set_f:
        if minutes >= HORIZON_H * 60.0:
            return None
        before = food.temp
        food.step(STEP_S, pit)
        minutes += STEP_S / 60.0
    # where in the last step it crossed: the answer does not move in 5-minute jumps
    return minutes - STEP_S / 60.0 * (food.temp - set_f) / (food.temp - before)


def _wobble(window: Window) -> float:
    """How far the pit is let move in the band: two degrees, or twice its
    wander in the window if that was more."""
    pits = [value for value in window.pit if value is not None]
    return max(2.0, 2.0 * (statistics.pstdev(pits) if len(pits) > 1 else 0.0))


def _band(run: Callable[[float], Food], tau: float, pit: float, set_f: float, minutes: float, wobble: float,
          stalled_for: float | None, phase: str, reading_min: float) -> float:
    """How far the estimate moves under other readings of the same data: plus
    and minus it. The time constant a fifth either way, the pit a couple of
    degrees either way and the scatter of the readings are independent, so
    they add in quadrature. Wherever a stall is not over, the stall lasting
    anything from nothing to the longest moves it further than any of those."""
    moves: list[float] = []
    for factor in (0.8, 1.25):                           # the time constant a fifth either way
        other = forward(run(tau * factor), pit, set_f)
        if other is not None:
            moves.append(other - minutes)
    for delta in (-wobble, wobble):                      # the pit a couple of degrees either way
        other = forward(run(tau), pit + delta, set_f)
        if other is not None:
            moves.append(other - minutes)
    band = math.sqrt(max(MIN_BAND_MIN ** 2, sum(move * move for move in moves) + reading_min ** 2))
    stall_moves: list[float] = []
    if phase in ("stalled", "leaving"):
        # a stall may run shorter or longer than the model has it: from over now
        # to the longest there could still be. (Real stalls sit anywhere from 150
        # to 170 °F, where the model's is at 150: so a piece still below the top
        # of the zone is allowed more stall, whether or not it has begun to climb.)
        for stall in (0.0, max(0.0, STALL_MAX_H - (stalled_for or 0.0))):
            other = forward(piece(run(tau).temp, tau, stall), pit, set_f)
            if other is not None:
                stall_moves.append(abs(other - minutes))
    elif phase == "before":
        for stall in (0.0, STALL_MAX_H):                 # the stall from nothing to the longest
            other = forward(piece(run(tau).temp, tau, stall), pit, set_f)
            if other is not None:
                stall_moves.append(abs(other - minutes))
    return max([band, *stall_moves])


def _reading_minutes(window: Window) -> float:
    """What the scatter of the food readings is worth in minutes: their
    spread about a straight line, over the rate they are rising at. The
    readings cannot place the finish closer than this. Capped, so that a
    piece hardly rising does not get a band of hours from it."""
    mean_t, mean_f = statistics.fmean(window.times), statistics.fmean(window.food)
    slope = _slope(window.times, window.food)
    spread = statistics.pstdev([f - (mean_f + slope * (t - mean_t)) for t, f in zip(window.times, window.food)])
    rate = _rate(window, len(window) - 1) or 0.0
    return min(60.0, spread / (rate / 60.0)) if rate > STALL_RATE_F_PER_H / 3 else 60.0


__all__ = ["Estimate", "Window", "estimate", "filled", "fit", "forward", "moisture_left", "piece", "pit_ahead", "reserve",
           "stall_onset", "tau_before_stall"]
