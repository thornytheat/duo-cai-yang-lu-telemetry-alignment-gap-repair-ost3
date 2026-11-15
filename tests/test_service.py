"""设备启用记录：解析校验、重叠合并、在役桶判定、区间外点标注与覆盖率口径。"""
from __future__ import annotations

import pytest

from app import grid, quality, service, settings
from app.telemetry import ImportError_

BASE = 1_750_000_000.0 // 60 * 60
B0 = int(BASE // 60)
WINDOW_START = BASE
WINDOW_END = BASE + 359 * settings.BUCKET_SECONDS  # 共 360 桶，与样例窗口同形


def _record(device: str, start_off: float, end_off: float | None) -> dict:
    return {
        "device_id": device,
        "start_ts": WINDOW_START + start_off * 60,
        "end_ts": None if end_off is None else WINDOW_START + end_off * 60,
    }


def test_parse_service_csv_requires_columns():
    with pytest.raises(ImportError_):
        service.parse_service_csv_text("device_id,start_ts\nD,x\n")


def test_parse_service_csv_blank_end_means_open_ended():
    text = (
        "device_id,start_ts,end_ts\n"
        "D,2025-01-01T00:00:00+08:00,\n"
    )
    records = service.parse_service_csv_text(text)
    assert records == [{"device_id": "D", "start_ts": records[0]["start_ts"], "end_ts": None}]


def test_parse_service_csv_rejects_end_before_start():
    text = (
        "device_id,start_ts,end_ts\n"
        "D,2025-01-01T02:00:00+08:00,2025-01-01T01:00:00+08:00\n"
    )
    with pytest.raises(ImportError_):
        service.parse_service_csv_text(text)


def test_overlapping_intervals_are_merged():
    merged = service._merge_intervals(
        [(100.0, 300.0), (200.0, 400.0), (500.0, 600.0), (350.0, 550.0)]
    )
    # 链式重叠（350 落在 [100,400) 内）整体合并为一个区间
    assert merged == [(100.0, 600.0)]
    # 真正不相交的两段保持独立
    assert service._merge_intervals([(0.0, 100.0), (200.0, 300.0)]) == [(0.0, 100.0), (200.0, 300.0)]


def test_touching_intervals_are_merged_and_open_ended_wins():
    merged = service._merge_intervals([(0.0, 100.0), (100.0, None)])
    assert merged == [(0.0, None)]


def test_build_service_map_unknown_device_rejected():
    with pytest.raises(ImportError_):
        service.build_service_map([_record("GHOST", 0, 10)], ["D1"], WINDOW_START, WINDOW_END)


def test_build_service_map_without_records_covers_full_window():
    smap = service.build_service_map([], ["D1"], WINDOW_START, WINDOW_END)
    assert len(smap["D1"].in_service_buckets) == 360
    assert smap["D1"].is_in_service_ts(WINDOW_START) is True


def test_build_service_map_clips_to_dataset_window():
    # 记录横跨窗口边界：桶集合只保留窗口内部分
    smap = service.build_service_map(
        [_record("D1", -30, 400)], ["D1"], WINDOW_START, WINDOW_END
    )
    window = smap["D1"]
    assert min(window.in_service_buckets) == B0
    assert max(window.in_service_buckets) == B0 + 359


def test_mid_batch_commission_excludes_pre_start_buckets():
    smap = service.build_service_map(
        [_record("D1", 120, 300)], ["D1"], WINDOW_START, WINDOW_END
    )
    window = smap["D1"]
    assert len(window.in_service_buckets) == 180  # [120, 300) 分钟
    assert window.is_in_service_bucket(B0 + 119) is False
    assert window.is_in_service_bucket(B0 + 120) is True
    assert window.is_in_service_bucket(B0 + 299) is True
    assert window.is_in_service_bucket(B0 + 300) is False
    # 投运前的点标 early，停用后的点标 late
    assert window.classify_point(WINDOW_START + 60) == service.EXCLUDE_EARLY
    assert window.classify_point(WINDOW_START + 301 * 60) == service.EXCLUDE_LATE
    assert window.classify_point(WINDOW_START + 200 * 60) is None


def test_resample_keeps_late_points_but_excludes_them_from_results():
    # 在役区间 [第 10 桶, 第 14 桶)；第 16 桶有一个停用后的迟到点
    smap = service.build_service_map(
        [_record("D1", 10, 14)], ["D1"], WINDOW_START, WINDOW_END
    )
    window = smap["D1"]
    points = [(B0 * 60 + (10 + i) * 60 + 5, 20.0 + i) for i in range(4)]
    points.append((B0 * 60 + 16 * 60 + 5, 99.0))  # 迟到点
    cells = grid.resample(points, 60, B0, B0 + 17, window)

    in_service = [c for c in cells if c["in_service"]]
    excluded = [c for c in cells if not c["in_service"]]
    assert len(in_service) == 4
    assert len(excluded) == 14
    # 迟到点保留在对应桶里并标 late，但桶没有 value、不参与统计
    late_cell = next(c for c in cells if c["bucket"] == B0 + 16)
    assert late_cell["value"] is None
    assert late_cell["excluded_points"][0]["reason"] == "late"
    assert late_cell["excluded_points"][0]["value"] == 99.0
    stats = grid.stats(cells)
    assert stats["excluded_points"] == 1
    assert stats["late_points"] == 1
    assert stats["sample_cells"] == 4


def test_hold_does_not_extend_curve_beyond_service_life():
    # 在役 [0, 3) 桶，第 4 桶起停用；即使后续桶出现稀疏点，hold 也不得延伸
    smap = service.build_service_map(
        [_record("D1", 0, 3)], ["D1"], WINDOW_START, WINDOW_END
    )
    window = smap["D1"]
    points = [
        (B0 * 60 + 5, 20.0),
        (B0 * 60 + 65, 21.0),
        (B0 * 60 + 125, 22.0),
        (B0 * 60 + 305, 50.0),  # 停用后的迟到点
    ]
    cells = grid.resample(points, 60, B0, B0 + 5, window)
    for cell in cells[3:]:
        assert cell["in_service"] is False
        assert cell["value"] is None
        assert cell["source"] == "empty"


def test_coverage_denominator_uses_in_service_minutes_only():
    smap = service.build_service_map(
        [_record("D1", 100, 200)], ["D1"], WINDOW_START, WINDOW_END
    )
    window = smap["D1"]
    # 在役 100 桶中 90 桶有原始点，其余为空
    points = [(B0 * 60 + (100 + i) * 60 + 5, float(i)) for i in range(90)]
    cells = grid.resample(points, 60, B0, B0 + 359, window)
    gaps = {"count": 0, "interpolated_cells": 0, "unrepaired_cells": 0}
    report = quality.coverage_report(cells, len(window.in_service_buckets), gaps)
    assert report["expected_cells"] == 100
    assert report["in_service_cells"] == 100
    assert report["excluded_cells"] == 260
    assert report["coverage"] == pytest.approx(0.9)
    assert report["raw_coverage"] == pytest.approx(0.9)
