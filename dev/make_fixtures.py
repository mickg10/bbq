"""Generate the fake device's page fixtures from the recon captures.

The fake (``dev/fake_cyberq.py``) serves the device's pages from
``dev/fixtures/`` so tests never depend on the gitignored captures
(``recon/config.xml``, ``recon/wifi.htm``, ``recon/email.htm`` — they carry
the device's credentials).

Sanitization (this script never prints the real values it scrubs):

* ``index.html``, ``control.htm``, ``system.htm``, ``reboot.htm`` are copied
  verbatim after a safety scan: they must not contain password inputs or
  credential-bearing field names.
* ``email.htm``: every SMTP field's ``value=`` is replaced with a clearly
  fake placeholder, keyed by field *name* (the real value never appears in
  this script or in its output).
* ``wifi.htm``: SSID/SSID2 and the MAC address (resetForm literals and the
  static table) are replaced with placeholders; HTTP_USER/HTTP_PASS are
  already empty in the page, and WIFI_KEY values are the device's own
  20-asterisk mask.

Run from the repo root:

    .venv/bin/python dev/make_fixtures.py

Fails loudly if a scrub did not take (output differs from input for every
scrubbed field), so a firmware update that changes the page shape cannot
silently leak the real values into the fixtures.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RECON = REPO / "recon"
FIXTURES = REPO / "dev" / "fixtures"

# Verbatim pages (no credentials expected; scanned to be sure).
VERBATIM = ("index.html", "control.htm", "system.htm", "reboot.htm")

# value="..." of these email.htm inputs is replaced with the placeholder.
EMAIL_PLACEHOLDERS = {
    "SMTP_HOST": "smtp.fixture.invalid",
    "SMTP_PORT": "0",
    "SMTP_USER": "user@fixture.invalid",
    "SMTP_PWD": "fixture-password",
    "SMTP_TO": "to@fixture.invalid",
    "SMTP_FROM": "from@fixture.invalid",
    "SMTP_SUBJ": "Fixture status e-mail",
    "SMTP_ALERT": "0",
}

# wifi.htm: JS resetForm literals to replace, field -> placeholder.
WIFI_JS_PLACEHOLDERS = {
    "SSID": "fixture-net",
    "SSID2": "fixture-away",
}
WIFI_MAC_RE = re.compile(r"\b00:1E:C0(?::[0-9A-F]{2}){3}\b")
WIFI_MAC_PLACEHOLDER = "00:1E:C0:00:00:00"

_SECRET_NAMES = re.compile(
    r"(?i)(password|passwd|pwd|secret|key|pass\b|token|credential)"
)


def _die(message: str) -> None:
    print(f"make_fixtures: {message}", file=sys.stderr)
    raise SystemExit(1)


def _scan_clean(name: str, html: str) -> None:
    """Refuse to copy a page verbatim if it looks like it holds secrets."""
    if "<input" not in html:
        return
    for match in re.finditer(r"<input\b[^>]*>", html, re.I):
        tag = match.group(0)
        if 'type="password"' in tag.lower() and re.search(
            r'value\s*=\s*"[^"]+"', tag, re.I
        ):
            _die(f"{name} has a password input with a value")
        name_attr = re.search(r'name\s*=\s*"([^"]+)"', tag, re.I)
        if name_attr and _SECRET_NAMES.search(name_attr.group(1)):
            if re.search(r'value\s*=\s*"[^"]+"', tag, re.I):
                _die(f"{name} sets a value for credential field {name_attr.group(1)!r}")


def _scrub_email(html: str) -> str:
    for field, placeholder in EMAIL_PLACEHOLDERS.items():
        pattern = re.compile(
            rf'(<input\b[^>]*name="{field}"[^>]*value=")[^"]*(")', re.I
        )
        html, count = pattern.subn(rf"\g<1>{placeholder}\g<2>", html)
        if count != 1:
            # the value attribute may come before the name in some versions
            pattern = re.compile(
                rf'(<input\b[^>]*value=")[^"]*("[^>]*name="{field}")', re.I
            )
            html, count = pattern.subn(rf"\g<1>{placeholder}\g<2>", html)
        if count != 1:
            _die(f"email.htm: could not scrub {field} (pattern matched {count})")
    return html


def _scrub_wifi(html: str) -> str:
    for field, placeholder in WIFI_JS_PLACEHOLDERS.items():
        pattern = re.compile(rf'(\.{field}\.value\s*=\s*")[^"]*(")')
        html, count = pattern.subn(rf"\g<1>{placeholder}\g<2>", html)
        if count != 1:
            _die(f"wifi.htm: could not scrub {field} (pattern matched {count})")
    html, count = WIFI_MAC_RE.subn(WIFI_MAC_PLACEHOLDER, html)
    if count < 1:
        _die("wifi.htm: MAC address not found")
    return html


def main() -> None:
    if not RECON.exists():
        _die("recon/ is missing; captures are required to build fixtures")

    FIXTURES.mkdir(parents=True, exist_ok=True)

    for name in VERBATIM:
        path = RECON / name
        if not path.exists():
            _die(f"recon/{name} is missing")
        html = path.read_text()
        _scan_clean(name, html)
        (FIXTURES / name).write_text(html)
        print(f"fixtures/{name}: verbatim (scanned clean)")

    email = RECON / "email.htm"
    if email.exists():
        out = _scrub_email(email.read_text())
        for field, placeholder in EMAIL_PLACEHOLDERS.items():
            if f'value="{placeholder}"' not in out:
                _die(f"email.htm scrub did not stick for {field}")
        (FIXTURES / "email.htm").write_text(out)
        print("fixtures/email.htm: scrubbed (placeholder SMTP values)")

    wifi = RECON / "wifi.htm"
    if wifi.exists():
        out = _scrub_wifi(wifi.read_text())
        if "fixture-net" not in out or "fixture-away" not in out:
            _die("wifi.htm SSID scrub did not stick")
        (FIXTURES / "wifi.htm").write_text(out)
        print("fixtures/wifi.htm: scrubbed (placeholder SSID/MAC)")


if __name__ == "__main__":
    main()
