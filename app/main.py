"""FastAPI 路由（薄）：只做参数解析、调用 pipeline、渲染页面。"""
from __future__ import annotations

import pathlib

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import pipeline, quality, seed, settings, store

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
        ),
    )


def _report(dataset, threshold: float) -> dict:
    key = (dataset.sha256, threshold)
    cached = STATE["report_cache"].get(key)
    if cached is None:
        cached = pipeline.build_report(dataset, threshold)
        STATE["report_cache"][key] = cached
    return cached


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
        dataset = seed.ensure_seed()["dataset"]
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
        "window": {"cells": dataset.bucket_count()},
    }


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
        "threshold_s": report["threshold_s"],
        "summary": report["summary"],
        "devices": report["devices"],
        "rows": [
            {
                "device_id": row["device_id"],
                "metric": row["metric"],
                "interval_s": row["interval_s"],
                "coverage": row["coverage"],
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
