"""启用记录更正编排：合并重叠区间 → 重算受影响设备 → 整批一致性核对 → 原子切换。

流程（任一环节失败都不改变当前生效版本，由 store 的单事务保证回滚）：
1. 解析/校验启用记录（设备必须存在、时间合法），合并重叠区间；
2. 用新区间构建候选 Dataset（不触碰线上 STATE 中的数据集）；
3. 对整批「设备 × 指标」重跑对齐流水线；
4. 整批一致性核对：每条序列的应到格 == 在役桶数、覆盖率 == 有效/应到、
   区间外点全部标注 excluded 且未参与任何计数；
5. 核对通过后在单个 SQLite 事务内写新版本、置 active、落报告快照；
   重跑同一批记录（合并结果与当前 active 相同）直接返回当前版本，不新增版本/报告。
"""
from __future__ import annotations

import dataclasses

from . import pipeline, service, settings, store
from .telemetry import ImportError_


def apply_service_records(dataset, records: list[dict], note: str = "人工更正启用记录") -> dict:
    """用一组启用记录更正数据集的在役口径，原子切换并返回切换结果。"""
    # 1) 校验 + 合并（直接构建，非法输入在此抛 ImportError_，不会写库）
    candidate_map = service.build_service_map(
        records, dataset.devices(), dataset.window_start, dataset.window_end
    )

    active = store.get_active_service_version(dataset.dataset_id)
    if active is not None and _same_as_active(active["records"], candidate_map, dataset):
        return {
            "changed": False,
            "version_id": active["version_id"],
            "reason": "提交的启用记录与当前生效版本合并后完全一致，未生成新版本",
            "report": store.get_report_snapshot(
                dataset.dataset_id, active["version_id"], settings.DEFAULT_OFFSET_THRESHOLD_S
            )
            or _build_and_snapshot(dataset, candidate_map, active["version_id"]),
        }

    # 2) 候选数据集（内存中替换 service_map，线上对象不动）
    candidate = dataclasses.replace(
        dataset,
        service_map=candidate_map,
        service_version_id="pending",
    )

    # 3) 整批重算
    report = pipeline.build_report(candidate, settings.DEFAULT_OFFSET_THRESHOLD_S)

    # 4) 整批一致性核对（不通过直接抛错，store 尚未写入任何内容）
    _verify_batch_consistency(candidate, report)

    # 5) 原子切换：版本 + active + 报告快照同一事务（快照内容里的版本号在事务后回填）
    version_id = store.replace_service_version(
        dataset.dataset_id,
        records,
        report,
        note,
    )

    # 6) 把线上数据集切到新在役口径
    dataset.service_map = candidate_map
    dataset.service_version_id = version_id
    report["service_version_id"] = version_id

    return {
        "changed": True,
        "version_id": version_id,
        "reason": "启用记录已更正，受影响设备已按实际在役时段重算并原子切换",
        "report": report,
    }


def _same_as_active(active_records: list[dict], candidate_map, dataset) -> bool:
    active_map = service.build_service_map(
        active_records, dataset.devices(), dataset.window_start, dataset.window_end
    )
    for device in dataset.devices():
        if active_map[device].intervals != candidate_map[device].intervals:
            return False
    return True


def _build_and_snapshot(dataset, service_map, version_id: str) -> dict:
    candidate = dataclasses.replace(dataset, service_map=service_map, service_version_id=version_id)
    report = pipeline.build_report(candidate, settings.DEFAULT_OFFSET_THRESHOLD_S)
    store.save_report_snapshot(dataset.dataset_id, version_id, report["threshold_s"], report)
    return report


def _verify_batch_consistency(dataset, report: dict) -> None:
    """整批核对：页面 / 导出 / JSON 共用同一份 coverage 数字，且在役口径自洽。"""
    expected_pairs = len(dataset.series)
    if len(report["rows"]) != expected_pairs:
        raise ImportError_(f"整批核对失败：序列数 {len(report['rows'])} != 期望 {expected_pairs}")
    seen: set[tuple[str, str]] = set()
    for row in report["rows"]:
        key = (row["device_id"], row["metric"])
        if key in seen:
            raise ImportError_(f"整批核对失败：{key} 重复出现在报告中")
        seen.add(key)
        cov = row["coverage"]
        window = dataset.service_window_for(row["device_id"])
        in_service_buckets = len(window.in_service_buckets)
        if cov["expected_cells"] != in_service_buckets:
            raise ImportError_(
                f"整批核对失败：{key} 应到格 {cov['expected_cells']} 与在役桶数 {in_service_buckets} 不一致"
            )
        if cov["in_service_cells"] != in_service_buckets:
            raise ImportError_(f"整批核对失败：{key} 网格在役格数与启用记录不一致")
        # 有效 + 缺失必须恰好覆盖全部在役桶
        if cov["valid_cells"] + cov["missing_cells"] != in_service_buckets:
            raise ImportError_(f"整批核对失败：{key} 有效/缺失格之和不等于在役桶数")
        rebuilt = cov["valid_cells"] / max(1, cov["expected_cells"])
        if abs(rebuilt - cov["coverage"]) > 1e-12:
            raise ImportError_(f"整批核对失败：{key} 覆盖率无法由有效/应到复算得到")
        # 原始 + 沿用 + 插补 = 有效
        if cov["raw_cells"] + cov["carried_cells"] + cov["interpolated_cells"] != cov["valid_cells"]:
            raise ImportError_(f"整批核对失败：{key} 有效格构成对不上")
        # 区间外点只能出现在 excluded 计数里，绝不能进入有效格
        series = dataset.series_for(row["device_id"], row["metric"])
        excluded = sum(
            1 for ts, _ in series.points if window.classify_point(ts) is not None
        )
        if cov["excluded_points"] != excluded:
            raise ImportError_(f"整批核对失败：{key} 区间外点计数 {cov['excluded_points']} != {excluded}")
        if cov["late_points"] > cov["excluded_points"]:
            raise ImportError_(f"整批核对失败：{key} 迟到点数不可能超过区间外点总数")
    if len(seen) != expected_pairs:
        raise ImportError_("整批核对失败：报告未覆盖全部「设备 × 指标」")


def bootstrap_service_map(dataset):
    """启动 / 导入时：优先用 DB 中 active 版本，否则用样例 CSV 种子并落首个版本。"""
    active = store.get_active_service_version(dataset.dataset_id)
    if active is not None:
        service_map = service.build_service_map(
            active["records"], dataset.devices(), dataset.window_start, dataset.window_end
        )
        dataset.service_map = service_map
        dataset.service_version_id = active["version_id"]
        return dataset, active["version_id"], False

    records = service.load_service_records_csv(settings.SERVICE_RECORDS_PATH)
    service_map = service.build_service_map(
        records, dataset.devices(), dataset.window_start, dataset.window_end
    )
    dataset.service_map = service_map
    # 首版直接落库并置 active（种子是可信本地文件，不经过人工更正核对流程）
    with store.transaction() as conn:
        version_id = store.save_service_version(
            conn, dataset.dataset_id, records, note="样例启用记录种子", activate=True
        )
    dataset.service_version_id = version_id
    candidate = dataclasses.replace(dataset, service_map=service_map, service_version_id=version_id)
    report = pipeline.build_report(candidate, settings.DEFAULT_OFFSET_THRESHOLD_S)
    store.save_report_snapshot(dataset.dataset_id, version_id, report["threshold_s"], report)
    return dataset, version_id, True
