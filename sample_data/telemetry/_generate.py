#!/usr/bin/env python3
"""生成产线 A 三台设备 6 小时遥测样例（确定性，无随机种子漂移）。

场景事实（与 README / TESTING.md 一致）：
- LINE-A-01：1s 采样，温度（唯一带 1s 快速缓冲的设备）
- LINE-A-02：5s 采样，温度 + 压力；02:10–02:55 采集器降级为 180s → 1 分钟网格需 hold（carried）
- LINE-A-03：60s 采样，温度 + 压力；设备时钟偏移 +37s、走时偏快 55ppm；
  05:00–05:50 归档器降级为 120s（carried）；压力通道 01:40–02:05 断采
- 全线两处缺采（所有设备、所有指标）：03:00–03:02（3 格）、04:10–04:14（5 格）
- LINE-A-02 温度 05:10–05:21 断采（12 格）
- LINE-A-03 压力 01:40–02:05 断采（25 格）

数值形态为三角波（分段线性），便于用阈值穿越点做时钟漂移配准；
噪声幅度远小于斜率，不影响穿越时刻的线性插值精度。
"""
from __future__ import annotations

import csv
import math
import pathlib
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
DAY = datetime(2026, 5, 12, 0, 0, 0, tzinfo=TZ)
SPAN_S = 6 * 3600

DEVICES = {
    "LINE-A-01": {"interval_s": 1, "metrics": ["temperature"], "offset_s": 0.0, "rate_ppm": 0.0},
    "LINE-A-02": {"interval_s": 5, "metrics": ["temperature", "pressure"], "offset_s": 0.0, "rate_ppm": 0.0},
    "LINE-A-03": {"interval_s": 60, "metrics": ["temperature", "pressure"], "offset_s": 37.0, "rate_ppm": 55.0},
}

# 指标形态：三角波 + 极小噪声（噪声 < 0.05% 幅值）
METRIC_SHAPE = {
    "temperature": {"base": 21.0, "amp": 4.0, "period_s": 8 * 60, "noise": 0.002, "decimals": 3},
    "pressure": {"base": 3.20, "amp": 0.40, "period_s": 6 * 60, "noise": 0.0008, "decimals": 4},
}

# 缺采窗口（线性分钟窗口，[start, end)），按 (device, metric) 或 "ALL"
OUTAGES = [
    ("ALL", "ALL", 3 * 3600, 3 * 3600 + 3 * 60),
    ("ALL", "ALL", 4 * 3600 + 10 * 60, 4 * 3600 + 15 * 60),
    ("LINE-A-02", "temperature", 5 * 3600 + 10 * 60, 5 * 3600 + 22 * 60),
    ("LINE-A-03", "pressure", 3600 + 40 * 60, 2 * 3600 + 5 * 60),
]

# 降级窗口（采集器把 cadence 调大，需要 1 分钟网格 hold）
DEGRADED = [
    ("LINE-A-02", "temperature", 2 * 3600 + 10 * 60, 2 * 3600 + 55 * 60, 180),
    ("LINE-A-03", "temperature", 5 * 3600, 5 * 3600 + 50 * 60, 120),
    ("LINE-A-03", "pressure", 5 * 3600, 5 * 3600 + 50 * 60, 120),
]


def triangle(t: float, base: float, amp: float, period: float) -> float:
    phase = (t % period) / period
    return base + amp * (4 * abs(phase - 0.5) - 1)


def noise(t: float, amp: float, salt: int) -> float:
    """确定性伪噪声：正弦叠加，无随机数依赖。"""
    return amp * (math.sin(t * 0.71 + salt) * 0.6 + math.sin(t * 1.93 + salt * 2.3) * 0.4)


def in_window(t: float, start: float, end: float) -> bool:
    return start <= t < end


def outage_for(device: str, metric: str, t: float) -> bool:
    for dev, met, start, end in OUTAGES:
        if dev not in ("ALL", device):
            continue
        if met not in ("ALL", metric):
            continue
        if in_window(t, start, end):
            return True
    return False


def cadence_for(device: str, metric: str, t: float, default: int) -> int:
    for dev, met, start, end, cadence in DEGRADED:
        if dev == device and met == metric and in_window(t, start, end):
            return cadence
    return default


def build_rows() -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for device, spec in DEVICES.items():
        offset = spec["offset_s"]
        drift = spec["rate_ppm"] / 1_000_000.0
        for metric in spec["metrics"]:
            shape = METRIC_SHAPE[metric]
            salt = 1 if metric == "temperature" else 7
            t = 0
            while t < SPAN_S:
                cadence = cadence_for(device, metric, t, spec["interval_s"])
                if not outage_for(device, metric, t):
                    value = triangle(t, shape["base"], shape["amp"], shape["period_s"])
                    value += noise(t, shape["noise"], salt)
                    # 设备本地时钟 = 真实时刻 + 固定偏移 + 走时误差
                    local = t + offset + drift * t
                    ts = (DAY + timedelta(seconds=local)).isoformat(timespec="milliseconds")
                    rows.append((device, ts, metric, f"{value:.{shape['decimals']}f}"))
                t += cadence
    return rows


def main() -> None:
    here = pathlib.Path(__file__).resolve().parent
    files = {
        "line-a-01-1s.csv": "LINE-A-01",
        "line-a-02-5s.csv": "LINE-A-02",
        "line-a-03-60s.csv": "LINE-A-03",
    }
    rows = build_rows()
    for name, device in files.items():
        subset = [r for r in rows if r[0] == device]
        with (here / name).open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["device_id", "ts", "metric", "value"])
            writer.writerows(subset)
        print(f"{name}: {len(subset)} rows")


if __name__ == "__main__":
    main()
