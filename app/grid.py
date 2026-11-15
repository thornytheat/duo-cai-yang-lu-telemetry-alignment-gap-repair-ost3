"""统一网格重采样：所有序列落到 1 分钟桶。

规则：
- 采样率高于桶（interval < 60s）→ 桶内 mean/max/min + 计数，source='agg'。
- 采样率低于桶（interval >= 60s）或该段上报间隔被拉长（降级）→ hold 沿用，carried=True。
  hold 只在「前后原始格相距 <= 3 格」且「前后采样点间距 > 1.5 桶」时发生；
  更长空缺留给缺口模块判定，1s 设备偶尔丢一格也不会被静默补值。
- 首格之前没有前值 → 不反向外推（保持空）。
"""
from __future__ import annotations

from . import settings


def bucket_of(ts: float) -> int:
    return int(ts // settings.BUCKET_SECONDS)


def bucket_start_iso(bucket: int) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(bucket * settings.BUCKET_SECONDS, tz=_tz()).isoformat(timespec="seconds")


def _tz():
    from datetime import timedelta, timezone

    return timezone(timedelta(hours=8))


def _cell(bucket: int, **kwargs) -> dict:
    base = {
        "bucket": bucket,
        "bucket_start": bucket_start_iso(bucket),
        "value": None,
        "mean": None,
        "max": None,
        "min": None,
        "samples": 0,
        "source": "empty",   # agg | sample | hold | interpolated | empty
        "carried": False,
        "filled": False,     # 缺口插补或沿用后是否为「非原始」值
        "first_ts": None,
        "last_ts": None,
    }
    base.update(kwargs)
    return base


def resample(
    points: list[tuple[float, float]],
    interval_s: int,
    start_bucket: int,
    end_bucket: int,
) -> list[dict]:
    """把一条序列重采样到 [start_bucket, end_bucket] 的 1 分钟格。"""
    grouped: dict[int, list[tuple[float, float]]] = {}
    for ts, value in points:
        b = bucket_of(ts)
        if b < start_bucket or b > end_bucket:
            continue
        grouped.setdefault(b, []).append((ts, value))

    high_rate = interval_s < settings.BUCKET_SECONDS
    cells: list[dict] = []
    for bucket in range(start_bucket, end_bucket + 1):
        samples = grouped.get(bucket)
        if not samples:
            cells.append(_cell(bucket))
            continue
        values = [v for _, v in samples]
        if high_rate:
            cells.append(
                _cell(
                    bucket,
                    first_ts=min(t for t, _ in samples),
                    last_ts=max(t for t, _ in samples),
                    value=sum(values) / len(values),
                    mean=sum(values) / len(values),
                    max=max(values),
                    min=min(values),
                    samples=len(values),
                    source="agg",
                )
            )
        else:
            last_ts, last_value = max(samples, key=lambda p: p[0])
            cells.append(
                _cell(
                    bucket,
                    first_ts=min(t for t, _ in samples),
                    last_ts=last_ts,
                    value=last_value,
                    mean=last_value,
                    max=last_value,
                    min=last_value,
                    samples=len(samples),
                    source="sample",
                )
            )
    _apply_hold(cells)
    return cells


def _apply_hold(cells: list[dict]) -> None:
    """低频/降级采样段：hold 沿用前值并计 carried。

    触发条件（两者同时满足）：
    - 前后原始格相距 <= HOLD_MAX_BRACKET_BUCKETS 格（更长的空缺交给缺口模块判定）；
    - 前后两个原始采样点的时间间距 > 1.5 桶（说明该段上报间隔确实稀疏，
      1s 设备偶尔丢一格时不静默补值）。
    """
    anchor_idx = [i for i, c in enumerate(cells) if c["source"] in ("agg", "sample")]
    if len(anchor_idx) < 2:
        return
    min_spacing_s = 1.5 * settings.BUCKET_SECONDS
    for prev, nxt in zip(anchor_idx, anchor_idx[1:]):
        distance = cells[nxt]["bucket"] - cells[prev]["bucket"]
        if distance <= 1 or distance > settings.HOLD_MAX_BRACKET_BUCKETS:
            continue
        prev_ts = cells[prev]["last_ts"]
        next_ts = cells[nxt]["first_ts"]
        if prev_ts is None or next_ts is None:
            continue
        if next_ts - prev_ts <= min_spacing_s:
            continue
        hold_value = cells[prev]["value"]
        for i in range(prev + 1, nxt):
            cells[i]["value"] = hold_value
            cells[i]["mean"] = hold_value
            cells[i]["max"] = hold_value
            cells[i]["min"] = hold_value
            cells[i]["source"] = "hold"
            cells[i]["carried"] = True
            cells[i]["filled"] = True


def stats(cells: list[dict]) -> dict:
    counts = {"agg": 0, "sample": 0, "hold": 0, "empty": 0}
    for cell in cells:
        counts[cell["source"]] = counts.get(cell["source"], 0) + 1
    raw = counts["agg"] + counts["sample"]
    return {
        "cells": len(cells),
        "agg_cells": counts["agg"],
        "sample_cells": counts["sample"],
        "held_cells": counts["hold"],
        "empty_cells": counts["empty"],
        "raw_cells": raw,
        "carried_cells": counts["hold"],
    }
