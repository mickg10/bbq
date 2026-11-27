"""The CyberQ state machine: statuses, timer, latched modes, the fan it drives.

What each status means, and which field carries it, follows the vendor's
guide: the V1.7 guide's table of status.xml values (OK all; HIGH and LOW cook
only; DONE food only; ERROR cook and food; HOLD, ALARM and SHUTDOWN timer
only) and the V2.3/3.1 guide's Control Setup section.
"""

from __future__ import annotations

import pytest

from sim.controllers import Mode
from sim.device import (
    CyberQDevice,
    DeviceSettings,
    Status,
    TimeoutAction,
    format_hhmmss,
    parse_hhmmss,
)

#: the recon capture: cook 75.5 °F on a 190 target, food1 74.6 on 180, 2/3 OPEN
CAPTURE = [755, 746, None, None]
#: a 215 °F pit, cold food1, nothing else plugged
PIT = [2150, 1500, None, None]
NONE = [None, None, None, None]


def make_device(**settings) -> CyberQDevice:
    base = dict(cook_set_f=190.0, food_set_f=[180.0, 180.0, 180.0], alarmdev_f=50.0)
    cutoff = settings.pop("safety_cutoff_f", None)  # the simulator's own guard, not a setting
    device = CyberQDevice(DeviceSettings(**{**base, **settings}))
    if cutoff is not None:
        device.safety_cutoff_f = cutoff
    return device


def run_out_of_time(device: CyberQDevice, action: int, readings=None) -> Status:
    """Set TIMEOUT to ``action``, let a one-second timer run out, step once."""
    device.set_timeout_action(action)
    device.set_timer_text("00:00:01")
    return device.step(0.0, 1.0, PIT if readings is None else readings)


def test_status_codes_are_the_stock_ui_order():
    assert [status.name for status in Status] == [
        "OK", "HIGH", "LOW", "DONE", "ERROR", "HOLD", "ALARM", "SHUTDOWN",
    ]
    assert [int(status) for status in Status] == list(range(8))


# -- statuses -----------------------------------------------------------------

def test_each_status_field_uses_its_own_subset_of_the_codes():
    device = make_device()
    seen = {name: set() for name in ("cook", "food1", "timer")}

    def take(readings) -> None:
        out = device.step(0.0, 1.0, readings)
        seen["cook"].add(out.statuses[0])
        seen["food1"].add(out.statuses[1])
        seen["timer"].add(out.timer_status)

    take([1800, 1700, None, None])   # everything fine
    take([2450, 1700, None, None])   # pit above target + alarm deviation
    take([1390, 1700, None, None])   # pit below target − alarm deviation
    take([2000, 1800, None, None])   # food at its target
    take(NONE)                       # everything open
    for action in (1, 2, 3):
        take_after = run_out_of_time(device, action, [1500, None, None, None])
        seen["cook"].add(take_after.statuses[0])
        seen["timer"].add(take_after.timer_status)
    assert seen == {
        "cook": {Status.OK, Status.HIGH, Status.LOW, Status.ERROR},
        "food1": {Status.OK, Status.DONE, Status.ERROR},
        "timer": {Status.OK, Status.HOLD, Status.ALARM, Status.SHUTDOWN},
    }


def test_the_recon_capture_statuses():
    out = make_device().step(0.0, 1.0, CAPTURE)
    assert out.statuses == (Status.LOW, Status.OK, Status.ERROR, Status.ERROR)
    assert out.timer_status is Status.OK
    assert out.fan_pct == 100  # 115 °F under target: full demand


def test_the_cook_probe_is_high_or_low_outside_the_alarm_band():
    device = make_device()
    assert device.step(0.0, 1.0, [2450, *NONE[1:]]).statuses[0] is Status.HIGH
    assert device.step(0.0, 1.0, [1390, *NONE[1:]]).statuses[0] is Status.LOW
    assert device.step(0.0, 1.0, [1800, *NONE[1:]]).statuses[0] is Status.OK
    assert device.step(0.0, 1.0, [2400, *NONE[1:]]).statuses[0] is Status.OK  # on the edge


def test_a_food_probe_is_ok_under_target_and_done_at_it():
    device = make_device()
    assert device.step(0.0, 1.0, [2000, 1799, None, None]).statuses[1] is Status.OK
    assert device.step(0.0, 1.0, [2000, 1800, None, None]).statuses[1] is Status.DONE


def test_food_done_stays_done_until_the_target_is_raised():
    device = make_device()
    device.step(0.0, 1.0, [2000, 1800, None, None])
    assert device.step(0.0, 1.0, [2000, 1750, None, None]).statuses[1] is Status.DONE
    device.set_food_set_tenths(0, 2000)
    assert device.step(0.0, 1.0, [2000, 1750, None, None]).statuses[1] is Status.OK


def test_low_is_a_cook_status_so_a_done_food_that_cools_stays_done():
    device = make_device()
    device.step(0.0, 1.0, [2000, 1800, None, None])
    assert device.step(0.0, 1.0, [2000, 1200, None, None]).statuses[1] is Status.DONE


def test_an_open_cook_probe_is_an_error_and_stops_the_fan():
    out = make_device(cook_set_f=225.0).step(0.0, 1.0, [None, 1500, None, None])
    assert out.statuses[0] is Status.ERROR
    assert out.fan_pct == 0 and out.duty == 0.0


# -- cook hold and the timeout actions ----------------------------------------

def test_food_being_done_does_not_put_the_pit_on_hold():
    """The guide has cook hold coming from the timer only; the automatic
    version of it is the ramp."""
    device = make_device(cook_set_f=225.0, cookhold_f=160.0)
    device.step(0.0, 1.0, [2250, 2030, None, 1500])
    out = device.step(0.0, 1.0, [2250, 2030, None, 1800])
    assert out.statuses == (Status.OK, Status.DONE, Status.ERROR, Status.DONE)
    assert not device.hold and out.timer_status is Status.OK
    assert (out.setpoint_f, out.cook_set_f) == (225.0, 225.0)


def test_cook_hold_is_the_timer_s_doing():
    """HOLD: "COOKSET is set to COOKHOLD value" — and the timer says so."""
    device = make_device(cook_set_f=225.0, cookhold_f=160.0)
    out = run_out_of_time(device, 1, [1500, None, None, None])
    assert device.hold and out.timer_status is Status.HOLD
    assert out.statuses[0] is Status.OK  # the cook probe itself is fine
    assert (out.setpoint_f, out.cook_set_f, out.fan_pct) == (160.0, 160.0, 33)  # (160 − 150) / 30
    assert out.timer == "00:00:00"


def test_hold_is_released_by_a_different_cook_setpoint_and_then_stays_off():
    device = make_device(cook_set_f=225.0)
    run_out_of_time(device, 1)
    assert device.hold and device.timer_status is Status.HOLD
    device.set_cook_set_tenths(2500)
    assert not device.hold and device.timer_status is Status.OK
    assert device.step(0.0, 1.0, [2250, 1800, None, None]).statuses[0] is Status.OK
    # the same value again changes nothing, so it would not release a hold
    device.hold = True
    device.set_cook_set_tenths(2500)
    assert device.hold


@pytest.mark.parametrize(
    "action, status, mode",
    [(1, Status.HOLD, Mode.HOLD), (2, Status.ALARM, Mode.NORMAL), (3, Status.SHUTDOWN, Mode.SHUTDOWN)],
)
def test_timeout_actions_do_what_the_guide_says(action, status, mode):
    """Each raises the timer status in its own name and leaves the cook probe
    to its temperature. NO ACTN does nothing; HOLD moves the setpoint; ALARM
    does nothing to the control; SHDN "turns the fan output off"."""
    device = make_device(timeout_action=action, cookhold_f=200.0, cook_set_f=225.0)
    out = run_out_of_time(device, action, [1750, None, None, None])  # within 50 °F of both targets
    assert out.timer_status is status and out.statuses[0] is Status.OK
    assert device.mode is mode
    if mode is Mode.SHUTDOWN:
        assert out.fan_pct == 0 and out.duty == 0.0
    elif mode is Mode.HOLD:
        assert out.fan_pct == 83   # (200 − 175) / 30
    else:
        assert out.fan_pct == 100  # (225 − 175) / 30
    if mode is Mode.HOLD:
        assert (out.setpoint_f, out.cook_set_f) == (200.0, 200.0)
    if mode is Mode.NORMAL:
        assert (out.setpoint_f, out.cook_set_f) == (225.0, 225.0)


def test_no_action_leaves_everything_as_it_was():
    device = make_device()
    out = run_out_of_time(device, 0)
    assert out.timer_status is Status.OK and device.mode is Mode.NORMAL
    assert out.statuses[0] is Status.OK and out.setpoint_f == 190.0


def test_a_new_timer_releases_the_timeout_modes():
    device = make_device(timeout_action=3)
    run_out_of_time(device, 3)
    assert device.mode is Mode.SHUTDOWN and device.timer_status is Status.SHUTDOWN
    device.set_timer_text("01:00:00")
    assert device.mode is Mode.NORMAL and device.timer_status is Status.OK
    assert device.timer.running


def test_the_over_temperature_guard_is_the_simulator_s_not_a_device_status():
    """The guide has no over-temperature shutdown. This guard is the
    simulator's own: the fan stops while the pit reads that hot, and comes
    back when it cools."""
    device = make_device(safety_cutoff_f=300.0, cook_set_f=225.0)
    out = device.step(0.0, 1.0, [3100, *NONE[1:]])
    assert out.fan_pct == 0 and out.duty == 0.0
    assert out.statuses[0] is Status.HIGH and out.timer_status is Status.OK
    assert not device.shutdown
    cooled = device.step(1.0, 1.0, [1500, *NONE[1:]])
    assert cooled.fan_pct == 100


# -- timer --------------------------------------------------------------------

def test_the_timer_counts_down_and_expires():
    device = make_device()
    assert (device.timer.text, device.timer.running, device.timer_status) == (
        "00:00:00", False, Status.OK,
    )
    device.set_timer_text("00:01:00")
    assert device.timer.running and device.timer_status is Status.OK  # running is not a status
    out = device.step(0.0, 30.0, PIT)
    assert (out.timer, out.timer_status) == ("00:00:30", Status.OK)
    out = device.step(30.0, 29.0, PIT)
    assert out.timer == "00:00:01"
    out = device.step(59.0, 1.0, PIT)
    assert (out.timer, out.timer_status) == ("00:00:00", Status.OK)  # No Action
    assert device.timer.expired and not device.timer.running


def test_zero_stops_and_clears_the_timer():
    device = make_device(timeout_action=3)
    device.set_timer_text("00:00:10")
    device.step(0.0, 10.0, PIT)
    assert device.timer.expired and device.timer_status is Status.SHUTDOWN
    device.set_timer_text("00:00:00")
    assert (device.timer.text, device.timer.running, device.timer.expired) == (
        "00:00:00", False, False,
    )
    # stopping a timer is not taking control back
    assert device.timer_status is Status.SHUTDOWN and device.mode is Mode.SHUTDOWN


def test_timer_text():
    assert parse_hhmmss("01:30:00") == 5400
    assert format_hhmmss(5400) == "01:30:00"
    assert format_hhmmss(3661.2) == "01:01:02"  # shown until the second has passed
    assert format_hhmmss(0) == "00:00:00"
    for bad in ("1:30:00", "01:60:00", "01:30:60", "013000", "", None):
        with pytest.raises(ValueError):
            parse_hhmmss(bad)


# -- safety, fan, settings ----------------------------------------------------

def test_the_fan_follows_the_demand_and_the_cycle():
    device = make_device(cook_set_f=225.0, propband_f=30.0, cyctime_s=6)
    out = device.step(0.0, 1.0, [2050, *NONE[1:]])
    assert out.fan_pct == 67  # (225 − 205) / 30
    duties = [device.step(float(t), 1.0, [2050, *NONE[1:]]).duty for t in range(6)]
    assert duties == pytest.approx([1.0, 1.0, 1.0, 1.0, 0.02, 0.0])


def test_settings_are_clamped_like_the_dashboard():
    device = make_device()
    device.set_cook_set_tenths(5000)
    assert device.settings.cook_set_f == 475.0
    device.set_food_set_tenths(1, 20)
    assert device.settings.food_set_f[1] == 32.0
    device.set_cookhold_tenths(0)
    assert device.settings.cookhold_f == 32.0
    device.set_alarmdev_tenths(-5)
    assert device.settings.alarmdev_f == 0.0
    device.set_propband_tenths(-5)
    assert device.settings.propband_f == 0.0
    device.set_cyctime_s(150)
    assert device.settings.cyctime_s == 99
    device.set_ramp(7)
    assert device.settings.ramp == 3
    assert device.set_timeout_action(9) is None
    assert device.settings.timeout_action is TimeoutAction.SHUTDOWN
    with pytest.raises(ValueError):
        device.set_food_set_tenths(3, 1800)
    # setpoints live at the wire's resolution
    device.set_cook_set_tenths(2255)
    assert device.settings.cook_set_f == 225.5


def test_the_defaults_are_the_recon_capture():
    s = DeviceSettings()
    assert (s.cook_set_f, s.cookhold_f, s.alarmdev_f, s.propband_f) == (190.0, 200.0, 50.0, 30.0)
    assert (s.cyctime_s, s.ramp, s.opendetect, s.timeout_action) == (
        6, 0, True, TimeoutAction.NONE,
    )
    with pytest.raises(ValueError):
        DeviceSettings(ramp=9)
    assert DeviceSettings(cook_set_f=1e9).cook_set_f == 475.0


def test_reset_clears_the_latches_and_the_timer():
    device = make_device(timeout_action=3)
    device.set_timer_text("00:00:05")
    device.step(0.0, 5.0, [2000, 1800, None, None])
    assert device.shutdown and device.timer_status is Status.SHUTDOWN and device.done[0]
    device.reset()
    assert device.mode is Mode.NORMAL and device.timer_status is Status.OK
    assert not device.timer.running
    assert device.done == [False, False, False]
