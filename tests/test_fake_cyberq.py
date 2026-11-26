"""The fake CyberQ itself: pages, XML state, every form POST, and the
request log the allowlist tests rely on (task 2 extension)."""

from __future__ import annotations

import httpx
import pytest

from dev import fake_cyberq


@pytest.fixture()
async def client():
    fake_cyberq.reset()
    transport = httpx.ASGITransport(app=fake_cyberq.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://fake.test"
    ) as c:
        yield c


def _post(path: str, **fields: str) -> httpx.Request:
    return path, fields


async def test_serves_all_pages_from_fixtures(client) -> None:
    pages = {
        "/": "Control Status",
        "/control.htm": "Control Setup",
        "/system.htm": "System Setup",
        "/email.htm": "E-Mail Setup",
        "/wifi.htm": "WIFI Setup",
        "/reboot.htm": "Reboot Device?",
    }
    for path, marker in pages.items():
        response = await client.get(path)
        assert response.status_code == 200
        assert marker in response.text, path


async def test_fixture_pages_carry_no_real_credentials(client) -> None:
    email = (await client.get("/email.htm")).text
    wifi = (await client.get("/wifi.htm")).text
    # sanitized fixture values, not the capture's
    assert 'value="fixture-password"' in email
    assert "fixture-net" in wifi
    assert "fixture-away" in wifi
    assert "00:1E:C0:00:00:00" in wifi
    # the masked keys the real device serves
    assert fake_cyberq.KEY_SENTINEL in wifi


async def test_xml_endpoints_seeded(client) -> None:
    all_xml = (await client.get("/all.xml")).text
    assert "<COOK_SET>1900</COOK_SET>" in all_xml
    assert "<FOOD2_TEMP>OPEN</FOOD2_TEMP>" in all_xml
    status = (await client.get("/status.xml")).text
    assert "<FAN_SHORTED>0</FAN_SHORTED>" in status
    config = (await client.get("/config.xml")).text
    # the real device masks WIFI_KEY and serves SMTP_PWD raw — so must the
    # fake, because that is what our parser has to defend against
    assert f"<WIFI_KEY>{fake_cyberq.KEY_SENTINEL}</WIFI_KEY>" in config
    assert "<SMTP_PWD>fixture-password</SMTP_PWD>" in config
    assert "<FWVER>3.1</FWVER>" in config
    assert "<SMTP_HOST>smtp.fixture.invalid</SMTP_HOST>" in config


async def test_ajax_status_poll(client) -> None:
    response = await client.post("/status.xml", content="IGNOREDTAG=0")
    assert response.status_code == 200
    assert "<nutcstatus>" in response.text


async def test_main_form_round_trip(client) -> None:
    await client.post(
        "/",
        content="EEAUTOFLUSH=0&COOK_SET=225&FOOD1_SET=190.5"
        "&COOK_NAME=Brisket&EEAUTOFLUSH=1&COOK_TIMER=01:30:00",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert fake_cyberq.STATE["COOK_SET"] == 2250
    assert fake_cyberq.STATE["FOOD1_SET"] == 1905
    assert fake_cyberq.STATE["COOK_NAME"] == "Brisket"
    assert fake_cyberq.STATE["TIMER_CURR"] == "01:30:00"
    all_xml = (await client.get("/all.xml")).text
    assert "<COOK_SET>2250</COOK_SET>" in all_xml
    assert "<COOK_NAME>Brisket</COOK_NAME>" in all_xml


async def test_control_form_round_trip(client) -> None:
    await client.post(
        "/control.htm",
        content="EEAUTOFLUSH=0&COOKHOLD=205.5&TIMEOUT_ACTION=2"
        "&ALARMDEV=25&COOK_RAMP=1&OPENDETECT=0&CYCTIME=8&PROPBAND=30"
        "&EEAUTOFLUSH=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert fake_cyberq.STATE["COOKHOLD"] == 2055
    assert fake_cyberq.STATE["ALARMDEV"] == 250
    assert fake_cyberq.STATE["PROPBAND"] == 300
    assert fake_cyberq.STATE["TIMEOUT_ACTION"] == 2
    assert fake_cyberq.STATE["COOK_RAMP"] == 1
    assert fake_cyberq.STATE["OPENDETECT"] == 0
    assert fake_cyberq.STATE["CYCTIME"] == 8
    config = (await client.get("/config.xml")).text
    assert "<COOKHOLD>2055</COOKHOLD>" in config
    assert "<OPENDETECT>0</OPENDETECT>" in config


async def test_system_form_round_trip(client) -> None:
    # system.htm's stock page posts only ONE EEAUTOFLUSH (=1)
    await client.post(
        "/system.htm",
        content="MENU_SCROLLING=1&LCD_BACKLIGHT=70&LCD_CONTRAST=20"
        "&DEG_UNITS=0&ALARM_BEEPS=3&KEY_BEEPS=1&EEAUTOFLUSH=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert fake_cyberq.STATE["MENU_SCROLLING"] == 1
    assert fake_cyberq.STATE["LCD_BACKLIGHT"] == 70
    assert fake_cyberq.STATE["LCD_CONTRAST"] == 20
    assert fake_cyberq.STATE["DEG_UNITS"] == 0
    assert fake_cyberq.STATE["ALARM_BEEPS"] == 3
    assert fake_cyberq.STATE["KEY_BEEPS"] == 1
    config = (await client.get("/config.xml")).text
    assert "<LCD_BACKLIGHT>70</LCD_BACKLIGHT>" in config


async def test_email_form_and_test_now(client) -> None:
    await client.post(
        "/email.htm",
        content="EEAUTOFLUSH=0&SMTP_HOST=relay.example.com&SMTP_PORT=587"
        "&SMTP_USER=me@example.com&SMTP_PWD=newpass&SMTP_TO=you@example.com"
        "&SMTP_FROM=me@example.com&SMTP_SUBJ=Dinner&SMTP_ALERT=15"
        "&EEAUTOFLUSH=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert fake_cyberq.STATE["SMTP_HOST"] == "relay.example.com"
    assert fake_cyberq.STATE["SMTP_PORT"] == 587
    assert fake_cyberq.STATE["SMTP_PWD"] == "newpass"
    assert fake_cyberq.STATE["SMTP_ALERT"] == 15
    assert fake_cyberq.STATE["EMAILS_SENT"] == 0

    await client.post(
        "/email.htm",
        content="EEAUTOFLUSH=0&SMTP_HOST=relay.example.com&SMTP_PORT=587"
        "&SMTP_USER=me@example.com&SMTP_PWD=newpass&SMTP_TO=you@example.com"
        "&SMTP_FROM=me@example.com&SMTP_SUBJ=Dinner&SMTP_ALERT=15"
        "&SMTP_NOW=1&EEAUTOFLUSH=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert fake_cyberq.STATE["EMAILS_SENT"] == 1
    assert fake_cyberq.STATE["EMAIL_ATTEMPTS"] == 1
    email_xml = (await client.get("/email.xml")).text
    assert "<EMAIL_ATTEMPTS>1</EMAIL_ATTEMPTS>" in email_xml


async def test_wifi_form_round_trip_and_key_sentinel(client) -> None:
    sentinel = fake_cyberq.KEY_SENTINEL
    await client.post(
        "/wifi.htm",
        content="EEAUTOFLUSH=0&HTTP_USER=&HTTP_PORT=8080"
        "&MULTI_WIFI_CHOICE=1"
        f"&IP=10.0.0.50&GW=10.0.0.1&NM=255.255.255.0&DNS=10.0.0.1"
        "&WIFIMODE=0&DHCP=1&SSID=newnet&WIFI_ENC=5"
        f"&WIFI_KEY={sentinel}"
        f"&IP2=&GW2=&NM2=&DNS2=&WIFIMODE2=1&DHCP2=1&SSID2=away2"
        f"&WIFI_ENC2=0&WIFI_KEY2={sentinel}&EEAUTOFLUSH=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    # sentinel keys keep the stored values
    assert fake_cyberq.STATE["WIFI_KEY"] == "fixture-wifi-key"
    assert fake_cyberq.STATE["WIFI_KEY2"] == "fixture-away-key"
    # everything else applies
    assert fake_cyberq.STATE["HTTP_PORT"] == 8080
    assert fake_cyberq.STATE["MULTI_WIFI_CHOICE"] == 1
    assert fake_cyberq.STATE["SSID"] == "newnet"
    assert fake_cyberq.STATE["WIFI_ENC"] == 5
    assert fake_cyberq.STATE["SSID2"] == "away2"
    config = (await client.get("/config.xml")).text
    assert "<SSID>newnet</SSID>" in config
    assert "<HTTP_PORT>8080</HTTP_PORT>" in config

    # a real new key overwrites the stored one
    await client.post(
        "/wifi.htm",
        content=f"EEAUTOFLUSH=0&WIFI_KEY=fresh-key&EEAUTOFLUSH=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert fake_cyberq.STATE["WIFI_KEY"] == "fresh-key"


async def test_scan_and_booting_are_recorded(client) -> None:
    assert fake_cyberq.STATE["SCANS"] == 0
    await client.post(
        "/scan.htm",
        content="WIFI_SCAN_START=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert fake_cyberq.STATE["SCANS"] == 1

    assert fake_cyberq.STATE["REBOOTS"] == 0
    await client.post(
        "/booting.htm",
        content="RESET_CPU=1&Yes=Yes",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert fake_cyberq.STATE["REBOOTS"] == 1


async def test_unknown_fields_are_ignored_like_the_device(client) -> None:
    await client.post(
        "/",
        content="COOK_SET=200&BOGUS_FIELD=x&_COOK_SET=200&IGNOREDTAG=3",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert fake_cyberq.STATE["COOK_SET"] == 2000
    assert "BOGUS_FIELD" not in fake_cyberq.STATE


async def test_request_log_records_forms_but_not_itself(client) -> None:
    await client.get("/all.xml")
    await client.post(
        "/",
        content="COOK_SET=210&EEAUTOFLUSH=0&EEAUTOFLUSH=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    log = (await client.get("/_log")).json()
    assert [entry["path"] for entry in log] == ["/all.xml", "/"]
    post_entry = log[-1]
    assert post_entry["method"] == "POST"
    assert ("COOK_SET", "210") in {
        (name, value) for name, value in post_entry["form"]
    }
    # fetching the log did not add an entry
    log2 = (await client.get("/_log")).json()
    assert len(log2) == len(log)
    # DELETE clears it
    await client.delete("/_log")
    assert (await client.get("/_log")).json() == []


async def test_reset_restores_defaults(client) -> None:
    await client.post(
        "/control.htm",
        content="COOKHOLD=300&EEAUTOFLUSH=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert fake_cyberq.STATE["COOKHOLD"] == 3000
    fake_cyberq.reset()
    assert fake_cyberq.STATE["COOKHOLD"] == 2000
    assert fake_cyberq.STATE["SSID"] == "fixture-net"
    assert fake_cyberq.REQUESTS == []
