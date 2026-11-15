"""样例数据幂等 seed：容器每次启动都可重复执行，不产生重复数据集。

遥测按内容 sha256 幂等；设备启用记录（在役区间）由 service_admin 引导：
DB 中有 active 版本就沿用，否则用样例 service_records.csv 落首个版本。
"""
from __future__ import annotations

from . import service_admin, settings, store, telemetry


def ensure_seed() -> dict:
    store.init_db()
    dataset = telemetry.load_sample_dataset()
    dataset_id = store.upsert_dataset(dataset)  # 按 sha256 幂等
    if store.get_meta("seed_version") != settings.SEED_VERSION:
        store.set_meta("seed_version", settings.SEED_VERSION)
    dataset, service_version_id, created = service_admin.bootstrap_service_map(dataset)
    return {
        "dataset_id": dataset_id,
        "sha256": dataset.sha256,
        "rows": dataset.rows,
        "devices": dataset.devices(),
        "service_version_id": service_version_id,
        "service_version_created": created,
        "dataset": dataset,
    }
