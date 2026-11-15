"""设备启用记录（在役区间）：解析、校验、重叠合并、在役判定与区间外点标注。

背景：新设备可能在批次中途投运，也可能在批次结束前停用。若不纳入启用记录，
- 投运前 / 停用后的空白会被误判为缺口，健康设备被降级；
- 停用后的迟到记录会把曲线（hold 沿用 / 插值 / 摘要）延伸到生命周期外。

口径（与 grid / gaps / summary / quality 全链路一致）：
- 一台设备的启用记录合并为若干互不重叠的在役区间 [start, end)，end=None 表示至今。
- 分钟桶 b 的起点 b*60 落在任一在役区间内 → 该桶「在役」，参与缺口与覆盖率；
  否则「不在役」：桶保留在网格中并标注 exclude_reason，但不参与任何结果计算。
- 区间外的原始点一律保留（不丢弃），按时间相对在役区间标为 late / early，同样不参与结果。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from . import settings
from .telemetry import ImportError_, parse_ts

SERVICE_COLUMNS = ("device_id", "start_ts", "end_ts")

# 区间外点的标注原因
EXCLUDE_LATE = "late"      # 停用后的迟到记录
EXCLUDE_EARLY = "early"    # 投运前的提前记录
EXCLUDE_OUTSIDE = "outside"


@dataclass
class ServiceWindow:
    """一台设备合并后的在役区间（epoch 秒，半开区间 [start, end)，end=None 表示至今）。"""

    device_id: str
    intervals: list[tuple[float, float | None]] = field(default_factory=list)
    # 在役分钟桶集合（桶起点落在区间内）；落在数据集网格外的桶不预先放入
    in_service_buckets: set[int] = field(default_factory=set)

    def is_in_service_ts(self, ts: float) -> bool:
        return any(start <= ts and (end is None or ts < end) for start, end in self.intervals)

    def is_in_service_bucket(self, bucket: int) -> bool:
        if self.in_service_buckets:
            return bucket in self.in_service_buckets
        return self.is_in_service_ts(bucket * settings.BUCKET_SECONDS)

    def classify_point(self, ts: float) -> str | None:
        """返回 None 表示在役；否则返回 early / late / outside 标注。"""
        if self.is_in_service_ts(ts):
            return None
        starts = [start for start, _ in self.intervals]
        ends = [end for _, end in self.intervals if end is not None]
        if starts and ts < min(starts):
            return EXCLUDE_EARLY
        if ends and ts >= max(ends):
            return EXCLUDE_LATE
        return EXCLUDE_OUTSIDE

    def intervals_iso(self) -> list[dict]:
        from .grid import bucket_start_iso

        out = []
        for start, end in self.intervals:
            sb = int(start // settings.BUCKET_SECONDS)
            out.append(
                {
                    "start": bucket_start_iso(sb),
                    "start_ts": start,
                    "end_ts": end,
                    "end": bucket_start_iso(int(end // settings.BUCKET_SECONDS)) if end is not None else None,
                }
            )
        return out


def parse_service_csv_text(text: str, source: str = "service_records.csv") -> list[dict]:
    """解析启用记录 CSV：device_id,start_ts,end_ts（end_ts 留空表示至今）。"""
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ImportError_(f"{source}：文件为空") from None
    header = [h.strip() for h in header]
    missing = [c for c in SERVICE_COLUMNS if c not in header]
    if missing:
        raise ImportError_(f"{source}：缺少必需列 {', '.join(missing)}")
    idx = {name: header.index(name) for name in SERVICE_COLUMNS}

    records: list[dict] = []
    for lineno, row in enumerate(reader, start=2):
        if not row or all(not (c or "").strip() for c in row):
            continue
        if len(row) < len(header):
            raise ImportError_(f"{source} 第 {lineno} 行：列数不足（期望 {len(header)} 列）")
        device = (row[idx["device_id"]] or "").strip()
        if not device:
            raise ImportError_(f"{source} 第 {lineno} 行：device_id 为空")
        start_raw = (row[idx["start_ts"]] or "").strip()
        end_raw = (row[idx["end_ts"]] or "").strip()
        if not start_raw:
            raise ImportError_(f"{source} 第 {lineno} 行：start_ts 为空")
        start = parse_ts(start_raw)
        end = parse_ts(end_raw) if end_raw else None
        if end is not None and end <= start:
            raise ImportError_(f"{source} 第 {lineno} 行：end_ts 必须晚于 start_ts")
        records.append({"device_id": device, "start_ts": start, "end_ts": end})
    return records


def _merge_intervals(intervals: list[tuple[float, float | None]]) -> list[tuple[float, float | None]]:
    """合并重叠 / 相接的区间；含开区间（end=None）时合并结果为开区间。"""
    ordered = sorted(intervals, key=lambda iv: iv[0])
    merged: list[list[float]] = []
    open_end = False
    for start, end in ordered:
        if end is None:
            open_end = True
            end = float("inf")
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    out: list[tuple[float, float | None]] = []
    for start, end in merged:
        if open_end and end == float("inf"):
            out.append((start, None))
        else:
            out.append((start, end))
    return out


def build_service_map(
    records: list[dict],
    known_devices: list[str],
    window_start: float,
    window_end: float,
) -> dict[str, ServiceWindow]:
    """校验并合并启用记录，返回每台设备的 ServiceWindow。

    - 未知设备直接报错（启用记录必须对应数据集内设备）；
    - 没有启用记录的设备按「整段窗口在役」处理，保证旧口径不变；
    - 区间被裁剪到数据集窗口 [window_start, window_end] 内；
    - 同时预计算每个桶的在役集合，供网格 O(1) 判定。
    """
    known = set(known_devices)
    grouped: dict[str, list[tuple[float, float | None]]] = {}
    for rec in records:
        device = rec["device_id"]
        if device not in known:
            raise ImportError_(f"启用记录引用了数据集内不存在的设备 {device!r}")
        grouped.setdefault(device, []).append((rec["start_ts"], rec["end_ts"]))

    start_bucket = int(window_start // settings.BUCKET_SECONDS)
    end_bucket = int(window_end // settings.BUCKET_SECONDS)
    service_map: dict[str, ServiceWindow] = {}
    for device in sorted(known):
        raw = grouped.get(device)
        if not raw:
            intervals: list[tuple[float, float | None]] = [(window_start, window_end + settings.BUCKET_SECONDS)]
        else:
            merged = _merge_intervals(raw)
            clipped: list[tuple[float, float | None]] = []
            for start, end in merged:
                s = max(start, window_start)
                e = None if end is None else min(end, window_end + settings.BUCKET_SECONDS)
                if e is not None and e <= s:
                    continue
                clipped.append((s, e))
            if not clipped:
                raise ImportError_(f"设备 {device} 的启用记录与数据集窗口无交集")
            intervals = clipped
        window = ServiceWindow(device_id=device, intervals=intervals)
        window.in_service_buckets = _buckets_in_intervals(intervals, start_bucket, end_bucket)
        service_map[device] = window
    return service_map


def _buckets_in_intervals(
    intervals: list[tuple[float, float | None]], start_bucket: int, end_bucket: int
) -> set[int]:
    buckets: set[int] = set()
    for bucket in range(start_bucket, end_bucket + 1):
        ts = bucket * settings.BUCKET_SECONDS
        if any(start <= ts and (end is None or ts < end) for start, end in intervals):
            buckets.add(bucket)
    return buckets


def load_service_records_csv(path) -> list[dict]:
    if not path.exists():
        return []
    return parse_service_csv_text(path.read_text(encoding="utf-8-sig"), path.name)
