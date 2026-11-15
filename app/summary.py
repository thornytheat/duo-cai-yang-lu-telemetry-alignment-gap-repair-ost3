"""小时摘要：每小时 min / max / mean / p95 + 缺口格数 + carried 格数。

p95 用线性插值分位数（与 statistics.quantiles(method='inclusive') 口径一致，但手写以便
无 numpy 也能精确控制边界）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import settings


def percentile(values: list[float], p: float) -> float | None:
    """线性插值分位数，p ∈ [0, 100]。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    if p <= 0:
        return ordered[0]
    if p >= 100:
        return ordered[-1]
    position = (len(ordered) - 1) * p / 100.0
    lo = int(position)
    hi = min(lo + 1, len(ordered) - 1)
    frac = position - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def hourly_summary(cells: list[dict], cells_per_hour: int = 60) -> list[dict]:
    """按小时聚合网格（只统计有值的格；空/未修复格计入缺口数）。"""
    buckets: dict[int, list[dict]] = {}
    for cell in cells:
        hour = cell["bucket"] // cells_per_hour
        buckets.setdefault(hour, []).append(cell)

    out: list[dict] = []
    for hour in sorted(buckets):
        group = sorted(buckets[hour], key=lambda c: c["bucket"])
        values = [c["value"] for c in group if c["value"] is not None]
        gap_cells = sum(1 for c in group if c["source"] == "empty")
        carried = sum(1 for c in group if c["carried"])
        unrepaired = sum(1 for c in group if c["source"] == "empty")
        out.append(
            {
                "hour_index": hour,
                "hour_start": _iso(hour * cells_per_hour * settings.BUCKET_SECONDS),
                "cells": len(group),
                "value_cells": len(values),
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "mean": sum(values) / len(values) if values else None,
                "p95": percentile(values, 95),
                "gap_cells": gap_cells,
                "unrepaired_cells": unrepaired,
                "carried_cells": carried,
            }
        )
    return out


def _iso(epoch: float) -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.fromtimestamp(epoch, tz=tz).isoformat(timespec="seconds")
