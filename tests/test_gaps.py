"""缺口判定：3 格插补、5 格 unrepaired、首尾不外推、hold 段不算缺口。"""
from __future__ import annotations

import pytest

from app import gaps, grid

BASE = 1_750_000_000.0 // 60 * 60
B0 = int(BASE // 60)


def build(present_values: dict[int, float], end_offset: int) -> list[dict]:
    """按「哪些桶有原始点」直接构造 1 分钟网格（便于精确控制缺口形状）。"""
    cells = []
    for offset in range(end_offset + 1):
        value = present_values.get(offset)
        cells.append(
            {
                "bucket": B0 + offset,
                "bucket_start": grid.bucket_start_iso(B0 + offset),
                "value": value,
                "mean": value,
                "max": value,
                "min": value,
                "samples": 1 if value is not None else 0,
                "source": "sample" if value is not None else "empty",
                "carried": False,
                "filled": False,
                "first_ts": B0 * 60 + offset * 60 + 5 if value is not None else None,
                "last_ts": B0 * 60 + offset * 60 + 5 if value is not None else None,
            }
        )
    return cells


def test_three_cell_gap_is_linearly_interpolated():
    cells = build({0: 10.0, 1: 11.0, 5: 15.0, 6: 16.0}, end_offset=6)
    result = gaps.find_gaps(cells)
    assert result["count"] == 1
    item = result["items"][0]
    assert item["cells"] == 3
    assert item["kind"] == "interpolated"
    assert result["interpolated_cells"] == 3
    assert result["unrepaired_cells"] == 0
    # 线性插补：17.0/4=0.25 递增；起始值 11.0
    assert [round(v, 4) for v in item["filled_values"]] == [12.0, 13.0, 14.0]
    assert cells[2]["value"] == pytest.approx(12.0)
    assert cells[4]["source"] == "interpolated"


def test_five_cell_gap_is_marked_unrepaired():
    cells = build({0: 10.0, 1: 11.0, 7: 17.0}, end_offset=7)
    result = gaps.find_gaps(cells)
    assert result["count"] == 1
    item = result["items"][0]
    assert item["cells"] == 5
    assert item["kind"] == "unrepaired"
    assert result["unrepaired_cells"] == 5
    assert item["filled_values"] == []
    assert cells[3]["value"] is None  # 不填值


def test_head_gap_is_not_extrapolated():
    cells = build({2: 12.0, 3: 13.0}, end_offset=3)
    result = gaps.find_gaps(cells)
    assert result["count"] == 1
    item = result["items"][0]
    assert item["cells"] == 2
    assert item["kind"] == "unrepaired"
    assert item["boundary"] is True
    assert cells[0]["value"] is None
    assert cells[1]["value"] is None


def test_tail_gap_is_not_extrapolated():
    cells = build({0: 10.0, 1: 11.0}, end_offset=3)
    result = gaps.find_gaps(cells)
    assert result["count"] == 1
    item = result["items"][0]
    assert item["cells"] == 2
    assert item["kind"] == "unrepaired"
    assert item["boundary"] is True
    assert cells[3]["value"] is None


def test_no_gap_when_all_cells_have_raw_points():
    cells = build({i: float(i) for i in range(6)}, end_offset=5)
    result = gaps.find_gaps(cells)
    assert result["count"] == 0
    assert result["interpolated_cells"] == 0
    assert result["items"] == []


def test_two_gaps_are_reported_separately():
    cells = build({0: 0.0, 1: 1.0, 5: 5.0, 6: 6.0, 12: 12.0}, end_offset=12)
    result = gaps.find_gaps(cells)
    assert result["count"] == 2
    kinds = sorted(item["kind"] for item in result["items"])
    assert kinds == ["interpolated", "unrepaired"]
    assert [item["cells"] for item in result["items"]] == [3, 5]


def test_hold_filled_cells_are_not_reported_as_gap():
    # 60s 设备：第 2 分钟缺报但前后采样点相距 120s -> hold 沿用，不算缺口
    points = [(B0 * 60 + 5, 20.0), (B0 * 60 + 65, 21.0), (B0 * 60 + 185, 23.0), (B0 * 60 + 245, 24.0)]
    cells = grid.resample(points, interval_s=60, start_bucket=B0, end_bucket=B0 + 4)
    assert cells[2]["carried"] is True
    result = gaps.find_gaps(cells)
    assert result["count"] == 0
    assert result["interpolated_cells"] == 0


def test_out_of_service_buckets_are_not_gaps():
    """投运前/停用后的空桶不是缺口：只在在役段内判定，边界缺口也不含区间外桶。"""
    from app import service

    window_start = B0 * 60
    window_end = B0 * 60 + 10 * 60
    smap = service.build_service_map(
        [{"device_id": "D", "start_ts": window_start + 3 * 60, "end_ts": window_start + 8 * 60}],
        ["D"],
        window_start,
        window_end,
    )
    # 在役段 [3, 8) 内：第 4、5、6 桶空（3 格缺口，前后原始桶相距 4 格 → 不走 hold）
    points = [
        (B0 * 60 + (3 * 60) + 5, 10.0),
        (B0 * 60 + (7 * 60) + 5, 13.0),
        # 停用后的迟到点：保留但不参与，也不制造缺口
        (B0 * 60 + (9 * 60) + 5, 99.0),
    ]
    cells = grid.resample(points, 60, B0, B0 + 10, smap["D"])
    result = gaps.find_gaps(cells)
    assert result["count"] == 1
    item = result["items"][0]
    assert item["kind"] == "interpolated"
    assert item["start_bucket"] == B0 + 4 and item["end_bucket"] == B0 + 6
    # 首尾 0..2 与 8..10 均为区间外桶，不计 boundary
    assert result["boundary_cells"] == 0
    assert cells[9]["in_service"] is False
    assert cells[9]["excluded_points"][0]["reason"] == "late"
