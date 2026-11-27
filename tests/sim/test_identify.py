"""`sim identify`: which law a recording shows, and when it cannot say.

The fits are checked against data the tool did not make. The simulator's own
controllers stand in for the device for P, P + offset and PI; the rate term,
which no simulator controller has, is restated line by line here. The shared
``recording`` fixture is the end-to-end case. So the answers are known.

The fits themselves are slow tests. The default run has the laws' arithmetic,
the selection rule, the splitting, the rate of rise and the refusals.
"""

from __future__ import annotations

import dataclasses
import math
import random

import pytest

from sim.cli import main
from sim.controllers import get_controller
from sim.controllers.base import ControlConfig
from sim.controllers.proportional import round_half_up
from sim.identify import (
    LAWS,
    SELECT_MARGIN_PP,
    SELECT_RATIO,
    Identification,
    IdentifyError,
    LawFit,
    Segment,
    format_identification,
    identify,
    ordinal,
    predict,
    pseudo_code,
    segments_from_samples,
    select_law,
)
from tests.sim.conftest import build_sim, record

SET = 225.0
BAND = 30.0   # the device's default proportional band (sim/device.py)
BIAS = 33.0   # the fan share the `bias` preset puts up front
KI = 0.1      # the `pi` preset's integral gain, % per °F below target per minute
#: the baseline's open-lid and start-up holds are not laws: switched off here,
#: since only the law is being read
HOLD_OFF = dict(lid_drop_f_per_min=1e9, startup_slew_f_per_min=1e9)
CFG = ControlConfig(setpoint_f=SET, hold_setpoint_f=200.0, propband_f=BAND, cyctime_s=6.0, opendetect=False)
CYCLE_LEVELS = {0, 17, 33, 50, 67, 83, 100}   # whole seconds of a 6 s cycle, as whole percent


# -- data with known answers ------------------------------------------------------------

def a_pit(n: int = 1080, dt: float = 10.0, seed: int = 1, noise: float = 0.12) -> tuple[list[float], list[float]]:
    """Times and a pit's readings, every 10 s for three hours by default: an
    approach to the setpoint with a slow wander around it, so that the rate of
    rise is not just a function of the error, and a little probe noise."""
    rng = random.Random(seed)
    pits = []
    for k in range(n):
        t = k * dt
        base = SET - 60.0 * math.exp(-t / 2400.0) + 8.0 * math.sin(t / 600.0)
        pits.append(round(base + rng.gauss(0.0, noise), 1))
    return [k * dt for k in range(n)], pits


def a_wander(n: int = 300, dt: float = 10.0, seed: int = 2) -> tuple[list[float], list[float]]:
    """Times and a pit's readings near the setpoint, so the output stays off
    its limits."""
    rng = random.Random(seed)
    pits = [round(SET - 8.0 + 6.0 * math.sin(k / 7.0) + rng.gauss(0.0, 0.2), 1) for k in range(n)]
    return [k * dt for k in range(n)], pits


def a_crossing(n: int = 300, dt: float = 10.0) -> tuple[list[float], list[float]]:
    """Times and a pit's readings on a straight line from 20 °F below the
    setpoint to 20 °F above it, crossing it between samples n/2 − 1 and n/2."""
    return [k * dt for k in range(n)], [SET - 20.0 + 40.0 * k / (n - 1) for k in range(n)]


def made_with(name: str, times, pits, **options) -> list[int]:
    """The output the simulator's ``name`` controller asks for along a pit's
    readings: an implementation of the law that is not the one under test."""
    law = get_controller(name, **HOLD_OFF, **options)
    return [law.step(t, pit, (None, None, None), CFG).fan_pct for t, pit in zip(times, pits)]


def by_hand(times, pits, rates, *, band, offset=0.0, ki=0.0, kd=0.0, integral=0.0) -> list[int]:
    """The law with every term, written out again here and rounded to whole
    percent, for the data no simulator controller makes."""
    out = []
    for k, (t, pit) in enumerate(zip(times, pits)):
        err = SET - pit
        value = offset + 100.0 * err / band + integral - kd * rates[k]
        pinned = (value >= 100.0 and err > 0.0) or (value <= 0.0 and err < 0.0)
        minutes = 0.0 if k == 0 else (times[k] - times[k - 1]) / 60.0
        if not pinned:
            integral = min(max(integral + ki * err * minutes, 0.0), 100.0)
        out.append(round_half_up(min(max(value, 0.0), 100.0)))
    return out


def a_closed_loop_cook(name: str, hours: float = 4.0):
    """A cook under ``name`` from 190 °F, as the dashboard would record it:
    (times, setpoints, pit readings, outputs), a row a minute."""
    sim = build_sim(
        cook_set_f=SET, controller=get_controller(name, **HOLD_OFF),
        preheat=190.0, dt=10.0, sample_every=6,
    )
    sim.run(hours * 3600.0)
    return (
        [r["t"] for r in sim.rows], [r["cook_set"] for r in sim.rows],
        [r["cook"] for r in sim.rows], [r["output_pct"] for r in sim.rows],
    )


def fit_of(times, pits, outs, *, skip=(), **options) -> Identification:
    """Fit the laws to parallel series with the setpoint at ``SET`` throughout;
    ``skip`` goes to the splitting, anything else to the fit."""
    return identify(segments_from_samples(times, [SET] * len(times), pits, outs, skip=skip), **options)


def law_named(result: Identification, name: str) -> LawFit:
    return next(fit for fit in result.fits if fit.law == name)


# -- the laws, by hand ------------------------------------------------------------------

def a_segment(**overrides) -> Segment:
    """Three samples a minute apart, 30, 15 and 0 °F below the setpoint.
    (``predict`` reads the times, setpoints and readings; the output is unused.)"""
    base = dict(
        times=(0.0, 60.0, 120.0), sets=(225.0,) * 3, pits=(195.0, 210.0, 225.0),
        output=(0, 0, 0), rate=(0.0,) * 3,
    )
    return Segment(**{**base, **overrides})


def test_each_law_gives_what_its_formula_says():
    seg = a_segment()
    assert predict(seg, "p", {"band": 30.0}) == [100.0, 50.0, 0.0]
    assert predict(seg, "p", {"band": 60.0}) == [50.0, 25.0, 0.0]
    assert predict(seg, "p", {"band": 15.0}) == [100.0, 100.0, 0.0]
    assert predict(seg, "p_offset", {"band": 30.0, "offset": 20.0}) == [100.0, 70.0, 20.0]
    assert predict(seg, "p_offset", {"band": 30.0, "offset": 90.0}) == [100.0, 100.0, 90.0]
    # the integral rides on top: it starts at 30 % and, a minute 15 °F below target, takes up 7.5
    assert predict(seg, "pi", {"band": 30.0, "ki": 0.5, "i0": 30.0}) == [100.0, 80.0, 37.5]
    # the rate term takes off kd percent for every °F/min of climb
    climbing = a_segment(pits=(210.0, 210.0, 210.0), rate=(0.0, 5.0, 10.0))
    term = {"band": 30.0, "ki": 0.0, "kd": 4.0, "i0": 0.0}
    assert predict(climbing, "pi_rate", term) == [50.0, 30.0, 10.0]


def test_the_integral_is_frozen_while_the_output_is_pinned():
    """The simulator's anti-windup: nothing accumulates towards a limit the
    output is already at."""
    # two minutes 75 °F below target with the output flat out: an unfrozen integral would have taken up 37.5
    flat_out = a_segment(pits=(150.0, 150.0, 215.0))
    law = {"band": 30.0, "ki": 0.5, "i0": 0.0}
    assert predict(flat_out, "pi", law) == pytest.approx([100.0, 100.0, 100.0 / 3.0])
    # above target with some integral in hand it unwinds ...
    falling = a_segment(pits=(230.0, 235.0, 225.0))
    assert predict(falling, "pi", {**law, "i0": 60.0}) == pytest.approx([130.0 / 3.0, 80.0 / 3.0, 55.0])
    # ... and does not go below zero: 34 % less ten minutes of 0.5 % per °F above target is -16 %
    floor = a_segment(times=(0.0, 600.0, 660.0), pits=(235.0, 235.0, 215.0))
    assert predict(floor, "pi", {**law, "i0": 34.0}) == pytest.approx([2.0 / 3.0, 2.0 / 3.0, 100.0 / 3.0])


def test_a_cycle_time_evaluates_the_law_once_a_cycle_and_counts_whole_seconds():
    """Cycle 6 s: the fan's on-time is a whole number of seconds, so the output
    is 0, 17, 33, 50, 67, 83 or 100 %, and it moves only at the cycle's
    instants however the pit moves in between."""
    times = [float(t) for t in range(0, 25, 2)]                 # a sample every 2 s
    # 30 °F below target until 8 s, 27 °F until 16 s, then 15 °F: each change lands mid-cycle
    pits = [195.0 if t < 8 else 198.0 if t < 16 else 210.0 for t in times]
    seg = Segment(tuple(times), (225.0,) * 13, tuple(pits), (0,) * 13, (0.0,) * 13)
    out = predict(seg, "p", {"band": 30.0}, cycle_s=6.0)
    assert out[:6] == [100.0] * 6                                # instants at 0 and 6 s; held to 10 s
    assert out[6:9] == pytest.approx([250.0 / 3.0] * 3)          # 90 % is 5.4 s: 5 s, from 12 s
    assert out[9:] == [50.0] * 4                                 # 50 % is 3 s, from 18 s
    assert all(value * 6.0 == pytest.approx(round(value * 6.0)) for value in out)
    assert {round(value) for value in out} <= CYCLE_LEVELS
    # the instants are the device's, not the recording's: 3 s along, the same
    shifted = predict(seg, "p", {"band": 30.0}, cycle_s=6.0, phase_s=3.0)
    assert shifted[2:5] == [100.0] * 3 and shifted[5:8] == pytest.approx([250.0 / 3.0] * 3)
    assert shifted[8:] == [50.0] * 5


def test_pseudo_code_names_the_numbers():
    assert pseudo_code("p", {"band": 30.04}) == "output = clamp((set − pit) / 30.0 · 100, 0, 100)"
    assert pseudo_code("p_offset", {"band": 30.0, "offset": 33.0}) == "output = clamp(33.0 + (set − pit) / 30.0 · 100, 0, 100)"
    # an integral law has no offset of its own: the up-front share is where the integral starts
    assert pseudo_code("pi", {"band": 29.87, "ki": 0.1}, 3.25) == (
        "output = clamp((set − pit) / 29.9 · 100 + I, 0, 100)    ki = 0.100: I, starting at 3.2 %, grows by "
        "ki · (set − pit) per minute; kept in 0..100, and frozen while the output is pinned"
    )
    assert pseudo_code("pi_rate", {"band": 30.0, "ki": 0.0, "kd": 10.0}).startswith(
        "output = clamp((set − pit) / 30.0 · 100 + I − 10.0 · rate, 0, 100)"
    )


def test_a_richer_law_has_to_beat_every_simpler_one_to_be_selected():
    plain = LawFit("p", {}, 5.0, 0.0, 0)
    # 8 % and 0.4 points better: under both margins
    assert select_law([plain, LawFit("p_offset", {}, 4.6, 0.0, 0)]).law == "p"
    # 12 % and 0.6 points better
    assert select_law([plain, LawFit("p_offset", {}, 4.4, 0.0, 0)]).law == "p_offset"
    # sub-margin steps must not add up: 5.0 -> 4.6 -> 4.5 -> 4.4 is still the plain law
    steps = [plain] + [LawFit(law, {}, rmse, 0.0, 0) for law, rmse in zip(LAWS[1:], (4.6, 4.5, 4.4))]
    assert select_law(steps).law == "p"
    # a law that clearly beats every simpler one is selected even where the one before it fell short
    leap = [plain, LawFit("p_offset", {}, 4.6, 0.0, 0), LawFit("pi", {}, 0.3, 0.0, 0), LawFit("pi_rate", {}, 0.29, 0.0, 0)]
    assert select_law(leap).law == "pi"
    assert select_law([plain]).law == "p"


def test_the_report_says_what_was_chosen_and_how_it_is_decided():
    fits = [
        LawFit("p", {"band": 12.4}, 21.9, 0.22, 100),
        LawFit("p_offset", {"band": 30.0, "offset": 33.0}, 0.24, 1.0, 200, selected=True),
        LawFit("pi", {"band": 30.0, "ki": 0.0}, 0.24, 1.0, 300, at_limit=("ki",)),
    ]
    result = Identification(fits, samples=400, samples_seen=400, duration_s=3 * 3600.0 - 10.0, segments=1,
                            cycle_s=None, rate_window_min=5.0)
    text = format_identification(result)
    assert "400 closed-loop samples over 3.0 h; rate of rise over 5 min" in text
    assert "P + offset *" in text
    assert "selected P + offset: output = clamp(33.0 + (set − pit) / 30.0 · 100, 0, 100)" in text
    assert f"a law is selected only if it cuts the RMS error of every simpler law by over {SELECT_RATIO:.0%} and " \
           f"{SELECT_MARGIN_PP:g} points" in text
    assert "a law is selected only if" not in format_identification(result, legend=False)
    # the thinnings the header can name
    assert [ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 101, 111, 112)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "23rd", "101st", "111th", "112th",
    ]
    thinned = dataclasses.replace(result, stride=6, samples=400)
    assert format_identification(thinned).splitlines()[0].startswith("400 closed-loop samples over 3.0 h, every 6th fitted")
    # the row of an integral law has no offset: band, then a dash where the offset would be
    pi_row = next(line for line in text.splitlines() if line.startswith("PI "))
    assert pi_row.split() == ["PI", "30.00", "–", "0.00", "–", "0.24", "100%"]
    integral = LawFit("pi", {"band": 29.9, "ki": 0.1}, 0.24, 1.0, 300, selected=True, starts=(3.25,))
    report = format_identification(dataclasses.replace(result, fits=[integral]), legend=False)
    assert report.splitlines()[-1].startswith(
        "selected PI: output = clamp((set − pit) / 29.9 · 100 + I, 0, 100)    ki = 0.100: I, starting at 3.2 %,"
    )
    # how well the numbers are pinned down is reported in a line of its own ...
    tested = dataclasses.replace(integral, certainty={"band": "within 20 %", "ki": "within 50 %", "i0": "not within a factor of 2"})
    lines = format_identification(dataclasses.replace(result, fits=[tested]), legend=False).splitlines()
    assert lines[-1] == ("  how well the recording pins these down: the band within 20 %; ki within 50 %; "
                         "the integral's starting value not within a factor of 2")
    # ... and where it ran, it replaces the older note about a number at the end of its range
    at_end = dataclasses.replace(integral, at_limit=("ki",))
    assert format_identification(dataclasses.replace(result, fits=[at_end]), legend=False).splitlines()[-1] == \
        "  at the limit of its search range, so not really determined: ki"
    both = dataclasses.replace(at_end, certainty={"band": "within 20 %", "ki": "within 20 %", "i0": "within 5 points"})
    assert "at the limit of its search range" not in format_identification(dataclasses.replace(result, fits=[both]))


# -- splitting, rate of rise and refusing ------------------------------------------------

def test_the_recording_is_split_where_the_probe_was_open_or_jumped():
    # ten readings, the probe out for two samples, two more, then 250 °F for two samples (it
    # went back in hot), then twenty as it settles
    times = [float(k) for k in range(36)]
    pits = [200.0 + 0.1 * k for k in range(10)] + [None, None, 201.2, 201.3, 250.0, 250.5] \
        + [202.0 + 0.1 * k for k in range(20)]
    segs = segments_from_samples(times, [SET] * 36, pits, [50] * 36)
    # each jump (up at 14 s, back down at 16 s) takes the two good readings before and after it
    assert [(len(s), s.times[0]) for s in segs] == [(10, 0.0), (17, 19.0)]
    # a hole longer than two minutes splits a recording, and a stretch left with a single
    # sample is not returned: it cannot say anything
    holed = [float(t) for t in range(0, 100, 10)] + [1000.0] + [float(t) for t in range(2000, 2100, 10)]
    split = segments_from_samples(holed, [SET] * 21, [200.0] * 21, [50] * 21)
    assert [(len(s), s.times[0]) for s in split] == [(10, 0.0), (10, 2000.0)]
    # ... and so does a window the caller leaves out
    windowed = segments_from_samples(times, [SET] * 36, [200.0] * 36, [50] * 36, skip=[(10.0, 19.0)])
    assert [(len(s), s.times[0]) for s in windowed] == [(10, 0.0), (16, 20.0)]
    assert segments_from_samples(times, [SET] * 36, [None] * 36, [50] * 36) == []
    # a sample without a setpoint or without an output recorded does not count either
    sets = [SET] * 12 + [None] + [SET] * 23
    assert [len(s) for s in segments_from_samples(times, sets, [200.0] * 36, [50] * 36)] == [12, 23]
    fans = [50] * 12 + [None] + [50] * 23
    assert [len(s) for s in segments_from_samples(times, [SET] * 36, [200.0] * 36, fans)] == [12, 23]


def test_the_rate_of_rise_is_the_slope_of_the_last_five_minutes():
    times = [float(k) for k in range(0, 1900, 10)]
    pits = [200.0 + 0.005 * t if t < 1200 else 206.0 for t in times]   # 0.3 °F/min for 20 min, then flat
    rates = segments_from_samples(times, [SET] * len(times), pits, [50] * len(times))[0].rate
    assert tuple(rates[:2]) == (0.0, 0.0)                               # no line through fewer than three readings
    assert all(r == pytest.approx(0.3) for r in rates[2:120])
    assert all(0.0 < r < 0.3 for r in rates[121:150])                   # the kink is inside the window
    assert all(r == 0.0 for r in rates[150:])


def test_refuses_when_the_pit_probe_was_never_plugged_in():
    times, _ = a_pit(300)
    segments = segments_from_samples(times, [SET] * 300, [None] * 300, [0] * 300)
    assert segments == []
    with pytest.raises(IdentifyError, match=r"pit probe reads OPEN in all 300 samples"):
        identify(segments, samples_total=300, good_samples=0)
    # asked directly, without the recording's counts, it can only say what it was given
    with pytest.raises(IdentifyError, match="no sample has both a pit reading and an output"):
        identify(segments)


def test_refuses_when_the_windows_left_out_take_everything():
    times, pits = a_wander(300)
    with pytest.raises(IdentifyError, match=r"nothing left to fit: 300 samples have the pit probe plugged in"):
        fit_of(times, pits, made_with("proportional", times, pits), skip=[(0.0, 1e9)], good_samples=300)


def test_refuses_when_the_output_never_moves():
    times, pits = a_pit(300)
    with pytest.raises(IdentifyError, match=r"output stayed at 0 % for all 300 samples \(the fan was off the whole time\)"):
        fit_of(times, pits, [0] * 300)


def test_refuses_when_the_output_only_sits_at_its_limits():
    """Full fan up to the setpoint, then none: at a limit every law looks the same."""
    times, pits = a_crossing()
    outs = [100 if pit < SET else 0 for pit in pits]
    outs[150] = 50                                       # the one sample the poller caught in between
    with pytest.raises(IdentifyError, match=r"pinned at 0 or 100 % in 299 of 300 samples and only 1 is in between"):
        fit_of(times, pits, outs)


def test_refuses_on_too_little_time_even_with_plenty_of_samples():
    times, pits = a_wander(200, dt=3.0)
    outs = made_with("proportional", times, pits)
    assert min(outs) > 0 and max(outs) < 100
    with pytest.raises(IdentifyError, match=r"too little closed-loop data: 200 samples over 10 min .*need at least 60 samples and 20 min"):
        fit_of(times, pits, outs)


def test_refuses_when_the_windows_left_out_take_almost_everything():
    times, pits = a_wander(300)
    with pytest.raises(IdentifyError, match=r"too little closed-loop data: 59 samples over 10 min"):
        fit_of(times, pits, made_with("proportional", times, pits), skip=[(0.0, 2400.0)])


def test_an_unknown_law_and_a_bad_cycle_are_named():
    times, pits = a_wander(200)
    outs = made_with("proportional", times, pits)
    with pytest.raises(IdentifyError, match=r"unknown law\(s\): pid \(known: p, p_offset, pi, pi_rate\)"):
        fit_of(times, pits, outs, laws=("pid",))
    with pytest.raises(IdentifyError, match=r"no law to fit: name at least one of p, p_offset, pi, pi_rate"):
        fit_of(times, pits, outs, laws=())
    # a cycle too short to be resolved between two samples, or none at all
    with pytest.raises(IdentifyError, match=r"--cycle must be a number of seconds, 0\.1 or more"):
        fit_of(times, pits, outs, cycle_s=0.0)
    with pytest.raises(IdentifyError, match=r"--cycle must be a number of seconds, 0\.1 or more"):
        fit_of(times, pits, outs, cycle_s=-6.0)


def test_the_recording_is_asked_how_well_it_pins_the_numbers_down():
    """Each number is forced from its fitted value with the others re-fitted.
    A band that is truly 30 °F does not survive being moved 20 %."""
    times, pits = a_wander(130)
    outs = made_with("proportional", times, pits)
    result = fit_of(times, pits, outs, laws=("p",))
    assert result.selected.params["band"] == pytest.approx(BAND, abs=0.5)
    assert result.selected.certainty == {"band": "within 20 %"}
    assert "how well the recording pins these down: the band within 20 %" in format_identification(result)
    # not asked: nothing is claimed, and the fit itself is the same
    quick = fit_of(times, pits, outs, laws=("p",), pin_down=False)
    assert quick.selected.certainty == {} and quick.selected.params == result.selected.params
    assert "pins these down" not in format_identification(quick)


def test_the_command_refuses_when_nothing_was_controlled(tmp_path, capsys):
    """The pit probe left unplugged, as the dashboard records it: OPEN, and the fan off."""
    sim = build_sim(controller=get_controller("proportional"), dt=2.0)
    sim.probe("cook").plugged = False
    sim.run(80.0)
    assert all(row["cook"] is None and row["output_pct"] == 0 for row in sim.rows)
    db = tmp_path / "history.db"
    samples = record(db, sim)
    written = db.stat().st_mtime_ns

    assert main(["identify", "--db", str(db)]) == 2

    assert f"pit probe reads OPEN in all {samples} samples" in capsys.readouterr().err
    assert db.stat().st_mtime_ns == written
    assert sorted(p.name for p in tmp_path.iterdir()) == ["history.db"]   # read-only: nothing left beside it


# -- the fits (slow) ----------------------------------------------------------------------

@pytest.mark.slow
def test_a_plain_proportional_law_is_recognised():
    times, pits = a_pit()
    result = fit_of(times, pits, made_with("proportional", times, pits))
    assert result.selected.law == "p"
    assert result.selected.params["band"] == pytest.approx(BAND, abs=0.5)
    assert result.selected.rmse < 0.5          # whole percent: 0.29 points is the floor
    # the richer laws cannot show what is not there
    assert law_named(result, "p_offset").params["offset"] == pytest.approx(0.0, abs=0.5)
    assert law_named(result, "pi").params["ki"] == pytest.approx(0.0, abs=0.02)


@pytest.mark.slow
def test_an_offset_is_told_from_a_narrower_band():
    times, pits = a_pit()
    result = fit_of(times, pits, made_with("bias", times, pits))
    assert result.selected.law == "p_offset"
    assert result.selected.params["band"] == pytest.approx(BAND, abs=1.0)
    assert result.selected.params["offset"] == pytest.approx(BIAS, abs=1.0)
    assert law_named(result, "p").rmse > 10.0   # a plain P has to make do with a band near 12, and still misses


@pytest.mark.slow
def test_integral_action_is_recognised():
    times, pits = a_pit()
    result = fit_of(times, pits, made_with("pi", times, pits))
    assert result.selected.law == "pi"
    assert result.selected.params["band"] == pytest.approx(BAND, abs=2.0)
    assert result.selected.params["ki"] == pytest.approx(KI, abs=0.03)
    assert law_named(result, "p_offset").rmse - result.selected.rmse > SELECT_MARGIN_PP
    assert law_named(result, "pi_rate").params["kd"] == pytest.approx(0.0, abs=1.0)   # no rate term in the data
    # the integral is frozen while the output is flat out, and saturates later: it is active for
    # under an hour, and where it started is not pinned down. The tool must say so, not print a number as if it were.
    assert result.selected.certainty["band"] == "within 20 %" and result.selected.certainty["ki"] == "within 20 %"
    assert result.selected.certainty["i0"] != "within 5 points"
    assert result.selected.starts[0] == pytest.approx(0.0, abs=30.0)


@pytest.mark.slow
def test_a_rate_term_is_found_when_the_output_leans_against_the_climb():
    times, pits = a_pit()
    seg = segments_from_samples(times, [SET] * len(times), pits, [50] * len(times))[0]
    outs = by_hand(times, pits, seg.rate, band=BAND, offset=20.0, kd=10.0)
    result = fit_of(times, pits, outs)
    assert result.selected.law == "pi_rate"
    assert result.selected.params["kd"] == pytest.approx(10.0, abs=1.0)
    assert result.selected.params["band"] == pytest.approx(BAND, abs=3.0)
    # the 20 % put up front is where the integral starts, and every number here is held tightly
    assert result.selected.starts[0] == pytest.approx(20.0, abs=1.0)
    assert result.selected.certainty == {"band": "within 20 %", "ki": "within 20 %", "kd": "within 20 %", "i0": "within 5 points"}
    assert law_named(result, "p_offset").rmse > 3.0   # without the term, the climb is a wobble of a few points


@pytest.mark.slow
def test_still_right_on_noisier_readings_and_whole_percent_output():
    times, pits = a_pit(noise=0.6, seed=7)
    outs = made_with("bias", times, pits)
    assert all(isinstance(value, int) for value in outs)
    result = fit_of(times, pits, outs)
    assert result.selected.law == "p_offset"
    assert result.selected.params["band"] == pytest.approx(BAND, abs=2.5)
    assert result.selected.params["offset"] == pytest.approx(BIAS, abs=2.0)


@pytest.mark.slow
def test_a_cycle_time_of_whole_seconds_is_undone():
    """On the device the law would run once a cycle and the fan's on-time be
    whole seconds. Read without being told the cycle, that alone leaves a
    residual; told, the law comes out right."""
    times = [float(t) for t in range(1500)]
    rng = random.Random(3)
    pits = [round(SET - 40.0 * math.exp(-t / 600.0) + rng.gauss(0.0, 0.12), 1) for t in times]
    cycled = []
    for k, pit in enumerate(pits):
        if k % 6 == 0:
            value = min(max(100.0 * (SET - pit) / BAND, 0.0), 100.0)   # the law's percent, clamped
            on_s = round(value * 6.0 / 100.0)                           # ... as whole seconds of a 6 s cycle
        cycled.append(round_half_up(on_s * 100.0 / 6.0))
    assert set(cycled) == CYCLE_LEVELS - {0}       # the pit never gets within 2.5 °F, so the fan is never off
    blind = fit_of(times, pits, cycled)
    told = fit_of(times, pits, cycled, cycle_s=6.0)
    assert told.selected.law == "p" and told.selected.params["band"] == pytest.approx(BAND, abs=2.0)
    assert told.selected.rmse < 1.0 < blind.selected.rmse


@pytest.mark.slow
@pytest.mark.parametrize("name, law, parameter, value, tolerance, certainty", [
    ("proportional", "p", "band", BAND, 1.5, {"band": "within 20 %"}),
    ("bias", "p_offset", "offset", BIAS, 2.0, {"band": "within 20 %", "offset": "within 20 %"}),
    ("pi", "pi", "ki", KI, 0.035, {"band": "within 20 %", "ki": "within 20 %", "i0": "within 5 points"}),
])
def test_a_closed_loop_cook_is_read_right(name, law, parameter, value, tolerance, certainty):
    """The whole loop: the law drives the plant, the plant answers, and the
    recording of that is read back."""
    times, sets, pits, outs = a_closed_loop_cook(name)
    result = identify(segments_from_samples(times, sets, pits, outs))
    assert result.selected.law == law
    assert result.selected.params[parameter] == pytest.approx(value, abs=tolerance)
    assert result.selected.within_2pp > 0.95
    assert result.selected.certainty == certainty


@pytest.mark.slow
def test_a_long_recording_is_thinned_and_says_so():
    times, pits = a_pit(5000)
    result = fit_of(times, pits, made_with("proportional", times, pits))
    assert (result.stride, result.samples, result.samples_seen) == (2, 2500, 5000)
    assert "5000 closed-loop samples over 13.9 h, every 2nd fitted" in format_identification(result)
    assert result.selected.law == "p" and result.selected.params["band"] == pytest.approx(BAND, abs=0.5)


@pytest.mark.slow
def test_a_recorded_cook_is_read_from_the_command_line(capsys, recording):
    """The shared recording: the default P controller, cold to 250 °F, 225 °F from 2 h."""
    assert main(["identify", "--db", str(recording.db)]) == 0
    out = capsys.readouterr().out
    assert "selected P: output = clamp((set − pit) / " in out
    assert recording.db.stat().st_mtime == recording.mtime
