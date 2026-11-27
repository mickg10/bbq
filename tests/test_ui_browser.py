"""Browser tests of the dashboard (slow; needs the `ui` extra).

The dashboard runs against dev/fake_cyberq.py on local ports, never a real
device. Playwright only loads pages and moves the mouse: no forms are used.
"""

from __future__ import annotations

import math
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

pytestmark = pytest.mark.slow
sync_api = pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory):
    fake_port, app_port = _free_port(), _free_port()
    db = tmp_path_factory.mktemp("ui") / "ui.db"
    uvicorn = [sys.executable, "-m", "uvicorn"]
    fake = subprocess.Popen(
        uvicorn + ["dev.fake_cyberq:app", "--port", str(fake_port), "--log-level", "warning"],
    )
    env = dict(os.environ, CYBERQ_URL=f"http://127.0.0.1:{fake_port}",
               DB_PATH=str(db), POLL_INTERVAL="1")
    app = subprocess.Popen(
        uvicorn + ["app.main:app", "--port", str(app_port), "--log-level", "warning"], env=env,
    )
    try:
        _wait(f"http://127.0.0.1:{fake_port}/all.xml")
        _wait(f"http://127.0.0.1:{app_port}/healthz")
        time.sleep(12)  # a few 5 s buckets of history for the 1h chart
        yield f"http://127.0.0.1:{app_port}"
    finally:
        for proc in (app, fake):
            proc.terminate()
            proc.wait(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            try:
                b = p.chromium.launch(channel="chrome")
            except Exception as exc:
                pytest.skip(f"no browser for playwright: {exc}")
        yield b
        b.close()


@pytest.fixture()
def dashboard_with_a_cook(tmp_path):
    """The dashboard against a fake CyberQ with a cook an hour old
    (tests/eta_device.py): the history seeded from the same script the live
    samples follow, so the estimate has a full hour to work on at once."""
    from app import db as appdb
    from app.device import parse_all_xml
    from dev import fake_cyberq
    from tests import eta_device

    fake_port, app_port = _free_port(), _free_port()
    db = tmp_path / "eta.db"
    started = int(time.time()) - 62 * 60           # an hour of history, and time for the app to come up
    conn = appdb.connect(str(db))
    for minute in range(63):
        appdb.insert_sample(conn, started + minute * 60,
                            parse_all_xml(fake_cyberq._all_xml(eta_device.state_at(started + minute * 60, started))))
    conn.close()
    uvicorn = [sys.executable, "-m", "uvicorn"]
    fake = subprocess.Popen(
        uvicorn + ["tests.eta_device:app", "--port", str(fake_port), "--log-level", "warning"],
        env=dict(os.environ, ETA_COOK_START=str(started)),
    )
    env = dict(os.environ, CYBERQ_URL=f"http://127.0.0.1:{fake_port}",
               DB_PATH=str(db), POLL_INTERVAL="1")
    app = subprocess.Popen(
        uvicorn + ["app.main:app", "--port", str(app_port), "--log-level", "warning"], env=env,
    )
    try:
        _wait(f"http://127.0.0.1:{fake_port}/all.xml")
        _wait(f"http://127.0.0.1:{app_port}/healthz")
        yield f"http://127.0.0.1:{app_port}"
    finally:
        for proc in (app, fake):
            proc.terminate()
            proc.wait(timeout=10)


def test_food_cards_show_when_the_food_will_be_done(dashboard_with_a_cook, browser):
    import datetime as dt
    import re

    page = browser.new_page(viewport={"width": 1280, "height": 1100})
    page.goto(dashboard_with_a_cook, wait_until="networkidle")
    eta1 = page.locator("#eta-food1")
    eta1.wait_for(state="visible", timeout=3000)
    # asked for with the page, not on the first 10-second tick: up within three seconds
    page.wait_for_function("document.querySelector('#eta-food1').textContent.includes('at about')", timeout=3000)
    shown = re.fullmatch(r"203 °F at about (\d\d):(\d\d) \(±(\d+) min\)", eta1.inner_text())
    assert shown, eta1.inner_text()
    # a lump with a 2 h time constant, nothing evaporating, goes from 190 °F to 203 °F in 55 minutes
    due = dt.datetime.fromtimestamp(time.time() + 2 * 3600 * math.log(35 / 22))
    assert abs((int(shown[1]) * 60 + int(shown[2])) - (due.hour * 60 + due.minute)) <= 3
    assert 15 <= int(shown[3]) <= 40
    assert page.locator("#eta-food2").inner_text() == "stalled: ETA uncertain"
    assert page.locator("#eta-food2").get_attribute("title").startswith("195 °F at about ")
    assert page.locator("#eta-food3").inner_text() == ""        # open: nothing shown
    assert page.locator("#eta-cook").count() == 0               # the cook card has no such line
    page.close()


def test_chart_hover_shows_time_and_coloured_values(dashboard, browser):
    page = browser.new_page(viewport={"width": 1280, "height": 1100})
    page.goto(f"{dashboard}/?range=1h", wait_until="networkidle")
    over = page.locator("#chart .u-over")
    over.wait_for(timeout=15000)
    box = over.bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] / 2)
    tip = page.locator(".chart-tip")
    tip.wait_for(state="visible", timeout=5000)
    text = tip.inner_text()
    assert "°F" in text and "Cook" in text
    # each value is drawn in its series' colour
    colour = tip.locator(".tip-row b").first.evaluate("e => e.style.color")
    assert colour.startswith("rgb")
    page.mouse.move(box["x"] - 50, box["y"] - 50)
    tip.wait_for(state="hidden", timeout=5000)
    page.close()


def test_chart_range_survives_reload(dashboard, browser):
    page = browser.new_page()
    page.goto(dashboard, wait_until="networkidle")
    page.click("#range-btns [data-range='6h']")
    assert "range=6h" in page.url
    page.goto(dashboard, wait_until="networkidle")  # no query: localStorage
    assert page.locator("#range-btns .active").get_attribute("data-range") == "6h"
    page.goto(f"{dashboard}/?range=1h", wait_until="networkidle")  # the URL wins
    assert page.locator("#range-btns .active").get_attribute("data-range") == "1h"
    page.close()
