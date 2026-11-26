"""Probe model: lag, seeded noise, tenths of a degree, OPEN."""

from __future__ import annotations

import math
import random
import statistics

import pytest

from sim.probes import Probe, to_tenths


def test_readings_are_tenths_of_a_degree_rounded_half_up():
    assert to_tenths(225.0) == 2250
    assert to_tenths(75.54) == 755
    assert to_tenths(75.55) == 756
    assert to_tenths(75.59) == 756


def test_without_noise_the_probe_reports_the_truth_quantized():
    probe = Probe(sigma_f=0.0)
    assert probe.step(1.0, 180.0, 70.0) == 1800
    assert probe.reading_f == 180.0
    probe.settle(225.0)
    assert probe.reading == 2250


def test_the_probe_lags_the_truth_by_tau_s():
    probe = Probe(tau_s=20.0, sigma_f=0.0)
    probe.settle(70.0)
    probe.step(20.0, 270.0, 70.0)
    assert probe.reading_f == pytest.approx(70.0 + 200.0 * (1 - math.exp(-1.0)), abs=0.05)
    for _ in range(60):
        probe.step(10.0, 270.0, 70.0)
    assert probe.reading == 2700


def test_a_probe_with_no_lag_follows_the_truth_exactly():
    probe = Probe(tau_s=0.0, sigma_f=0.0)
    assert probe.step(1.0, 123.4, 70.0) == 1234


def test_the_same_seed_gives_the_same_readings():
    def stream(seed: int) -> list[int]:
        probe = Probe(rng=random.Random(seed))
        return [probe.step(1.0, 225.0, 70.0) for _ in range(200)]

    assert stream(7) == stream(7)
    assert stream(7) != stream(8)


def test_the_noise_is_gaussian_with_the_configured_width():
    probe = Probe(sigma_f=0.15, rng=random.Random(3))
    errors = [probe.step(1.0, 225.0, 70.0) - 2250 for _ in range(4000)]  # in tenths
    assert abs(statistics.mean(errors)) < 0.1
    assert 1.3 < statistics.pstdev(errors) < 1.7  # 0.15 °F = 1.5 tenths


def test_unplugged_reads_open_and_warms_back_in_when_plugged():
    probe = Probe(sigma_f=0.0)
    probe.settle(200.0)
    probe.plugged = False
    assert probe.step(30.0, 200.0, 70.0) is None  # OPEN, whatever the truth is
    assert probe.reading_f is None
    for _ in range(120):
        probe.step(10.0, 200.0, 70.0)  # lying in the 70 °F air
    probe.plugged = True
    first = probe.step(0.0, 200.0, 70.0)
    assert first is not None and abs(first / 10.0 - 70.0) < 0.6  # no snap to the truth
    second = probe.step(5.0, 200.0, 70.0)
    assert second is not None and 70.0 < second / 10.0 < 120.0


def test_bad_parameters_are_rejected():
    with pytest.raises(ValueError):
        Probe(tau_s=-1.0)
    with pytest.raises(ValueError):
        Probe(sigma_f=-0.1)
