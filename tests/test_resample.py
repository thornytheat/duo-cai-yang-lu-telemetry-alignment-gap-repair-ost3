"""1 分钟网格重采样：高采样率聚合、低采样率 hold（carried）、计数与来源标记。"""
from __future__ import annotations

import pytest

from app import grid

BASE = 1_750_000_000.0 // 60 * 60  # 对齐到整分钟的基准时刻
B0 = int(BASE // 60)


def points_in_bucket(bucket: int, values: list[float], step: float) -> list[tuple[float, float]]:
    start = bucket * 60
    return [(start + i * step, v) for i, v in enumerate(values)]


def test_1s_series_aggregated_into_minute_cells():
    values = [float(i + 1) for i in range(60)]  # 1..60，每个原始点 1s
    points = points_in_bucket(B0, values, 1.0)
    cells = grid.resample(points, interval_s=1, start_bucket=B0, end_bucket=B0 + 2)

    first = cells[0]
    assert first["source"] == "agg"
    assert first["samples"] == 60
    assert first["mean"] == pytest.approx(30.5)
    assert first["min"] == pytest.approx(1.0)
    assert first["max"] == pytest.approx(60.0)
    assert first["value"] == pytest.approx(30.5)
    assert first["carried"] is False
    # 没有原始点的桶保持空，不静默补值
    assert cells[1]["source"] == "empty"
    assert cells[1]["value"] is None


def test_5s_series_aggregated_with_12_samples():
    points = [(B0 * 60 + i * 5, 10.0 + i) for i in range(12)]
    cells = grid.resample(points, interval_s=5, start_bucket=B0, end_bucket=B0)
    assert cells[0]["samples"] == 12
    assert cells[0]["min"] == pytest.approx(10.0)
    assert cells[0]["max"] == pytest.approx(21.0)


def test_60s_series_uses_sample_value_directly():
    points = [(B0 * 60 + 37, 3.25), (B0 * 60 + 60 + 37, 3.30)]
    cells = grid.resample(points, interval_s=60, start_bucket=B0, end_bucket=B0 + 1)
    assert cells[0]["source"] == "sample"
    assert cells[0]["value"] == pytest.approx(3.25)
    assert cells[0]["samples"] == 1
    assert cells[0]["carried"] is False


def test_60s_series_holds_missing_minute_with_carried_flag():
    # 60s 设备：第 0、1 分钟有值，第 2 分钟缺报，第 3 分钟恢复
    points = [
        (B0 * 60 + 5, 20.0),
        (B0 * 60 + 65, 21.0),
        (B0 * 60 + 185, 23.0),
    ]
    cells = grid.resample(points, interval_s=60, start_bucket=B0, end_bucket=B0 + 3)
    held = cells[2]
    assert held["source"] == "hold"
    assert held["carried"] is True
    assert held["filled"] is True
    assert held["value"] == pytest.approx(21.0)  # 沿用前值

    stats = grid.stats(cells)
    assert stats["carried_cells"] == 1
    assert stats["sample_cells"] == 3
    assert stats["empty_cells"] == 0


def test_high_rate_single_missing_minute_is_not_held():
    # 1s 设备只缺 1 分钟：前后采样点相距约 61s，不该被 hold 掩盖
    points = [(B0 * 60 + float(i), 5.0) for i in range(60)]
    points += [(B0 * 60 + 120 + float(i), 6.0) for i in range(60)]
    cells = grid.resample(points, interval_s=1, start_bucket=B0, end_bucket=B0 + 2)
    assert cells[1]["source"] == "empty"
    assert cells[1]["carried"] is False
    assert grid.stats(cells)["carried_cells"] == 0


def test_long_outage_is_left_empty_for_gap_module():
    # 中间 5 分钟没有数据：远超 hold 上限，保持空，交给缺口判定
    points = [(B0 * 60 + float(i), 5.0) for i in range(60)]
    points += [(B0 * 60 + 360 + float(i), 6.0) for i in range(60)]
    cells = grid.resample(points, interval_s=1, start_bucket=B0, end_bucket=B0 + 6)
    for cell in cells[1:6]:
        assert cell["source"] == "empty"
        assert cell["value"] is None
    assert grid.stats(cells)["carried_cells"] == 0


def test_leading_buckets_are_not_backfilled():
    points = [(B0 * 60 + 120 + float(i), 7.0) for i in range(60)]
    cells = grid.resample(points, interval_s=1, start_bucket=B0, end_bucket=B0 + 2)
    assert cells[0]["value"] is None
    assert cells[1]["value"] is None
    assert cells[2]["source"] == "agg"
