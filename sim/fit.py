"""System identification (docs/SIMULATOR.md, ``sim fit``).

Estimates the cooker's parameters from a recorded cook: the recording's own
fan output is replayed through the plant (open loop, :mod:`sim.history`) and
the parameters are nudged until the modelled pit follows the measured one.

What temperatures can tell is worth being clear about. Divide the pit's heat
balance through by the shell's conductance and only ratios are left: the
time constant ``c_pit / ua_pit``, the gain ``q_max / ua_pit``, the leak past
a stopped fan ``draft_min``, and — more roughly — how long the coals take to
answer the fan, ``tau_fire``. Scale ``c_pit``, ``ua_pit`` and ``q_max``
together and the temperature does not change at all, so no recording can
choose that scale. Accordingly ``ua_pit`` is *held* as the scale of BTUs (its
default, or whatever ``--params`` gives) and the other four are fitted.
Naming ``ua_pit`` in ``--fit`` fits it too, and then only the prior decides
the scale. What such a cooker cannot give is absolute BTUs: its fuel use is
only right if its ``ua_pit`` is.

Every fitted parameter pays ``prior`` °F² per octave it sits from its
starting value, which settles whatever the recording leaves open (the
stopped-fan leak, unless the fan stopped) near where it started without
standing in the way of a real difference.

A recording with no heat-up in it cannot say anything about the plant. That
is reported as it is — along with what such a recording *can* give, the air
temperature and the probes' noise — instead of being fitted anyway. And a
lid that came off is not in the recording: leave that window out
(``exclude``), or its aftermath will dominate the fit.

The output is a YAML file of plant parameters, for ``sim compare`` and for a
scenario's ``cooker:`` section.
"""

from __future__ import annotations

import dataclasses
import math
import pathlib
import statistics
import time
from typing import Callable, Sequence

import yaml

from sim.food import FoodParams
from sim.history import MIN_NOISE_SAMPLES, Track, replay, score, transients
from sim.plant import PlantParams

#: what is fitted by default. ua_pit is deliberately not among them: it sets
#: the scale of BTUs, which temperatures cannot show (see the module
#: docstring). Name it in ``fitted`` to fit it anyway.
DEFAULT_FIT = ("c_pit", "q_max", "tau_fire", "draft_min")
#: how far any parameter may move from its start, in octaves
BOUND_OCTAVES = 3.0
#: a pit that moved less than this (robustly) has nothing to teach
MIN_EXCITATION_F = 25.0
#: cost, in °F², of a parameter sitting one octave from where it started
DEFAULT_PRIOR = 0.05

@dataclasses.dataclass
class FitResult:
    params: PlantParams                 # the plant, fitted or left as it was
    ambient_f: float
    noise_f: dict[str, float]           # probe -> sd of its reading, °F
    excitation_f: float
    identified: bool
    reason: str
    fitted: tuple[str, ...]
    before: dict                        # open-loop score, starting parameters
    after: dict                         # open-loop score, resulting parameters
    octaves: dict[str, float]           # log2(fitted / start), per fitted parameter
    evaluations: int = 0
    seconds: float = 0.0


class FitError(ValueError):
    """A fit asked for something the recording or the parameters cannot give."""


# -- what a recording says at a glance -------------------------------------------

def _values(series: Sequence[float | None]) -> list[float]:
    return [value for value in series if value is not None]


def _percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def robust_range(series: Sequence[float | None]) -> float:
    """90th minus 10th percentile: the spread that is not a spike or a dip."""
    values = _values(series)
    return _percentile(values, 0.9) - _percentile(values, 0.1) if values else 0.0


def probe_noise(series: Sequence[float | None]) -> float:
    """Reading noise as the sd of successive differences over √2, leaving
    out the plug and unplug transients (:func:`sim.history.transients`).

    Readings come in tenths of a degree, so with ordinary probe noise more
    than half of the differences are exactly zero: a median-based estimate
    would say there is none.
    """
    spike = transients(series)
    differences = [
        b - a for i, (a, b) in enumerate(zip(series, series[1:]))
        if a is not None and b is not None and not spike[i] and not spike[i + 1]
    ]
    if len(differences) < MIN_NOISE_SAMPLES:
        return 0.0
    return statistics.stdev(differences) / math.sqrt(2.0)


def estimate_ambient(track: Track) -> float:
    """The air temperature, as well as the recording can say.

    A cook that starts cold begins at air temperature, so that is the
    coldest the pit's first five minutes show (over three-sample smoothing,
    so one low reading cannot fool it) — not the average of them, by which
    time a quick fire has already lifted it. A recording with no heat-up is
    all air temperature, and its whole median is used. A cook that starts
    hot says nothing about the air: pass ``ambient_f`` for those.
    """
    cook = track.readings["cook"]
    head = [v for t, v in zip(track.times, cook) if t <= 300 and v is not None]
    if robust_range(cook) >= MIN_EXCITATION_F and len(head) >= 3:
        smoothed = [statistics.mean(head[i:i + 3]) for i in range(len(head) - 2)]
        return round(min(smoothed), 1)
    values = _values(cook)
    return round(statistics.median(values), 1) if values else 70.0


# -- the optimiser ----------------------------------------------------------------

def _clamp(value: float, bound: float) -> float:
    return min(max(value, -bound), bound)


def _nelder_mead(
    objective: Callable[[Sequence[float]], float], start: Sequence[float], *,
    step: float, max_evaluations: int,
) -> tuple[list[float], float, int]:
    """Downhill simplex in ``len(start)`` dimensions. Returns the best point
    ever evaluated, its value, and how many evaluations were spent."""
    n = len(start)
    simplex = [list(start)]
    for i in range(n):
        corner = list(start)
        corner[i] += step
        simplex.append(corner)
    values = [objective(corner) for corner in simplex]
    evaluations = n + 1
    while evaluations < max_evaluations:
        order = sorted(range(n + 1), key=lambda k: values[k])
        simplex, values = [simplex[k] for k in order], [values[k] for k in order]
        spread = max(abs(simplex[k][i] - simplex[0][i]) for k in range(1, n + 1) for i in range(n))
        if values[-1] - values[0] < 1e-6 and spread < 1e-3:
            break  # the simplex has collapsed onto an answer
        centroid = [sum(simplex[k][i] for k in range(n)) / n for i in range(n)]
        worst = simplex[-1]

        def moved(scale_from_worst: float) -> list[float]:
            return [c + scale_from_worst * (c - w) for c, w in zip(centroid, worst)]

        reflected = moved(1.0)
        f_reflected = objective(reflected)
        evaluations += 1
        if f_reflected < values[0]:
            expanded = moved(2.0)
            f_expanded = objective(expanded)
            evaluations += 1
            simplex[-1], values[-1] = (
                (expanded, f_expanded) if f_expanded < f_reflected else (reflected, f_reflected)
            )
        elif f_reflected < values[-2]:
            simplex[-1], values[-1] = reflected, f_reflected
        else:
            contracted = moved(-0.5)
            f_contracted = objective(contracted)
            evaluations += 1
            if f_contracted < values[-1]:
                simplex[-1], values[-1] = contracted, f_contracted
            else:
                for k in range(1, n + 1):  # shrink towards the best corner
                    simplex[k] = [(a + b) / 2 for a, b in zip(simplex[0], simplex[k])]
                    values[k] = objective(simplex[k])
                evaluations += n
    best = min(range(n + 1), key=lambda k: values[k])
    return simplex[best], values[best], evaluations


# -- the fit ----------------------------------------------------------------------

def fit_plant(
    track: Track, *, ambient_f: float | None = None, params: PlantParams | None = None,
    fitted: Sequence[str] = DEFAULT_FIT, fuel_btu: float = 200_000.0,
    foods: dict[str, FoodParams] | None = None,
    dt: float = 10.0, max_evaluations: int = 400, prior: float = DEFAULT_PRIOR,
    exclude: Sequence[tuple[float, float]] = (),
    progress: Callable[[int, float], None] | None = None,
) -> FitResult:
    """Fit ``fitted`` plant parameters to the recording, open loop.

    ``foods`` is what was on the food probes (probe -> parameters): meat the
    model is not told about comes out of the fire instead, as a few degrees
    of error the fit will try to explain with the wrong parameters.
    ``exclude`` holds (start, end) windows in seconds from the recording's
    first sample that are left out of the objective. ``progress(evaluations,
    rmse)`` is called every twenty evaluations when given. The pit's mean
    squared error is what is minimised, plus the prior.
    """
    params = params if params is not None else PlantParams()
    names = tuple(fitted)
    if not names:
        raise FitError("nothing to fit: name at least one plant parameter")
    known = {field.name for field in dataclasses.fields(PlantParams)}
    unknown = [name for name in names if name not in known]
    if unknown:
        raise FitError(f"not plant parameters: {', '.join(unknown)}; choose from {', '.join(sorted(known))}")
    start = {name: getattr(params, name) for name in names}
    if any(value <= 0 for value in start.values()):
        raise FitError("every parameter being fitted must start above zero")

    ambient_f = estimate_ambient(track) if ambient_f is None else float(ambient_f)
    noise = {
        probe: probe_noise(series)
        for probe, series in track.readings.items()
        if len(_values(series)) >= MIN_NOISE_SAMPLES
    }
    excitation = robust_range(track.readings["cook"])

    def run(with_params: PlantParams) -> dict:
        played = replay(
            track, ambient_f=ambient_f, params=with_params, fuel_btu=fuel_btu, foods=foods,
            open_loop=True, dt=dt,
        )
        return score(track, played.simulated, exclude=exclude)

    def rmse_f(scored: dict) -> float:
        return scored["probes"]["cook"].get("rmse", math.inf)

    before = run(params)
    if excitation < MIN_EXCITATION_F:
        return FitResult(
            params, ambient_f, noise, excitation, False,
            f"the pit moved only {excitation:.1f} °F over the recording: no heat-up to learn from",
            (), before, before, {},
        )

    best: dict = {"x": [0.0] * len(names), "loss": math.inf}
    count = [0]

    def loss(x: Sequence[float]) -> float:
        clamped = [_clamp(octaves, BOUND_OCTAVES) for octaves in x]
        candidate = PlantParams(**{**params.__dict__, **{
            name: start[name] * 2.0 ** octaves for name, octaves in zip(names, clamped)
        }})
        value = rmse_f(run(candidate)) ** 2 + prior * sum(octaves * octaves for octaves in clamped)
        count[0] += 1
        if value < best["loss"]:
            best["x"], best["loss"] = clamped, value
        if progress is not None and count[0] % 20 == 0:
            progress(count[0], math.sqrt(max(best["loss"], 0.0)))
        return value

    started = time.monotonic()
    _nelder_mead(loss, [0.0] * len(names), step=0.5, max_evaluations=max_evaluations)
    octaves = dict(zip(names, best["x"]))
    result = PlantParams(**{**params.__dict__, **{
        name: start[name] * 2.0 ** octaves[name] for name in names
    }})
    return FitResult(
        result, ambient_f, noise, excitation, True, "", names, before, run(result), octaves,
        count[0], time.monotonic() - started,
    )


# -- files and text ----------------------------------------------------------------

def write_params(path: pathlib.Path | str, result: FitResult, *, samples: int | None = None) -> pathlib.Path:
    """The fitted cooker as YAML, for ``sim compare --params`` and a scenario's
    ``cooker:`` section."""
    path = pathlib.Path(path)
    document: dict = {"ambient_f": result.ambient_f}
    if result.identified:
        document["fitted"] = list(result.fitted)
        document["pit_rmse_f"] = round(result.after["probes"]["cook"].get("rmse", 0.0), 3)
    document["plant"] = {field.name: getattr(result.params, field.name) for field in dataclasses.fields(PlantParams)}
    if samples is not None:
        document["samples"] = samples
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Cooker parameters, written by `python -m sim fit`.\n"
        "# For `sim compare --params` and for a scenario's cooker: section.\n"
        + yaml.safe_dump(document, sort_keys=False)
    )
    return path


def load_params(path: pathlib.Path | str) -> tuple[PlantParams, float | None]:
    """Read such a file: (plant parameters, ambient temperature or None).
    A bare mapping of plant parameters is accepted too."""
    path = pathlib.Path(path)
    try:
        document = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise FitError(f"cannot read parameters from {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise FitError(f"{path}: expected a mapping")
    plant = document.get("plant", document)
    if not isinstance(plant, dict):
        raise FitError(f"{path}: 'plant' should be a mapping of parameters")
    known = {field.name for field in dataclasses.fields(PlantParams)}
    try:
        params = PlantParams(**{key: value for key, value in plant.items() if key in known})
    except (TypeError, ValueError) as exc:
        raise FitError(f"{path}: {exc}") from exc
    ambient = document.get("ambient_f")
    return params, None if ambient is None else float(ambient)


def format_fit(result: FitResult) -> str:
    """A fit, as a few lines of text."""
    noise = ", ".join(f"{probe} {sd:.2f}" for probe, sd in result.noise_f.items())
    lines = [
        f"air temperature {result.ambient_f:.1f} °F; reading noise (sd), °F: {noise or '–'}",
        f"the pit moved {result.excitation_f:.1f} °F (10th to 90th percentile) over "
        f"{result.before['samples']} samples",
    ]
    if not result.identified:
        lines.append(f"  {result.reason}.")
        lines.append("  Plant parameters left as they were: the air temperature and the probe noise")
        lines.append("  are all this recording can give.")
        return "\n".join(lines)
    lines.append("  fitting " + ", ".join(result.fitted) + " in open loop:")
    lines.append(f"  {'parameter':<12}{'start':>10}{'fitted':>10}{'octaves':>9}")
    for name, octaves in result.octaves.items():
        lines.append(
            f"  {name:<12}{getattr(result.params, name) / 2.0 ** octaves:>10.4g}"
            f"{getattr(result.params, name):>10.4g}{octaves:>+9.2f}"
        )
    before = result.before["probes"]["cook"].get("rmse")
    after = result.after["probes"]["cook"].get("rmse")
    shown = ["–" if value is None else f"{value:.2f}" for value in (before, after)]
    lines.append(
        f"  pit RMSE {shown[0]} -> {shown[1]} °F; "
        f"{result.evaluations} evaluations in {result.seconds:.0f} s"
    )
    return "\n".join(lines)
