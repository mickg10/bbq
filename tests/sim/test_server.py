"""The HTTP facade: the fake's routes, the real device's bytes, a live model.

Everything here runs in-process without the app's lifespan, so nothing steps
unless a test steps the simulator itself. tests/sim/test_server_live.py covers
the served, stepping app and the dashboard against it.
"""

from __future__ import annotations

import pathlib
import xml.etree.ElementTree as ET

import httpx
import pytest

from app.device import (
    parse_all_xml,
    parse_config_xml,
    parse_email_page,
    parse_status_xml,
    parse_wifi_page,
)
from dev import fake_cyberq
from sim import server
from sim.device import Status, TimeoutAction
from sim.food import Food, preset

RECON = pathlib.Path(__file__).resolve().parents[2] / "recon"
BASE = "http://sim.test"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}
PAGES = ("/", "/control.htm", "/system.htm", "/email.htm", "/wifi.htm", "/reboot.htm")
DOCUMENTS = ("/all.xml", "/status.xml", "/config.xml", "/email.xml")
PAGE_FILES = ("index.html", "control.htm", "system.htm", "reboot.htm")


@pytest.fixture()
def served(make_sim):
    """A simulator 40 minutes into a cold start with a brisket on food1, its
    app, and the wire state behind it."""
    sim = make_sim(
        cook_set_f=225.0, food=Food(preset("brisket"), temp_f=40.0), max_rows=600,
    )
    sim.run(40 * 60)
    app = server.create_app(sim, speed=1.0)
    return sim, app, app.state.wire


@pytest.fixture()
async def client(served):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=served[1]), base_url=BASE
    ) as c:
        yield c


@pytest.fixture()
async def fake_client(served, monkeypatch):
    """The fake, frozen (no drift on reads) and holding the simulator's
    exact state, so the two can be compared like for like."""
    fake_cyberq.reset()
    monkeypatch.setattr(fake_cyberq, "_tick", lambda: None)
    fake_cyberq.STATE.update(served[2].render_state())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake_cyberq.app), base_url=BASE
    ) as c:
        yield c
    fake_cyberq.reset()


async def _fetch(client, method: str, path: str):
    return await client.request(method, path, content="IGNOREDTAG=0" if method == "POST" else None)


def _lines(body: bytes) -> list[bytes]:
    """A body as lines, without line endings, trailing whitespace or comments."""
    return [
        line.rstrip()
        for line in body.replace(b"\r\n", b"\n").split(b"\n")
        if line.strip() and not line.lstrip().startswith(b"<!--")
    ]


async def _get(app, path: str):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE
    ) as c:
        return await c.get(path)


# -- same routes, same bytes as the fake --------------------------------------

async def test_every_endpoint_is_the_fake_serving_the_same_state(client, fake_client):
    """Pages: the fake's, to the byte, line endings aside. XML: the fake's
    too, except that the simulator also restores what the fake's templates
    leave out of the device's documents (its comment block, two padded lines,
    no final newline). test_xml_is_byte_for_byte_the_real_device_s pins the
    result to the captures."""
    for path in PAGES:
        theirs, ours = await fake_client.get(path), await client.get(path)
        assert (ours.status_code, ours.headers["content-type"]) == (
            theirs.status_code, theirs.headers["content-type"]
        ), path
        assert ours.content.replace(b"\r\n", b"\n") == theirs.content, path
    for method, path in [("GET", p) for p in DOCUMENTS] + [
        ("POST", "/status.xml"), ("POST", "/email.xml"),
    ]:
        theirs, ours = await _fetch(fake_client, method, path), await _fetch(client, method, path)
        assert (ours.status_code, ours.headers["content-type"]) == (
            theirs.status_code, theirs.headers["content-type"]
        ), path
        assert _lines(ours.content) == _lines(theirs.content), path


def test_the_route_table_is_the_fake_s(served):
    def table(app) -> set[tuple[str, str]]:
        return {
            (method, route.path)
            for route in app.routes
            for method in getattr(route, "methods", ())
        }

    assert table(served[1]) == table(fake_cyberq.app)


async def test_lines_end_like_the_device_s_not_like_the_templates(make_sim):
    crlf = server.create_app(make_sim(), speed=1.0)
    lf_only = server.create_app(make_sim(), speed=1.0, crlf=False)
    for path in ("/", "/all.xml"):
        device = (await _get(crlf, path)).content
        template = (await _get(lf_only, path)).content
        assert b"\r\n" not in template and b"\n" in template
        assert device.count(b"\n") == device.count(b"\r\n") > 0  # CRLF, no bare LF
        assert device == template.replace(b"\n", b"\r\n")


async def test_nothing_steps_until_the_app_runs(served, client):
    sim = served[0]
    steps = sim.steps
    for path in PAGES + DOCUMENTS:
        await client.get(path)
    assert sim.steps == steps


# -- byte for byte the real device --------------------------------------------

def _capture(name: str) -> pathlib.Path:
    path = RECON / name
    if not path.exists():
        pytest.skip(f"recon/{name} is a gitignored local capture")
    return path


@pytest.mark.parametrize("name", PAGE_FILES)
def test_pages_are_byte_for_byte_the_real_device_s(name):
    """recon/ holds raw captures of the box. With the settings it had when
    they were taken (the fake's DEFAULTS are those), the simulator serves
    those very bytes."""
    served = server.device_bytes(fake_cyberq.render_page(name, dict(fake_cyberq.DEFAULTS)))
    assert served == _capture(name).read_bytes()


def _captured_state(path: pathlib.Path) -> dict:
    """A capture flattened into a STATE-layout dict: every element's text by
    tag, sections' children included."""
    state = dict(fake_cyberq.DEFAULTS)
    for element in ET.parse(path).getroot():
        for node in element if len(element) else (element,):
            state[node.tag] = node.text
    return state


@pytest.mark.parametrize("name", ("all.xml", "status.xml", "config.xml"))
def test_xml_is_byte_for_byte_the_real_device_s(name):
    """Rendered at the readings and settings of its capture — credentials
    included, for config.xml — the simulator's XML is the capture's bytes.
    Nothing from the capture is printed if this fails."""
    path = _capture(name)
    render = {
        "all.xml": fake_cyberq.render_all_xml,
        "status.xml": fake_cyberq.render_status_xml,
        "config.xml": fake_cyberq.render_config_xml,
    }[name]
    served = server.device_bytes(render(_captured_state(path)), xml=f"/{name}")
    real = path.read_bytes()
    if served != real:
        real_lines, served_lines = real.split(b"\r\n"), served.split(b"\r\n")
        line = next(i for i, (a, b) in enumerate(zip(real_lines, served_lines)) if a != b)
        pytest.fail(
            f"{name}: served bytes differ from the capture on line {line + 1} "
            f"(that line is {len(real_lines[line])} bytes in the capture, "
            f"{len(served_lines[line])} served; documents are {len(real)} vs {len(served)} bytes)"
        )


async def test_the_served_documents_parse_with_the_dashboard_s_parsers(client, served):
    sim = served[0]
    sim.preheat(225.0)
    for _ in range(5):
        sim.step()
    all_xml = parse_all_xml((await client.get("/all.xml")).text)
    status = parse_status_xml((await client.get("/status.xml")).text)
    config = parse_config_xml((await client.get("/config.xml")).text)
    assert 224.0 < all_xml["probes"]["cook"]["temp"] < 226.0
    assert all_xml["probes"]["cook"]["set"] == 225.0
    assert 55.0 < all_xml["probes"]["food1"]["temp"] < 70.0   # the brisket, 40 min in
    assert all_xml["probes"]["food2"]["temp"] is None         # OPEN
    assert all_xml["probes"]["food2"]["status"] == Status.ERROR
    assert status["probes"]["cook"]["temp"] == all_xml["probes"]["cook"]["temp"]
    assert status["output_pct"] == all_xml["output_pct"] == 0  # at target: fan off
    assert (config["control"]["propband"], config["control"]["cyctime"]) == (300, 6)
    assert config["fwver"] == "3.1"


# -- forms reach the simulated device -----------------------------------------

async def test_the_main_form_steers_the_device_and_starts_its_timer(client, served):
    sim = served[0]
    before = sim.device.output.fan_pct
    response = await client.post(
        "/",
        content="EEAUTOFLUSH=0&COOK_SET=250&FOOD1_SET=190.5&COOK_NAME=Brisket"
                "&EEAUTOFLUSH=1&COOK_TIMER=01:30:00",
        headers=FORM,
    )
    assert response.status_code == 200
    assert sim.device.settings.cook_set_f == 250.0
    assert sim.device.settings.food_set_f[0] == 190.5
    assert sim.device.timer.text == "01:30:00"
    for _ in range(60):
        sim.step()
    sample = parse_all_xml((await client.get("/all.xml")).text)
    assert sample["probes"]["cook"]["set"] == 250.0
    assert sample["probes"]["cook"]["name"] == "Brisket"
    assert (sample["timer_curr"], sample["timer_status"]) == ("01:29:00", 1)
    # chasing a target 25 °F further off than a moment ago: most of the fan
    assert sample["output_pct"] >= 80 > before


async def test_the_control_form_changes_what_the_controller_runs_on(client, make_sim):
    sim = make_sim(preheat=215.0, cook_set_f=225.0)  # 10 under target, fire holding it
    app = server.create_app(sim, speed=1.0)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE
    ) as c:
        for _ in range(5):
            sim.step()
        # 10 °F short, in a 30 °F band: a third of the fan (probe noise wobbles it)
        assert 30 <= sim.device.output.fan_pct <= 36
        response = await c.post(
            "/control.htm",
            content="EEAUTOFLUSH=0&COOKHOLD=180&ALARMDEV=25&PROPBAND=10&CYCTIME=8"
                    "&COOK_RAMP=1&OPENDETECT=0&TIMEOUT_ACTION=1&EEAUTOFLUSH=1",
            headers=FORM,
        )
        assert response.status_code == 200
        s = sim.device.settings
        assert (s.cookhold_f, s.alarmdev_f, s.propband_f, s.cyctime_s) == (180.0, 25.0, 10.0, 8)
        assert (s.ramp, s.opendetect, s.timeout_action) == (1, False, TimeoutAction.HOLD)
        control = parse_config_xml((await c.get("/config.xml")).text)["control"]
        assert (control["cookhold"], control["propband"], control["cyctime"]) == (1800, 100, 8)
        cook = parse_all_xml((await c.get("/all.xml")).text)
        assert (cook["propband"], cook["cyctime"], cook["ramp"]) == (100, 8, 1)  # cook copies
        # not just stored: the same 10 °F error is now a whole band
        for _ in range(5):
            sim.step()
        assert sim.device.output.fan_pct == 100


async def test_setpoints_are_clamped_the_way_the_dashboard_would(client, served):
    sim = served[0]
    await client.post("/", content="COOK_SET=999&FOOD1_SET=-40&FOOD2_SET=180", headers=FORM)
    assert sim.device.settings.cook_set_f == 475.0
    assert sim.device.settings.food_set_f[:2] == [32.0, 180.0]
    all_xml = (await client.get("/all.xml")).text
    assert "<COOK_SET>4750</COOK_SET>" in all_xml
    assert "<FOOD1_SET>320</FOOD1_SET>" in all_xml


async def test_a_bad_field_is_ignored_like_the_device_does(client, served):
    sim = served[0]
    before = (sim.device.settings.cook_set_f, sim.device.timer.text)
    await client.post(
        "/", content="COOK_SET=abc&COOK_TIMER=25:99:99&BOGUS=1&_COOK_SET=1&EEAUTOFLUSH=1",
        headers=FORM,
    )
    assert (sim.device.settings.cook_set_f, sim.device.timer.text) == before


async def test_pass_through_settings_round_trip(client, served):
    """System, e-mail and WiFi settings mean nothing to the model: they are
    stored and served the way the device's CGI stores and serves them."""
    wire = served[2]
    await client.post(
        "/system.htm",
        content="MENU_SCROLLING=1&LCD_BACKLIGHT=70&LCD_CONTRAST=20&DEG_UNITS=1"
                "&ALARM_BEEPS=3&KEY_BEEPS=1&EEAUTOFLUSH=1",
        headers=FORM,
    )
    await client.post(
        "/email.htm",
        content="EEAUTOFLUSH=0&SMTP_HOST=relay.example.com&SMTP_PORT=587&SMTP_USER=me"
                "&SMTP_PWD=new-pass&SMTP_TO=you@example.com&SMTP_FROM=me@example.com"
                "&SMTP_SUBJ=Dinner&SMTP_ALERT=15&SMTP_NOW=1&EEAUTOFLUSH=1",
        headers=FORM,
    )
    await client.post(
        "/wifi.htm",
        content="EEAUTOFLUSH=0&HTTP_PORT=8080&MULTI_WIFI_CHOICE=1&IP=10.0.0.50"
                "&GW=10.0.0.1&NM=255.255.255.0&DNS=10.0.0.1&WIFIMODE=0&DHCP=1"
                "&SSID=newnet&WIFI_ENC=5&WIFI_KEY=********************&IP2=&GW2=&NM2=&DNS2="
                "&WIFIMODE2=1&DHCP2=1&SSID2=away2&WIFI_ENC2=0&WIFI_KEY2=fresh-key"
                "&EEAUTOFLUSH=1",
        headers=FORM,
    )
    assert (wire.settings["LCD_BACKLIGHT"], wire.settings["SMTP_HOST"]) == (
        70, "relay.example.com",
    )
    assert wire.settings["WIFI_KEY"] == "fixture-wifi-key"  # the mask means "keep it"
    assert wire.settings["WIFI_KEY2"] == "fresh-key"
    assert wire.settings["EMAILS_SENT"] == 1
    # and the dashboard's own parsers read them back off the served pages
    wifi = parse_wifi_page((await client.get("/wifi.htm")).text)
    assert (wifi["home"]["ssid"], wifi["http_port"]) == ("newnet", 8080)
    assert parse_email_page((await client.get("/email.htm")).text)["smtp_host"] == "relay.example.com"
    config = parse_config_xml((await client.get("/config.xml")).text)
    assert (config["system"]["lcd_backlight"], config["wifi"]["http_port"]) == (70, 8080)
    assert "<EMAIL_SUCCESS>1</EMAIL_SUCCESS>" in (await client.get("/email.xml")).text
    # the model never noticed
    assert served[0].device.settings.cook_set_f == 225.0


async def test_the_pages_show_what_was_written(client):
    await client.post("/", content="COOK_NAME=Brisket&COOK_SET=250&EEAUTOFLUSH=1", headers=FORM)
    index = (await client.get("/")).text
    assert 'document.mainForm.COOK_NAME.value = "Brisket";' in index
    assert "document.mainForm._COOK_SET.value = TempPICToHTML(2500, 0);" in index
    await client.post("/system.htm", content="DEG_UNITS=0&LCD_BACKLIGHT=55&EEAUTOFLUSH=1", headers=FORM)
    assert "var displayInF=0;" in (await client.get("/control.htm")).text
    assert "document.mainForm.LCD_BACKLIGHT.value = 55;" in (await client.get("/system.htm")).text


async def test_a_food_probe_with_nothing_on_it_reads_open(client):
    status = parse_status_xml((await client.get("/status.xml")).text)
    assert status["probes"]["food2"]["temp"] is None
    assert status["probes"]["food2"]["status"] == Status.ERROR
    assert status["fan_shorted"] == 0


async def test_scan_and_booting_are_recorded_and_change_nothing_else(client, served):
    sim, _, wire = served
    before = (sim.steps, sim.clock.t, sim.device.settings.cook_set_f, sim.device.hold)
    scan = await client.post("/scan.htm", content="WIFI_SCAN_START=1", headers=FORM)
    boot = await client.post("/booting.htm", content="RESET_CPU=1&Yes=Yes", headers=FORM)
    assert (scan.status_code, boot.status_code) == (200, 200)
    assert (wire.settings["SCANS"], wire.settings["REBOOTS"]) == (1, 1)
    assert (sim.steps, sim.clock.t, sim.device.settings.cook_set_f, sim.device.hold) == before
    log = (await client.get("/_log")).json()
    assert [(entry["path"], dict(entry["form"])) for entry in log[-2:]] == [
        ("/scan.htm", {"WIFI_SCAN_START": "1"}),
        ("/booting.htm", {"RESET_CPU": "1", "Yes": "Yes"}),
    ]


async def test_the_request_log_excludes_itself_and_clears(client):
    await client.get("/all.xml")
    await client.post("/status.xml", content="IGNOREDTAG=0")
    log = (await client.get("/_log")).json()
    assert [entry["method"] + " " + entry["path"] for entry in log] == [
        "GET /all.xml", "POST /status.xml",
    ]
    assert (await client.delete("/_log")).json() == {"cleared": True}
    assert (await client.get("/_log")).json() == []
