"""缺口判定与修复。

判定：网格内没有原始点（source='empty'），且前后都有原始点 → gap。
修复：gap 长度 <= 3 格 → 线性插补（interpolated）；> 3 格 → unrepaired。
边界：序列首尾不外推——开头/结尾的空缺只登记为 unrepaired(boundary)，绝不填值。
在役：不在役桶（in_service=False，投运前/停用后）不参与缺口判定与修复，
     也不会被当作序列首尾空缺——生命周期外的空白不是缺口。
"""
from __future__ import annotations

from . import settings


def _in_service(cell: dict) -> bool:
    return cell.get("in_service", True)


def find_gaps(cells: list[dict]) -> dict:
    """在（已 hold 的）网格上找出缺口并给出修复结论。"""
    raw_idx = [
        i
        for i, c in enumerate(cells)
        if _in_service(c) and c["source"] in ("agg", "sample")
    ]
    gaps: list[dict] = []

    if not raw_idx:
        return {
            "items": [],
            "count": 0,
            "interpolated_cells": 0,
            "unrepaired_cells": 0,
            "boundary_cells": 0,
        }

    # 首部空缺（仅限在役段）：不外推
    first = raw_idx[0]
    head = [i for i in range(0, first) if _in_service(cells[i])]
    if head:
        gaps.append(
            _gap_item(
                cells,
                start_idx=head[0],
                end_idx=head[-1],
                kind="unrepaired",
                reason="在役段开头空缺，首端不外推",
                prev_idx=None,
                next_idx=first,
            )
        )

    for prev, nxt in zip(raw_idx, raw_idx[1:]):
        run_idx = [i for i in range(prev + 1, nxt) if _in_service(cells[i])]
        run_len = len(run_idx)
        if run_len == 0:
            continue
        bracket = cells[nxt]["bucket"] - cells[prev]["bucket"]
        if bracket <= settings.HOLD_MAX_BRACKET_BUCKETS:
            continue  # 已由 hold 沿用（carried），不算缺口
        kind = "interpolated" if run_len <= settings.INTERPOLATE_MAX_CELLS else "unrepaired"
        reason = (
            f"连续 {run_len} 格无原始点（前后原始格相距 {bracket} 分钟），"
            + ("允许线性插补（≤3 格）" if kind == "interpolated" else "超过 3 格，标记未修复")
        )
        gaps.append(
            _gap_item(
                cells,
                start_idx=run_idx[0],
                end_idx=run_idx[-1],
                kind=kind,
                reason=reason,
                prev_idx=prev,
                next_idx=nxt,
            )
        )

    # 尾部空缺（仅限在役段）：不外推
    last = raw_idx[-1]
    tail = [i for i in range(last + 1, len(cells)) if _in_service(cells[i])]
    if tail:
        gaps.append(
            _gap_item(
                cells,
                start_idx=tail[0],
                end_idx=tail[-1],
                kind="unrepaired",
                reason="在役段结尾空缺，尾端不外推",
                prev_idx=last,
                next_idx=None,
            )
        )

    interpolated = 0
    unrepaired = 0
    boundary = 0
    for item in gaps:
        if item["kind"] == "interpolated":
            interpolated += item["cells"]
        else:
            unrepaired += item["cells"]
            if item["boundary"]:
                boundary += item["cells"]
    return {
        "items": gaps,
        "count": len(gaps),
        "interpolated_cells": interpolated,
        "unrepaired_cells": unrepaired,
        "boundary_cells": boundary,
    }


def _gap_item(
    cells: list[dict],
    start_idx: int,
    end_idx: int,
    kind: str,
    reason: str,
    prev_idx: int | None,
    next_idx: int | None,
) -> dict:
    run = cells[start_idx : end_idx + 1]
    length = len(run)
    boundary = prev_idx is None or next_idx is None
    filled: list[float] = []
    if kind == "interpolated" and prev_idx is not None and next_idx is not None:
        v0 = cells[prev_idx]["value"]
        v1 = cells[next_idx]["value"]
        span = cells[next_idx]["bucket"] - cells[prev_idx]["bucket"]
        for offset, cell in enumerate(run, start=1):
            value = v0 + (v1 - v0) * offset / span
            cell["value"] = value
            cell["mean"] = value
            cell["max"] = value
            cell["min"] = value
            cell["source"] = "interpolated"
            cell["filled"] = True
            filled.append(value)
    return {
        "start_bucket": run[0]["bucket"],
        "start_time": run[0]["bucket_start"],
        "end_bucket": run[-1]["bucket"],
        "end_time": run[-1]["bucket_start"],
        "cells": length,
        "kind": kind,
        "reason": reason,
        "boundary": boundary,
        "prev_time": cells[prev_idx]["bucket_start"] if prev_idx is not None else None,
        "next_time": cells[next_idx]["bucket_start"] if next_idx is not None else None,
        "filled_values": filled,
    }


def apply_repair(cells: list[dict]) -> list[dict]:
    """返回补齐后的网格（find_gaps 已就地写入插补值，这里做一次显式返回便于测试）。"""
    return cells
