"""API 端到端：导入、对齐、结果页、错误输入 4xx、质量报告与 CSV 导出。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        # 主流程第一步：幂等导入样例遥测
        resp = test_client.post("/api/import")
        assert resp.status_code == 200, resp.text
        yield test_client


def align(client: TestClient, **payload):
    return client.post("/api/align", json=payload)


def test_health(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["app"] == "tsgrid"


def test_import_is_idempotent(client: TestClient):
    first = client.post("/api/import").json()
    second = client.post("/api/import").json()
    assert first["dataset_id"] == second["dataset_id"]
    assert first["sha256"] == second["sha256"]
    assert first["rows"] > 20000
    assert len(first["devices"]) == 3
    assert {d["device_id"] for d in first["devices"]} == {"LINE-A-01", "LINE-A-02", "LINE-A-03"}
    assert first["window"]["cells"] == 360


def test_drifted_device_offset_is_about_37_seconds(client: TestClient):
    resp = align(client, device_id="LINE-A-03", metric="temperature", offset_threshold=5)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["drift"]["available"] is True
    assert abs(data["drift"]["offset_s"] - 37.0) < 1.0
    assert data["drift"]["drifted"] is True
    assert data["drift"]["rmse_s"] < 1.0
    assert data["reference"]["device_id"] == "LINE-A-01"
    # 温度序列：3 格插补缺口 + 5 格未修复缺口 = 2 处
    assert data["gaps"]["count"] == 2
    assert data["gaps"]["interpolated_cells"] == 3
    assert data["gaps"]["unrepaired_cells"] == 5
    assert data["coverage"]["grade"] == "A"
    assert data["coverage"]["expected_cells"] == 360
    assert data["interval_s"] == 60
    assert data["run_id"].startswith("run-")


def test_low_rate_grades_and_carried_counts(client: TestClient):
    temperature = align(client, device_id="LINE-A-02", metric="temperature").json()
    assert temperature["drift"]["drifted"] is False          # 5s 设备时钟正常
    assert temperature["coverage"]["grade"] == "B"
    assert temperature["coverage"]["carried_cells"] == 30    # 3 分钟降级段被 hold
    assert temperature["gaps"]["count"] == 3

    pressure = align(client, device_id="LINE-A-03", metric="pressure").json()
    assert pressure["coverage"]["grade"] == "C"
    assert abs(pressure["drift"]["offset_s"] - 37.0) < 1.0
    assert pressure["gaps"]["unrepaired_cells"] == 30


def test_reference_device_reports_no_self_comparison(client: TestClient):
    data = align(client, device_id="LINE-A-01", metric="temperature").json()
    assert data["reference"]["self_reference"] is True
    assert data["drift"]["available"] is False
    assert data["drift"]["offset_s"] is None


def test_threshold_change_flips_verdict(client: TestClient):
    strict = align(client, device_id="LINE-A-02", metric="temperature", offset_threshold=0.005).json()
    assert strict["drift"]["drifted"] is True


def test_unknown_device_returns_4xx_with_reason(client: TestClient):
    resp = align(client, device_id="LINE-A-99", metric="temperature")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "LINE-A-99" in detail
    assert "可用" in detail


def test_metric_not_on_device_returns_4xx(client: TestClient):
    resp = align(client, device_id="LINE-A-01", metric="pressure")
    assert resp.status_code == 400
    assert "pressure" in resp.json()["detail"]


def test_bad_threshold_returns_4xx(client: TestClient):
    resp = align(client, device_id="LINE-A-03", metric="temperature", offset_threshold="abc")
    assert resp.status_code == 400
    assert "阈值" in resp.json()["detail"]

    resp_negative = align(client, device_id="LINE-A-03", metric="temperature", offset_threshold=-5)
    assert resp_negative.status_code == 400


def test_missing_fields_return_4xx(client: TestClient):
    assert client.post("/api/align", json={"metric": "temperature"}).status_code == 400
    assert client.post("/api/align", json={"device_id": "LINE-A-03"}).status_code == 400


def test_result_page_renders_evidence(client: TestClient):
    data = align(client, device_id="LINE-A-03", metric="temperature").json()
    resp = client.get(f"/result/{data['run_id']}")
    assert resp.status_code == 200
    html = resp.text
    for expected in ["时钟漂移估计", "校正建议", "1 分钟网格预览", "缺口明细", "小时摘要", "覆盖率与分级", "37."]:
        assert expected in html


def test_unknown_run_returns_404(client: TestClient):
    assert client.get("/result/run-doesnotexist").status_code == 404
    assert client.get("/api/runs/run-doesnotexist").status_code == 404


def test_pages_render(client: TestClient):
    for path in ["/", "/workbench", "/review"]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "遥测" in resp.text or "对齐" in resp.text


def test_report_and_csv_export(client: TestClient):
    report = client.get("/api/report").json()
    assert len(report["rows"]) == 5
    grades = {row["coverage"]["grade"] for row in report["rows"]}
    assert grades == {"A", "B", "C"}
    assert len(report["devices"]) == 3
    assert report["summary"]["total_gaps"] >= 5

    csv_resp = client.get("/api/report.csv")
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    lines = [line for line in csv_resp.text.strip().splitlines() if line]
    assert lines[0].startswith("device_id,metric,interval_s")
    assert len(lines) == 6  # 表头 + 5 条序列


def test_alignment_pipeline_stages_recorded(client: TestClient):
    data = align(client, device_id="LINE-A-03", metric="temperature").json()
    names = [stage["name"] for stage in data["stages"]]
    assert names == ["数据导入", "漂移估计", "网格重采样", "缺口判定", "小时摘要", "质量评分"]
    assert data["total_ms"] > 0
    assert len(data["hourly"]) == 6
    assert len(data["preview"]) == 60
