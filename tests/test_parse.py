"""Parsers: recon captures, the scrubbed fake, and the setup pages (task 3)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app.device import (
    DeviceParseError,
    WriteValidationError,
    check_no_reset_fields,
    parse_all_xml,
    parse_config_xml,
    parse_email_page,
    parse_setup_form,
    parse_status_xml,
    parse_wifi_page,
)
from dev.fake_cyberq import CONFIG_XML

RECON = Path(__file__).resolve().parent.parent / "recon"
FIXTURES = Path(__file__).resolve().parent.parent / "dev" / "fixtures"


def test_parse_all_xml_from_capture() -> None:
    sample = parse_all_xml((RECON / "all.xml").read_text())
    assert sample["probes"]["cook"] == {
        "name": "Cook",
        "temp": 75.5,
        "set": 190.0,
        "status": 2,
    }
    assert sample["probes"]["food1"]["temp"] == 74.6
    assert sample["probes"]["food1"]["set"] == 180.0
    assert sample["probes"]["food2"]["temp"] is None  # OPEN = no probe
    assert sample["probes"]["food2"]["status"] == 4
    assert sample["probes"]["food3"]["temp"] is None
    assert sample["output_pct"] == 100
    assert sample["timer_curr"] == "00:00:00"
    assert sample["timer_status"] == 0
    assert sample["deg_units"] == 1
    assert sample["cyctime"] == 6
    assert sample["propband"] == 300
    assert sample["ramp"] == 0


def test_parse_status_xml_from_capture() -> None:
    status = parse_status_xml((RECON / "status.xml").read_text())
    assert status["probes"]["cook"]["temp"] == 75.5
    assert status["probes"]["cook"]["status"] == 2
    assert status["probes"]["food1"]["temp"] == 74.6
    assert status["probes"]["food2"]["temp"] is None
    assert status["output_pct"] == 100
    assert status["fan_shorted"] == 0
    assert "name" not in status["probes"]["cook"]  # status.xml has no names


def _raw_secret(path: Path, tag: str, parent: str | None) -> str | None:
    """Read a credential value straight out of the raw XML — only to prove
    it does not survive parsing (the value never appears in this file)."""
    root = ET.fromstring(path.read_text())
    node = root.find(parent) if parent else root
    element = node.find(tag)
    return element.text if element is not None else None


def _assert_config_shape(config: dict, source: Path) -> None:
    # four sections + fwver, per the scope update
    assert set(config) == {"system", "control", "email", "wifi", "fwver"}
    assert config["fwver"] == "3.1"
    assert config["system"]["lcd_backlight"] == 50
    assert config["system"]["lcd_contrast"] == 10
    assert config["system"]["deg_units"] == 1
    assert config["control"]["cyctime"] == 6
    assert config["control"]["propband"] == 300
    assert config["control"]["cookhold"] == 2000
    assert config["control"]["opendetect"] == 1
    # email section minus the password
    assert "smtp_host" in config["email"]
    assert "smtp_port" in config["email"]
    assert "smtp_pwd" not in config["email"]
    # wifi section minus the key
    assert "ssid" in config["wifi"]
    assert "mac" in config["wifi"]
    assert "wifi_key" not in config["wifi"]
    # and nothing credential-shaped anywhere in the result
    blob = json.dumps(config)
    for banned in ("smtp_pwd", "wifi_key", "fixture-password"):
        assert banned not in blob.lower(), banned
    if source.name == "config.xml":  # the real capture: prove the raw
        # secret values from the XML do not survive the parse
        raw_pwd = _raw_secret(source, "SMTP_PWD", "SMTP")
        raw_key = _raw_secret(source, "WIFI_KEY", "WIFI")
        if raw_pwd:
            assert raw_pwd not in blob
        if raw_key:
            assert raw_key not in blob


def test_parse_config_xml_scrubbed_fake() -> None:
    _assert_config_shape(parse_config_xml(CONFIG_XML), Path("fake"))


def test_parse_config_xml_from_capture() -> None:
    path = RECON / "config.xml"
    if not path.exists():
        pytest.skip("recon/config.xml is a gitignored local capture")
    _assert_config_shape(parse_config_xml(path.read_text()), path)


def test_parse_config_secrets_never_survive() -> None:
    """Feed the parser a config with obviously-marked secret values and
    prove they are dropped while every other field survives."""
    xml = CONFIG_XML.replace(
        "<SMTP_PWD>fixture-password</SMTP_PWD>",
        "<SMTP_PWD>canary-pwd-1234</SMTP_PWD>",
    ).replace(
        f"<WIFI_KEY>{'*' * 20}</WIFI_KEY>",
        "<WIFI_KEY>canary-key-5678</WIFI_KEY>",
    )
    config = parse_config_xml(xml)
    blob = json.dumps(config)
    assert "canary-pwd-1234" not in blob
    assert "canary-key-5678" not in blob
    assert config["email"]["smtp_host"] == "smtp.fixture.invalid"
    assert config["wifi"]["ssid"] == "fixture-net"


def test_parse_errors() -> None:
    with pytest.raises(DeviceParseError):
        parse_all_xml("not xml at all")
    with pytest.raises(DeviceParseError):
        parse_all_xml("<nutcallstatus/>")  # missing probe blocks
    with pytest.raises(DeviceParseError):
        parse_status_xml("<nutcstatus><COOK_TEMP>abc</COOK_TEMP></nutcstatus>")
    with pytest.raises(DeviceParseError):
        parse_config_xml("<nutcallstatus><SYSTEM/></nutcallstatus>")  # missing blocks


def test_parse_setup_form_reads_mainform_capture() -> None:
    html = (RECON / "index.html").read_text()
    fields = {f["name"]: f for f in parse_setup_form(html)}
    # the stock mainForm's controls, as reviewed from the capture
    assert fields["COOK_SET"]["type"] == "hidden"
    assert fields["COOK_NAME"]["maxlength"] == "31"
    assert fields["_COOK_TIMER"]["maxlength"] == "8"
    assert fields["EEAUTOFLUSH"]["value"] in ("0", "1")
    # COOK_TIMER is appended by JS at submit time — not a static control
    assert "COOK_TIMER" not in fields


def test_parse_setup_form_reads_control_capture() -> None:
    # the control page's writable fields, reviewed read-only: the device
    # reads the hidden COOKHOLD/ALARMDEV/PROPBAND; the _-copies are display
    fields = {f["name"] for f in parse_setup_form((RECON / "control.htm").read_text())}
    assert fields == {
        "EEAUTOFLUSH",
        "CYCTIME", "PROPBAND", "COOKHOLD", "ALARMDEV", "COOK_RAMP",
        "OPENDETECT", "TIMEOUT_ACTION",
        "_ALARMDEV", "_COOKHOLD", "_PROPBAND",
    }


def test_reset_page_fields_are_denied() -> None:
    # evidence from the capture: reboot.htm POSTs RESET_CPU — our guard
    # denies that field name no matter where it comes from
    reboot = (RECON / "reboot.htm").read_text()
    fields = [(f["name"], f["value"]) for f in parse_setup_form(reboot)]
    assert "RESET_CPU" in {name for name, _value in fields}
    with pytest.raises(WriteValidationError):
        check_no_reset_fields(fields)


# ---------------------------------------------------------------------------
# setup page parsers (current values from the sanitized fixtures)
# ---------------------------------------------------------------------------

def test_parse_email_page_from_fixture() -> None:
    email = parse_email_page((FIXTURES / "email.htm").read_text())
    assert email["smtp_host"] == "smtp.fixture.invalid"
    assert email["smtp_port"] == 0
    assert email["smtp_user"] == "user@fixture.invalid"
    assert email["smtp_alert"] == 0
    # the device serves the password in the page; the parser must surface
    # it for the write-echo ONLY — no read API may ever return this dict
    assert email["smtp_pwd"] == "fixture-password"
    with pytest.raises(DeviceParseError):
        parse_email_page("<html><input name='SMTP_HOST'></html>")


def test_parse_wifi_page_from_fixture() -> None:
    wifi = parse_wifi_page((FIXTURES / "wifi.htm").read_text())
    assert wifi["multi_wifi_choice"] == 0
    assert wifi["http_user"] == ""
    assert wifi["http_port"] == 80
    assert wifi["home"]["ssid"] == "fixture-net"
    assert wifi["home"]["ip"] == "192.168.101.10"
    assert wifi["home"]["wifi_mode"] == 0
    assert wifi["home"]["dhcp"] == 1
    assert wifi["home"]["wifi_enc"] == 6
    assert wifi["away"]["ssid"] == "fixture-away"
    assert wifi["away"]["wifi_mode"] == 1
    assert wifi["away"]["wifi_enc"] == 1
    # write-only fields are never extracted, by construction
    def flat(value) -> list[str]:
        if isinstance(value, dict):
            out: list[str] = []
            for key, item in value.items():
                out.extend([key, *flat(item)])
            return out
        return []
    names = flat(wifi)
    assert "http_pass" not in names
    assert "wifi_key" not in names
    with pytest.raises(DeviceParseError):
        parse_wifi_page("<html>no literals</html>")


def test_parse_wifi_page_from_scrubbed_recon() -> None:
    """The sanitizer keeps the page shape intact, so the parser also works
    on the real (locally present, gitignored) capture."""
    path = RECON / "wifi.htm"
    if not path.exists():
        pytest.skip("recon/wifi.htm is a gitignored local capture")
    wifi = parse_wifi_page(path.read_text())
    assert wifi["home"]["ssid"]  # the real SSID parses; never asserted/echoed
    assert wifi["away"]["ssid"]
