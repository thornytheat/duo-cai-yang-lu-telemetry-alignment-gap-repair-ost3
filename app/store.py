"""SQLite 访问层：数据集元信息、对齐运行结果、启用记录版本、质量报告快照。

命名卷 tsgrid-data:/app/data。

启用记录更正的事务边界（replace_service_version）：
同一 SQLite 事务内完成「写新版本 → 置 active → 落整批报告快照」，
任一步失败整事务回滚，旧 active 版本与旧快照保持不变（失败恢复旧版）。
"""
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
CREATE TABLE IF NOT EXISTS service_versions (
    version_id   TEXT PRIMARY KEY,
    dataset_id   TEXT,
    records_json TEXT,
    created_at   TEXT,
    is_active    INTEGER DEFAULT 0,
    note         TEXT
);
CREATE TABLE IF NOT EXISTS report_snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    dataset_id   TEXT,
    service_version_id TEXT,
    threshold_s  REAL,
    created_at   TEXT,
    payload_json TEXT
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


def transaction() -> sqlite3.Connection:
    """显式事务连接：with store.transaction() as conn，异常自动回滚。"""
    init_db()
    return _connect()


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


# ---------------------------------------------------------------------------
# 设备启用记录版本（更正 → 原子切换，失败回滚旧版）
# ---------------------------------------------------------------------------

def save_service_version(
    conn: sqlite3.Connection,
    dataset_id: str,
    records: list[dict],
    note: str,
    activate: bool,
) -> str:
    """在给定事务连接内写入一个启用记录版本，按需置为 active（先清旧 active）。"""
    version_id = "svc-" + uuid.uuid4().hex[:10]
    conn.execute(
        "INSERT INTO service_versions (version_id, dataset_id, records_json, created_at, is_active, note)"
        " VALUES (?,?,?,?,?,?)",
        (version_id, dataset_id, json.dumps(records, ensure_ascii=False), _now(), 1 if activate else 0, note),
    )
    if activate:
        conn.execute("UPDATE service_versions SET is_active = 0 WHERE dataset_id = ?", (dataset_id,))
        conn.execute("UPDATE service_versions SET is_active = 1 WHERE version_id = ?", (version_id,))
    return version_id


def get_active_service_version(dataset_id: str) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM service_versions WHERE dataset_id = ? AND is_active = 1 ORDER BY created_at DESC LIMIT 1",
            (dataset_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "version_id": row["version_id"],
        "dataset_id": row["dataset_id"],
        "records": json.loads(row["records_json"]),
        "created_at": row["created_at"],
        "note": row["note"],
    }


def list_service_versions(dataset_id: str) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT version_id, created_at, is_active, note FROM service_versions"
            " WHERE dataset_id = ? ORDER BY created_at",
            (dataset_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def replace_service_version(
    dataset_id: str,
    records: list[dict],
    report_payload: dict,
    note: str,
) -> str:
    """原子切换：写新版本 + 置 active + 落整批报告快照，同一事务，失败整体回滚。

    报告快照 id 由 (dataset, service_version, threshold) 决定，
    同批重跑命中既有快照，不新增报告行。
    """
    init_db()
    with _connect() as conn:
        version_id = save_service_version(conn, dataset_id, records, note, activate=True)
        # 报告快照自包含真实版本号（快照主键也按该版本号派生）
        report_payload = {**report_payload, "service_version_id": version_id}
        snapshot_id = report_snapshot_id(dataset_id, version_id, report_payload["threshold_s"])
        conn.execute(
            "INSERT OR IGNORE INTO report_snapshots"
            " (snapshot_id, dataset_id, service_version_id, threshold_s, created_at, payload_json)"
            " VALUES (?,?,?,?,?,?)",
            (
                snapshot_id,
                dataset_id,
                version_id,
                report_payload["threshold_s"],
                _now(),
                json.dumps(report_payload, ensure_ascii=False),
            ),
        )
    return version_id


# ---------------------------------------------------------------------------
# 质量报告快照：同批（数据集 + 启用版本 + 阈值）重跑不新增报告
# ---------------------------------------------------------------------------

def report_snapshot_id(dataset_id: str, service_version_id: str | None, threshold_s: float) -> str:
    raw = f"{dataset_id}|{service_version_id or 'no-service'}|{float(threshold_s):g}"
    return "rpt-" + uuid.uuid5(uuid.NAMESPACE_URL, raw).hex[:12]


def save_report_snapshot(
    dataset_id: str,
    service_version_id: str | None,
    threshold_s: float,
    payload: dict,
) -> str:
    init_db()
    snapshot_id = report_snapshot_id(dataset_id, service_version_id, threshold_s)
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO report_snapshots"
            " (snapshot_id, dataset_id, service_version_id, threshold_s, created_at, payload_json)"
            " VALUES (?,?,?,?,?,?)",
            (
                snapshot_id,
                dataset_id,
                service_version_id,
                threshold_s,
                _now(),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
    return snapshot_id


def get_report_snapshot(
    dataset_id: str, service_version_id: str | None, threshold_s: float
) -> dict | None:
    init_db()
    snapshot_id = report_snapshot_id(dataset_id, service_version_id, threshold_s)
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM report_snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
    if not row:
        return None
    return json.loads(row["payload_json"])
