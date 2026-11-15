"""质量报告：覆盖率、分级与跨设备对比。

coverage = 有效格 / 应到格；有效格 = 重采样后有值（原始 / 沿用 / 插补）的格子。
分级：>= 0.98 A，>= 0.95 B，其余 C。
"""
from __future__ import annotations

import csv
import io

from . import settings


def grade_for(coverage: float | None) -> str:
    if coverage is None:
        return "-"
    if coverage >= settings.GRADE_A_COVERAGE:
        return "A"
    if coverage >= settings.GRADE_B_COVERAGE:
        return "B"
    return "C"


def coverage_report(cells: list[dict], expected_cells: int, gaps: dict) -> dict:
    values = [c for c in cells if c["value"] is not None]
    raw = [c for c in cells if c["source"] in ("agg", "sample")]
    carried = [c for c in cells if c.get("carried")]
    interpolated = [c for c in cells if c["source"] == "interpolated"]
    missing = [c for c in cells if c["value"] is None]
    expected = max(1, expected_cells)
    coverage = len(values) / expected
    return {
        "expected_cells": expected,
        "grid_cells": len(cells),
        "raw_cells": len(raw),
        "carried_cells": len(carried),
        "interpolated_cells": len(interpolated),
        "missing_cells": len(missing),
        "valid_cells": len(values),
        "coverage": coverage,
        "raw_coverage": len(raw) / expected,
        "grade": grade_for(coverage),
        "gap_count": gaps.get("count", 0),
        "gap_cells": gaps.get("interpolated_cells", 0) + gaps.get("unrepaired_cells", 0),
        "unrepaired_cells": gaps.get("unrepaired_cells", 0),
    }


def device_rollup(rows: list[dict]) -> list[dict]:
    """按设备汇总（取该设备最差指标的覆盖率与分级）。"""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["device_id"], []).append(row)
    out: list[dict] = []
    for device, items in grouped.items():
        worst = min(items, key=lambda r: r["coverage"]["coverage"])
        drift_rows = [r for r in items if r.get("drift") and r["drift"].get("offset_s") is not None]
        offset = None
        if drift_rows:
            offset = drift_rows[0]["drift"]["offset_s"]
        out.append(
            {
                "device_id": device,
                "interval_s": items[0]["interval_s"],
                "metrics": [r["metric"] for r in sorted(items, key=lambda r: r["metric"])],
                "coverage": worst["coverage"]["coverage"],
                "grade": worst["coverage"]["grade"],
                "worst_metric": worst["metric"],
                "gap_count": sum(r["coverage"]["gap_count"] for r in items),
                "carried_cells": sum(r["coverage"]["carried_cells"] for r in items),
                "unrepaired_cells": sum(r["coverage"]["unrepaired_cells"] for r in items),
                "offset_s": offset,
                "drifted": any(r.get("drift") and r["drift"].get("drifted") for r in items),
            }
        )
    out.sort(key=lambda d: d["device_id"])
    return out


def report_summary(rows: list[dict]) -> dict:
    grades = [r["coverage"]["grade"] for r in rows if r["coverage"]["grade"] != "-"]
    counts = {"A": 0, "B": 0, "C": 0}
    for g in grades:
        counts[g] = counts.get(g, 0) + 1
    drifted = [r for r in rows if r.get("drift") and r["drift"].get("drifted")]
    return {
        "pairs": len(rows),
        "grade_counts": counts,
        "drifted_pairs": len(drifted),
        "avg_coverage": sum(r["coverage"]["coverage"] for r in rows) / len(rows) if rows else None,
        "total_gaps": sum(r["coverage"]["gap_count"] for r in rows),
        "total_carried": sum(r["coverage"]["carried_cells"] for r in rows),
        "total_unrepaired": sum(r["coverage"]["unrepaired_cells"] for r in rows),
    }


CSV_COLUMNS = [
    "device_id",
    "metric",
    "interval_s",
    "expected_cells",
    "raw_cells",
    "carried_cells",
    "interpolated_cells",
    "unrepaired_cells",
    "valid_cells",
    "coverage",
    "grade",
    "gap_count",
    "offset_s",
    "rate_ppm",
    "rmse_s",
    "drifted",
]


def report_csv(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        drift = row.get("drift") or {}
        cov = row["coverage"]
        writer.writerow(
            [
                row["device_id"],
                row["metric"],
                row["interval_s"],
                cov["expected_cells"],
                cov["raw_cells"],
                cov["carried_cells"],
                cov["interpolated_cells"],
                cov["unrepaired_cells"],
                cov["valid_cells"],
                f"{cov['coverage']:.4f}",
                cov["grade"],
                cov["gap_count"],
                "" if drift.get("offset_s") is None else f"{drift['offset_s']:.3f}",
                "" if drift.get("rate_ppm") is None else f"{drift['rate_ppm']:.1f}",
                "" if drift.get("rmse_s") is None else f"{drift['rmse_s']:.3f}",
                "" if drift.get("drifted") is None else ("yes" if drift["drifted"] else "no"),
            ]
        )
    return buffer.getvalue()
