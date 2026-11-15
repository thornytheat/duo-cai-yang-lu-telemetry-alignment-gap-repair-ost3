"""样例数据幂等 seed：容器每次启动都可重复执行，不产生重复数据集。"""
from __future__ import annotations

from . import settings, store, telemetry


def ensure_seed() -> dict:
    store.init_db()
    dataset = telemetry.load_sample_dataset()
    dataset_id = store.upsert_dataset(dataset)  # 按 sha256 幂等
    if store.get_meta("seed_version") != settings.SEED_VERSION:
        store.set_meta("seed_version", settings.SEED_VERSION)
    return {
        "dataset_id": dataset_id,
        "sha256": dataset.sha256,
        "rows": dataset.rows,
        "devices": dataset.devices(),
        "dataset": dataset,
    }
