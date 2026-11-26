"""Pydantic request/response models for the API (task 4)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProbePatch(BaseModel):
    """PATCH /api/probes/{cook|food1|food2|food3} — at least one field."""

    name: str | None = Field(default=None, max_length=31)
    set_f: float | None = None


class TimerPut(BaseModel):
    """PUT /api/timer — the cook timer as HH:MM:SS."""

    hhmmss: str


class ControlPatch(BaseModel):
    """PATCH /api/config/control — control-loop settings (°F where thermal)."""

    cookhold: float | None = None
    alarmdev: float | None = None
    propband: float | None = None
    timeout_action: int | None = None
    cook_ramp: int | None = None
    opendetect: int | None = None
    cyctime: int | None = None


class SystemPatch(BaseModel):
    """PATCH /api/config/system — device/system settings."""

    menu_scrolling: int | None = None
    lcd_backlight: int | None = None
    lcd_contrast: int | None = None
    deg_units: int | None = None
    alarm_beeps: int | None = None
    key_beeps: int | None = None


class EmailPatch(BaseModel):
    """PATCH /api/config/email — SMTP alert settings.

    smtp_pwd is write-only: accepted here, never echoed, stored or logged
    (the audit row records the change with redacted values).
    """

    smtp_host: str | None = Field(default=None, max_length=63)
    smtp_port: int | None = None
    smtp_user: str | None = Field(default=None, max_length=63)
    smtp_pwd: str | None = Field(default=None, max_length=63)
    smtp_to: str | None = Field(default=None, max_length=63)
    smtp_from: str | None = Field(default=None, max_length=63)
    smtp_subj: str | None = Field(default=None, max_length=63)
    smtp_alert: int | None = None


class WifiPatch(BaseModel):
    """PATCH /api/config/wifi — network settings. DANGEROUS: a bad write
    can strand the device off the network, so ``confirm: true`` is
    required. Keys/passwords are write-only (sentinel = keep stored key
    unless a new value is given)."""

    confirm: bool = False
    # HTTP interface
    http_user: str | None = Field(default=None, max_length=32)
    http_pass: str | None = Field(default=None, max_length=32)
    http_port: int | None = None
    multi_wifi_choice: int | None = None
    # home (active) profile
    ssid: str | None = Field(default=None, max_length=31)
    wifi_key: str | None = Field(default=None, max_length=64)
    wifi_enc: int | None = None
    wifi_mode: int | None = None
    dhcp: int | None = None
    ip: str | None = None
    gw: str | None = None
    nm: str | None = None
    dns: str | None = None
    # away profile
    away_ssid: str | None = Field(default=None, max_length=31)
    away_wifi_key: str | None = Field(default=None, max_length=64)
    away_wifi_enc: int | None = None
    away_wifi_mode: int | None = None
    away_dhcp: int | None = None
    away_ip: str | None = None
    away_gw: str | None = None
    away_nm: str | None = None
    away_dns: str | None = None


class RebootPost(BaseModel):
    """POST /api/reboot — requires {'confirm': true}; the endpoint itself
    only exists when the deployment enables ALLOW_REBOOT (default off)."""

    confirm: bool = False


class CookCreate(BaseModel):
    """POST /api/cooks — start a named cook session."""

    name: str = Field(min_length=1, max_length=64)
    notes: str | None = Field(default=None, max_length=500)


class CookPatch(BaseModel):
    """PATCH /api/cooks/{id} — rename, re-note or end a session."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    notes: str | None = Field(default=None, max_length=500)
    end: bool | None = None
