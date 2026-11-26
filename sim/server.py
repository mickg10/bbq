"""HTTP facade: a simulated CyberQ on the wire (docs/SIMULATOR.md).

Same endpoints, same bytes, same forms as the real box, because the pages,
the XML and the form handling are ``dev.fake_cyberq.py``'s — fed with the
simulator's state instead of the fake's. Only what is behind them differs:
temperatures, statuses, the fan and the timer come live from the simulator,
stepped in the background at ``speed``× real time, and every mainForm POST is
applied to the simulated device.

    python -m sim serve --speed 60 --port 8099
    CYBERQ_URL=http://127.0.0.1:8099 uvicorn app.main:app --port 8080

Bytes: what goes out is what the device put out. The pages and XML come from
the shared templates, which carry the recon captures' content; what the
templates leave out — the device's CRLF line endings, the comment block it
opens config.xml with, its XML documents' missing final newline and the
trailing spaces on two of their lines — :func:`device_bytes` puts back on
the way out. tests/sim/test_server.py holds the result against the captures,
byte for byte. (There is no capture of ``email.xml``, so it goes out as the
template writes it.)

As in the fake, ``booting.htm`` (reboot) is accepted and *recorded only* —
there is nothing here to reset — and ``GET /_log`` / ``DELETE /_log`` expose
and clear the request log, for the dashboard's allowlist tests.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from dev import fake_cyberq
from sim.clock import SimClock
from sim.device import CyberQDevice
from sim.engine import PROBE_NAMES, Simulator

FIXTURES_DIR = fake_cyberq.FIXTURES_DIR

_TEMP_KEYS = tuple(f"{name.upper()}_TEMP" for name in PROBE_NAMES)
_STATUS_KEYS = tuple(f"{name.upper()}_STATUS" for name in PROBE_NAMES)

#: What the device prints that the shared templates leave out, by XML endpoint.
#: (Seen in the recon captures; the templates match them tag for tag.)
_XML_QUIRKS: dict[str, dict] = {
    "/all.xml": {
        "comments": "",
        "padding": {"<!--all temperatures sent by browser": "  ", "<TIMER_CURR>": "   "},
    },
    "/status.xml": {
        "comments": "",
        "padding": {"<!--all temperatures sent by browser": "  "},
    },
    "/config.xml": {
        # the template for this one starts straight at <COOK>
        "comments": (
            "   <!--this is similar to all.xml, but with more values-->\n"
            "   <!--all temperatures are displayed in tenths F, regardless of setting of unit-->\n"
            "   <!--all temperatures sent by browser to unit should be in F.  you can send"
            " tenths F with a decimal place, ex: 123.5-->\n"
        ),
        "padding": {"<!--all temperatures sent by browser": "  ", "<TIMER_CURR>": "   "},
    },
}


def device_bytes(text: str, crlf: bool = True, xml: str | None = None) -> bytes:
    """The bytes the real box puts on the wire for a rendered body.

    Every line of every capture in ``recon/`` ends in CRLF, while the shared
    templates are written with LF: put it back. XML documents (pass their
    endpoint as ``xml``) also stop at the closing tag with no final newline,
    and open with a comment block and pad two lines with trailing spaces as
    in :data:`_XML_QUIRKS`. With this, and the stock pages, what the simulator
    serves is what the device served.
    """
    if xml is not None:
        quirks = _XML_QUIRKS.get(xml, {})
        if quirks.get("comments"):
            text = text.replace("<nutcallstatus>\n", "<nutcallstatus>\n" + quirks["comments"], 1)
        padded = []
        for line in text.rstrip("\n").split("\n"):
            stripped = line.lstrip()
            pad = next((p for start, p in quirks.get("padding", {}).items() if stripped.startswith(start)), "")
            padded.append(line + pad)
        text = "\n".join(padded)
    return text.replace("\n", "\r\n").encode() if crlf else text.encode()


def _tenths(value: float) -> int:
    return int(round(value * 10.0))


async def _step_forever(sim: Simulator, poll_s: float) -> None:
    """Keep the simulator in step with the wall clock."""
    while True:
        for _ in range(sim.clock.steps_due(sim.dt)):
            sim.step()
        await asyncio.sleep(poll_s)


class WireState:
    """The simulated device as the wire sees it.

    ``settings`` is a dict in dev.fake_cyberq's STATE layout, seeded from its
    DEFAULTS (placeholder e-mail and WiFi identity included). The settings the
    control loop runs on belong to the simulated device: a POST hands them to
    the device's clamping setters, and the device's values are what goes back
    on the wire. Everything else is stored and served as the device's CGI
    would store and serve it.
    """

    def __init__(self, sim: Simulator, base: dict | None = None) -> None:
        self.sim = sim
        self.device: CyberQDevice = sim.device
        self.settings = dict(fake_cyberq.DEFAULTS if base is None else base)
        self.pull()

    def pull(self) -> None:
        """Write the device's settings into the wire dict."""
        s, w = self.device.settings, self.settings
        setpoints = (s.cook_set_f, *s.food_set_f)
        for key, value in zip(("COOK_SET", "FOOD1_SET", "FOOD2_SET", "FOOD3_SET"), setpoints):
            w[key] = _tenths(value)
        w["COOKHOLD"] = _tenths(s.cookhold_f)
        w["ALARMDEV"] = _tenths(s.alarmdev_f)
        w["PROPBAND"] = w["COOK_PROPBAND"] = _tenths(s.propband_f)
        w["CYCTIME"] = w["COOK_CYCTIME"] = s.cyctime_s
        w["COOK_RAMP"] = s.ramp
        w["OPENDETECT"] = int(s.opendetect)
        w["TIMEOUT_ACTION"] = int(s.timeout_action)

    def push(self) -> None:
        """Hand the wire dict's control settings to the device, clamped."""
        d, w = self.device, self.settings
        d.set_cook_set_tenths(w["COOK_SET"])
        for index, key in enumerate(("FOOD1_SET", "FOOD2_SET", "FOOD3_SET")):
            d.set_food_set_tenths(index, w[key])
        d.set_cookhold_tenths(w["COOKHOLD"])
        d.set_alarmdev_tenths(w["ALARMDEV"])
        d.set_propband_tenths(w["PROPBAND"])
        d.set_cyctime_s(w["CYCTIME"])
        d.set_ramp(w["COOK_RAMP"])
        d.set_opendetect(bool(w["OPENDETECT"]))
        d.set_timeout_action(w["TIMEOUT_ACTION"])
        self.pull()

    def apply_form(self, path: str, form) -> None:
        """A mainForm POST, applied the way the device's CGI applies it."""
        fake_cyberq.apply_form(path, form, self.settings)
        self.push()
        timer = form.get("COOK_TIMER")
        if timer is not None:
            try:
                self.device.set_timer_text(str(timer))
            except ValueError:
                pass  # not HH:MM:SS: ignored, like any other bad field

    def render_state(self) -> dict:
        """The settings plus what the simulator is showing right now."""
        out = self.device.output
        state = dict(self.settings)
        for index, key in enumerate(_TEMP_KEYS):
            state[key] = self.sim.probes[index].reading  # None -> OPEN
        for key, status in zip(_STATUS_KEYS, out.statuses):
            state[key] = int(status)
        state["COOK_SET"] = _tenths(self.device.reported_cook_set_f)  # hold target while holding
        state["OUTPUT_PERCENT"] = out.fan_pct
        # the timer as it stands right now, so a timer that was just set is
        # already showing when the device answers the write that set it
        state["TIMER_CURR"] = self.device.timer.text
        state["TIMER_STATUS"] = self.device.timer.status
        state["FAN_SHORTED"] = 0
        return state


def create_app(
    sim: Simulator, *, speed: float = 1.0, poll_s: float = 0.02, crlf: bool = True
) -> FastAPI:
    """A FastAPI app that *is* a simulated CyberQ, driven by ``sim``.

    The simulator is stepped by a background task on the app's own event
    loop, so handlers and the model never run at the same time and need no
    locking. Nothing steps until the app's lifespan starts. ``crlf`` off
    serves the shared templates' LF endings instead of the device's.
    """
    wire = WireState(sim)
    requests: list[dict] = []
    sim.clock = SimClock(t=sim.clock.t, speed=speed, realtime=True)

    def html(text: str) -> HTMLResponse:
        return HTMLResponse(device_bytes(text, crlf))

    def xml(text: str, endpoint: str) -> PlainTextResponse:
        return PlainTextResponse(device_bytes(text, crlf, endpoint), media_type="text/xml")

    def page(name: str) -> Response:
        try:
            return html(fake_cyberq.render_page(name, wire.render_state()))
        except ValueError:  # a setting that is not a number: serve the fixture
            return html((FIXTURES_DIR / name).read_text())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        sim.clock.start()
        task = asyncio.create_task(_step_forever(sim, poll_s))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Simulated CyberQ WiFi", lifespan=lifespan)

    @app.middleware("http")
    async def _record_request(request: Request, call_next):
        if request.url.path != "/_log":  # test instrumentation, not device I/O
            requests.append({"method": request.method, "path": request.url.path})
        return await call_next(request)

    # -- pages --------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index() -> Response:
        return page("index.html")

    @app.get("/control.htm", response_class=HTMLResponse)
    async def control_page() -> Response:
        return page("control.htm")

    @app.get("/system.htm", response_class=HTMLResponse)
    async def system_page() -> Response:
        return page("system.htm")

    @app.get("/email.htm", response_class=HTMLResponse)
    async def email_page() -> Response:
        try:
            return html(fake_cyberq.render_email_page(wire.render_state()))
        except FileNotFoundError:
            return page("email.htm")

    @app.get("/wifi.htm", response_class=HTMLResponse)
    async def wifi_page() -> Response:
        try:
            return html(fake_cyberq.render_wifi_page(wire.render_state()))
        except FileNotFoundError:
            return page("wifi.htm")

    @app.get("/reboot.htm", response_class=HTMLResponse)
    async def reboot_page() -> Response:
        return page("reboot.htm")

    # -- XML ----------------------------------------------------------------

    @app.get("/all.xml")
    async def all_xml() -> Response:
        return xml(fake_cyberq.render_all_xml(wire.render_state()), "/all.xml")

    @app.get("/status.xml")
    async def status_xml() -> Response:
        return xml(fake_cyberq.render_status_xml(wire.render_state()), "/status.xml")

    # The stock UI polls via POST status.xml with an IGNOREDTAG body (an iOS 6
    # workaround on the device's CGI). Accept it like the real device does.
    @app.post("/status.xml")
    async def status_xml_post() -> Response:
        return xml(fake_cyberq.render_status_xml(wire.render_state()), "/status.xml")

    @app.get("/config.xml")
    async def config_xml() -> Response:
        return xml(fake_cyberq.render_config_xml(wire.render_state()), "/config.xml")

    @app.get("/email.xml")
    async def email_xml_get() -> Response:
        return xml(fake_cyberq.render_email_xml(wire.render_state()), "/email.xml")

    @app.post("/email.xml")
    async def email_xml_post() -> Response:
        return xml(fake_cyberq.render_email_xml(wire.render_state()), "/email.xml")

    # -- forms --------------------------------------------------------------

    async def _handle_form(request: Request, name: str) -> Response:
        """Accept a stock mainForm POST to its own page and apply it."""
        form = await request.form()
        if requests and requests[-1]["path"] == request.url.path:
            requests[-1]["form"] = list(form.multi_items())
        wire.apply_form(request.url.path, form)
        # Real device re-renders the page
        return page(name)

    @app.post("/")
    async def main_form(request: Request) -> Response:
        return await _handle_form(request, "index.html")

    @app.post("/control.htm")
    async def control_form(request: Request) -> Response:
        return await _handle_form(request, "control.htm")

    @app.post("/system.htm")
    async def system_form(request: Request) -> Response:
        return await _handle_form(request, "system.htm")

    @app.post("/email.htm")
    async def email_form(request: Request) -> Response:
        return await _handle_form(request, "email.htm")

    @app.post("/wifi.htm")
    async def wifi_form(request: Request) -> Response:
        return await _handle_form(request, "wifi.htm")

    @app.post("/scan.htm")
    async def scan_form(request: Request) -> Response:
        """WIFI_SCAN_START=1 — the stock scan form posts here."""
        form = await request.form()
        if requests and requests[-1]["path"] == "/scan.htm":
            requests[-1]["form"] = list(form.multi_items())
        if form.get("WIFI_SCAN_START") == "1":
            wire.settings["SCANS"] += 1
        return HTMLResponse(
            "<html><body><H2>Scanning...</H2>"
            "<p>The simulator has no radios; nothing to find.</p></body></html>"
        )

    @app.post("/booting.htm")
    async def booting_form(request: Request) -> Response:
        """RESET_CPU=1 — a reboot. Recorded and otherwise ignored: there is
        nothing here to reset, and our client must never POST here."""
        form = await request.form()
        if requests and requests[-1]["path"] == "/booting.htm":
            requests[-1]["form"] = list(form.multi_items())
        if form.get("RESET_CPU") == "1":
            wire.settings["REBOOTS"] += 1
        return HTMLResponse(
            "<html><body><H2>Rebooting...</H2>"
            "<p>(the simulator does not actually reboot)</p></body></html>"
        )

    # -- test instrumentation, as in the fake ---------------------------------

    @app.get("/_log")
    async def get_log() -> JSONResponse:
        return JSONResponse(requests)

    @app.delete("/_log")
    async def clear_log() -> JSONResponse:
        requests.clear()
        return JSONResponse({"cleared": True})

    app.state.sim = sim
    app.state.wire = wire
    app.state.requests = requests
    return app
