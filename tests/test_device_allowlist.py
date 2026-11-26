"""The allowlist contract: what the client may and may never do (task 3,
reworked to the scope update: every setup page is readable and POSTable —
everything except firmware reset).

These tests are the executable version of the safety section in DESIGN.md:
no URL outside READ_PATHS/WRITE_PATHS is ever hit (so booting.htm and
reboot.htm are unreachable except through the flag-gated reboot), every POST
carries only that page's allowlisted fields, no reset-like field name is
ever sent, and every value is validated and clamped before it leaves the
process. The reboot path itself is flag-gated and off by default.
"""

from __future__ import annotations

import pytest

from app.device import (
    ALL_WRITE_FIELDS,
    CONTROL_WRITE_FIELDS,
    DENIED_PATHS,
    EMAIL_WRITE_FIELDS,
    KEY_SENTINEL,
    READ_PATHS,
    SCAN_WRITE_FIELDS,
    SYSTEM_WRITE_FIELDS,
    WIFI_WRITE_FIELDS,
    WRITE_FIELDS,
    WRITE_PATHS,
    CyberQClient,
    DeviceUrlError,
    WriteValidationError,
    build_control_form,
    build_scan_form,
    build_write_form,
    check_no_reset_fields,
    clamp_set,
    validate_name,
    validate_timer,
    validate_wifi_key,
)
from dev import fake_cyberq


async def _exercise_everything(client: CyberQClient) -> None:
    """Every read and write the product ever performs."""
    await client.read_all()
    await client.read_status()
    config = await client.read_config()
    await client.read_email_page()
    wifi = await client.read_wifi_page()
    current = await client.read_all()
    await client.write_probes(
        current, names={"cook": "Brisket"}, sets={"cook": 225.0}, timer="02:30:00"
    )
    await client.write_control(config["control"], {"cookhold": 205.5})
    await client.write_system(config["system"], {"lcd_backlight": 70})
    await client.write_email(await client.read_email_page(), {"smtp_port": 25})
    await client.write_wifi(wifi, {"ssid": "newnet"})
    await client.wifi_scan()


# ---------------------------------------------------------------------------
# URL allowlist — the core safety property
# ---------------------------------------------------------------------------

async def test_reads_and_writes_only_hit_allowlisted_paths(
    fake_device: str, device_log
) -> None:
    async with CyberQClient(fake_device) as client:
        await _exercise_everything(client)

    entries = [e for e in device_log() if e["path"] != "/_log"]
    assert entries, "expected the traffic to be logged"
    for entry in entries:
        assert entry["path"] in READ_PATHS | WRITE_PATHS, entry
    posts = [e for e in entries if e["method"] == "POST"]
    assert {e["path"] for e in posts} <= WRITE_PATHS
    # every form field is allowlisted for its page and none is reset-like
    for entry in posts:
        for name, _value in entry.get("form", []):
            assert name in ALL_WRITE_FIELDS, name
            assert not any(
                word in name.upper()
                for word in ("RESET", "REBOOT", "BOOTING", "FACTORY",
                             "DEFAULT", "FIRMWARE", "UPGRADE", "FLASH")
            ), name
    # the fake would happily accept a reboot — we never sent one
    assert fake_cyberq.STATE["REBOOTS"] == 0


async def test_client_refuses_non_allowlisted_requests(fake_device: str, device_log) -> None:
    async with CyberQClient(fake_device) as client:
        for method, path in [
            ("GET", "/reboot.htm"),
            ("GET", "/booting.htm"),
            ("POST", "/booting.htm"),
            ("POST", "/reboot.htm"),
            ("GET", "/logo.jpg"),
            ("POST", "/all.xml"),
            ("POST", "/config.xml"),
            ("DELETE", "/"),
            ("PUT", "/all.xml"),
            ("GET", "/status.xml/../../etc/passwd"),
        ]:
            with pytest.raises(DeviceUrlError):
                await client._request(method, path)

    # refusals never leave the process: nothing extra in the log
    entries = [e for e in device_log() if e["path"] != "/_log"]
    assert entries == [], entries


def test_booting_and_reboot_are_explicitly_denied() -> None:
    assert DENIED_PATHS == {"/booting.htm", "/reboot.htm"}
    assert not (DENIED_PATHS & READ_PATHS)
    assert not (DENIED_PATHS & WRITE_PATHS)


# ---------------------------------------------------------------------------
# reboot: flag-gated, off by default, the only reset-like write anywhere
# ---------------------------------------------------------------------------

async def test_reboot_disabled_by_default(fake_device: str, device_log) -> None:
    async with CyberQClient(fake_device) as client:
        with pytest.raises(DeviceUrlError):
            await client.reboot()
    assert fake_cyberq.STATE["REBOOTS"] == 0
    entries = [e for e in device_log() if e["path"] != "/_log"]
    assert entries == [], entries


async def test_reboot_when_flagged_is_the_only_booting_hit(
    fake_device: str, device_log
) -> None:
    async with CyberQClient(fake_device, allow_reboot=True) as client:
        await client.reboot()
    assert fake_cyberq.STATE["REBOOTS"] == 1
    posts = [e for e in device_log() if e["method"] == "POST"]
    assert posts[-1]["path"] == "/booting.htm"
    assert ("RESET_CPU", "1") in [(n, v) for n, v in posts[-1]["form"]]


# ---------------------------------------------------------------------------
# reset-like field names — the second deny layer
# ---------------------------------------------------------------------------

def test_reset_like_fields_are_denied() -> None:
    for bad in (
        [("RESET_CPU", "1")],
        [("REBOOT", "1")],
        [("BOOTING", "1")],
        [("FACTORY_RESET", "1")],
        [("DEFAULTS", "1")],
        [("FIRMWARE_UPGRADE", "1")],
        [("FLASH_IMAGE", "1")],
        [("FOOD1_SET", "180"), ("WIFI_RESTORE_DEFAULTS", "1")],
        [("NOT_A_FIELD", "1")],  # outside the allowlists
    ):
        with pytest.raises(WriteValidationError):
            check_no_reset_fields(bad)


def test_reset_like_names_cover_the_design_regex() -> None:
    for name in ("FACTORY", "DEFAULT", "FIRMWARE", "UPGRADE", "FLASH",
                 "RESET", "REBOOT", "BOOTING"):
        with pytest.raises(WriteValidationError):
            check_no_reset_fields([(f"X_{name}_X", "1")])


# ---------------------------------------------------------------------------
# write form shapes — mirroring the stock forms exactly
# ---------------------------------------------------------------------------

async def test_write_sends_the_full_mainform(fake_device: str, device_log) -> None:
    async with CyberQClient(fake_device) as client:
        current = await client.read_all()
        await client.write_probes(current, sets={"cook": 225.0})

    posts = [e for e in device_log() if e["method"] == "POST"]
    form = posts[-1]["form"]  # JSON round-trip: list of [name, value] lists
    names = [name for name, _value in form]
    # hidden fields in DOM order, both EEAUTOFLUSH copies included
    assert names[:5] == ["EEAUTOFLUSH", "COOK_SET", "FOOD1_SET", "FOOD2_SET", "FOOD3_SET"]
    assert names[5:9] == ["COOK_NAME", "FOOD1_NAME", "FOOD2_NAME", "FOOD3_NAME"]
    assert names[9] == "EEAUTOFLUSH"
    # unchanged values ride along, like a stock UI submit
    assert ["COOK_SET", "225"] in form
    assert ["FOOD1_SET", "180"] in form
    assert ["FOOD2_SET", "180"] in form
    # the timer is only sent when explicitly set
    assert "COOK_TIMER" not in names


async def test_write_includes_timer_only_when_given(fake_device: str, device_log) -> None:
    async with CyberQClient(fake_device) as client:
        current = await client.read_all()
        await client.write_probes(current, timer="01:30:00")
    posts = [e for e in device_log() if e["method"] == "POST"]
    assert ["COOK_TIMER", "01:30:00"] in posts[-1]["form"]


def test_build_write_form_shape() -> None:
    fields = build_write_form(
        _current(), names={"cook": "Brisket"}, sets={"cook": 225.0}, timer="01:00:00"
    )
    assert [name for name, _value in fields] == [
        "EEAUTOFLUSH", "COOK_SET", "FOOD1_SET", "FOOD2_SET", "FOOD3_SET",
        "COOK_NAME", "FOOD1_NAME", "FOOD2_NAME", "FOOD3_NAME",
        "EEAUTOFLUSH", "COOK_TIMER",
    ]
    assert ("COOK_SET", "225") in fields
    assert ("FOOD1_SET", "180") in fields
    assert ("COOK_NAME", "Brisket") in fields
    assert ("COOK_TIMER", "01:00:00") in fields


def test_build_write_form_rejects_unknown_probes() -> None:
    with pytest.raises(WriteValidationError):
        build_write_form(_current(), sets={"food4": 100.0})
    with pytest.raises(WriteValidationError):
        build_write_form(_current(), names={"oven": "x"})


# ---------------------------------------------------------------------------
# per-page writes: shape and round-trip
# ---------------------------------------------------------------------------

async def test_write_control_round_trip(fake_device: str, device_log) -> None:
    async with CyberQClient(fake_device) as client:
        config = await client.read_config()
        result = await client.write_control(
            config["control"],
            {"cookhold": 205.5, "timeout_action": 2, "alarmdev": 25,
             "cook_ramp": 1, "opendetect": 0, "cyctime": 8, "propband": 30},
        )
    assert result["cookhold"] == 2055  # tenths °F on the device
    assert result["timeout_action"] == 2
    assert result["alarmdev"] == 250
    assert result["cook_ramp"] == 1
    assert result["opendetect"] == 0
    assert result["cyctime"] == 8
    assert result["propband"] == 300

    posts = [e for e in device_log() if e["method"] == "POST"]
    assert posts[-1]["path"] == "/control.htm"
    form = dict(posts[-1]["form"])
    # wire format: °F with one decimal, hidden (un-prefixed) field names
    assert form["COOKHOLD"] == "205.5"
    assert form["ALARMDEV"] == "25"
    assert form["PROPBAND"] == "30"
    assert form["TIMEOUT_ACTION"] == "2"
    assert set(n for n, _ in posts[-1]["form"]) <= CONTROL_WRITE_FIELDS


async def test_write_control_clamps(fake_device: str) -> None:
    async with CyberQClient(fake_device) as client:
        config = await client.read_config()
        result = await client.write_control(
            config["control"], {"cookhold": 999.0, "alarmdev": -5.0, "cyctime": 500}
        )
    assert result["cookhold"] == 4750
    assert result["alarmdev"] == 0
    assert result["cyctime"] == 99


async def test_write_system_round_trip(fake_device: str, device_log) -> None:
    async with CyberQClient(fake_device) as client:
        config = await client.read_config()
        result = await client.write_system(
            config["system"],
            {"menu_scrolling": 1, "lcd_backlight": 70, "deg_units": 1,
             "alarm_beeps": 3},
        )
    assert result["menu_scrolling"] == 1
    assert result["lcd_backlight"] == 70
    assert result["alarm_beeps"] == 3

    posts = [e for e in device_log() if e["method"] == "POST"]
    assert posts[-1]["path"] == "/system.htm"
    names = [n for n, _ in posts[-1]["form"]]
    # stock quirk: system.htm submits exactly ONE EEAUTOFLUSH (the =1)
    assert names.count("EEAUTOFLUSH") == 1
    assert set(names) <= SYSTEM_WRITE_FIELDS


async def test_write_email_round_trip_and_test_now(
    fake_device: str, device_log
) -> None:
    async with CyberQClient(fake_device) as client:
        current = await client.read_email_page()
        result = await client.write_email(
            current, {"smtp_host": "relay.example.com", "smtp_port": 587},
            send_test=True,
        )
    assert result["smtp_host"] == "relay.example.com"
    assert result["smtp_port"] == 587
    assert "smtp_pwd" not in result  # never in any read-back
    assert fake_cyberq.STATE["EMAILS_SENT"] == 1

    posts = [e for e in device_log() if e["method"] == "POST"]
    assert posts[-1]["path"] == "/email.htm"
    form = dict(posts[-1]["form"])
    assert form["SMTP_NOW"] == "1"
    # stock parity: the full form re-sends the device-served password. It
    # lives only in this POST body — never in a log, a DB row or a response.
    assert form["SMTP_PWD"] == "fixture-password"
    assert set(n for n, _ in posts[-1]["form"]) <= EMAIL_WRITE_FIELDS


async def test_write_wifi_round_trip_and_key_sentinel(
    fake_device: str, device_log
) -> None:
    async with CyberQClient(fake_device) as client:
        wifi = await client.read_wifi_page()
        result = await client.write_wifi(
            wifi, {"ssid": "newnet", "multi_wifi_choice": 1}
        )
    assert result["home"]["ssid"] == "newnet"  # pages render from state
    assert result["multi_wifi_choice"] == 1

    posts = [e for e in device_log() if e["method"] == "POST"]
    assert posts[-1]["path"] == "/wifi.htm"
    names = [n for n, _ in posts[-1]["form"]]
    form = dict(posts[-1]["form"])
    # keys default to the keep-sentinel; HTTP auth fields are NOT sent
    # unless explicitly provided (blank would disable device auth)
    assert form["WIFI_KEY"] == KEY_SENTINEL
    assert form["WIFI_KEY2"] == KEY_SENTINEL
    assert "HTTP_USER" not in names
    assert "HTTP_PASS" not in names
    assert set(names) <= WIFI_WRITE_FIELDS

    # a new key and explicit HTTP auth are sent when provided
    async with CyberQClient(fake_device) as client:
        wifi = await client.read_wifi_page()
        await client.write_wifi(
            wifi,
            {"wifi_key": "new-phrase-key", "http_user": "pitboss",
             "http_pass": "seekrit", "http_port": 8080},
        )
    posts = [e for e in device_log() if e["method"] == "POST"]
    form = dict(posts[-1]["form"])
    assert form["WIFI_KEY"] == "new-phrase-key"
    assert form["HTTP_USER"] == "pitboss"
    assert form["HTTP_PASS"] == "seekrit"
    assert form["HTTP_PORT"] == "8080"


def test_wifi_key_validation_mirrors_the_stock_page() -> None:
    assert validate_wifi_key(6, KEY_SENTINEL) == KEY_SENTINEL  # keep sentinel
    assert validate_wifi_key(0, "anything") == "anything"  # open
    assert validate_wifi_key(1, "0123456789") == "0123456789"  # WEP40: 10 hex
    with pytest.raises(WriteValidationError):
        validate_wifi_key(1, "12345")  # too short
    with pytest.raises(WriteValidationError):
        validate_wifi_key(1, "zzzzzzzzzz")  # not hex
    assert validate_wifi_key(2, "a" * 26) == "a" * 26  # WEP104
    with pytest.raises(WriteValidationError):
        validate_wifi_key(2, "a" * 10)
    assert validate_wifi_key(5, "f" * 64) == "f" * 64  # WPA2 key
    with pytest.raises(WriteValidationError):
        validate_wifi_key(5, "f" * 63)
    assert validate_wifi_key(4, "a phrase ok") == "a phrase ok"  # phrase >= 8
    with pytest.raises(WriteValidationError):
        validate_wifi_key(4, "short")


async def test_wifi_scan(fake_device: str, device_log) -> None:
    async with CyberQClient(fake_device) as client:
        await client.wifi_scan()
    assert fake_cyberq.STATE["SCANS"] == 1
    posts = [e for e in device_log() if e["method"] == "POST"]
    assert posts[-1]["path"] == "/scan.htm"
    assert [n for n, _ in posts[-1]["form"]] == ["WIFI_SCAN_START"]
    assert set(n for n, _ in posts[-1]["form"]) <= SCAN_WRITE_FIELDS


def test_build_scan_form_is_minimal() -> None:
    assert build_scan_form() == [("WIFI_SCAN_START", "1")]


def test_delta_fields_carry_no_32_offset() -> None:
    """ALARMDEV and PROPBAND are temperature DELTAS: the stock JS converts
    them with the no32 path (TempHTMLToPIC(v, no32=1)) — a °F↔°C layer must
    never add/subtract 32 for them, while COOKHOLD is an absolute
    temperature. Our °F-native API pins that: the wire value is exactly the
    delta in °F (and exactly the temperature for COOKHOLD)."""
    current = {
        "cookhold": 2000, "alarmdev": 500, "propband": 300,
        "timeout_action": 0, "cook_ramp": 0, "opendetect": 1, "cyctime": 6,
    }
    fields = dict(
        build_control_form(
            current, {"cookhold": 100.0, "alarmdev": 100.0, "propband": 100.0}
        )
    )
    assert fields["ALARMDEV"] == "100"  # delta: never +32
    assert fields["PROPBAND"] == "100"  # delta: never +32
    assert fields["COOKHOLD"] == "100"  # absolute °F passes through


async def test_email_password_never_reaches_any_log(fake_device: str) -> None:
    """Write-echo secrecy: with DEBUG logging capturing everything the
    process emits during a write (root logger — this also catches the httpx
    and httpcore loggers, which log request bodies at DEBUG), neither the
    new nor the echoed password may appear in any log record."""
    import logging

    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture()
    root = logging.getLogger()
    old_level, old_handlers = root.level, list(root.handlers)
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        async with CyberQClient(fake_device) as client:
            current = await client.read_email_page()
            await client.write_email(current, {"smtp_pwd": "super-secret-pw"})
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        for handler_ in old_handlers:
            root.addHandler(handler_)

    blob = "\n".join(
        f"{record.name} {record.levelname} {record.getMessage()}" for record in records
    )
    assert "super-secret-pw" not in blob
    assert "fixture-password" not in blob
    assert records, "expected some DEBUG records (httpx/httpcore noise) to prove capture"


# ---------------------------------------------------------------------------
# probe writes: validation and clamping (unchanged from the first pass)
# ---------------------------------------------------------------------------

async def test_write_round_trip(fake_device: str) -> None:
    async with CyberQClient(fake_device) as client:
        current = await client.read_all()
        result = await client.write_probes(
            current,
            names={"cook": "Brisket", "food1": "Plate"},
            sets={"cook": 275.0, "food1": 160.5},
            timer="01:30:00",
        )
    assert result["probes"]["cook"]["name"] == "Brisket"
    assert result["probes"]["food1"]["name"] == "Plate"
    assert result["probes"]["cook"]["set"] == 275.0
    # setpoints go to the device as whole °F (stock UI parity): 160.5 -> 161
    assert result["probes"]["food1"]["set"] == 161.0
    assert result["timer_curr"] == "01:30:00"


async def test_write_clamps_setpoints(fake_device: str) -> None:
    async with CyberQClient(fake_device) as client:
        current = await client.read_all()
        result = await client.write_probes(
            current, sets={"cook": 999.0, "food1": 1.0, "food2": 350.0, "food3": -10.0}
        )
    assert result["probes"]["cook"]["set"] == 475.0
    assert result["probes"]["food1"]["set"] == 32.0
    assert result["probes"]["food2"]["set"] == 300.0
    assert result["probes"]["food3"]["set"] == 32.0


def test_clamp_set_rounds_like_the_stock_ui() -> None:
    # JS Math.round is half-up, not banker's rounding
    assert clamp_set("cook", 190.5) == 191
    assert clamp_set("cook", 190.4) == 190
    assert clamp_set("cook", 190.6) == 191
    assert clamp_set("food1", 150.0) == 150
    with pytest.raises(WriteValidationError):
        clamp_set("nosuch", 100.0)


def test_name_validation() -> None:
    assert validate_name("Brisket") == "Brisket"
    assert validate_name("  Padded  ") == "Padded"
    assert validate_name("x" * 16) == "x" * 16
    with pytest.raises(WriteValidationError):
        validate_name("")
    with pytest.raises(WriteValidationError):
        validate_name("   ")
    with pytest.raises(WriteValidationError):
        validate_name("x" * 17)
    with pytest.raises(WriteValidationError):
        validate_name("bad\x1fcontrol char")
    with pytest.raises(WriteValidationError):
        validate_name("not ascii: é")


def test_timer_validation() -> None:
    assert validate_timer("02:30:00") == "02:30:00"
    assert validate_timer(" 02:30:00 ") == "02:30:00"
    for bad in ("2:30:00", "02:60:00", "02:30:60", "023000", "aa:bb:cc", "", "02:3:00"):
        with pytest.raises(WriteValidationError):
            validate_timer(bad)


def test_names_and_sets_validated_before_any_request() -> None:
    # invalid values raise while *building* the form — nothing is sent
    with pytest.raises(WriteValidationError):
        build_write_form(_current(), names={"cook": "x" * 17})
    with pytest.raises(WriteValidationError):
        build_write_form(_current(), timer="02:99:00")


def _current() -> dict:
    return {
        "probes": {
            probe: {"name": "Name", "temp": 70.0, "set": 180.0, "status": 0}
            for probe in ("cook", "food1", "food2", "food3")
        }
    }
