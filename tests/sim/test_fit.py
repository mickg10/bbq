"""`sim fit`: what a recording says at a glance, and the plant learned from it.

The ``recording`` fixture (conftest) is three hours of the simulator playing a
cooker whose parameters are known and are not the defaults, with a brisket on
food1. Only ratios to the shell's conductance show in temperatures, so with
``ua_pit`` held at its default the fit should land on the same *ratios* the
recording was made with:

    c_pit  60 x 70 / 90 = 46.7        q_max  26000 x 70 / 90 = 20222
    tau_fire 300                      draft_min 0.07 (weakly identified)
"""

from __future__ import annotations

import contextlib
import random

import pytest

from sim.fit import (
    BOUND_OCTAVES,
    DEFAULT_FIT,
    MIN_EXCITATION_F,
    FitError,
    _nelder_mead,
    estimate_ambient,
    fit_plant,
    format_fit,
    load_params,
    probe_noise,
    robust_range,
    write_params,
)
from sim.food import preset
from sim.history import Track, load_track, open_history
from sim.plant import PlantParams
from sim.probes import Probe
from tests.sim.conftest import AIR_F

#: what is in the cooker during the recording: the fit has to be told, or the
#: meat's heat comes out of the fire's parameters instead
BRISKET = {"food1": preset("brisket")}


def _track(recording) -> Track:
    with contextlib.closing(open_history(recording.db)) as conn:
        return load_track(conn)


def _idle_track(hours: float = 2.0, air: float = 76.5, seed: int = 0) -> Track:
    """A cooker with no fire in it: the pit sits at air temperature."""
    probe = Probe(sigma_f=0.15, rng=random.Random(seed))
    times = [float(i) * 5 for i in range(int(hours * 3600 / 5))]
    readings = [probe.step(5.0, air, air) / 10 for _ in times]
    return Track(
        1_790_000_000, times,
        {"cook": readings, "food1": [None] * len(times), "food2": [None] * len(times), "food3": [None] * len(times)},
        {probe: [190.0] * len(times) for probe in ("cook", "food1", "food2", "food3")},
        [100] * len(times),
    )


# -- what a recording says -----------------------------------------------------

def test_what_a_heat_up_recording_says(recording):
    track = _track(recording)
    assert robust_range(track.readings["cook"]) > 60.0  # 68 to about 235 °F
    assert estimate_ambient(track) == pytest.approx(AIR_F, abs=0.3)  # a cold start begins at air temperature
    assert 0.10 < probe_noise(track.readings["cook"]) < 0.22  # made with sigma 0.15


def test_what_an_idle_recording_says():
    track = _idle_track()
    assert robust_range(track.readings["cook"]) < 1.0 < MIN_EXCITATION_F
    assert estimate_ambient(track) == pytest.approx(76.5, abs=0.1)  # all of it is air temperature
    assert 0.10 < probe_noise(track.readings["cook"]) < 0.22


def test_probe_noise_against_known_levels():
    def readings(sigma: float, seed: int = 1) -> list[float]:
        probe = Probe(sigma_f=sigma, rng=random.Random(seed))
        return [probe.step(1.0, 200.0, 70.0) / 10 for _ in range(3000)]

    assert probe_noise(readings(0.0)) == 0.0
    assert probe_noise(readings(0.15)) == pytest.approx(0.15, abs=0.02)
    assert probe_noise(readings(0.5)) == pytest.approx(0.5, abs=0.04)
    spiked = readings(0.15)
    spiked[1000] = 300.0  # a plug transient is not noise
    assert probe_noise(spiked) == pytest.approx(0.15, abs=0.02)
    assert probe_noise([75.0] * 50) == 0.0  # too little to say
    gappy = [None, 75.1, None, 75.0, 75.3, 74.9, 75.1, 75.0, 75.2, 74.8] * 10
    assert 0.15 < probe_noise(gappy) < 0.25  # open samples are simply skipped


def test_robust_range_is_the_tenths_that_are_not_a_spike_or_a_dip():
    assert robust_range([200.0] * 50 + [290.0] * 2 + [150.0] * 2) == 0.0
    assert robust_range(list(range(0, 101))) == 80.0
    assert robust_range([None, None]) == 0.0


# -- the optimiser ---------------------------------------------------------------

def test_nelder_mead_finds_a_minimum():
    center = [1.5, -2.0, 0.25]
    x, value, evaluations = _nelder_mead(
        lambda p: sum((a - b) ** 2 for a, b in zip(p, center)), [0.0] * 3, step=0.5, max_evaluations=300
    )
    assert x == pytest.approx(center, abs=1e-2) and value < 1e-4 and evaluations <= 300

    def banana(p):  # a curved valley, minimum at (1, 1)
        return (1 - p[0]) ** 2 + 100 * (p[1] - p[0] ** 2) ** 2

    x, value, _ = _nelder_mead(banana, [-1.0, 1.0], step=0.5, max_evaluations=400)
    assert x == pytest.approx((1.0, 1.0), abs=0.05) and value < 0.5


def test_nelder_mead_stops_when_the_evaluations_run_out():
    evaluations = []
    x, value, spent = _nelder_mead(lambda p: evaluations.append(p) or 1.0, [0.0, 0.0], step=0.5, max_evaluations=50)
    assert spent <= 50 + 2 and len(evaluations) == spent and value == 1.0
    assert BOUND_OCTAVES == 3.0


# -- fitting ---------------------------------------------------------------------

def test_an_idle_recording_is_not_fitted():
    track = _idle_track()
    progress = []
    result = fit_plant(track, ambient_f=None, dt=10.0, progress=lambda n, r: progress.append(n))
    assert result.identified is False and "no heat-up to learn from" in result.reason
    assert result.params == PlantParams() and result.before == result.after
    assert (result.evaluations, result.octaves, result.fitted) == (0, {}, ())
    assert result.ambient_f == pytest.approx(76.5, abs=0.1) and progress == []
    assert result.noise_f == {"cook": pytest.approx(0.15, abs=0.03)}
    text = format_fit(result)
    assert "air temperature 76.5 °F" in text and "no heat-up to learn from" in text
    assert "Plant parameters left as they were" in text and "fitting" not in text


def test_fit_refuses_what_it_cannot_do(recording):
    track = _track(recording)
    with pytest.raises(FitError, match="nothing to fit"):
        fit_plant(track, ambient_f=AIR_F, fitted=())
    with pytest.raises(FitError, match="not plant parameters: thrust"):
        fit_plant(track, ambient_f=AIR_F, fitted=("c_pit", "thrust"))
    with pytest.raises(FitError, match="must start above zero"):
        fit_plant(
            track, ambient_f=AIR_F, params=PlantParams(draft_min=0.0), fitted=("draft_min",), max_evaluations=2
        )


def test_progress_is_reported_every_twenty_evaluations(recording):
    track = _track(recording)
    seen = []
    result = fit_plant(
        track, ambient_f=AIR_F, foods=BRISKET, dt=30.0, max_evaluations=45, progress=lambda n, r: seen.append(n)
    )
    assert result.identified and result.evaluations <= 45 + 5
    assert seen == [20, 40]


@pytest.mark.slow
def test_meat_the_fit_was_not_told_about_comes_out_of_the_fire(recording):
    """Without the brisket, the replay runs hot and the fit explains it with
    a smaller fire. The pit still fits; the parameters are wrong."""
    track = _track(recording)
    result = fit_plant(track, ambient_f=AIR_F, dt=20.0, max_evaluations=200, exclude=[(0.0, 1800.0)])
    assert result.after["probes"]["cook"]["rmse"] < 1.0
    assert result.params.q_max < 26_000.0 * 70 / 90 * 0.96


@pytest.mark.slow
def test_fit_recovers_the_ratios_the_recording_was_made_with(recording):
    track = _track(recording)
    result = fit_plant(
        track, ambient_f=AIR_F, foods=BRISKET, dt=20.0, max_evaluations=400,
        exclude=[(0.0, 1800.0)],  # how far the coals had come up at the first sample is a guess
    )
    assert result.identified and result.fitted == DEFAULT_FIT
    assert set(result.octaves) == {"c_pit", "q_max", "tau_fire", "draft_min"}
    assert "ua_pit" not in result.octaves  # the scale of BTUs is held, not fitted
    assert result.params.ua_pit == PlantParams().ua_pit
    # the recording's ratios, on the default's scale
    assert result.params.c_pit == pytest.approx(60.0 * 70 / 90, rel=0.05)
    assert result.params.q_max == pytest.approx(26_000.0 * 70 / 90, rel=0.05)
    assert result.params.tau_fire == pytest.approx(300.0, rel=0.15)
    assert 0.035 < result.params.draft_min < 0.1  # weakly identified, and said to be
    assert result.params.c_pit / result.params.ua_pit == pytest.approx(60.0 / 90.0, rel=0.04)
    assert result.params.q_max / result.params.ua_pit == pytest.approx(26_000.0 / 90.0, rel=0.03)
    assert result.before["probes"]["cook"]["rmse"] > 5.0
    assert result.after["probes"]["cook"]["rmse"] < 0.6  # the noise floor
    assert result.evaluations <= 400 + 5 and result.seconds < 60
    text = format_fit(result)
    assert "fitting c_pit, q_max, tau_fire, draft_min in open loop" in text
    assert "pit RMSE" in text and "draft_min" in text and "octaves" in text


# -- parameter files --------------------------------------------------------------

def test_a_parameters_file_round_trips(tmp_path):
    idle = fit_plant(_idle_track(), ambient_f=None)
    path = write_params(tmp_path / "fit" / "cooker.yaml", idle, samples=500)
    assert path == tmp_path / "fit" / "cooker.yaml"
    text = path.read_text()
    assert text.startswith("# Cooker parameters, written by `python -m sim fit`.")
    assert "fitted:" not in text and "samples: 500" in text  # nothing was fitted, so nothing is claimed
    params, ambient = load_params(path)
    assert params == PlantParams() and ambient == pytest.approx(76.5, abs=0.1)
    # a bare mapping of plant parameters is accepted too
    bare = tmp_path / "bare.yaml"
    bare.write_text("c_pit: 55\nua_pit: 80\n")
    assert load_params(bare) == (PlantParams(c_pit=55.0, ua_pit=80.0), None)
    junk = tmp_path / "junk.yaml"
    for content in ("- a\n- list\n", "plant: 5\n", "c_pit: wide\n"):
        junk.write_text(content)
        with pytest.raises(FitError):
            load_params(junk)
    with pytest.raises(FitError, match="cannot read"):
        load_params(tmp_path / "missing.yaml")


def test_a_fitted_file_says_what_was_fitted(tmp_path, recording):
    track = _track(recording)
    result = fit_plant(
        track, ambient_f=AIR_F, foods=BRISKET, fitted=("tau_fire",), dt=30.0, max_evaluations=12
    )
    path = write_params(tmp_path / "cooker.yaml", result, samples=len(track.times))
    document = path.read_text()
    assert "fitted:\n- tau_fire\n" in document and "pit_rmse_f:" in document
    assert "samples: 2160" in document and f"ambient_f: {AIR_F}" in document
    params, ambient = load_params(path)
    assert params == result.params and ambient == AIR_F
