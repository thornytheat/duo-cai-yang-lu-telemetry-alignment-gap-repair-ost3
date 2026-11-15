"""SQLite 访问层：数据集元信息、对齐运行结果。命名卷 tsgrid-data:/app/data。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from . import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY,
    sha256     TEXT UNIQUE,
    source     TEXT,
    rows       INTEGER,
    files_json TEXT,
    inventory_json TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    dataset_id  TEXT,
    device_id   TEXT,
    metric      TEXT,
    threshold_s REAL,
    created_at  TEXT,
    payload_json TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _connect() -> sqlite3.Connection:
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def upsert_dataset(dataset) -> str:
    """按内容 sha256 幂等：同一份数据重复导入返回既有 dataset_id。"""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT dataset_id FROM datasets WHERE sha256 = ?", (dataset.sha256,)).fetchone()
        if row:
            return row["dataset_id"]
        conn.execute(
            "INSERT INTO datasets (dataset_id, sha256, source, rows, files_json, inventory_json, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                dataset.dataset_id,
                dataset.sha256,
                dataset.source,
                dataset.rows,
                json.dumps(dataset.files, ensure_ascii=False),
                json.dumps(dataset.inventory(), ensure_ascii=False),
                _now(),
            ),
        )
        return dataset.dataset_id


def list_datasets() -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT dataset_id, sha256, source, rows, files_json, inventory_json, created_at"
            " FROM datasets ORDER BY created_at"
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["files"] = json.loads(item.pop("files_json") or "[]")
        item["inventory"] = json.loads(item.pop("inventory_json") or "[]")
        out.append(item)
    return out


def save_run(dataset_id: str, payload: dict) -> str:
    init_db()
    run_id = "run-" + uuid.uuid4().hex[:10]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, dataset_id, device_id, metric, threshold_s, created_at, payload_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                run_id,
                dataset_id,
                payload.get("device_id"),
                payload.get("metric"),
                payload.get("threshold_s"),
                _now(),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
    return run_id


def get_run(run_id: str) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not row:
        return None
    payload = json.loads(row["payload_json"])
    payload["run_id"] = row["run_id"]
    payload["created_at"] = row["created_at"]
    return payload


def list_runs(limit: int = 20) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, device_id, metric, threshold_s, created_at FROM runs"
            " ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_meta(key: str) -> str | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
