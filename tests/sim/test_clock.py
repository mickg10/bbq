"""The simulated clock: exact in headless mode, wall-paced in realtime."""

from __future__ import annotations

import pytest

from sim.clock import SimClock


def test_headless_time_moves_only_by_whole_advances():
    clock = SimClock()
    for _ in range(10):
        clock.advance(1.0)
    assert clock.t == 10.0
    # nothing is ever "owed" to the wall clock
    assert clock.steps_due(1.0) == 0
    with pytest.raises(ValueError):
        clock.advance(-1.0)


def test_speed_and_start_are_validated():
    with pytest.raises(ValueError):
        SimClock(speed=0)
    with pytest.raises(ValueError):
        SimClock(t=-1.0)


def test_realtime_owes_steps_at_the_configured_speed():
    wall = [0.0]
    clock = SimClock(speed=60.0, realtime=True, monotonic=lambda: wall[0])
    assert clock.steps_due(1.0) == 0  # the first call only anchors
    wall[0] = 2.5                    # 2.5 s of wall time = 150 s of sim time
    assert clock.steps_due(1.0, max_backlog=1000.0) == 150
    for _ in range(150):
        clock.advance(1.0)
    assert clock.t == 150.0
    assert clock.steps_due(1.0) == 0
    wall[0] += 1.0 / 60.0            # one more second of sim time
    assert clock.steps_due(1.0) == 1
    clock.advance(1.0)
    # a short pause is caught up in full; two minutes is the default ceiling
    wall[0] += 1.5                   # 1.5 s of wall = 90 s of sim time
    assert clock.steps_due(1.0) == 90
    for _ in range(90):
        clock.advance(1.0)
    assert clock.steps_due(1.0) == 0


def test_realtime_a_long_stall_skips_ahead_instead_of_replaying_hours():
    wall = [0.0]
    clock = SimClock(speed=1.0, realtime=True, monotonic=lambda: wall[0])
    clock.steps_due(1.0)             # anchor
    wall[0] = 3600.0                 # the laptop slept for an hour
    assert clock.steps_due(1.0, max_backlog=120.0) == 120
    for _ in range(120):
        clock.advance(1.0)
    assert clock.t == 120.0
    # and it is back in step with the wall clock, not 58 minutes behind
    assert clock.steps_due(1.0) == 0


def test_realtime_steps_are_whole_so_the_trajectory_is_the_fixed_step_one():
    wall = [0.0]
    clock = SimClock(speed=10.0, realtime=True, monotonic=lambda: wall[0])
    clock.steps_due(1.0)
    taken = []
    for _ in range(25):
        wall[0] += 0.37              # an awkward pace: 3.7 s of sim time per look
        for _ in range(clock.steps_due(1.0)):
            clock.advance(1.0)
            taken.append(clock.t)
    assert taken == list(range(1, len(taken) + 1))  # every step exactly 1 s
    assert len(taken) >= 90
