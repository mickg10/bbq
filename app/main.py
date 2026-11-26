"""FastAPI application: API from DESIGN.md plus the static UI mount (task 4,
extended to the scope update: all four config sections, per-section PATCHes,
email test, wifi scan, and a flag-gated reboot).

Environment:
- CYBERQ_URL    base URL of the device (default http://192.168.17.6)
- DB_PATH       SQLite path (default bbq.db; /data/bbq.db in the container)
- POLL_INTERVAL seconds between /all.xml polls (default 5; 0 disables the
                background loop — tests then drive poll_once via /api/status)
- CONFIG_TTL    seconds to cache /api/config (default 30)
- ALLOW_REBOOT  "1" to expose POST /api/reboot (default off). Firmware/factory
                reset is never implemented, with or without this flag.

All device access goes through app.device.CyberQClient — this module never
builds a device URL or form field itself. Credential fields (SMTP_PWD,
WIFI_KEY*, HTTP_PASS) are write-only: accepted in PATCH bodies, never
echoed in responses, and recorded in the audit table with NULL values.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles

from app import db
from app.device import PROBES, CyberQClient, WriteValidationError
from app.models import (
    ControlPatch,
    CookCreate,
    CookPatch,
    EmailPatch,
    ProbePatch,
    RebootPost,
    SystemPatch,
    TimerPut,
    WifiPatch,
)
from app.poller import Poller

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
MAX_HISTORY_POINTS = 20_000

#: Credential fields per section: audit rows log the change with NULL
#: old/new values; responses never contain them.
_SECRET_KEYS = {
    "email": {"smtp_pwd"},
    "wifi": {"http_pass", "wifi_key", "wifi_key2"},
}


class Settings:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = env if env is not None else os.environ
        self.cyberq_url = env.get("CYBERQ_URL", "http://192.168.17.6")
        self.db_path = env.get("DB_PATH", "bbq.db")
        self.poll_interval = float(env.get("POLL_INTERVAL", "5"))
        self.config_ttl = float(env.get("CONFIG_TTL", "30"))
        self.allow_reboot = env.get("ALLOW_REBOOT", "0").lower() in (
            "1", "true", "yes", "on",
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        conn = db.connect(settings.db_path)
        client = CyberQClient(settings.cyberq_url, allow_reboot=settings.allow_reboot)
        poller = Poller(client, conn, interval=settings.poll_interval)
        app.state.settings = settings
        app.state.conn = conn
        app.state.client = client
        app.state.poller = poller
        app.state.config_cache: dict | None = None
        app.state.config_cache_ts: float = 0.0
        task = None
        if settings.poll_interval > 0:
            task = asyncio.create_task(poller.run())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
            await client.aclose()
            conn.close()

    app = FastAPI(title="bbq", lifespan=lifespan)

    # ------------------------------------------------------------------ health

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    # ------------------------------------------------------------------ status

    @app.get("/api/status")
    async def api_status(request: Request) -> dict:
        poller: Poller = request.app.state.poller
        if poller.latest is None:
            # lazy seed: covers a fresh start before the first poll (and
            # tests that run with POLL_INTERVAL=0)
            await poller.poll_once()
        sample = poller.latest or db.latest_sample(request.app.state.conn)
        return {"sample": sample, "device": poller.device_state()}

    @app.get("/api/history")
    async def api_history(
        request: Request,
        t_from: int | None = Query(default=None, alias="from", ge=0),
        t_to: int | None = Query(default=None, alias="to", ge=0),
        step: int | None = Query(default=None, ge=1),
    ) -> dict:
        now = int(time.time())
        t_to = t_to if t_to is not None else now
        t_from = t_from if t_from is not None else t_to - 24 * 3600
        step = step if step is not None else 300
        if t_from >= t_to:
            raise HTTPException(422, "'from' must be before 'to'")
        if (t_to - t_from) / step > MAX_HISTORY_POINTS:
            raise HTTPException(
                422, f"range/step yields too many points (max {MAX_HISTORY_POINTS})"
            )
        points = db.history(request.app.state.conn, t_from, t_to, step)
        return {"from": t_from, "to": t_to, "step": step, "points": points}

    # ------------------------------------------------------------------ config

    async def _merged_config(request: Request) -> dict:
        """config.xml sections + the wifi page's profiles/HTTP settings,
        with credentials already dropped by the parsers."""
        client: CyberQClient = request.app.state.client
        config = await client.read_config()
        wifi_page = await client.read_wifi_page()
        return {
            "fwver": config["fwver"],
            "system": config["system"],
            "control": config["control"],
            "email": config["email"],
            "wifi": {
                "multi_wifi_choice": wifi_page["multi_wifi_choice"],
                "http_user": wifi_page["http_user"],
                "http_port": wifi_page["http_port"],
                "mac": config["wifi"].get("mac"),
                "home": wifi_page["home"],
                "away": wifi_page["away"],
            },
        }

    @app.get("/api/config")
    async def api_config(request: Request) -> dict:
        settings: Settings = request.app.state.settings
        if (
            request.app.state.config_cache is None
            or time.time() - request.app.state.config_cache_ts > settings.config_ttl
        ):
            try:
                request.app.state.config_cache = await _merged_config(request)
                request.app.state.config_cache_ts = time.time()
            except Exception as exc:
                if request.app.state.config_cache is None:
                    raise HTTPException(503, f"device unreachable: {exc}") from exc
        return request.app.state.config_cache

    def _invalidate_config_cache(request: Request) -> None:
        request.app.state.config_cache = None

    def _device_error(exc: Exception) -> HTTPException:
        return HTTPException(503, f"device unreachable: {exc}")

    def _audit_config(
        request: Request,
        section: str,
        changes: dict,
        current: dict,
        fresh: dict | None,
    ) -> None:
        """One audit row per changed field. Secret fields record the change
        with NULL values — never the secret itself."""
        ip = request.client.host if request.client else None
        ts = int(time.time())
        secrets = _SECRET_KEYS.get(section, set())
        for key in changes:
            field = f"{section}.{key}"
            if key in secrets:
                db.add_audit(request.app.state.conn, ts, field, None, None, ip)
                continue
            if section == "wifi" and key.startswith("away_"):
                rest = key[len("away_"):]
                old = (current.get("away") or {}).get(rest)
                new = (fresh.get("away") or {}).get(rest) if fresh else None
            else:
                old = current.get(key) if section != "wifi" else (
                    (current.get("home") or {}).get(key)
                )
                new = (fresh.get(key) if fresh else None) if section != "wifi" else (
                    (fresh.get("home") or {}).get(key) if fresh else None
                )
            db.add_audit(request.app.state.conn, ts, field, old, new, ip)

    def _check_wifi_confirm(changes: dict) -> dict:
        if "confirm" not in changes or changes["confirm"] is not True:
            raise HTTPException(
                400, "wifi writes are dangerous: require {'confirm': true}"
            )
        changes = {k: v for k, v in changes.items() if k != "confirm"}
        if not changes:
            raise HTTPException(400, "nothing to change beyond the confirm flag")
        return changes

    async def _patch_config(request: Request, section: str, changes: dict) -> dict:
        """Shared PATCH flow: read current, write, audit (secrets redacted),
        invalidate the config cache, return the fresh section."""
        client: CyberQClient = request.app.state.client
        try:
            if section == "email":
                current = await client.read_email_page()
                fresh = await client.write_email(current, changes)
            elif section == "wifi":
                current = await client.read_wifi_page()
                fresh = await client.write_wifi(current, changes)
            else:
                current = (await _merged_config(request))[section]
                if section == "control":
                    fresh = await client.write_control(current, changes)
                else:
                    fresh = await client.write_system(current, changes)
        except WriteValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise _device_error(exc) from exc
        _audit_config(request, section, changes, current, fresh)
        _invalidate_config_cache(request)
        return {"set": True, "config": {section: fresh}}

    @app.patch("/api/config/control")
    async def api_patch_control(body: ControlPatch, request: Request) -> dict:
        changes = body.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(400, "nothing to change")
        return await _patch_config(request, "control", changes)

    @app.patch("/api/config/system")
    async def api_patch_system(body: SystemPatch, request: Request) -> dict:
        changes = body.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(400, "nothing to change")
        return await _patch_config(request, "system", changes)

    @app.patch("/api/config/email")
    async def api_patch_email(body: EmailPatch, request: Request) -> dict:
        changes = body.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(400, "nothing to change")
        return await _patch_config(request, "email", changes)

    @app.patch("/api/config/wifi")
    async def api_patch_wifi(body: WifiPatch, request: Request) -> dict:
        changes = body.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(400, "nothing to change")
        return await _patch_config(
            request, "wifi", _check_wifi_confirm(changes)
        )

    @app.post("/api/email/test")
    async def api_email_test(request: Request) -> dict:
        """Send a test e-mail (SMTP_NOW=1 on the stock email form)."""
        client: CyberQClient = request.app.state.client
        try:
            current = await client.read_email_page()
            await client.write_email(current, {}, send_test=True)
        except Exception as exc:
            raise _device_error(exc) from exc
        db.add_audit(
            request.app.state.conn, int(time.time()), "email.test", None, None,
            request.client.host if request.client else None,
        )
        return {"sent": True}

    @app.post("/api/wifi/scan")
    async def api_wifi_scan(request: Request) -> dict:
        client: CyberQClient = request.app.state.client
        try:
            await client.wifi_scan()
        except Exception as exc:
            raise _device_error(exc) from exc
        db.add_audit(
            request.app.state.conn, int(time.time()), "wifi.scan", None, None,
            request.client.host if request.client else None,
        )
        return {"started": True}

    @app.get("/api/features")
    async def api_features(request: Request) -> dict[str, bool]:
        """Optional capabilities the UI should show (reboot is opt-in)."""
        return {"reboot": request.app.state.settings.allow_reboot}

    @app.post("/api/reboot")
    async def api_reboot(request: Request) -> dict:
        """Plain reboot (NOT factory/firmware reset — that has no code path).

        Only exists when the deployment sets ALLOW_REBOOT=1; requires
        {'confirm': true}. Off by default: without the flag the endpoint
        refuses with 403.
        """
        settings: Settings = request.app.state.settings
        client: CyberQClient = request.app.state.client
        if not settings.allow_reboot:
            raise HTTPException(403, "reboot is disabled (ALLOW_REBOOT=0)")
        body = await request.json() or {}
        if body.get("confirm") is not True:
            raise HTTPException(400, "reboot requires {'confirm': true}")
        try:
            await client.reboot()
        except Exception as exc:
            raise _device_error(exc) from exc
        db.add_audit(
            request.app.state.conn, int(time.time()), "reboot", None, None,
            request.client.host if request.client else None,
        )
        return {"rebooting": True}

    # ------------------------------------------------------------------ writes

    async def _current_sample(request: Request) -> dict:
        poller: Poller = request.app.state.poller
        if poller.latest is None:
            await poller.poll_once()
        if poller.latest is None:
            raise HTTPException(503, "device unreachable: no current sample")
        return poller.latest

    @app.patch("/api/probes/{probe}")
    async def api_patch_probe(
        probe: str, body: ProbePatch, request: Request
    ) -> dict:
        if probe not in PROBES:
            raise HTTPException(404, f"unknown probe {probe!r}")
        if body.name is None and body.set_f is None:
            raise HTTPException(400, "nothing to change: provide 'name' and/or 'set_f'")
        current = await _current_sample(request)
        client: CyberQClient = request.app.state.client
        names = {probe: body.name} if body.name is not None else None
        sets = {probe: body.set_f} if body.set_f is not None else None
        try:
            result = await client.write_probes(current, names=names, sets=sets)
        except WriteValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        ts = int(time.time())
        db.add_audit(
            request.app.state.conn, ts, f"{probe}.name",
            current["probes"][probe]["name"], result["probes"][probe]["name"],
            request.client.host if request.client else None,
        )
        db.add_audit(
            request.app.state.conn, ts, f"{probe}.set",
            current["probes"][probe]["set"], result["probes"][probe]["set"],
            request.client.host if request.client else None,
        )
        return {"sample": {"ts": ts, **result}}

    @app.put("/api/timer")
    async def api_put_timer(body: TimerPut, request: Request) -> dict:
        current = await _current_sample(request)
        client: CyberQClient = request.app.state.client
        try:
            result = await client.write_probes(current, timer=body.hhmmss)
        except WriteValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        ts = int(time.time())
        db.add_audit(
            request.app.state.conn, ts, "timer",
            current["timer_curr"], result["timer_curr"],
            request.client.host if request.client else None,
        )
        return {"sample": {"ts": ts, **result}}

    # ------------------------------------------------------------------ cooks

    @app.post("/api/cooks")
    async def api_create_cook(body: CookCreate, request: Request) -> dict:
        cook_id = db.create_cook(request.app.state.conn, body.name, body.notes)
        return db.get_cook(request.app.state.conn, cook_id)  # type: ignore[return-value]

    @app.get("/api/cooks")
    async def api_list_cooks(request: Request) -> list[dict]:
        return db.list_cooks(request.app.state.conn)

    @app.patch("/api/cooks/{cook_id}")
    async def api_patch_cook(cook_id: int, body: CookPatch, request: Request) -> dict:
        ended_ts = int(time.time()) if body.end else None
        if not db.update_cook(
            request.app.state.conn, cook_id,
            name=body.name, notes=body.notes, ended_ts=ended_ts,
        ):
            raise HTTPException(404, f"no cook {cook_id}")
        return db.get_cook(request.app.state.conn, cook_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------ static UI

    if _STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")

    return app


app = create_app()
