"""Device client for the CyberQ WiFi (task 3, reworked to the scope update).

Responsibilities:
- parse /all.xml, /status.xml, /config.xml and the setup pages (email, wifi),
- expose allowlisted reads and allowlisted, validated form POSTs for every
  page in the DESIGN inventory — everything except firmware reset,
- deny ``booting.htm``/``reboot.htm`` and any reset-like field name.

Safety model (see DESIGN.md and the package invariants in ``app/__init__``):
this is the ONLY module that talks to the device, and it is structurally
incapable of reaching anything dangerous:

* every request funnels through :meth:`CyberQClient._request`, which allows
  only ``GET`` on ``READ_PATHS`` and ``POST`` on ``WRITE_PATHS`` —
  ``booting.htm`` (the reboot/reset entry point) and ``reboot.htm`` are in
  neither set, so they can only be reached through the flag-gated
  ``reboot()`` (firmware reset has no code path at all);
* each write is a form POST to that page, built by a ``build_*_form``
  function from a fixed per-page field allowlist, with every value
  validated and clamped first; a final guard denies reset-like field names
  (``FACTORY|DEFAULT|FIRMWARE|UPGRADE|FLASH|RESET|REBOOT|BOOTING``) outright;
* the config parser drops credential fields on parse (``WIFI_KEY``,
  ``SMTP_PWD``); the wifi page parser never extracts ``HTTP_PASS`` or the
  WiFi keys at all. ``SMTP_PWD`` is the one exception: the email page
  serves the current password in the clear, and a stock form POST re-sends
  it — :meth:`CyberQClient.write_email` reads it back purely to echo it in
  the POST. It is held in a local variable for the duration of one request
  and never logged, stored or returned.

Wire format (confirmed from the recon captures):
- temperatures are tenths of °F in XML (``757`` = 75.7 °F, ``OPEN`` = no
  probe); POSTed temperatures are °F — whole on the index page (the stock
  UI's ``Math.round``), with up to one decimal on control.htm
  (``Math.round(v*10)/10``);
- ``EEAUTOFLUSH`` is sent 0-then-1 on every page except system.htm, whose
  stock page has the 0 copy outside the form (so only 1 is sent);
- ``WIFI_KEY``/``WIFI_KEY2``: the 20-asterisk mask means "keep the stored
  key";
- the device reads the hidden ``COOKHOLD``/``ALARMDEV``/``PROPBAND`` (no
  underscore) — the ``_``-prefixed inputs are display copies.
"""

from __future__ import annotations

import ipaddress
import math
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urlencode

import httpx

# ---------------------------------------------------------------------------
# allowlists — the safety core of this module
# ---------------------------------------------------------------------------

#: The only paths the client may GET. "/" and the setup pages round out the
#: XML endpoints (the pages carry current form values for wifi/email).
READ_PATHS = frozenset(
    {"/", "/all.xml", "/status.xml", "/config.xml",
     "/control.htm", "/system.htm", "/email.htm", "/wifi.htm"}
)

#: The only paths the client may POST to — each page's mainForm posts to
#: its own page; scan.htm carries WIFI_SCAN_START.
WRITE_PATHS = frozenset(
    {"/", "/control.htm", "/system.htm", "/email.htm", "/wifi.htm", "/scan.htm"}
)

#: Never reachable through the generic read/write paths. The ALLOW_REBOOT
#: flag unlocks a single confirm-gated reboot path (``reboot()``); there is
#: no code path that could ever perform a factory/firmware reset.
DENIED_PATHS = frozenset({"/booting.htm", "/reboot.htm"})

#: The reboot form body (reboot.htm's "Yes" form). The ONLY sanctioned
#: reset-like write, reachable only via CyberQClient.reboot() with
#: allow_reboot enabled (default off) — never through the config writers.
REBOOT_FORM = [("RESET_CPU", "1")]

#: Fields the stock mainForm POSTs to "/". Nothing else may ever be sent.
WRITE_FIELDS = frozenset(
    {
        "EEAUTOFLUSH",
        "COOK_NAME",
        "FOOD1_NAME",
        "FOOD2_NAME",
        "FOOD3_NAME",
        "COOK_SET",
        "FOOD1_SET",
        "FOOD2_SET",
        "FOOD3_SET",
        "COOK_TIMER",
    }
)

CONTROL_WRITE_FIELDS = frozenset(
    {
        "EEAUTOFLUSH", "COOKHOLD", "TIMEOUT_ACTION", "ALARMDEV",
        "COOK_RAMP", "OPENDETECT", "CYCTIME", "PROPBAND",
    }
)

SYSTEM_WRITE_FIELDS = frozenset(
    {
        "EEAUTOFLUSH", "MENU_SCROLLING", "LCD_BACKLIGHT", "LCD_CONTRAST",
        "DEG_UNITS", "ALARM_BEEPS", "KEY_BEEPS",
    }
)

EMAIL_WRITE_FIELDS = frozenset(
    {
        "EEAUTOFLUSH", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PWD",
        "SMTP_TO", "SMTP_FROM", "SMTP_SUBJ", "SMTP_ALERT", "SMTP_NOW",
    }
)

WIFI_WRITE_FIELDS = frozenset(
    {
        "EEAUTOFLUSH", "HTTP_USER", "HTTP_PASS", "HTTP_PORT",
        "MULTI_WIFI_CHOICE",
        "IP", "GW", "NM", "DNS", "WIFIMODE", "DHCP", "SSID", "WIFI_ENC",
        "WIFI_KEY",
        "IP2", "GW2", "NM2", "DNS2", "WIFIMODE2", "DHCP2", "SSID2",
        "WIFI_ENC2", "WIFI_KEY2",
    }
)

SCAN_WRITE_FIELDS = frozenset({"WIFI_SCAN_START"})

#: Every field any write may carry.
ALL_WRITE_FIELDS = (
    WRITE_FIELDS | CONTROL_WRITE_FIELDS | SYSTEM_WRITE_FIELDS
    | EMAIL_WRITE_FIELDS | WIFI_WRITE_FIELDS | SCAN_WRITE_FIELDS
)

#: Credential fields: write-only. Never logged, stored, or returned.
SECRET_FIELDS = frozenset({"SMTP_PWD", "HTTP_PASS", "WIFI_KEY", "WIFI_KEY2"})

#: The WiFi key mask the device serves; submitting it back means "keep the
#: stored key" (see submitFrom_checkKey in the stock wifi.htm).
KEY_SENTINEL = "********************"

PROBES = ("cook", "food1", "food2", "food3")
_PROBE_FIELD = {"cook": "COOK", "food1": "FOOD1", "food2": "FOOD2", "food3": "FOOD3"}

#: Probe status codes, as in the stock UI's statusValToStr.
STATUS_TEXT = ("OK", "HIGH", "LOW", "DONE", "ERROR", "HOLD", "ALARM", "SHUTDOWN")

#: Limits from DESIGN.md. The stock UI's maxlength is 31; we hold v1 to the
#: design's tighter 16.
NAME_MAX = 16
SET_LIMITS = {
    "cook": (32, 475),  # °F
    "food1": (32, 300),
    "food2": (32, 300),
    "food3": (32, 300),
}

_TIMER_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})$")

#: A field name that can never be sent, whatever the page: firmware/factory
#: reset, upgrade, flash, reboot. (DESIGN: "any field matching
#: /FACTORY|DEFAULT|FIRMWARE|UPTGRADE|FLASH/i", plus the RESET/REBOOT family.)
_RESET_FIELD_RE = re.compile(
    r"FACTORY|DEFAULT|FIRMWARE|UPGRADE|FLASH|RESET|REBOOT|BOOTING", re.I
)


class DeviceUrlError(RuntimeError):
    """A request outside the path allowlist was attempted (never sent)."""


class DeviceParseError(ValueError):
    """The device returned XML/HTML we could not parse."""


class WriteValidationError(ValueError):
    """A write value failed validation (never sent to the device)."""


# ---------------------------------------------------------------------------
# parsing (pure functions — no I/O)
# ---------------------------------------------------------------------------

def _text_of(parent: ET.Element, tag: str) -> str | None:
    element = parent.find(tag)
    if element is None or element.text is None:
        return None
    return element.text.strip()


def _int_of(parent: ET.Element, tag: str) -> int:
    value = _text_of(parent, tag)
    if value is None:
        raise DeviceParseError(f"missing <{tag}>")
    try:
        return int(value)
    except ValueError as exc:
        raise DeviceParseError(f"non-integer <{tag}>: {value!r}") from exc


def _temp(value: str | None) -> float | None:
    """Tenths of °F on the wire -> °F; "OPEN" (no probe) -> None."""
    if value is None:
        return None
    if value == "OPEN":
        return None
    try:
        return int(value) / 10
    except ValueError as exc:
        raise DeviceParseError(f"bad temperature {value!r}") from exc


def _parse_probe_block(parent: ET.Element, field: str) -> dict:
    name = _text_of(parent, f"{field}_NAME")
    temp = _temp(_text_of(parent, f"{field}_TEMP"))
    set_raw = _text_of(parent, f"{field}_SET")
    status = _int_of(parent, f"{field}_STATUS")
    if set_raw in (None, "OPEN"):
        raise DeviceParseError(f"missing setpoint for {field}")
    return {
        "name": name if name is not None else "",
        "temp": temp,
        "set": int(set_raw) / 10,
        "status": status,
    }


def parse_all_xml(text: str) -> dict:
    """Parse /all.xml (the poller's source) into a sample dict.

    Shape::

        {"probes": {"cook": {"name", "temp", "set", "status"}, ...},
         "output_pct", "timer_curr", "timer_status", "deg_units",
         "cyctime", "propband", "ramp"}
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise DeviceParseError(f"bad all.xml: {exc}") from exc
    probes = {}
    for probe, field in _PROBE_FIELD.items():
        block = root.find(field)
        if block is None:
            raise DeviceParseError(f"missing <{field}> block")
        probes[probe] = _parse_probe_block(block, field)
    return {
        "probes": probes,
        "output_pct": _int_of(root, "OUTPUT_PERCENT"),
        "timer_curr": _text_of(root, "TIMER_CURR") or "",
        "timer_status": _int_of(root, "TIMER_STATUS"),
        "deg_units": _int_of(root, "DEG_UNITS"),
        "cyctime": _int_of(root, "COOK_CYCTIME"),
        "propband": _int_of(root, "COOK_PROPBAND"),
        "ramp": _int_of(root, "COOK_RAMP"),
    }


def parse_status_xml(text: str) -> dict:
    """Parse /status.xml (flat <nutcstatus>). Same shape as all.xml minus
    names/setpoints, plus fan_shorted."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise DeviceParseError(f"bad status.xml: {exc}") from exc
    probes = {}
    for probe, field in _PROBE_FIELD.items():
        probes[probe] = {
            "temp": _temp(_text_of(root, f"{field}_TEMP")),
            "status": _int_of(root, f"{field}_STATUS"),
        }
    return {
        "probes": probes,
        "output_pct": _int_of(root, "OUTPUT_PERCENT"),
        "timer_curr": _text_of(root, "TIMER_CURR") or "",
        "timer_status": _int_of(root, "TIMER_STATUS"),
        "deg_units": _int_of(root, "DEG_UNITS"),
        "cyctime": _int_of(root, "COOK_CYCTIME"),
        "propband": _int_of(root, "COOK_PROPBAND"),
        "ramp": _int_of(root, "COOK_RAMP"),
        "fan_shorted": _int_of(root, "FAN_SHORTED"),
    }


def _section_dict(node: ET.Element, drop: Iterable[str]) -> dict:
    """Flatten an XML section into {lowercased tag: value}, ints where
    possible, dropping the given (credential) tags entirely."""
    drop = set(drop)
    out: dict = {}
    for child in node:
        if child.tag in drop:
            continue
        text = (child.text or "").strip()
        try:
            out[child.tag.lower()] = int(text)
        except ValueError:
            out[child.tag.lower()] = text
    return out


def parse_config_xml(text: str) -> dict:
    """Parse /config.xml into the four config sections, secrets dropped.

    The WIFI and SMTP blocks are kept (the scope update exposes them) but
    ``WIFI_KEY`` and ``SMTP_PWD`` are dropped on parse and never stored,
    logged or returned. ``FWVER`` rides along.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise DeviceParseError(f"bad config.xml: {exc}") from exc
    config: dict = {"fwver": _text_of(root, "FWVER") or ""}
    for section, drop in (
        ("SYSTEM", ()),
        ("CONTROL", ()),
        ("SMTP", ("SMTP_PWD",)),
        ("WIFI", ("WIFI_KEY",)),
    ):
        node = root.find(section)
        if node is None:
            raise DeviceParseError(f"missing <{section}> block")
        key = {"SMTP": "email", "WIFI": "wifi"}.get(section, section.lower())
        config[key] = _section_dict(node, drop)
    return config


class _FormParser(HTMLParser):
    """Collects form controls from a device setup page."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ("input", "select", "textarea"):
            return
        attr = {k.lower(): v for k, v in attrs}
        if attr.get("type") in ("hidden", "text", "password") or tag != "input":
            self.fields.append(
                {
                    "tag": tag,
                    "name": attr.get("name", ""),
                    "type": attr.get("type", tag),
                    "value": attr.get("value", ""),
                    "maxlength": attr.get("maxlength"),
                }
            )


def parse_setup_form(html: str) -> list[dict]:
    """Parse the <form> controls out of a setup page. Pure function."""
    parser = _FormParser()
    parser.feed(html)
    return parser.fields


# -- email.htm ---------------------------------------------------------------

#: Fields parsed from email.htm's inputs. SMTP_PWD is included on purpose:
#: the device serves the current password in the page (that is why the
#: capture is gitignored) and a stock form submit re-sends it. The value is
#: used ONLY as the echo in write_email's POST — never logged, stored or
#: returned. Everything else (redaction on read) happens at parse time.
EMAIL_PAGE_FIELDS = (
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PWD",
    "SMTP_TO", "SMTP_FROM", "SMTP_SUBJ", "SMTP_ALERT",
)


def parse_email_page(html: str) -> dict:
    """Current email settings from email.htm's input values.

    Returns lowercase keys; ``smtp_pwd`` included (write-echo only — see
    module docstring). Raises if any expected input is missing, so a page
    redesign fails loudly instead of silently writing bad forms.
    """
    values = {
        field["name"]: field["value"]
        for field in parse_setup_form(html)
        if field["name"]
    }
    out: dict = {}
    for name in EMAIL_PAGE_FIELDS:
        if name not in values:
            raise DeviceParseError(f"email.htm: missing input {name}")
        key = name.lower()
        if key == "smtp_pwd":
            out[key] = values[name]
        elif key in ("smtp_port", "smtp_alert"):
            try:
                out[key] = int(values[name])
            except ValueError as exc:
                raise DeviceParseError(f"email.htm: bad {name}") from exc
        else:
            out[key] = values[name]
    return out


# -- wifi.htm ----------------------------------------------------------------

_WIFI_JS_VALUE = "document.mainForm.{field}.value\\s*=\\s*\"([^\"]*)\""
_WIFI_DHCP_RE = re.compile(r'var dhcpVal([12])\s*=\s*"(\d+)"')

#: Never parsed from the wifi page, by construction: HTTP_PASS is
#: write-only, and the WIFI_KEY values in the page are the device's mask.
WIFI_PAGE_NEVER_PARSED = ("HTTP_PASS", "WIFI_KEY", "WIFI_KEY2")


def _js_literal(html: str, field: str) -> str:
    match = re.search(_WIFI_JS_VALUE.format(field=field), html)
    if match is None:
        raise DeviceParseError(f"wifi.htm: missing {field} literal")
    return match.group(1)


def parse_wifi_page(html: str) -> dict:
    """Current wifi settings from wifi.htm's resetForm JS literals.

    Shape::

        {"multi_wifi_choice": int, "http_user": str, "http_port": int,
         "home": {ip, gw, nm, dns, wifi_mode, dhcp, ssid, wifi_enc},
         "away": {same keys}}

    HTTP_PASS and the WiFi keys are never extracted. Raises if an expected
    literal is missing, so a page redesign fails loudly.
    """
    mode_home = re.search(r"wifiModeReset\('WIFIMODE',\s*\"(\d+)\"", html)
    mode_away = re.search(r"wifiModeReset\('WIFIMODE2',\s*\"(\d+)\"", html)
    enc_home = re.search(r"wifiEncReset\('WIFI_ENC',\s*\"(\d+)\"", html)
    enc_away = re.search(r"wifiEncReset\('WIFI_ENC2',\s*\"(\d+)\"", html)
    dhcp = {m: v for m, v in _WIFI_DHCP_RE.findall(html)}
    if not (mode_home and mode_away and enc_home and enc_away and "1" in dhcp and "2" in dhcp):
        raise DeviceParseError("wifi.htm: missing mode/enc/dhcp literals")

    def profile(suffix: str) -> dict:
        return {
            "ip": _js_literal(html, f"IP{suffix}"),
            "gw": _js_literal(html, f"GW{suffix}"),
            "nm": _js_literal(html, f"NM{suffix}"),
            "dns": _js_literal(html, f"DNS{suffix}"),
            "wifi_mode": int(mode_away.group(1) if suffix else mode_home.group(1)),
            "dhcp": int(dhcp["2" if suffix else "1"]),
            "ssid": _js_literal(html, f"SSID{suffix}"),
            "wifi_enc": int(enc_away.group(1) if suffix else enc_home.group(1)),
        }

    return {
        "multi_wifi_choice": int(_js_literal(html, "MULTI_WIFI_CHOICE")),
        "http_user": _js_literal(html, "HTTP_USER"),
        "http_port": int(_js_literal(html, "HTTP_PORT")),
        "home": profile(""),
        "away": profile("2"),
    }


# ---------------------------------------------------------------------------
# write validation (values are checked before any POST is built)
# ---------------------------------------------------------------------------

def validate_name(value: str) -> str:
    """Probe name: non-empty, printable ASCII, at most NAME_MAX chars."""
    if not isinstance(value, str):
        raise WriteValidationError("name must be a string")
    name = value.strip()
    if not name:
        raise WriteValidationError("name must not be empty")
    if len(name) > NAME_MAX:
        raise WriteValidationError(f"name longer than {NAME_MAX} characters")
    if any(not (0x20 <= ord(ch) <= 0x7E) for ch in name):
        raise WriteValidationError("name must be printable ASCII")
    return name


def clamp_set(probe: str, value: float) -> int:
    """Setpoint in °F (float) -> whole °F int, clamped to the probe limits.

    Rounding is half-up, matching the stock UI's Math.round."""
    if probe not in SET_LIMITS:
        raise WriteValidationError(f"unknown probe {probe!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise WriteValidationError(f"setpoint {value!r} is not a number") from exc
    if not math.isfinite(number):
        raise WriteValidationError("setpoint must be finite")
    low, high = SET_LIMITS[probe]
    whole = math.floor(number + 0.5)  # JS-style round-half-up
    return max(low, min(high, whole))


def validate_timer(value: str) -> str:
    """Cook timer, HH:MM:SS with minutes/seconds < 60 (as the stock UI's
    regex plus real-time sanity)."""
    if not isinstance(value, str):
        raise WriteValidationError("timer must be a string")
    match = _TIMER_RE.fullmatch(value.strip())
    if match is None:
        raise WriteValidationError("timer must be HH:MM:SS")
    hours, minutes, seconds = (int(g) for g in match.groups())
    if minutes > 59 or seconds > 59:
        raise WriteValidationError("timer minutes/seconds must be < 60")
    return value.strip()


def _clamp_int(value, low: int, high: int, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise WriteValidationError(f"{label} must be an integer") from exc
    return max(low, min(high, number))


def _clamp_float(value, low: float, high: float, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise WriteValidationError(f"{label} must be a number") from exc
    if not math.isfinite(number):
        raise WriteValidationError(f"{label} must be finite")
    return max(low, min(high, number))


def _wire_temp(value: float) -> str:
    """°F -> wire string with at most one decimal (control.htm's
    Math.round(v*10)/10), trailing zeros stripped: 200 -> '200'."""
    rounded = math.floor(value * 10 + 0.5) / 10
    return f"{rounded:.1f}".rstrip("0").rstrip(".")


def _validate_ascii(value, label: str, max_len: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise WriteValidationError(f"{label} must be a string")
    text = value.strip()
    if not text and not allow_empty:
        raise WriteValidationError(f"{label} must not be empty")
    if len(text) > max_len:
        raise WriteValidationError(f"{label} longer than {max_len} characters")
    if any(not (0x20 <= ord(ch) <= 0x7E) for ch in text):
        raise WriteValidationError(f"{label} must be printable ASCII")
    return text


def _validate_ipv4(value, label: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise WriteValidationError(f"{label} must be a string")
    text = value.strip()
    if not text:
        if allow_empty:
            return ""
        raise WriteValidationError(f"{label} must not be empty")
    octets = text.split(".")
    if len(octets) != 4 or not all(o.isdigit() and 0 <= int(o) <= 255 for o in octets):
        raise WriteValidationError(f"{label} must be a dotted-quad IPv4 address")
    return text


#: Per-encryption WiFi key rules, mirroring submitFrom_checkKey in the
#: stock wifi.htm. The sentinel always passes (it means "keep the key").
def validate_wifi_key(enc: int, key: str) -> str:
    if not isinstance(key, str):
        raise WriteValidationError("wifi key must be a string")
    if key == KEY_SENTINEL:
        return key
    if enc == 0:  # open — anything goes
        return key
    if enc in (1, 9):  # WEP40 (10 hex) / WEP auto (10 or 26 hex)
        ok_len = len(key) == 10 or (enc == 9 and len(key) == 26)
    elif enc == 2:  # WEP104 (26 hex)
        ok_len = len(key) == 26
    elif enc in (3, 5, 7):  # WPA/WPA2/Auto key (64 hex)
        ok_len = len(key) == 64
    else:  # phrase modes: at least 8 chars
        ok_len = len(key) >= 8
        if ok_len:
            return key
        raise WriteValidationError("wifi phrase must be at least 8 characters")
    if not ok_len:
        raise WriteValidationError(f"wifi key length wrong for enc mode {enc}")
    if not re.fullmatch(r"[0-9A-Fa-f]+", key):
        raise WriteValidationError(f"wifi key for enc mode {enc} must be hex")
    return key


def _is_reset_like(field_name: str) -> bool:
    return _RESET_FIELD_RE.search(field_name) is not None


def check_no_reset_fields(fields: Iterable[tuple[str, str]]) -> None:
    """Guard: no reset-like field name may ever be sent to the device, and
    no field outside the write allowlists.

    ``build_*_form`` calls this on its own output; it is public so the
    tests can assert the invariant directly."""
    for name, _value in fields:
        if _is_reset_like(name):
            raise WriteValidationError(f"reset-like field denied: {name!r}")
        if name not in ALL_WRITE_FIELDS:
            raise WriteValidationError(f"field not in write allowlist: {name!r}")


# ---------------------------------------------------------------------------
# write building — one builder per page, mirroring the stock forms
# ---------------------------------------------------------------------------

def build_write_form(
    current: dict,
    names: dict[str, str] | None = None,
    sets: dict[str, float] | None = None,
    timer: str | None = None,
) -> list[tuple[str, str]]:
    """Build the full stock mainForm POST body for "/".

    Mirrors the device's own index.html exactly: the browser always submits
    every name and setpoint (unchanged ones included) plus both EEAUTOFLUSH
    copies in DOM order, and COOK_TIMER only when the entry is valid. Sending
    the complete form — rather than partial updates — keeps the device's
    state consistent with what a stock UI session would have sent.
    """
    names = names or {}
    sets = sets or {}
    for probe in (*names, *sets):
        if probe not in PROBES:
            raise WriteValidationError(f"unknown probe {probe!r}")
    if timer is not None:
        timer = validate_timer(timer)

    current_probes = current.get("probes", {})
    form_names = {
        probe: validate_name(names.get(probe, current_probes[probe]["name"]))
        for probe in PROBES
    }
    form_sets = {
        probe: clamp_set(probe, sets.get(probe, current_probes[probe]["set"]))
        for probe in PROBES
    }

    fields: list[tuple[str, str]] = [("EEAUTOFLUSH", "0")]
    for probe in PROBES:  # DOM order: setpoints first ...
        fields.append((f"{_PROBE_FIELD[probe]}_SET", str(form_sets[probe])))
    for probe in PROBES:  # ... then the names
        fields.append((f"{_PROBE_FIELD[probe]}_NAME", form_names[probe]))
    fields.append(("EEAUTOFLUSH", "1"))
    if timer is not None:
        fields.append(("COOK_TIMER", timer))

    check_no_reset_fields(fields)  # invariant guard on our own output
    return fields


def build_control_form(current: dict, changes: dict) -> list[tuple[str, str]]:
    """Build the control.htm mainForm POST body.

    ``current`` is the parsed CONTROL section (temps in tenths °F, from
    read_config); ``changes`` carries °F values for cookhold/alarmdev/
    propband and plain ints for the rest. Wire values are °F with at most
    one decimal, exactly like the stock page's submit handler.
    """
    try:
        cookhold = _clamp_float(
            changes.get("cookhold", current["cookhold"] / 10), 32, 475, "cookhold"
        )
        alarmdev = _clamp_float(
            changes.get("alarmdev", current["alarmdev"] / 10), 0, 475, "alarmdev"
        )
        propband = _clamp_float(
            changes.get("propband", current["propband"] / 10), 0, 475, "propband"
        )
        timeout_action = _clamp_int(
            changes.get("timeout_action", current["timeout_action"]), 0, 3,
            "timeout_action",
        )
        cook_ramp = _clamp_int(
            changes.get("cook_ramp", current["cook_ramp"]), 0, 3, "cook_ramp"
        )
        opendetect = _clamp_int(
            changes.get("opendetect", current["opendetect"]), 0, 1, "opendetect"
        )
        cyctime = _clamp_int(
            changes.get("cyctime", current["cyctime"]), 0, 99, "cyctime"
        )
    except KeyError as exc:
        raise WriteValidationError(f"missing current control value: {exc}") from exc

    fields = [
        ("EEAUTOFLUSH", "0"),
        ("COOKHOLD", _wire_temp(cookhold)),
        ("TIMEOUT_ACTION", str(timeout_action)),
        ("ALARMDEV", _wire_temp(alarmdev)),
        ("COOK_RAMP", str(cook_ramp)),
        ("OPENDETECT", str(opendetect)),
        ("CYCTIME", str(cyctime)),
        ("PROPBAND", _wire_temp(propband)),
        ("EEAUTOFLUSH", "1"),
    ]
    check_no_reset_fields(fields)
    return fields


def build_system_form(current: dict, changes: dict) -> list[tuple[str, str]]:
    """Build the system.htm mainForm POST body.

    Note the stock quirk: system.htm's EEAUTOFLUSH=0 input sits OUTSIDE the
    form, so the stock page submits exactly one EEAUTOFLUSH (the trailing
    1). We mirror that.
    """
    try:
        fields = [
            ("MENU_SCROLLING", str(_clamp_int(
                changes.get("menu_scrolling", current["menu_scrolling"]), 0, 1,
                "menu_scrolling"))),
            ("LCD_BACKLIGHT", str(_clamp_int(
                changes.get("lcd_backlight", current["lcd_backlight"]), 0, 100,
                "lcd_backlight"))),
            ("LCD_CONTRAST", str(_clamp_int(
                changes.get("lcd_contrast", current["lcd_contrast"]), 0, 100,
                "lcd_contrast"))),
            ("DEG_UNITS", str(_clamp_int(
                changes.get("deg_units", current["deg_units"]), 0, 1, "deg_units"))),
            ("ALARM_BEEPS", str(_clamp_int(
                changes.get("alarm_beeps", current["alarm_beeps"]), 0, 5,
                "alarm_beeps"))),
            ("KEY_BEEPS", str(_clamp_int(
                changes.get("key_beeps", current["key_beeps"]), 0, 1, "key_beeps"))),
            ("EEAUTOFLUSH", "1"),
        ]
    except KeyError as exc:
        raise WriteValidationError(f"missing current system value: {exc}") from exc
    check_no_reset_fields(fields)
    return fields


def build_email_form(
    current: dict, changes: dict, send_test: bool = False
) -> list[tuple[str, str]]:
    """Build the email.htm mainForm POST body.

    ``current`` is parse_email_page's result — including ``smtp_pwd``, the
    device-served password that a stock submit re-sends (write-echo only;
    never logged, stored or returned). ``changes`` may override any field;
    ``smtp_pwd`` in changes replaces the echo. ``send_test`` adds
    SMTP_NOW=1 (the checkbox the stock page says must be checked for the
    device to send mail now).
    """
    try:
        merged = dict(current)
        merged.update(changes)
        fields = [
            ("EEAUTOFLUSH", "0"),
            ("SMTP_HOST", _validate_ascii(merged["smtp_host"], "smtp_host", 63, True)),
            ("SMTP_PORT", str(_clamp_int(merged["smtp_port"], 0, 65535, "smtp_port"))),
            ("SMTP_USER", _validate_ascii(merged["smtp_user"], "smtp_user", 63, True)),
            ("SMTP_PWD", _validate_ascii(merged["smtp_pwd"], "smtp_pwd", 63, True)),
            ("SMTP_TO", _validate_ascii(merged["smtp_to"], "smtp_to", 63, True)),
            ("SMTP_FROM", _validate_ascii(merged["smtp_from"], "smtp_from", 63, True)),
            ("SMTP_SUBJ", _validate_ascii(merged["smtp_subj"], "smtp_subj", 63, True)),
            ("SMTP_ALERT", str(_clamp_int(
                merged["smtp_alert"], 0, 999, "smtp_alert"))),
        ]
        if send_test:
            fields.append(("SMTP_NOW", "1"))
        fields.append(("EEAUTOFLUSH", "1"))
    except KeyError as exc:
        raise WriteValidationError(f"missing current email value: {exc}") from exc
    check_no_reset_fields(fields)
    return fields


_WIFI_PROFILE_KEYS = ("ip", "gw", "nm", "dns", "wifi_mode", "dhcp", "ssid", "wifi_enc")


def build_wifi_form(current: dict, changes: dict) -> list[tuple[str, str]]:
    """Build the wifi.htm mainForm POST body.

    ``current`` is parse_wifi_page's result; ``changes`` uses the same
    flat/​profile keys, plus ``http_user``/``http_pass``/``http_port`` and
    ``wifi_key``/``wifi_key2``.

    Safety-relevant behaviors, mirroring the stock page where safe:
    - ``WIFI_KEY``/``WIFI_KEY2`` default to the 20-asterisk sentinel
      ("keep the stored key") unless a new key is provided;
    - ``HTTP_USER``/``HTTP_PASS`` are included ONLY when provided in
      ``changes``: the page hint says blank disables auth protection, and
      the stock page serves them empty, so echoing blanks could turn auth
      off. Omitting the fields is the conservative choice.
    """
    if "home" not in current or "away" not in current:
        raise WriteValidationError("wifi form needs parsed home and away profiles")

    home = dict(current["home"])
    away = dict(current["away"])
    for key in _WIFI_PROFILE_KEYS:
        if key in changes:
            home[key] = changes[key]
        if f"away_{key}" in changes:
            away[key] = changes[f"away_{key}"]

    http_user = changes.get("http_user", None)
    http_pass = changes.get("http_pass", None)
    http_port = _clamp_int(
        changes.get("http_port", current["http_port"]), 1, 65535, "http_port"
    )
    multi = _clamp_int(
        changes.get("multi_wifi_choice", current["multi_wifi_choice"]), 0, 1,
        "multi_wifi_choice",
    )

    wifi_key = validate_wifi_key(
        int(home["wifi_enc"]), changes.get("wifi_key", KEY_SENTINEL)
    )
    wifi_key2 = validate_wifi_key(
        int(away["wifi_enc"]), changes.get("wifi_key2", KEY_SENTINEL)
    )

    fields: list[tuple[str, str]] = [("EEAUTOFLUSH", "0")]
    if http_user is not None:
        fields.append(("HTTP_USER", _validate_ascii(http_user, "http_user", 32, True)))
    if http_pass is not None:
        fields.append(("HTTP_PASS", _validate_ascii(http_pass, "http_pass", 32, True)))
    fields.append(("HTTP_PORT", str(http_port)))
    fields.append(("MULTI_WIFI_CHOICE", str(multi)))
    # home profile, then away — stock DOM order
    fields.append(("IP", _validate_ipv4(home["ip"], "ip")))
    fields.append(("GW", _validate_ipv4(home["gw"], "gw")))
    fields.append(("NM", _validate_ipv4(home["nm"], "nm")))
    fields.append(("DNS", _validate_ipv4(home["dns"], "dns")))
    fields.append(("WIFIMODE", str(_clamp_int(home["wifi_mode"], 0, 1, "wifi_mode"))))
    fields.append(("DHCP", str(_clamp_int(home["dhcp"], 0, 1, "dhcp"))))
    fields.append(("SSID", _validate_ascii(home["ssid"], "ssid", 31)))
    fields.append(("WIFI_ENC", str(_clamp_int(home["wifi_enc"], 0, 9, "wifi_enc"))))
    fields.append(("WIFI_KEY", wifi_key))
    fields.append(("IP2", _validate_ipv4(away["ip"], "away ip", True)))
    fields.append(("GW2", _validate_ipv4(away["gw"], "away gw", True)))
    fields.append(("NM2", _validate_ipv4(away["nm"], "away nm", True)))
    fields.append(("DNS2", _validate_ipv4(away["dns"], "away dns", True)))
    fields.append(("WIFIMODE2", str(_clamp_int(away["wifi_mode"], 0, 1, "wifi_mode2"))))
    fields.append(("DHCP2", str(_clamp_int(away["dhcp"], 0, 1, "dhcp2"))))
    fields.append(("SSID2", _validate_ascii(away["ssid"], "away ssid", 31, True)))
    fields.append(("WIFI_ENC2", str(_clamp_int(away["wifi_enc"], 0, 9, "wifi_enc2"))))
    fields.append(("WIFI_KEY2", wifi_key2))
    fields.append(("EEAUTOFLUSH", "1"))

    check_no_reset_fields(fields)
    return fields


def build_scan_form() -> list[tuple[str, str]]:
    """The scan.htm form: just WIFI_SCAN_START=1."""
    fields = [("WIFI_SCAN_START", "1")]
    check_no_reset_fields(fields)
    return fields


# ---------------------------------------------------------------------------
# the client
# ---------------------------------------------------------------------------

class CyberQClient:
    """Async client for one CyberQ WiFi.

    All requests funnel through :meth:`_request`, which enforces the
    method-scoped path allowlists before anything leaves the process.
    Reads are GETs to the XML endpoints and setup pages; writes are the
    per-page mainForm POSTs built by the ``build_*_form`` functions.
    """

    def __init__(
        self,
        base_url: str = "http://192.168.17.6",
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
        allow_reboot: bool = False,
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._allow_reboot = allow_reboot

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "CyberQClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        content: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        allowed = READ_PATHS if method == "GET" else (
            WRITE_PATHS if method == "POST" else frozenset()
        )
        if path not in allowed:
            hint = (
                f" (denied: {sorted(DENIED_PATHS)})"
                if path in DENIED_PATHS else ""
            )
            raise DeviceUrlError(
                f"{method} {path!r} is not allowlisted{hint}"
            )
        response = await self._client.request(method, path, content=content, headers=headers)
        response.raise_for_status()
        return response

    async def _post_form(self, path: str, fields: list[tuple[str, str]]) -> None:
        # httpx does not encode duplicate form keys (the EEAUTOFLUSH pair),
        # so the body is urlencoded explicitly.
        await self._request(
            "POST", path,
            content=urlencode(fields),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    # -- reads ---------------------------------------------------------------

    async def read_all(self) -> dict:
        """GET /all.xml -> parsed sample (names, temps, setpoints, statuses)."""
        response = await self._request("GET", "/all.xml")
        return parse_all_xml(response.text)

    async def read_status(self) -> dict:
        """GET /status.xml -> parsed status (adds fan_shorted)."""
        response = await self._request("GET", "/status.xml")
        return parse_status_xml(response.text)

    async def read_config(self) -> dict:
        """GET /config.xml -> all four sections, secrets dropped on parse
        (WIFI_KEY, SMTP_PWD never stored, logged or returned)."""
        response = await self._request("GET", "/config.xml")
        return parse_config_xml(response.text)

    async def read_email_page(self) -> dict:
        """GET /email.htm -> current email settings, including the
        device-served ``smtp_pwd`` (write-echo only: transient, never
        logged, stored or returned — see module docstring)."""
        response = await self._request("GET", "/email.htm")
        return parse_email_page(response.text)

    async def read_wifi_page(self) -> dict:
        """GET /wifi.htm -> current wifi state (both profiles, HTTP_USER,
        port, MULTI_WIFI_CHOICE). HTTP_PASS and the keys are never
        extracted."""
        response = await self._request("GET", "/wifi.htm")
        return parse_wifi_page(response.text)

    # -- writes ----------------------------------------------------------------

    async def write_probes(
        self,
        current: dict,
        names: dict[str, str] | None = None,
        sets: dict[str, float] | None = None,
        timer: str | None = None,
    ) -> dict:
        """Apply name/setpoint/timer changes via the stock mainForm POST.

        ``current`` is the latest :meth:`read_all` sample; the full form is
        always sent (unchanged values included), exactly like the stock UI.
        Returns a fresh :meth:`read_all` sample so callers see the device's
        actual new state, not the requested one.
        """
        fields = build_write_form(current, names=names, sets=sets, timer=timer)
        await self._post_form("/", fields)
        return await self.read_all()

    async def write_control(self, current: dict, changes: dict) -> dict:
        """POST control.htm's mainForm. ``current`` is read_config()'s
        ``control`` section; ``changes`` carries the fields to change (°F
        for temperatures). Returns the fresh control section."""
        await self._post_form("/control.htm", build_control_form(current, changes))
        return (await self.read_config())["control"]

    async def write_system(self, current: dict, changes: dict) -> dict:
        """POST system.htm's mainForm. Returns the fresh system section."""
        await self._post_form("/system.htm", build_system_form(current, changes))
        return (await self.read_config())["system"]

    async def write_email(
        self, current: dict, changes: dict, send_test: bool = False
    ) -> dict:
        """POST email.htm's mainForm (full form, password echoed from the
        page read — never logged, stored or returned). ``send_test`` adds
        SMTP_NOW=1. Returns the fresh email section from config.xml (which
        by construction carries no password)."""
        await self._post_form(
            "/email.htm", build_email_form(current, changes, send_test=send_test)
        )
        return (await self.read_config())["email"]

    async def write_wifi(self, current: dict, changes: dict) -> dict:
        """POST wifi.htm's mainForm. WiFi writes can strand the device off
        the network — the API layer requires an explicit confirm before
        calling this. Keys default to the keep-sentinel; HTTP auth fields
        are sent only when provided. Returns the fresh wifi page state."""
        await self._post_form("/wifi.htm", build_wifi_form(current, changes))
        return await self.read_wifi_page()

    async def wifi_scan(self) -> None:
        """POST scan.htm with WIFI_SCAN_START=1 (perform a WiFi scan)."""
        await self._post_form("/scan.htm", build_scan_form())

    async def reboot(self) -> None:
        """POST booting.htm with RESET_CPU=1 — a plain reboot, NOT a factory
        or firmware reset (no such code path exists anywhere).

        Disabled unless the client was built with ``allow_reboot=True``
        (the API layer gates that on ALLOW_REBOOT, default off, plus an
        explicit ``{"confirm": true}``). This is the one and only
        reset-like write in the codebase.
        """
        if not self._allow_reboot:
            raise DeviceUrlError(
                "reboot is disabled (ALLOW_REBOOT=0); booting.htm stays denied"
            )
        # Not via _post_form: RESET_CPU is intentionally outside the write
        # allowlists and is denied by check_no_reset_fields on purpose.
        response = await self._client.request(
            "POST", "/booting.htm",
            content=urlencode(REBOOT_FORM),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
