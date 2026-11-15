"""编排：一条 (设备, 指标) 的对齐流水线。

1 数据导入（序列与基准选择）→ 2 漂移估计 → 3 网格重采样 → 4 缺口判定
→ 5 小时摘要 → 6 质量评分。路由只调用本模块，不写业务规则。
"""
from __future__ import annotations

import time

from . import drift, gaps, grid, quality, settings, summary
from .telemetry import Dataset, Series


def reference_for(dataset: Dataset, metric: str, target_device: str) -> tuple[Series | None, str | None]:
    """同一指标下采样率最高的序列作为时钟基准；目标设备本身即基准时返回 (自身, 自身)。"""
    candidates = [
        ser for (device, met), ser in dataset.series.items() if met == metric
    ]
    if not candidates:
        return None, None
    candidates.sort(key=lambda s: (s.interval_s, s.device_id))
    best = candidates[0]
    if best.device_id == target_device:
        return best, best.device_id
    return best, best.device_id


def run_alignment(
    dataset: Dataset,
    device_id: str,
    metric: str,
    threshold_s: float = settings.DEFAULT_OFFSET_THRESHOLD_S,
) -> dict:
    stages: list[dict] = []
    started = time.perf_counter()

    def stage(name: str, detail: str, begin: float) -> None:
        stages.append({"name": name, "detail": detail, "ms": round((time.perf_counter() - begin) * 1000, 2)})

    # 1) 导入/取序列
    t0 = time.perf_counter()
    series = dataset.series_for(device_id, metric)
    if series is None:
        raise KeyError(f"设备 {device_id} 没有指标 {metric}")
    ref_series, ref_device = reference_for(dataset, metric, device_id)
    self_reference = ref_series is not None and ref_series.device_id == device_id
    stage("数据导入", f"{len(series.points)} 点 / 采样率 {series.interval_s}s", t0)

    # 2) 漂移估计
    t0 = time.perf_counter()
    if self_reference:
        drift_result = {
            "available": False,
            "reason": "该指标最高频序列来自本设备，作为线级时钟基准，不能与自己比对",
            "threshold_s": threshold_s,
            "offset_s": None,
            "rate_ppm": None,
            "rmse_s": None,
            "anchors": 0,
            "drifted": None,
            "verdict": "本设备是该指标的时钟基准，无需漂移校正",
            "suggestion": "基准设备时间视为可信；可对其他设备执行对齐以查看偏移。",
        }
        stage("漂移估计", "基准设备，跳过自比", t0)
    else:
        drift_result = drift.estimate_drift(
            ref_series.points, series.points, dataset.window_start, threshold_s
        )
        drift_result["available"] = True
        drift_result["reference_device"] = ref_device
        drift_result["reference_interval_s"] = ref_series.interval_s
        stage("漂移估计", f"锚点 {drift_result['anchors']} 个，offset {drift_result['offset_s']:+.1f}s", t0)

    # 3) 网格重采样
    t0 = time.perf_counter()
    start_bucket = int(dataset.window_start // settings.BUCKET_SECONDS)
    end_bucket = int(dataset.window_end // settings.BUCKET_SECONDS)
    cells = grid.resample(series.points, series.interval_s, start_bucket, end_bucket)
    grid_stats = grid.stats(cells)
    stage("网格重采样", f"{grid_stats['cells']} 格（聚合 {grid_stats['agg_cells']} / 采样 {grid_stats['sample_cells']} / 沿用 {grid_stats['held_cells']}）", t0)

    # 4) 缺口判定与修复
    t0 = time.perf_counter()
    gap_result = gaps.find_gaps(cells)
    gaps.apply_repair(cells)
    stage("缺口判定", f"缺口 {gap_result['count']} 处，插补 {gap_result['interpolated_cells']} 格，未修复 {gap_result['unrepaired_cells']} 格", t0)

    # 5) 小时摘要
    t0 = time.perf_counter()
    hourly = summary.hourly_summary(cells)
    stage("小时摘要", f"{len(hourly)} 个小时桶", t0)

    # 6) 质量评分
    t0 = time.perf_counter()
    expected = dataset.bucket_count()
    coverage = quality.coverage_report(cells, expected, gap_result)
    stage("质量评分", f"覆盖率 {coverage['coverage']:.4f} → {coverage['grade']} 级", t0)

    payload = {
        "dataset_id": dataset.dataset_id,
        "device_id": device_id,
        "metric": metric,
        "metric_label": settings.METRIC_LABEL.get(metric, metric),
        "metric_unit": settings.METRIC_UNIT.get(metric, ""),
        "interval_s": series.interval_s,
        "threshold_s": threshold_s,
        "window": {
            "start": grid.bucket_start_iso(start_bucket),
            "end": grid.bucket_start_iso(end_bucket),
            "cells": expected,
            "bucket_seconds": settings.BUCKET_SECONDS,
        },
        "drift": drift_result,
        "grid": grid_stats,
        "gaps": gap_result,
        "coverage": coverage,
        "hourly": hourly,
        "preview": cells[:60],
        "reference": {
            "device_id": ref_device,
            "interval_s": ref_series.interval_s if ref_series else None,
            "self_reference": self_reference,
        },
        "stages": stages,
        "total_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    return payload


def build_report(dataset: Dataset, threshold_s: float = settings.DEFAULT_OFFSET_THRESHOLD_S) -> dict:
    rows: list[dict] = []
    for (device, metric), series in sorted(dataset.series.items()):
        payload = run_alignment(dataset, device, metric, threshold_s)
        rows.append(
            {
                "device_id": device,
                "metric": metric,
                "metric_label": payload["metric_label"],
                "interval_s": series.interval_s,
                "coverage": payload["coverage"],
                "drift": payload["drift"],
                "gaps": payload["gaps"],
            }
        )
    return {
        "dataset_id": dataset.dataset_id,
        "threshold_s": threshold_s,
        "rows": rows,
        "summary": quality.report_summary(rows),
        "devices": quality.device_rollup(rows),
        "csv": quality.report_csv(rows),
    }
