"""时钟漂移估计：构造已知 offset / 已知走时偏差，验证拟合、阈值判定与残差。"""
from __future__ import annotations

import math

import pytest

from app import drift, settings

START = 1_750_000_000.0  # 固定基准时刻，保证测试可复现


def triangle(t: float, period: float = 480.0, amp: float = 4.0, base: float = 21.0) -> float:
    phase = (t % period) / period
    return base + amp * (4 * abs(phase - 0.5) - 1)


def make_series(offset_s: float, rate_ppm: float, interval_s: int, span_s: int = 7200) -> list[tuple[float, float]]:
    """按物理时刻生成序列；设备时钟 = 真实时刻 + offset + rate*t。"""
    points: list[tuple[float, float]] = []
    t = 0.0
    while t < span_s:
        local = t + offset_s + (rate_ppm / 1e6) * t
        points.append((START + local, triangle(t)))
        t += interval_s
    return points


def test_fit_affine_recovers_exact_line():
    xs = [0.0, 60.0, 120.0, 180.0, 240.0]
    ys = [3.0 + 2.0 * x for x in xs]
    fit = drift.fit_affine(xs, ys)
    assert fit["a"] == pytest.approx(2.0, abs=1e-9)
    assert fit["b"] == pytest.approx(3.0, abs=1e-6)
    assert fit["rmse_s"] == pytest.approx(0.0, abs=1e-9)


def test_threshold_crossings_interpolates_between_samples():
    hits = drift.threshold_crossings([(0.0, 0.0), (10.0, 10.0)], 5.0)
    assert len(hits) == 1
    assert hits[0] == pytest.approx(5.0, abs=1e-9)


def test_threshold_crossings_skips_sparse_segments():
    # 相邻采样间隔 200s > 90s 上限，该区段不产生锚点
    assert drift.threshold_crossings([(0.0, 0.0), (200.0, 10.0)], 5.0) == []


def test_known_offset_positive():
    ref = make_series(0.0, 0.0, 1)
    dev = make_series(12.5, 0.0, 60)
    result = drift.estimate_drift(ref, dev, START)
    assert result["offset_s"] == pytest.approx(12.5, abs=0.3)
    assert result["drifted"] is True
    assert result["rmse_s"] < 0.5
    assert result["anchors"] >= settings.DRIFT_MIN_ANCHORS


def test_known_offset_negative():
    ref = make_series(0.0, 0.0, 1)
    dev = make_series(-8.0, 0.0, 60)
    result = drift.estimate_drift(ref, dev, START)
    assert result["offset_s"] == pytest.approx(-8.0, abs=0.3)
    assert result["drifted"] is True


def test_offset_below_threshold_is_not_drift():
    ref = make_series(0.0, 0.0, 1)
    dev = make_series(1.2, 0.0, 60)
    result = drift.estimate_drift(ref, dev, START, threshold_s=5.0)
    assert abs(result["offset_s"]) < 5.0
    assert result["drifted"] is False
    assert result["threshold_s"] == 5.0


def test_threshold_is_respected_by_caller():
    ref = make_series(0.0, 0.0, 1)
    dev = make_series(3.0, 0.0, 60)
    assert drift.estimate_drift(ref, dev, START, threshold_s=5.0)["drifted"] is False
    assert drift.estimate_drift(ref, dev, START, threshold_s=2.0)["drifted"] is True


def test_rate_ppm_and_slope_detected():
    ref = make_series(0.0, 0.0, 1)
    dev = make_series(0.0, 500.0, 60)  # 设备走时快 500ppm
    result = drift.estimate_drift(ref, dev, START)
    assert result["rate_ppm"] == pytest.approx(500.0, abs=30.0)
    assert result["a"] == pytest.approx(1.0 / (1.0 + 500e-6), abs=5e-5)
    assert abs(result["offset_s"]) < 0.5


def test_rmse_is_residual_after_correction():
    ref = make_series(0.0, 0.0, 1)
    noisy = [(ts, value + 0.002 * math.sin(i)) for i, (ts, value) in enumerate(make_series(37.0, 0.0, 60))]
    result = drift.estimate_drift(ref, noisy, START)
    assert abs(result["offset_s"] - 37.0) < 0.5
    assert 0.0 <= result["rmse_s"] < 0.5


def test_missing_chunk_in_device_series_still_aligns():
    dev = make_series(37.0, 0.0, 60)
    # 删掉中间 20 分钟（模拟断采）：鲁棒配对不应整体串位
    dev = [p for i, p in enumerate(dev) if not (2000 <= i < 2020)]
    ref = make_series(0.0, 0.0, 1)
    result = drift.estimate_drift(ref, dev, START)
    assert result["offset_s"] == pytest.approx(37.0, abs=0.6)


def test_insufficient_points_raises():
    with pytest.raises(drift.DriftError):
        drift.estimate_drift([(START, 1.0)], [(START, 1.0)], START)


def test_flat_series_has_no_anchors():
    flat_ref = [(START + i, 5.0) for i in range(120)]
    flat_dev = [(START + i * 60, 5.0) for i in range(60)]
    with pytest.raises(drift.DriftError):
        drift.estimate_drift(flat_ref, flat_dev, START)
