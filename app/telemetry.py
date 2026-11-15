"""数据导入：遥测 CSV 解析、字段校验、采样率识别、设备清单。

输入 CSV 列固定为 device_id,ts,metric,value；ts 为带时区的 ISO-8601。
本模块只做「读入 + 校验 + 归组」，不做任何对齐计算。
"""
from __future__ import annotations

import hashlib
import pathlib
import statistics
from dataclasses import dataclass, field
from datetime import datetime

from . import settings


class ImportError_(ValueError):
    """导入/校验失败，message 直接回显给使用者。"""


REQUIRED_COLUMNS = ("device_id", "ts", "metric", "value")


@dataclass
class Series:
    device_id: str
    metric: str
    points: list[tuple[float, float]] = field(default_factory=list)  # (epoch_seconds, value)
    interval_s: int = 60
    source: str = ""

    def times(self) -> list[float]:
        return [p[0] for p in self.points]

    def values(self) -> list[float]:
        return [p[1] for p in self.points]


@dataclass
class Dataset:
    dataset_id: str
    sha256: str
    source: str
    rows: int
    files: list[str]
    series: dict[tuple[str, str], Series]
    window_start: float = 0.0
    window_end: float = 0.0

    def inventory(self) -> list[dict]:
        devices: dict[str, dict] = {}
        for (device, metric), ser in sorted(self.series.items()):
            slot = devices.setdefault(
                device,
                {"device_id": device, "metrics": [], "rows": 0, "interval_s": ser.interval_s, "sources": []},
            )
            slot["metrics"].append({"metric": metric, "label": settings.METRIC_LABEL.get(metric, metric), "points": len(ser.points)})
            slot["rows"] += len(ser.points)
            if ser.source not in slot["sources"]:
                slot["sources"].append(ser.source)
        out = []
        for device, slot in devices.items():
            rates = {s.interval_s for (d, _), s in self.series.items() if d == device}
            slot["interval_s"] = min(rates) if rates else 0
            slot["rate_label"] = _rate_label(slot["interval_s"])
            slot["metrics"].sort(key=lambda m: m["metric"])
            out.append(slot)
        out.sort(key=lambda d: d["device_id"])
        return out

    def devices(self) -> list[str]:
        return sorted({d for (d, _) in self.series})

    def metrics_for(self, device: str) -> list[str]:
        return sorted(m for (d, m) in self.series if d == device)

    def series_for(self, device: str, metric: str) -> Series | None:
        return self.series.get((device, metric))

    def bucket_count(self) -> int:
        return int(self.window_end // settings.BUCKET_SECONDS - self.window_start // settings.BUCKET_SECONDS) + 1


def _rate_label(interval_s: int) -> str:
    return f"{interval_s}s"


def parse_ts(raw: str) -> float:
    text = (raw or "").strip()
    if not text:
        raise ImportError_("ts 为空")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ImportError_(f"ts 不是合法 ISO-8601：{raw!r}") from exc
    if dt.tzinfo is None:
        raise ImportError_(f"ts 缺少时区信息：{raw!r}")
    return dt.timestamp()


def parse_csv_text(text: str, source: str, series: dict[tuple[str, str], Series]) -> int:
    """把一份 CSV 文本并入 series，返回行数；字段非法时抛 ImportError_。"""
    import csv
    import io

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ImportError_(f"{source}：文件为空") from None
    header = [h.strip() for h in header]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise ImportError_(f"{source}：缺少必需列 {', '.join(missing)}")
    idx = {name: header.index(name) for name in REQUIRED_COLUMNS}

    rows = 0
    for lineno, row in enumerate(reader, start=2):
        if not row or all(not (c or "").strip() for c in row):
            continue
        if len(row) < len(header):
            raise ImportError_(f"{source} 第 {lineno} 行：列数不足（期望 {len(header)} 列）")
        device = (row[idx["device_id"]] or "").strip()
        metric = (row[idx["metric"]] or "").strip()
        if not device:
            raise ImportError_(f"{source} 第 {lineno} 行：device_id 为空")
        if metric not in settings.METRICS:
            raise ImportError_(f"{source} 第 {lineno} 行：metric 只允许 {'/'.join(settings.METRICS)}，实际 {metric!r}")
        ts = parse_ts(row[idx["ts"]])
        raw_value = (row[idx["value"]] or "").strip()
        try:
            value = float(raw_value)
        except ValueError:
            raise ImportError_(f"{source} 第 {lineno} 行：value 不是数字 {raw_value!r}") from None
        key = (device, metric)
        slot = series.setdefault(key, Series(device_id=device, metric=metric, source=source))
        slot.points.append((ts, value))
        rows += 1
    if rows == 0:
        raise ImportError_(f"{source}：没有数据行")
    return rows


def detect_interval(points: list[tuple[float, float]]) -> int:
    """用相邻间隔中位数识别采样率（秒），取整到秒。"""
    if len(points) < 3:
        return settings.BUCKET_SECONDS
    times = sorted(p[0] for p in points)
    deltas = [b - a for a, b in zip(times, times[1:]) if b - a > 0]
    if not deltas:
        return settings.BUCKET_SECONDS
    return max(1, int(round(statistics.median(deltas))))


def load_dataset(paths: list[pathlib.Path]) -> Dataset:
    """从一组 CSV 文件构建 Dataset；同 (device, metric) 的多文件会合并。"""
    if not paths:
        raise ImportError_("没有可导入的遥测文件（sample_data/telemetry 为空）")
    digest = hashlib.sha256()
    series: dict[tuple[str, str], Series] = {}
    total_rows = 0
    names: list[str] = []
    for path in sorted(paths, key=lambda p: p.name):
        blob = path.read_bytes()
        digest.update(path.name.encode("utf-8"))
        digest.update(blob)
        names.append(path.name)
        total_rows += parse_csv_text(blob.decode("utf-8-sig"), path.name, series)
    for ser in series.values():
        ser.points.sort(key=lambda p: p[0])
        ser.points = _dedupe(ser.points)
        ser.interval_s = detect_interval(ser.points)

    all_times = [p[0] for ser in series.values() for p in ser.points]
    bucket = settings.BUCKET_SECONDS
    start = (min(all_times) // bucket) * bucket
    end = (max(all_times) // bucket) * bucket
    sha = digest.hexdigest()
    return Dataset(
        dataset_id=f"ds-{sha[:12]}",
        sha256=sha,
        source="sample_data/telemetry",
        rows=total_rows,
        files=names,
        series=series,
        window_start=start,
        window_end=end,
    )


def _dedupe(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """同一时刻重复上报：保留最后一条（重复行计数由 dataset.rows - 去重后条数体现）。"""
    out: list[tuple[float, float]] = []
    for ts, value in points:
        if out and out[-1][0] == ts:
            out[-1] = (ts, value)
        else:
            out.append((ts, value))
    return out


def load_sample_dataset() -> Dataset:
    if not settings.SAMPLE_DIR.exists():
        raise ImportError_(f"样例目录不存在：{settings.SAMPLE_DIR}")
    paths = sorted(settings.SAMPLE_DIR.glob("*.csv"))
    return load_dataset(paths)
