"""Which control law a recording shows (docs/SIMULATOR.md).

``output_pct`` is the device's own report of its fan demand, in percent. Each
candidate law maps the error (set − pit), and for the richer laws some state,
to a percent. Every law is run over the recorded samples and its parameters
are chosen to minimise the RMS error in percentage points.

The laws, simplest first (err = set − pit in °F; all clamped to 0..100 %):

``p``
    100 · err / band
``p_offset``
    offset + 100 · err / band
``pi``
    100 · err / band + I, where I starts at a fitted value, grows by
    ki · err per minute, is held in 0..100, and is frozen while the output is
    already pinned at the limit the error pushes it towards (the simulator's
    anti-windup). No separate offset: it would be I's starting value again.
``pi_rate``
    ``pi`` − kd · rate, the pit's rate of rise over ``rate_window`` minutes

A law is selected when it lowers the RMS error of *every* simpler law by more
than :data:`SELECT_RATIO` and more than :data:`SELECT_MARGIN_PP`, and no richer
law does. Recorded output is in whole percent, so smaller differences mean
nothing. (Comparing each law with the one selected before it would let a chain
of sub-margin improvements add up to a selection.)

With a cycle time (``--cycle``) the law is evaluated once per cycle at the
device's own instants and its on-time is a whole number of seconds, so the
output only takes multiples of 100 / cycle and is held between instants.

This is a black-box fit. It says which of these shapes the recording is
consistent with, and with what numbers; it cannot tell a law the device runs
but none of these can express.
"""

from __future__ import annotations

import dataclasses
import math
import statistics
from typing import Sequence

from sim.fit import _nelder_mead
from sim.history import MAX_GAP_S, Track, transients

LAWS = ("p", "p_offset", "pi", "pi_rate")
#: a law must cut the RMS error of every simpler law by this fraction ...
SELECT_RATIO = 0.10
#: ... and by this many percentage points of RMS error
SELECT_MARGIN_PP = 0.5
#: less than this does not say anything
MIN_DURATION_S = 20 * 60.0
MIN_SAMPLES = 60
#: ... and this many of the fitted samples must have the output off its limits
MIN_UNPINNED_SAMPLES = 60
#: a stretch shorter than this is left out: too short to say anything, and with
#: the integral laws every stretch costs the fit a free parameter
MIN_SEGMENT_SAMPLES = 10
#: long recordings are thinned to this many samples before fitting
MAX_FIT_SAMPLES = 4000
DEFAULT_RATE_WINDOW_MIN = 5.0
DEFAULT_EVALUATIONS = 600
#: phases of the cycle to try against the recording (the device's instants are not known)
CYCLE_PHASES = 8
#: the shortest cycle time accepted, seconds
MIN_CYCLE_S = 0.1

#: each law's shared parameters, in the order they are fitted. An integral law
#: has no separate offset: it would be the same quantity as the integral's
#: starting value, which is fitted too (one per stretch: ``i0``)
_LAW_PARAMS = {
    "p": ("band",),
    "p_offset": ("band", "offset"),
    "pi": ("band", "ki"),
    "pi_rate": ("band", "ki", "kd"),
}
#: each parameter: its unit, and the range it is fitted in
_PARAM_INFO = {
    "band": ("°F", 1.0, 500.0),
    "offset": ("%", 0.0, 100.0),
    "ki": ("% per °F below target, per minute", 0.0, 5.0),
    "kd": ("% per °F/min of rise", 0.0, 100.0),
    "i0": ("%", 0.0, 100.0),   # the integral where a stretch starts
}
_UNITS = {"band": "°F", "offset": "%", "ki": "%/(°F·min)", "kd": "%/(°F/min)"}


class IdentifyError(ValueError):
    """A recording that cannot tell the laws apart."""


# -- the data ---------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Segment:
    """One uninterrupted stretch of closed-loop samples (times in seconds)."""

    times: tuple[float, ...]
    sets: tuple[float, ...]      # °F, the setpoint in force
    pits: tuple[float, ...]      # °F, the pit reading
    output: tuple[float, ...]    # %, what the device reported
    rate: tuple[float, ...]      # °F/min, the pit's rate of rise at that sample

    def __len__(self) -> int:
        return len(self.times)

    @property
    def duration(self) -> float:
        return self.times[-1] - self.times[0]


def _rates(times: Sequence[float], pits: Sequence[float], window_s: float) -> tuple[float, ...]:
    """Rate of rise at each sample: a least-squares line through the readings
    of the trailing ``window_s``, in °F/min (0 where there is not a window)."""
    out = []
    lo = 0
    for k, t in enumerate(times):
        while times[lo] < t - window_s:
            lo += 1
        n = k - lo + 1
        if n < 3:
            out.append(0.0)
            continue
        ts = times[lo:k + 1]
        ps = pits[lo:k + 1]
        mt, mp = statistics.fmean(ts), statistics.fmean(ps)
        sxx = sum((x - mt) ** 2 for x in ts)
        sxy = sum((x - mt) * (y - mp) for x, y in zip(ts, ps))
        out.append(sxy / sxx * 60.0 if sxx > 0.0 else 0.0)
    return tuple(out)


def _make_segment(times: list[float], sets: list[float], pits: list[float], output: list[float],
                  rate_window_min: float) -> Segment:
    return Segment(
        tuple(times), tuple(sets), tuple(pits), tuple(output),
        _rates(times, pits, rate_window_min * 60.0),
    )


def segments_from_samples(
    times: Sequence[float], sets: Sequence[float | None], pits: Sequence[float | None],
    output: Sequence[float | None], *, max_gap_s: float = MAX_GAP_S,
    rate_window_min: float = DEFAULT_RATE_WINDOW_MIN, skip: Sequence[tuple[float, float]] = (),
    min_samples: int = MIN_SEGMENT_SAMPLES,
) -> list[Segment]:
    """The closed-loop stretches of parallel sample series, as ``Segment``\\ s.

    A sample counts only with a pit reading, a setpoint and an output. The
    recording is
    split where the pit probe was open, around a reading that jumps (a plug or
    unplug: :func:`sim.history.transients`, the same samples ``sim compare``
    leaves out of its score), where a gap is longer than ``max_gap_s``, and
    around the ``skip`` windows. A stretch shorter than ``min_samples`` is not
    returned: too short to say anything, and with the integral laws every
    stretch costs the fit a free parameter (its starting integral).
    """
    ok = [p is not None and o is not None and s is not None for p, o, s in zip(pits, output, sets)]
    for k, transient in enumerate(transients(pits)):
        if transient:
            ok[k] = False
    for k, t in enumerate(times):
        if any(a <= t <= b for a, b in skip):
            ok[k] = False

    pieces: list[list[int]] = []
    current: list[int] = []
    for k in range(len(times)):
        good = ok[k] and (not current or times[k] - times[current[-1]] <= max_gap_s)
        if good:
            current.append(k)
        else:
            if current:
                pieces.append(current)
            current = [k] if ok[k] else []
    if current:
        pieces.append(current)
    return [
        _make_segment([times[k] for k in piece], [sets[k] for k in piece], [pits[k] for k in piece],
                      [output[k] for k in piece], rate_window_min)
        for piece in pieces if len(piece) >= min_samples
    ]


def segments_from_track(track: Track, *, rate_window_min: float = DEFAULT_RATE_WINDOW_MIN,
                        skip: Sequence[tuple[float, float]] = ()) -> list[Segment]:
    """The closed-loop segments of a recorded track (the pit probe's series)."""
    return segments_from_samples(
        track.times, track.sets["cook"], track.readings["cook"], track.output,
        rate_window_min=rate_window_min, skip=skip,
    )


def _thin(segments: Sequence[Segment], limit: int) -> tuple[list[Segment], int]:
    total = sum(len(s) for s in segments)
    if total <= limit:
        return list(segments), 1
    stride = math.ceil(total / limit)
    return [
        dataclasses.replace(s, **{f: getattr(s, f)[::stride] for f in ("times", "sets", "pits", "output", "rate")})
        for s in segments
    ], stride


def _check(segments: Sequence[Segment], *, samples_total: int, good_samples: int) -> None:
    """Refuse, saying why, when the recording cannot tell the laws apart.

    In order of how much each one explains: an open probe, then nothing left
    once the ``skip`` windows are out, a constant output, too little data, and
    last an output that only sits at its limits (which is only worth saying of
    a recording that is long enough otherwise).
    """
    if good_samples == 0:
        raise IdentifyError(
            f"no closed-loop data: the pit probe reads OPEN in all {samples_total} samples, so nothing "
            "was being controlled. Plug it in for the next cook."
        )
    samples = sum(len(s) for s in segments)
    if samples == 0:
        raise IdentifyError(
            f"nothing left to fit: {good_samples} samples have the pit probe plugged in, and every one of "
            "them is in a window left out with --skip."
        )
    outputs = [o for s in segments for o in s.output]
    if math.isclose(min(outputs), max(outputs), abs_tol=1e-9):
        value = outputs[0]
        why = {0.0: "the fan was off the whole time", 100.0: "the fan was flat out the whole time"}.get(value, "")
        pits = [p for s in segments for p in s.pits]
        sets = [v for s in segments for v in s.sets]
        raise IdentifyError(
            f"no closed-loop data: the output stayed at {value:g} % for all {samples} samples"
            + (f" ({why})" if why else "")
            + f". The pit ran {min(pits):.0f}–{max(pits):.0f} °F against a setpoint of "
            f"{min(sets):.0f}–{max(sets):.0f} °F. A constant output cannot tell laws apart."
        )
    duration = sum(s.duration for s in segments)
    if samples < MIN_SAMPLES or duration < MIN_DURATION_S:
        raise IdentifyError(
            f"too little closed-loop data: {samples} samples over {duration / 60:.0f} min with the pit "
            f"probe plugged in; need at least {MIN_SAMPLES} samples and {MIN_DURATION_S / 60:.0f} min."
        )
    unpinned = sum(1 for o in outputs if 0.0 < o < 100.0)
    if unpinned < MIN_UNPINNED_SAMPLES:
        raise IdentifyError(
            f"the law cannot be read from this recording: the output is pinned at 0 or 100 % in "
            f"{samples - unpinned} of {samples} samples and only {unpinned} "
            f"{'is' if unpinned == 1 else 'are'} in between. Where the output sits at a limit, every law "
            "looks the same. A cook with the pit settled near its setpoint gives samples in between."
        )


# -- the laws ---------------------------------------------------------------------

def _clamp_pct(value: float) -> float:
    return min(max(value, 0.0), 100.0)


def _demand(err: float, law: str, p: dict[str, float], integral: float, rate: float) -> float:
    out = 100.0 * err / p["band"]
    if law == "p_offset":
        out += p["offset"]
    if law in ("pi", "pi_rate"):
        out += integral
    if law == "pi_rate":
        out -= p["kd"] * rate
    return out


def predict(segment: Segment, law: str, params: dict[str, float], *, cycle_s: float = 0.0,
            phase_s: float = 0.0) -> list[float]:
    """What a law would have reported on the recorded samples.

    ``params`` holds the law's parameters and, for the laws with an integral,
    ``i0``: the integral at the start of this segment (it is fitted, since the
    device's is not known).
    """
    has_integral = law in ("pi", "pi_rate")
    integral = params.get("i0", 0.0)
    out: list[float] = []
    held: float | None = None
    next_update = segment.times[0] + phase_s

    def evaluate(err: float, rate: float, span: float) -> float:
        nonlocal integral
        value = _demand(err, law, params, integral, rate)
        if has_integral and span > 0.0:
            saturated = (value >= 100.0 and err > 0.0) or (value <= 0.0 and err < 0.0)
            if not saturated:
                integral = min(max(integral + params["ki"] * err * span / 60.0, 0.0), 100.0)
        return _clamp_pct(value)

    for k, (t, set_f, pit) in enumerate(zip(segment.times, segment.sets, segment.pits)):
        err = set_f - pit
        if cycle_s <= 0.0:
            span = t - segment.times[k - 1] if k else 0.0
            out.append(evaluate(err, segment.rate[k], span))
            continue
        if held is None:  # the first sample: as if a cycle began there
            held = evaluate(err, segment.rate[k], 0.0)
        while next_update <= t + 1e-9:
            value = evaluate(err, segment.rate[k], cycle_s)
            on_s = round(value * cycle_s / 100.0)   # whole seconds of fan per cycle
            held = _clamp_pct(on_s * 100.0 / cycle_s)
            next_update += cycle_s
        out.append(held)
    return out


def pseudo_code(law: str, params: dict[str, float], i0: float | None = None) -> str:
    """The law in one line, with its fitted numbers. ``i0`` is the integral's
    fitted starting value, when there was a single stretch to fit."""
    prop = f"(set − pit) / {params['band']:.1f} · 100"
    if law == "p":
        return f"output = clamp({prop}, 0, 100)"
    if law == "p_offset":
        return f"output = clamp({params['offset']:.1f} + {prop}, 0, 100)"
    terms = prop + " + I"
    if law == "pi_rate":
        terms += f" − {params['kd']:.1f} · rate"
    start = f", starting at {i0:.1f} %," if i0 is not None else ""
    tail = (f"    ki = {params['ki']:.3f}: I{start} grows by ki · (set − pit) per minute; kept in 0..100, "
            "and frozen while the output is pinned")
    return f"output = clamp({terms}, 0, 100)" + tail


# -- the fit ----------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class LawFit:
    law: str
    params: dict[str, float]
    rmse: float            # percentage points, over every fitted sample
    within_2pp: float      # share of samples within ±2 points
    evaluations: int
    selected: bool = False
    at_limit: tuple[str, ...] = ()   # parameters that ran into their range
    starts: tuple[float, ...] = ()   # an integral law: the integral at the start of each stretch, %
    #: the selected law only: each number, and how well the recording pins it
    #: down (from :func:`_determined`; empty where it was not asked)
    certainty: dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def label(self) -> str:
        return {"p": "P", "p_offset": "P + offset", "pi": "PI", "pi_rate": "PI + rate"}[self.law]


@dataclasses.dataclass(frozen=True)
class Identification:
    fits: list[LawFit]
    samples: int            # fitted
    samples_seen: int       # fitted before thinning: those with a pit reading and an output
    duration_s: float
    segments: int
    cycle_s: float | None
    rate_window_min: float
    stride: int = 1
    pinned: float = 0.0     # share of fitted samples at 0 or 100 %

    @property
    def selected(self) -> LawFit:
        return next(fit for fit in self.fits if fit.selected)


def select_law(fits: Sequence[LawFit]) -> LawFit:
    """The law to report: the richest one in ``fits`` (simplest first) that
    lowers the RMS error of every simpler law by more than ``SELECT_RATIO``
    and more than ``SELECT_MARGIN_PP``; the first if none does."""
    chosen = fits[0]
    for k in range(1, len(fits)):
        simpler = min(fit.rmse for fit in fits[:k])
        if fits[k].rmse < simpler * (1.0 - SELECT_RATIO) and simpler - fits[k].rmse > SELECT_MARGIN_PP:
            chosen = fits[k]
    return chosen


def _rmse(segments: Sequence[Segment], law: str, params_by_segment: Sequence[dict[str, float]],
          cycle_s: float, phase_s: float) -> tuple[float, float]:
    """(RMS error, share within 2 points) of one parameter set over all segments."""
    squared = 0.0
    n = 0
    close = 0
    for seg, p in zip(segments, params_by_segment):
        for got, want in zip(predict(seg, law, p, cycle_s=cycle_s, phase_s=phase_s), seg.output):
            d = got - want
            squared += d * d
            close += abs(d) <= 2.0
            n += 1
    return math.sqrt(squared / n), close / n


def _to_x(names: Sequence[str], values: dict[str, float]) -> list[float]:
    """``values`` of ``names`` in the simplex's coordinates: 0 at the bottom
    of a parameter's range, 1 at the top."""
    return [(values[n] - _PARAM_INFO[n][1]) / (_PARAM_INFO[n][2] - _PARAM_INFO[n][1]) for n in names]


def _starting_points(law: str, segment: Segment) -> list[list[float]]:
    """Normalised starting points for the simplex: a coarse grid over the
    parameters that matter. The objective has several local minima (a line
    through the wrong corner of the data, an integral that does nothing), so
    one start is not enough."""
    bands = (5.0, 15.0, 30.0, 60.0, 150.0)
    grid: list[dict[str, float]] = []
    if law == "p":
        grid = [{"band": b} for b in bands]
    elif law == "p_offset":
        grid = [{"band": b, "offset": o} for b in bands for o in (0.0, 25.0, 50.0)]
    else:
        i0 = min(max(segment.output[0] / 100.0, 0.0), 1.0)
        grid = [
            {"band": b, "ki": ki, "i0": i0}
            for b in (10.0, 30.0, 60.0) for ki in (0.03, 0.15, 0.8)
        ]
        if law == "pi_rate":
            for g in grid:
                g["kd"] = 5.0
    return [_to_x(list(_LAW_PARAMS[law]) + (["i0"] if law in ("pi", "pi_rate") else []), g) for g in grid]


def _fit_law(segments: Sequence[Segment], law: str, *, cycle_s: float, phase_s: float,
             max_evaluations: int) -> tuple[list[dict[str, float]], int]:
    """Best parameters per segment for one law; the shared parameters are the
    same in every segment, the starting integral is one per segment."""
    names = list(_LAW_PARAMS[law])
    n_i0 = len(segments) if law in ("pi", "pi_rate") else 0
    lo_hi = [(_PARAM_INFO[n][1], _PARAM_INFO[n][2]) for n in names]

    def build(x: Sequence[float]) -> list[dict[str, float]]:
        shared = {n: lo + min(max(x[i], 0.0), 1.0) * (hi - lo) for i, (n, (lo, hi)) in enumerate(zip(names, lo_hi))}
        if not n_i0:
            return [shared] * len(segments)
        return [{**shared, "i0": min(max(x[len(names) + k], 0.0), 1.0) * 100.0} for k in range(n_i0)]

    def objective(x: Sequence[float]) -> float:
        return _rmse(segments, law, build(x), cycle_s, phase_s)[0]

    # one starting point per law; with several segments it carries the first
    # segment's starting integral, repeated for the others
    starts = _starting_points(law, segments[0])
    starts = [x + [x[-1]] * (n_i0 - 1) for x in starts]
    ranked = sorted(starts, key=objective)
    best, spent = (None, math.inf), 0
    tried: list[list[float]] = []
    for start in ranked:
        if any(max(abs(a - b) for a, b in zip(start, seen)) < 0.05 for seen in tried):
            continue
        tried.append(start)
        point, value, used = _nelder_mead(objective, start, step=0.1, max_evaluations=max_evaluations)
        spent += used
        if value < best[1]:
            best = (build(point), value)
        if len(tried) >= 3 or spent >= max_evaluations * 3:
            break
    return best[0], spent


def _refit(
    segments: Sequence[Segment], law: str, start: Sequence[dict[str, float]], locked: dict[str, float], *,
    cycle_s: float, phase_s: float, max_evaluations: int,
) -> tuple[list[dict[str, float]], float]:
    """The best fit with the parameters in ``locked`` held at those values and
    the others free, setting out from ``start`` (one dict per segment).
    Returns the parameters found and their RMS error."""
    free = [n for n in _LAW_PARAMS[law] if n not in locked]
    free_starts = law in ("pi", "pi_rate") and "i0" not in locked

    def build(x: Sequence[float]) -> list[dict[str, float]]:
        shared = dict(locked)
        for i, n in enumerate(free):
            _, lo, hi = _PARAM_INFO[n]
            shared[n] = lo + min(max(x[i], 0.0), 1.0) * (hi - lo)
        if not free_starts:
            return [shared] * len(segments)
        return [{**shared, "i0": min(max(x[len(free) + k], 0.0), 1.0) * 100.0} for k in range(len(segments))]

    x0 = [(start[0][n] - _PARAM_INFO[n][1]) / (_PARAM_INFO[n][2] - _PARAM_INFO[n][1]) for n in free]
    if free_starts:
        x0 += [min(max(start[k]["i0"], 0.0), 100.0) / 100.0 for k in range(len(segments))]
    if not x0:   # the law's only number is locked (plain P): there is nothing to re-fit
        return build(x0), _rmse(segments, law, build(x0), cycle_s, phase_s)[0]
    point, value, _ = _nelder_mead(
        lambda x: _rmse(segments, law, build(x), cycle_s, phase_s)[0], x0, step=0.05,
        max_evaluations=max_evaluations,
    )
    return build(point), value


#: how far a fitted number can be forced from its value, the others re-fitted,
#: before the fit counts as worse: by this many percentage points of RMS error ...
PROFILE_TOL_PP = 0.05
#: ... or this share of it. Output is recorded in whole percent: smaller rises are noise.
PROFILE_TOL_SHARE = 0.05
#: the displacements tried, outward from the fitted value, as factors for the
#: band, the offset, ki and kd (each tried downwards and upwards) ...
PROFILE_FACTORS = (1.2, 1.5, 2.0)
#: ... and as points of output for the integral's starting value
PROFILE_START_STEPS_PP = (5.0, 15.0, 30.0)
#: a factor of a number at or below this is a factor of this instead, so that
#: a number fitted at zero is still moved (by a 20th, a 10th and a fifth of it)
_PROFILE_FLOORS = {"band": 5.0, "offset": 5.0, "ki": 0.05, "kd": 2.5}
_FACTOR_WORDS = ("20 %", "50 %", "a factor of 2")
_STEP_WORDS = ("5 points", "15 points", "30 points")
_IN_WORDS = {
    "band": "the band", "offset": "the offset", "ki": "ki", "kd": "kd",
    "i0": "the integral's starting value",
}


def _determined(
    segments: Sequence[Segment], law: str, by_segment: Sequence[dict[str, float]], rmse: float, *,
    cycle_s: float, phase_s: float, max_evaluations: int,
) -> dict[str, str]:
    """How well the recording pins each of a law's numbers down.

    A number is forced away from its fitted value in steps, the others
    re-fitted, as long as the RMS error stays within tolerance of ``rmse``.
    What is reported is the first step the fit did not tolerate: "within 20 %"
    when a 20 % move already made it worse, "not within a factor of 2" when
    even halving or doubling left it as good as the best fit.
    """
    tolerance = max(PROFILE_TOL_PP, PROFILE_TOL_SHARE * rmse)

    def tolerated(name: str, moves: Sequence[float]) -> bool:
        return all(
            _refit(segments, law, by_segment, {name: value}, cycle_s=cycle_s, phase_s=phase_s,
                   max_evaluations=max_evaluations)[1] <= rmse + tolerance
            for value in moves
        )

    def walk(name: str, value: float, steps: Sequence[set[float]], words: Sequence[str]) -> str:
        how_far = 0
        for moves in steps:
            moves = [move for move in moves if move != value]   # a side at the end of the range has nothing to test
            if moves and not tolerated(name, moves):
                break
            how_far += 1
        return f"within {words[how_far]}" if how_far < len(words) else f"not within {words[-1]}"

    verdicts = {}
    for name, value in by_segment[0].items():
        if name == "i0":
            continue
        _, low, high = _PARAM_INFO[name]
        scale = max(value, _PROFILE_FLOORS[name])
        # downwards, a factor off means divided by it: a move of scale · (1 − 1 / factor)
        verdicts[name] = walk(name, value, [
            {min(max(value - scale * (1.0 - 1.0 / factor), low), high),
             min(max(value + scale * (factor - 1.0), low), high)}
            for factor in PROFILE_FACTORS
        ], _FACTOR_WORDS)
    if "i0" in by_segment[0]:
        value = by_segment[0]["i0"]
        verdicts["i0"] = walk("i0", value, [
            {min(max(value + sign * step, 0.0), 100.0) for sign in (-1.0, 1.0)}
            for step in PROFILE_START_STEPS_PP
        ], _STEP_WORDS)
    return verdicts


def identify(
    segments: Sequence[Segment], *, cycle_s: float | None = None,
    rate_window_min: float = DEFAULT_RATE_WINDOW_MIN, laws: Sequence[str] = LAWS,
    max_evaluations: int = DEFAULT_EVALUATIONS, samples_total: int | None = None,
    good_samples: int | None = None, pin_down: bool = True,
) -> Identification:
    """Fit every law to the closed-loop ``segments`` and choose among them.

    ``samples_total`` (the recording's sample count) and ``good_samples`` (how
    many of them have a pit reading and an output) are read only when refusing;
    both default to what the ``segments`` hold. With ``pin_down`` the selected
    law's numbers are also tested for how well the recording determines them
    (:attr:`LawFit.certainty`).
    """
    if not laws:
        raise IdentifyError(f"no law to fit: name at least one of {', '.join(LAWS)}")
    unknown = [law for law in laws if law not in LAWS]
    if unknown:
        raise IdentifyError(f"unknown law(s): {', '.join(unknown)} (known: {', '.join(LAWS)})")
    held = sum(len(s) for s in segments)
    if held == 0 and good_samples is None:
        # the command passes the count and gets the more specific message
        raise IdentifyError("no closed-loop data: no sample has both a pit reading and an output")
    _check(
        segments, samples_total=samples_total if samples_total is not None else held,
        good_samples=good_samples if good_samples is not None else held,
    )
    # shorter than that would have the law evaluated millions of times between two samples
    if cycle_s is not None and float(cycle_s) < MIN_CYCLE_S:
        raise IdentifyError(f"--cycle must be a number of seconds, {MIN_CYCLE_S:g} or more")
    cycle = float(cycle_s) if cycle_s else 0.0
    fitted, stride = _thin(segments, MAX_FIT_SAMPLES)

    phase = 0.0
    if cycle > 0.0:
        # the device's cycle instants are not known: pick the phase that lets the
        # simplest law fit best, and keep it for the others
        best = None
        for k in range(CYCLE_PHASES):
            candidate = cycle * k / CYCLE_PHASES
            params, _ = _fit_law(fitted, "p", cycle_s=cycle, phase_s=candidate, max_evaluations=max_evaluations // 2)
            value = _rmse(fitted, "p", params, cycle, candidate)[0]
            if best is None or value < best[1]:
                best = (candidate, value)
        phase = best[0]

    fits: list[LawFit] = []
    fitted_by_law: dict[str, list[dict[str, float]]] = {}
    for law in LAWS:
        if law not in laws:
            continue
        by_segment, spent = _fit_law(fitted, law, cycle_s=cycle, phase_s=phase, max_evaluations=max_evaluations)
        fitted_by_law[law] = by_segment
        rmse, close = _rmse(fitted, law, by_segment, cycle, phase)
        shared = {k: v for k, v in by_segment[0].items() if k != "i0"}
        at_limit = tuple(
            name for name in shared
            if shared[name] <= _PARAM_INFO[name][1] + 0.01 * (_PARAM_INFO[name][2] - _PARAM_INFO[name][1])
            or shared[name] >= _PARAM_INFO[name][2] - 0.01 * (_PARAM_INFO[name][2] - _PARAM_INFO[name][1])
        )
        starts = tuple(p["i0"] for p in by_segment) if "i0" in by_segment[0] else ()
        fits.append(LawFit(law, shared, rmse, close, spent, at_limit=at_limit, starts=starts))

    chosen = select_law(fits)
    certainty = {} if not pin_down else _determined(
        fitted, chosen.law, fitted_by_law[chosen.law], chosen.rmse, cycle_s=cycle, phase_s=phase,
        max_evaluations=max_evaluations // 2,
    )
    fits = [
        dataclasses.replace(fit, selected=fit is chosen, certainty=certainty if fit is chosen else {})
        for fit in fits
    ]
    outputs = [o for seg in fitted for o in seg.output]
    return Identification(
        fits=fits, samples=sum(len(s) for s in fitted),
        samples_seen=held,
        duration_s=sum(s.duration for s in segments), segments=len(fitted), cycle_s=cycle_s,
        rate_window_min=rate_window_min, stride=stride,
        pinned=sum(1 for o in outputs if o <= 0.0 or o >= 100.0) / len(outputs),
    )


# -- the report -------------------------------------------------------------------

def ordinal(n: int) -> str:
    """``1st``, ``2nd``, ``3rd``, ``4th`` ... ``11th``, ``12th``, ``13th``."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def format_identification(result: Identification, *, legend: bool = True) -> str:
    span = result.duration_s / 3600.0
    head = (
        f"{result.samples_seen} closed-loop samples over {span:.1f} h"
        + (f", {result.segments} stretch(es)" if result.segments != 1 else "")
        + (f", every {ordinal(result.stride)} fitted" if result.stride > 1 else "")
        + (f", cycle {result.cycle_s:g} s" if result.cycle_s else "")
        + f"; rate of rise over {result.rate_window_min:g} min"
    )
    columns = ("band", "offset", "ki", "kd")
    lines = [head, ""]
    lines.append(f"{'law':10}" + "".join(f"{c + ' ' + _UNITS[c]:>18}" for c in columns)
                 + f"{'RMSE pp':>10}{'±2 pp':>8}")
    for fit in result.fits:
        cells = "".join(f"{fit.params[c]:18.2f}" if c in fit.params else f"{'–':>18}" for c in columns)
        lines.append(f"{fit.label + (' *' if fit.selected else ''):10}{cells}{fit.rmse:10.2f}{fit.within_2pp:8.0%}")
    chosen = result.selected
    single = chosen.starts[0] if len(chosen.starts) == 1 else None
    lines += ["", f"selected {chosen.label}: {pseudo_code(chosen.law, chosen.params, single)}"]
    if chosen.certainty:
        lines.append("  how well the recording pins these down: " + "; ".join(
            f"{_IN_WORDS[name]} {verdict}" for name, verdict in chosen.certainty.items()
        ))
    notes = []
    # the direct test above says this better where it ran; this stands in where it did not
    if chosen.at_limit and not chosen.certainty:
        notes.append("at the limit of its search range, so not really determined: " + ", ".join(chosen.at_limit))
    if result.pinned > 0.5:
        notes.append(f"{result.pinned:.0%} of the samples are pinned at 0 or 100 %: the band is set by the rest")
    if legend:
        notes.append(f"a law is selected only if it cuts the RMS error of every simpler law by over "
                     f"{SELECT_RATIO:.0%} and {SELECT_MARGIN_PP:g} points (output is recorded in whole percent)")
        notes.append("ki: the integral's growth, % per °F below target per minute; kd: % per °F/min of rise")
        notes.append("'pins down': each number was moved 20 %, 50 % and a factor of 2 from its value (the others "
                     "re-fitted); 'within 20 %' means a 20 % move already made the fit worse, 'not within a "
                     "factor of 2' that halving or doubling left it as good")
    for note in notes:
        lines.append(f"  {note}")
    return "\n".join(lines)


__all__ = [
    "CYCLE_PHASES", "DEFAULT_EVALUATIONS", "DEFAULT_RATE_WINDOW_MIN", "Identification", "IdentifyError",
    "LAWS", "LawFit", "MAX_FIT_SAMPLES", "MIN_CYCLE_S", "MIN_DURATION_S", "MIN_SAMPLES", "MIN_SEGMENT_SAMPLES",
    "MIN_UNPINNED_SAMPLES", "PROFILE_FACTORS", "PROFILE_START_STEPS_PP", "PROFILE_TOL_PP", "PROFILE_TOL_SHARE",
    "SELECT_MARGIN_PP", "SELECT_RATIO",
    "Segment", "format_identification", "identify", "ordinal", "predict", "pseudo_code", "segments_from_samples",
    "segments_from_track", "select_law",
]
