"""FastAPI 路由（薄）：只做参数解析、调用 pipeline、渲染页面。"""
from __future__ import annotations

import pathlib

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import pipeline, quality, seed, service_admin, settings, store
from .telemetry import ImportError_

BASE = pathlib.Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
app = FastAPI(title=settings.APP_NAME)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

STATE: dict = {"dataset": None, "report_cache": {}}

# 工作台默认演示设备：样例中唯一判定时钟漂移的设备（+37s），便于一眼看到偏移结论；
# 只是模板默认选中项，可随时改选其它设备/指标，不影响任何业务规则。
DEFAULT_WORKBENCH_DEVICE = "LINE-A-03"


def get_dataset():
    if STATE["dataset"] is None:
        STATE["dataset"] = seed.ensure_seed()["dataset"]
    return STATE["dataset"]


def fmt(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return str(value)


def pct(value, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.{digits}f}%"


templates.env.filters["fmt"] = fmt
templates.env.filters["pct"] = pct
templates.env.globals["app_name"] = settings.APP_NAME
templates.env.globals["metric_labels"] = settings.METRIC_LABEL
templates.env.globals["metric_units"] = settings.METRIC_UNIT


def _ctx(request: Request, **kwargs) -> dict:
    base = {"request": request, "app_name": settings.APP_NAME, "active": "", "page": ""}
    base.update(kwargs)
    return base


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "app": settings.SHORT_NAME, "service": "tsgrid-app"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    dataset = get_dataset()
    inventory = dataset.inventory()
    runs = store.list_runs(limit=5)
    datasets = store.list_datasets()
    # 首页统计（只做汇总，不改变任何业务判定）：复用质量报告缓存，逐条流水线结果保持一致。
    report = _report(dataset, settings.DEFAULT_OFFSET_THRESHOLD_S)
    summary = report["summary"]
    stats = {
        "device_count": len(inventory),
        "metric_count": len({m["metric"] for dev in inventory for m in dev["metrics"]}),
        "pair_count": summary["pairs"],
        "avg_coverage": summary["avg_coverage"],
        "grade_counts": summary["grade_counts"],
        "total_gaps": summary["total_gaps"],
        "drifted_pairs": summary["drifted_pairs"],
        "run_count": len(store.list_runs(limit=200)),
    }
    last_run = store.get_run(runs[0]["run_id"]) if runs else None
    return templates.TemplateResponse(
        request,
        "index.html",
        _ctx(
            request,
            page="index",
            active="home",
            inventory=inventory,
            dataset=dataset,
            datasets=datasets,
            runs=runs,
            last_run=last_run,
            stats=stats,
            bucket_count=dataset.bucket_count(),
            metrics=settings.METRICS,
            threshold=settings.DEFAULT_OFFSET_THRESHOLD_S,
            seeded=bool(datasets),
        ),
    )


@app.get("/workbench", response_class=HTMLResponse)
def workbench(request: Request):
    dataset = get_dataset()
    inventory = dataset.inventory()
    devices = dataset.devices()
    threshold = settings.DEFAULT_OFFSET_THRESHOLD_S

    # 预演目标：?device=&metric= 显式指定，否则默认演示设备；不写库、不影响接口。
    device_id = request.query_params.get("device") or ""
    if device_id not in devices:
        if DEFAULT_WORKBENCH_DEVICE in devices:
            device_id = DEFAULT_WORKBENCH_DEVICE
        else:
            device_id = devices[0] if devices else ""
    selected_metrics = dataset.metrics_for(device_id) if device_id else []
    metric = request.query_params.get("metric") or ""
    if metric not in selected_metrics:
        metric = selected_metrics[0] if selected_metrics else ""

    # 右侧预演：复用同一条 6 步流水线（pipeline.run_alignment），只展示不落库。
    preview = pipeline.run_alignment(dataset, device_id, metric, threshold) if metric else None

    return templates.TemplateResponse(
        request,
        "workbench.html",
        _ctx(
            request,
            page="workbench",
            active="workbench",
            inventory=inventory,
            dataset=dataset,
            metrics=sorted({m for (_, m) in dataset.series}),
            metric_labels=settings.METRIC_LABEL,
            threshold=threshold,
            bucket_count=dataset.bucket_count(),
            runs=store.list_runs(limit=8),
            preview=preview,
            selected_device=device_id,
            selected_metric=metric,
            selected_metrics=selected_metrics,
        ),
    )


@app.get("/result/{run_id}", response_class=HTMLResponse)
def result(request: Request, run_id: str):
    payload = store.get_run(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"运行 {run_id} 不存在，请回到工作台重新执行对齐")
    return templates.TemplateResponse(
        request,
        "result.html",
        _ctx(
            request,
            page="result",
            active="workbench",
            run=payload,
            metric_label=payload.get("metric_label"),
            metric_unit=payload.get("metric_unit", ""),
        ),
    )


@app.get("/review", response_class=HTMLResponse)
def review(request: Request):
    dataset = get_dataset()
    report = _report(dataset, settings.DEFAULT_OFFSET_THRESHOLD_S)
    report_metrics = []
    seen = set()
    for row in report["rows"]:
        if row["metric"] not in seen:
            seen.add(row["metric"])
            report_metrics.append({"metric": row["metric"], "label": row["metric_label"]})
    service_windows = []
    for device in dataset.devices():
        window = dataset.service_window_for(device)
        service_windows.append(
            {
                "device_id": device,
                "intervals": window.intervals_iso() if window else [],
                "in_service_cells": dataset.expected_buckets_for(device),
                "grid_cells": dataset.bucket_count(),
            }
        )
    return templates.TemplateResponse(
        request,
        "review.html",
        _ctx(
            request,
            page="review",
            active="review",
            dataset=dataset,
            report=report,
            report_metrics=report_metrics,
            service_windows=service_windows,
            service_version_id=dataset.service_version_id,
        ),
    )


def _report(dataset, threshold: float) -> dict:
    """整批质量报告：同 (数据集, 启用版本, 阈值) 重跑不新增报告。

    优先内存缓存 → DB 快照（重跑同批直接命中）→ 现算并落快照。
    页面 / JSON / CSV 导出都从这一份 report 取数，在役范围与覆盖率口径天然一致。
    """
    key = (dataset.sha256, dataset.service_version_id, threshold)
    cached = STATE["report_cache"].get(key)
    if cached is not None:
        return cached
    snapshotted = store.get_report_snapshot(dataset.dataset_id, dataset.service_version_id, threshold)
    if snapshotted is not None:
        STATE["report_cache"][key] = snapshotted
        return snapshotted
    report = pipeline.build_report(dataset, threshold)
    store.save_report_snapshot(dataset.dataset_id, dataset.service_version_id, threshold, report)
    STATE["report_cache"][key] = report
    return report


def _parse_threshold(raw) -> float:
    if raw is None or raw == "":
        return settings.DEFAULT_OFFSET_THRESHOLD_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"偏移阈值必须是数字，实际收到 {raw!r}") from None
    if value <= 0 or value > 3600:
        raise HTTPException(status_code=400, detail="偏移阈值需在 0–3600 秒之间")
    return value


@app.post("/api/import")
async def api_import(request: Request):
    """重新载入样例遥测（按内容 sha256 幂等，不产生重复数据集）。"""
    try:
        seeded = seed.ensure_seed()
        dataset = seeded["dataset"]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"导入失败：{exc}") from exc
    STATE["dataset"] = dataset
    STATE["report_cache"].clear()
    return {
        "ok": True,
        "dataset_id": dataset.dataset_id,
        "sha256": dataset.sha256,
        "rows": dataset.rows,
        "files": dataset.files,
        "devices": dataset.inventory(),
        "service_version_id": dataset.service_version_id,
        "window": {"cells": dataset.bucket_count()},
    }


@app.get("/api/service-records")
def api_service_records():
    """返回当前生效的启用记录版本与每台设备合并后的在役区间。"""
    dataset = get_dataset()
    active = store.get_active_service_version(dataset.dataset_id)
    windows = []
    for device in dataset.devices():
        window = dataset.service_window_for(device)
        windows.append(
            {
                "device_id": device,
                "intervals": window.intervals_iso() if window else [],
                "in_service_cells": dataset.expected_buckets_for(device),
            }
        )
    return {
        "dataset_id": dataset.dataset_id,
        "active_version_id": dataset.service_version_id,
        "active_version": active,
        "versions": store.list_service_versions(dataset.dataset_id),
        "devices": windows,
    }


@app.post("/api/service-records")
async def api_replace_service_records(request: Request):
    """更正启用记录：合并重叠区间 → 整批重算 → 一致性核对 → 原子切换，失败恢复旧版。

    请求体：{"records": [{"device_id": ..., "start_ts": ISO8601, "end_ts": ISO8601 或 null}],
             "note"?: 备注}。重跑同批（合并后与 active 一致）不新增版本/报告。
    """
    dataset = get_dataset()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from None
    if not isinstance(body, dict) or not isinstance(body.get("records"), list):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象且含 records 数组")
    raw_records = body["records"]
    parsed: list[dict] = []
    for idx, rec in enumerate(raw_records):
        if not isinstance(rec, dict):
            raise HTTPException(status_code=400, detail=f"第 {idx + 1} 条记录不是对象")
        device = (rec.get("device_id") or "").strip()
        if not device:
            raise HTTPException(status_code=400, detail=f"第 {idx + 1} 条记录缺少 device_id")
        start_raw = (rec.get("start_ts") or "").strip()
        if not start_raw:
            raise HTTPException(status_code=400, detail=f"第 {idx + 1} 条记录缺少 start_ts")
        try:
            start = _parse_iso(start_raw)
        except ImportError_ as exc:
            raise HTTPException(status_code=400, detail=f"第 {idx + 1} 条 start_ts 非法：{exc}") from None
        end_raw = rec.get("end_ts")
        end = None
        if end_raw not in (None, ""):
            try:
                end = _parse_iso(str(end_raw).strip())
            except ImportError_ as exc:
                raise HTTPException(status_code=400, detail=f"第 {idx + 1} 条 end_ts 非法：{exc}") from None
            if end <= start:
                raise HTTPException(status_code=400, detail=f"第 {idx + 1} 条记录 end_ts 必须晚于 start_ts")
        parsed.append({"device_id": device, "start_ts": start, "end_ts": end})

    note = str(body.get("note") or "人工更正启用记录")[:200]
    before_version = dataset.service_version_id
    try:
        result = service_admin.apply_service_records(dataset, parsed, note=note)
    except ImportError_ as exc:
        # 校验 / 整批核对失败：事务已回滚，线上数据集与旧版本保持不变
        raise HTTPException(status_code=422, detail=f"启用记录更正失败，已保留旧版：{exc}") from None
    except Exception as exc:  # noqa: BLE001
        # 重算阶段任何异常都不得留下半成品：未进入事务即无写入，旧版保持生效
        raise HTTPException(status_code=422, detail=f"启用记录更正失败，已保留旧版：{exc}") from None
    STATE["report_cache"].clear()
    report = result["report"]
    return {
        "ok": True,
        "changed": result["changed"],
        "version_id": result["version_id"],
        "previous_version_id": before_version,
        "reason": result["reason"],
        "summary": report["summary"],
        "devices": report["devices"],
    }


def _parse_iso(raw: str) -> float:
    from .telemetry import parse_ts

    return parse_ts(raw)


@app.post("/api/align")
async def api_align(request: Request):
    dataset = get_dataset()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

    device_id = (body.get("device_id") or "").strip()
    metric = (body.get("metric") or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="缺少 device_id")
    if not metric:
        raise HTTPException(status_code=400, detail="缺少 metric")
    if device_id not in dataset.devices():
        raise HTTPException(
            status_code=400,
            detail=f"设备 {device_id} 不在当前数据集（可用：{'、'.join(dataset.devices())}）",
        )
    if metric not in dataset.metrics_for(device_id):
        raise HTTPException(
            status_code=400,
            detail=f"设备 {device_id} 没有指标 {metric}（可用：{'、'.join(dataset.metrics_for(device_id))}）",
        )
    threshold = _parse_threshold(body.get("offset_threshold"))

    try:
        payload = pipeline.run_alignment(dataset, device_id, metric, threshold)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"对齐失败：{exc}") from exc

    run_id = store.save_run(dataset.dataset_id, payload)
    payload_out = dict(payload)
    payload_out["run_id"] = run_id
    return payload_out


@app.get("/api/runs/{run_id}")
def api_run(run_id: str):
    payload = store.get_run(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"运行 {run_id} 不存在")
    return payload


@app.get("/api/report")
def api_report(threshold: float | None = None):
    dataset = get_dataset()
    report = _report(dataset, _parse_threshold(threshold))
    return {
        "dataset_id": report["dataset_id"],
        "service_version_id": report.get("service_version_id"),
        "threshold_s": report["threshold_s"],
        "summary": report["summary"],
        "devices": report["devices"],
        "rows": [
            {
                "device_id": row["device_id"],
                "metric": row["metric"],
                "interval_s": row["interval_s"],
                "coverage": row["coverage"],
                "service": row.get("service"),
                "drift": {
                    "available": row["drift"].get("available"),
                    "offset_s": row["drift"].get("offset_s"),
                    "rate_ppm": row["drift"].get("rate_ppm"),
                    "rmse_s": row["drift"].get("rmse_s"),
                    "drifted": row["drift"].get("drifted"),
                },
                "gap_count": row["gaps"]["count"],
            }
            for row in report["rows"]
        ],
    }


@app.get("/api/report.csv", response_class=PlainTextResponse)
def api_report_csv(threshold: float | None = None):
    dataset = get_dataset()
    report = _report(dataset, _parse_threshold(threshold))
    return PlainTextResponse(
        report["csv"],
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="tsgrid-quality-{dataset.dataset_id}.csv"'},
    )


@app.get("/api/datasets")
def api_datasets():
    return {"datasets": store.list_datasets()}
