"""The baseline control law: proportional demand, cycle-time PWM, lid detect, ramp."""

from __future__ import annotations

import random

import pytest

from sim.controllers import get_controller
from sim.controllers.base import ControlConfig, Mode, duty_in_step
from sim.controllers.proportional import ProportionalController, proportional_demand

NO_FOOD = (None, None, None)


def _cfg(**overrides) -> ControlConfig:
    base = dict(setpoint_f=225.0, hold_setpoint_f=200.0, propband_f=30.0, cyctime_s=6)
    return ControlConfig(**{**base, **overrides})


def _feed(law: ProportionalController, temps, cfg: ControlConfig, start: float = 0.0):
    out = None
    for i, temp in enumerate(temps):
        out = law.step(start + i, temp, NO_FOOD, cfg)
    return out


# -- demand -------------------------------------------------------------------

def test_demand_is_proportional_inside_the_band():
    law = ProportionalController()

    def fan(pit: float) -> int:
        return law.step(0.0, pit, NO_FOOD, _cfg()).fan_pct

    assert fan(195.0) == 100  # a whole band below: full
    assert fan(150.0) == 100
    assert fan(200.0) == 83   # (225 − 200) / 30
    assert fan(210.0) == 50
    assert fan(225.0) == 0    # at target: stopped
    assert fan(240.0) == 0    # above it: still stopped


def test_a_zero_band_is_plain_on_off():
    law = ProportionalController()
    cfg = _cfg(propband_f=0.0)
    assert law.step(0.0, 224.9, NO_FOOD, cfg).fan_pct == 100
    assert law.step(0.0, 225.0, NO_FOOD, cfg).fan_pct == 0
    assert (proportional_demand(5.0, 0.0), proportional_demand(-5.0, 0.0)) == (100, 0)


def test_hold_mode_drives_to_cookhold_and_skips_the_ramp():
    law = ProportionalController()
    cfg = _cfg(ramp=1, mode=Mode.HOLD, food_set_f=(203.0, None, None))
    out = law.step(0.0, 190.0, (150.0, None, None), cfg)
    assert (out.setpoint_f, out.fan_pct, out.ramp_active) == (200.0, 33, False)


def test_shutdown_mode_stops_the_fan():
    out = ProportionalController().step(0.0, 150.0, NO_FOOD, _cfg(mode=Mode.SHUTDOWN))
    assert (out.fan_pct, out.setpoint_f) == (0, 225.0)


def test_no_pit_probe_means_no_demand():
    out = ProportionalController().step(0.0, None, NO_FOOD, _cfg())
    assert out.fan_pct == 0 and not out.lid_hold


# -- ramp ---------------------------------------------------------------------

def test_ramp_slides_the_target_down_over_the_last_span():
    law = ProportionalController()
    cfg = _cfg(ramp=1, food_set_f=(203.0, 180.0, 180.0))
    far = law.step(0.0, 150.0, (120.0, None, None), cfg)
    assert (far.setpoint_f, far.ramp_active) == (225.0, False)
    halfway = law.step(0.0, 150.0, (188.0, None, None), cfg)  # 15 °F short
    assert halfway.setpoint_f == pytest.approx(203.0 + (225.0 - 203.0) * 0.5)
    assert halfway.ramp_active
    at_target = law.step(0.0, 150.0, (204.0, None, None), cfg)
    assert (at_target.setpoint_f, at_target.ramp_active) == (203.0, True)


def test_ramp_needs_a_plugged_probe_and_a_lower_food_target():
    law = ProportionalController()
    open_probe = law.step(0.0, 150.0, (150.0, None, None), _cfg(ramp=2))
    assert (open_probe.setpoint_f, open_probe.ramp_active) == (225.0, False)
    higher = law.step(0.0, 150.0, (249.0, None, None), _cfg(ramp=1, food_set_f=(250., None, None)))
    assert (higher.setpoint_f, higher.ramp_active) == (225.0, False)


def test_ramp_is_off_unless_a_probe_is_chosen():
    out = ProportionalController().step(0.0, 150.0, (204.0, None, None), _cfg())
    assert out.setpoint_f == 225.0 and not out.ramp_active


# -- cycle-time PWM -----------------------------------------------------------

def test_the_fan_runs_fan_percent_of_each_cycle():
    # 50 % of a 6 s cycle: three seconds on, three off
    assert [duty_in_step(t, 1.0, 50, 6.0) for t in range(6)] == [1, 1, 1, 0, 0, 0]
    assert sum(duty_in_step(t, 1.0, 50, 6.0) for t in range(60)) / 60 == pytest.approx(0.5)
    assert (duty_in_step(0, 1.0, 0, 6.0), duty_in_step(0, 1.0, 100, 6.0)) == (0.0, 1.0)
    # 67 %: 4.02 s on
    assert [round(duty_in_step(t, 1.0, 67, 6.0), 2) for t in range(6)] == [1, 1, 1, 1, 0.02, 0]


def test_a_step_shorter_than_the_cycle_gets_its_share():
    # [2.75, 3.25) straddles the off point at 3.0: a quarter second of each
    assert duty_in_step(2.75, 0.5, 50, 6.0) == pytest.approx(0.5)
    assert duty_in_step(3.0, 1.0, 50, 6.0) == 0.0


def test_a_step_longer_than_the_cycle_averages_out():
    # 7 s from t = 0 at 50 % of 6 s: 3 s, then the next cycle's first second
    assert duty_in_step(0.0, 7.0, 50, 6.0) == pytest.approx(4.0 / 7.0)


def test_no_cycle_time_means_a_continuous_output():
    assert duty_in_step(0.0, 1.0, 37, 0.0) == pytest.approx(0.37)
    assert duty_in_step(123.4, 5.0, 37, 0.0) == pytest.approx(0.37)


def test_windows_line_up_on_t_zero():
    assert duty_in_step(60.0, 1.0, 50, 6.0) == 1.0
    assert duty_in_step(63.0, 1.0, 50, 6.0) == 0.0


# -- open-lid detect ----------------------------------------------------------

def test_a_fast_drop_is_taken_for_the_lid_and_holds_the_fan_off():
    law = ProportionalController()
    cfg = _cfg()
    steady = _feed(law, [220.0] * 120, cfg)
    assert not steady.lid_hold and steady.fan_pct == 17  # (225 − 220) / 30
    drop = [220.0 - 0.5 * k for k in range(1, 61)]  # 30 °F in a minute
    falling = _feed(law, drop, cfg, start=120.0)
    assert falling.lid_hold and falling.fan_pct == 0  # it would be 100 otherwise
    # the lid is shut on a 190 °F pit: still held for a good while, then let go
    drop_end = 120.0 + len(drop) - 1
    released_at = None
    for t in range(180, 900):
        out = law.step(float(t), 190.0, NO_FOOD, cfg)
        if not out.lid_hold:
            released_at = t
            break
    assert released_at is not None
    assert drop_end + 100 <= released_at <= drop_end + 260
    assert out.fan_pct == 100


def test_the_detector_starts_quickly():
    law = ProportionalController()
    cfg = _cfg()
    _feed(law, [220.0] * 120, cfg)
    outs = [law.step(120.0 + i, 220.0 - 0.5 * (i + 1), NO_FOOD, cfg) for i in range(60)]
    first = next(i for i, out in enumerate(outs) if out.lid_hold)
    assert 5 <= first <= 20  # seconds after the drop began
    assert law.last_rate_f_per_min == pytest.approx(-30.0)


def test_lid_detect_can_be_switched_off():
    law = ProportionalController()
    cfg = _cfg(opendetect=False)
    out = _feed(law, [220.0 - 0.5 * k for k in range(61)], cfg)
    assert not out.lid_hold and out.fan_pct == 100


def test_a_slow_drift_is_not_the_lid():
    law = ProportionalController()
    out = _feed(law, [220.0 - 0.05 * k for k in range(601)], _cfg())  # 3 °F/min
    assert not out.lid_hold and out.fan_pct == 100


def test_a_rising_pit_is_not_the_lid_and_reset_forgets_the_window():
    law = ProportionalController()
    cfg = _cfg()
    rising = _feed(law, [150.0 + 0.5 * k for k in range(61)], cfg)
    assert not rising.lid_hold and rising.fan_pct == 100
    _feed(law, [220.0 - 0.5 * k for k in range(61)], cfg, start=1000.0)
    assert law.step(1100.0, 190.0, NO_FOOD, cfg).lid_hold
    law.reset()
    assert not law.step(1100.0, 190.0, NO_FOOD, cfg).lid_hold


def test_probe_noise_never_trips_the_detector_even_at_the_very_start():
    """Regression: with a second or two of history, a tenth of a degree of
    noise used to read as degrees per minute and hold the fan off."""
    rng = random.Random(11)
    law = ProportionalController()
    cfg = _cfg()
    noisy = [215.0 + rng.gauss(0.0, 0.15) for _ in range(1800)]
    outs = [law.step(float(i), round(temp, 1), NO_FOOD, cfg) for i, temp in enumerate(noisy)]
    assert not any(out.lid_hold for out in outs)
    assert all(out.fan_pct > 0 for out in outs)  # 10 °F under target, in the band


# -- bias and the pi variant --------------------------------------------------

def test_bias_lifts_the_whole_curve():
    assert proportional_demand(30.0, 30.0) == 100
    assert proportional_demand(30.0, 30.0, bias_pct=20.0) == 100  # already flat out
    assert proportional_demand(0.0, 30.0, bias_pct=20.0) == 20    # at target it remains
    assert proportional_demand(-3.0, 30.0, bias_pct=20.0) == 10   # 3 over: 20 − 10
    assert proportional_demand(-9.0, 30.0, bias_pct=20.0) == 0
    assert proportional_demand(5.0, 0.0, bias_pct=50.0) == 100    # on/off ignores it


def test_the_documented_law_is_unbiased_by_default():
    assert get_controller("proportional").bias_pct == 0.0
    assert get_controller("proportional", bias_pct=15.0).bias_pct == 15.0
    assert get_controller("pi").ki == 0.1 and get_controller("pi", ki=0.4).ki == 0.4
    with pytest.raises(ValueError):
        get_controller("pi", ki=-1.0)


def _minutes(law, cfg, temp, minutes, t):
    for _ in range(minutes):
        out = law.step(t, temp, NO_FOOD, cfg)
        t += 60.0
    return out, t


def test_pi_stops_accumulating_while_the_proportional_term_is_flat_out():
    law = get_controller("pi", ki=100.0)  # a huge gain, to make the point quickly
    out, _ = _minutes(law, _cfg(), 150.0, 600, 0.0)  # 75 below target all along
    assert out.fan_pct == 100 and law.integral == 0.0  # nothing wound up


def test_pi_integral_is_clamped_and_unwinds():
    law = get_controller("pi", ki=6.0)  # % per °F per minute
    cfg = _cfg(propband_f=1000.0)       # a very wide band: almost pure integral
    out, t = _minutes(law, cfg, 220.0, 100, 0.0)  # 100 minutes 5 °F below
    assert law.integral == 100.0        # 3000 without the clamp
    assert out.fan_pct == 100
    out, _ = _minutes(law, cfg, 230.0, 100, t)    # 100 minutes 5 °F above
    assert law.integral == 0.0 and out.fan_pct == 0


def test_pi_is_frozen_while_the_lid_hold_has_the_fan_off():
    law = get_controller("pi", ki=6.0)
    cfg = _cfg(propband_f=1000.0)
    _, t = _minutes(law, cfg, 223.0, 6, 0.0)  # six calls, five minutes of drift: 5 × 12
    assert law.integral == pytest.approx(60.0)
    for k in range(1, 61):  # a lid-open drop
        out = law.step(t, 223.0 - 0.5 * k, NO_FOOD, cfg)
        t += 1.0
    assert out.lid_hold
    frozen = law.integral
    out, _ = _minutes(law, cfg, 193.0, 2, t)
    assert out.lid_hold and frozen > 60.0
    assert law.integral == frozen


def test_pi_reset_and_on_off():
    law = get_controller("pi", ki=6.0)
    cfg = _cfg(propband_f=1000.0)
    _, _ = _minutes(law, cfg, 220.0, 5, 0.0)
    assert law.integral > 0
    law.reset()
    assert law.integral == 0.0
    bang_bang = _cfg(propband_f=0.0)
    assert law.step(0.0, 150.0, NO_FOOD, bang_bang).fan_pct == 100
    assert law.step(60.0, 226.0, NO_FOOD, bang_bang).fan_pct == 0
    assert law.integral == 0.0


# -- the plug-in registry -----------------------------------------------------

def test_controllers_are_found_by_name():
    assert get_controller("proportional").name == "proportional"
    assert get_controller("pi").name == "pi"
    assert get_controller("firmware").name == "firmware"
    with pytest.raises(ValueError):
        get_controller("pid")


def test_control_config_is_validated():
    with pytest.raises(ValueError):
        _cfg(food_set_f=(180.0, 180.0))
    with pytest.raises(ValueError):
        _cfg(ramp=4)
