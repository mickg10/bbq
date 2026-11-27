"""SQLite (stdlib) schema and helpers for samples, cooks and audit (task 4).

WAL mode, no ORM. Temperatures are °F floats; ``OPEN`` (no probe) is NULL.
The history query buckets by ``step`` seconds and averages each bucket, so
NULLs (probe absent) simply do not contribute to that bucket's average.
"""

from __future__ import annotations

import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts INTEGER NOT NULL,
    cook REAL, food1 REAL, food2 REAL, food3 REAL,
    cook_set REAL, food1_set REAL, food2_set REAL, food3_set REAL,
    output_pct INTEGER,
    cook_status INTEGER, food1_status INTEGER,
    food2_status INTEGER, food3_status INTEGER,
    timer TEXT, timer_status INTEGER
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples (ts);

CREATE TABLE IF NOT EXISTS cooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    started_ts INTEGER NOT NULL,
    ended_ts INTEGER,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS audit (
    ts INTEGER NOT NULL,
    field TEXT NOT NULL,
    old TEXT,
    new TEXT,
    client_ip TEXT
);
"""

_PROBES = ("cook", "food1", "food2", "food3")


def connect(path: str) -> sqlite3.Connection:
    """Open (and initialize) the database at ``path``.

    The parent directory is created if missing, so a bare container with
    an unmounted volume fails soft only when the mount is truly absent.
    """
    import os

    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def insert_sample(conn: sqlite3.Connection, ts: int, sample: dict) -> None:
    """Insert one parsed /all.xml sample (see app.device.parse_all_xml)."""
    probes = sample["probes"]
    conn.execute(
        "INSERT INTO samples (ts, cook, food1, food2, food3,"
        " cook_set, food1_set, food2_set, food3_set, output_pct,"
        " cook_status, food1_status, food2_status, food3_status,"
        " timer, timer_status)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            ts,
            *(probes[p]["temp"] for p in _PROBES),
            *(probes[p]["set"] for p in _PROBES),
            sample["output_pct"],
            *(probes[p]["status"] for p in _PROBES),
            sample["timer_curr"],
            sample["timer_status"],
        ),
    )
    conn.commit()


def latest_sample(conn: sqlite3.Connection, ts_to: int | None = None) -> dict | None:
    """The most recent stored sample at or before ``ts_to`` (unix seconds;
    any, by default), or None (fallback when the device is unreachable and
    nothing live is cached)."""
    row = conn.execute(
        "SELECT * FROM samples WHERE (? IS NULL OR ts <= ?) ORDER BY ts DESC LIMIT 1",
        (ts_to, ts_to),
    ).fetchone()
    return dict(row) if row is not None else None


def history(conn: sqlite3.Connection, t_from: int, t_to: int, step: int) -> list[dict]:
    """Downsampled series: per-bucket averages of temps, setpoints and fan %.

    ``step`` is in seconds; buckets are ``floor(ts/step)*step``.
    """
    if step <= 0:
        raise ValueError("step must be positive")
    rows = conn.execute(
        "SELECT (CAST(ts / ? AS INTEGER) * ?) AS bucket,"
        " AVG(cook) AS cook, AVG(food1) AS food1, AVG(food2) AS food2,"
        " AVG(food3) AS food3,"
        " AVG(cook_set) AS cook_set, AVG(food1_set) AS food1_set,"
        " AVG(food2_set) AS food2_set, AVG(food3_set) AS food3_set,"
        " AVG(output_pct) AS output_pct"
        " FROM samples WHERE ts >= ? AND ts <= ?"
        " GROUP BY bucket ORDER BY bucket",
        (step, step, t_from, t_to),
    ).fetchall()
    points = []
    for row in rows:
        point = dict(row)
        point["ts"] = point.pop("bucket")
        points.append(point)
    return points


def add_audit(
    conn: sqlite3.Connection,
    ts: int,
    field: str,
    old: Any,
    new: Any,
    client_ip: str | None,
) -> None:
    conn.execute(
        "INSERT INTO audit (ts, field, old, new, client_ip) VALUES (?,?,?,?,?)",
        (ts, field, _jsonable(old), _jsonable(new), client_ip),
    )
    conn.commit()


def list_audit(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM audit ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def _jsonable(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return repr(value)
    return str(value)


# ---------------------------------------------------------------------------
# cooks
# ---------------------------------------------------------------------------

def create_cook(
    conn: sqlite3.Connection,
    name: str,
    notes: str | None = None,
    started_ts: int | None = None,
) -> int:
    import time

    cursor = conn.execute(
        "INSERT INTO cooks (name, started_ts, ended_ts, notes) VALUES (?,?,?,?)",
        (name, started_ts if started_ts is not None else int(time.time()), None, notes),
    )
    conn.commit()
    return int(cursor.lastrowid)


def get_cook(conn: sqlite3.Connection, cook_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM cooks WHERE id = ?", (cook_id,)).fetchone()
    return dict(row) if row is not None else None


def list_cooks(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM cooks ORDER BY started_ts DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def update_cook(
    conn: sqlite3.Connection,
    cook_id: int,
    name: str | None = None,
    notes: str | None = None,
    ended_ts: int | None = None,
) -> bool:
    """Update the given fields; returns False if the cook does not exist."""
    if get_cook(conn, cook_id) is None:
        return False
    if name is not None:
        conn.execute("UPDATE cooks SET name = ? WHERE id = ?", (name, cook_id))
    if notes is not None:
        conn.execute("UPDATE cooks SET notes = ? WHERE id = ?", (notes, cook_id))
    if ended_ts is not None:
        conn.execute("UPDATE cooks SET ended_ts = ? WHERE id = ?", (ended_ts, cook_id))
    conn.commit()
    return True
