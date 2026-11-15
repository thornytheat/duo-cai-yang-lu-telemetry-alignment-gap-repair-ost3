"""启用记录更正 API：查询、幂等重跑、原子切换、失败回滚、页面/导出口径一致。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import service, service_admin, settings, store
from app.main import app

TZ = timezone(timedelta(hours=8))


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=TZ).isoformat(timespec="seconds")


def _seed_records() -> list[dict]:
    records = service.load_service_records_csv(settings.SERVICE_RECORDS_PATH)
    return [
        {"device_id": r["device_id"], "start_ts": _iso(r["start_ts"]), "end_ts": _iso(r["end_ts"])}
        for r in records
    ]


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        resp = test_client.post("/api/import")
        assert resp.status_code == 200, resp.text
        yield test_client


@pytest.fixture(autouse=True)
def restore_seed_records(client: TestClient):
    """每个用例后恢复种子启用记录，避免污染其它测试模块的共享数据集。"""
    yield
    client.post("/api/service-records", json={"records": _seed_records(), "note": "测试后恢复种子"})


def _snapshot_count(dataset_id: str) -> int:
    with store.transaction() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM report_snapshots WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()
    return row["n"]


def test_service_records_listed_with_merged_intervals(client: TestClient):
    data = client.get("/api/service-records").json()
    a04 = next(d for d in data["devices"] if d["device_id"] == "LINE-A-04")
    # 两条重叠记录 [02:00,03:30) + [03:30,05:30) 合并为单个在役区间
    assert len(a04["intervals"]) == 1
    assert "T02:00:00" in a04["intervals"][0]["start"]
    assert "T05:30:00" in a04["intervals"][0]["end"]
    assert a04["in_service_cells"] == 210  # 02:00–05:30 共 210 分钟
    assert data["active_version_id"].startswith("svc-")


def test_line_a04_alignment_excludes_blank_and_keeps_late_points(client: TestClient):
    data = client.post(
        "/api/align", json={"device_id": "LINE-A-04", "metric": "temperature"}
    ).json()
    # 应到分钟数按在役时段计算，而不是整窗 360
    assert data["coverage"]["expected_cells"] == 210
    assert data["coverage"]["in_service_cells"] == 210
    assert data["coverage"]["excluded_cells"] == 150
    # 4 个停用后迟到点全部保留并标明不参与
    assert data["coverage"]["late_points"] == 4
    assert data["service"]["late_points"] == 4
    assert {p["reason"] for p in data["excluded_preview"]} == {"late"}
    # 摘要只覆盖 02–05 四个含在役桶的小时
    assert [h["hour_index"] for h in data["hourly"]] == [494034, 494035, 494036, 494037]


def test_rerun_same_batch_creates_no_new_version_or_report(client: TestClient):
    dataset = client.get("/api/service-records").json()
    version_before = dataset["active_version_id"]
    snap_before = _snapshot_count(dataset["dataset_id"])

    resp = client.post("/api/service-records", json={"records": _seed_records()})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is False
    assert body["version_id"] == version_before

    # 重跑质量报告也不新增快照
    client.get("/api/report")
    client.get("/api/report")
    assert _snapshot_count(dataset["dataset_id"]) == snap_before


def test_correction_recomputes_and_atomically_switches(client: TestClient):
    before = client.get("/api/service-records").json()
    # 更正：LINE-A-04 改为整窗在役（模拟启用记录补录纠正）
    records = [
        r for r in _seed_records() if r["device_id"] != "LINE-A-04"
    ] + [
        {
            "device_id": "LINE-A-04",
            "start_ts": "2026-05-12T00:00:00+08:00",
            "end_ts": "2026-05-12T06:00:00+08:00",
        }
    ]
    resp = client.post("/api/service-records", json={"records": records, "note": "补录全周期在役"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is True
    assert body["previous_version_id"] == before["active_version_id"]
    assert body["version_id"] != before["active_version_id"]

    after = client.get("/api/service-records").json()
    assert after["active_version_id"] == body["version_id"]
    a04 = next(d for d in after["devices"] if d["device_id"] == "LINE-A-04")
    assert a04["in_service_cells"] == 360

    align = client.post(
        "/api/align", json={"device_id": "LINE-A-04", "metric": "temperature"}
    ).json()
    assert align["coverage"]["expected_cells"] == 360
    assert "T00:00:00" in align["service"]["intervals"][0]["start"]
    # 旧版本仍保留在库里可追溯
    versions = {v["version_id"]: v["is_active"] for v in after["versions"]}
    assert versions[before["active_version_id"]] == 0
    assert versions[body["version_id"]] == 1


def test_invalid_records_are_rejected_and_old_version_survives(client: TestClient):
    before = client.get("/api/service-records").json()
    resp = client.post(
        "/api/service-records",
        json={"records": [{"device_id": "LINE-A-99", "start_ts": "2026-05-12T01:00:00+08:00", "end_ts": None}]},
    )
    assert resp.status_code == 422
    assert "旧版" in resp.json()["detail"]
    after = client.get("/api/service-records").json()
    assert after["active_version_id"] == before["active_version_id"]

    # 时间非法同样 4xx
    bad = client.post(
        "/api/service-records",
        json={"records": [{"device_id": "LINE-A-04", "start_ts": "not-a-time", "end_ts": None}]},
    )
    assert bad.status_code == 400
    # 请求体形状错误 / 缺字段也是 4xx，不触碰当前版本
    assert client.post("/api/service-records", json={}).status_code == 400
    assert client.post("/api/service-records", json={"records": "nope"}).status_code == 400
    assert client.post(
        "/api/service-records",
        json={"records": [{"device_id": "", "start_ts": "2026-05-12T01:00:00+08:00"}]},
    ).status_code == 400
    assert client.get("/api/service-records").json()["active_version_id"] == before["active_version_id"]


def test_empty_records_mean_full_window_for_every_device(client: TestClient):
    # 空记录是合法输入：所有设备回退为整窗在役（LINE-A-04 应到恢复 360）
    resp = client.post("/api/service-records", json={"records": [], "note": "清空特殊在役口径"})
    assert resp.status_code == 200, resp.text
    data = client.get("/api/service-records").json()
    a04 = next(d for d in data["devices"] if d["device_id"] == "LINE-A-04")
    assert a04["in_service_cells"] == 360
    align = client.post(
        "/api/align", json={"device_id": "LINE-A-04", "metric": "temperature"}
    ).json()
    # 整窗口径下原迟到点落在窗口内，不再属于区间外点
    assert align["coverage"]["expected_cells"] == 360
    assert align["coverage"]["late_points"] == 0
    assert align["coverage"]["excluded_points"] == 0


def test_batch_verification_failure_rolls_back(client: TestClient, monkeypatch):
    before = client.get("/api/service-records").json()
    real_build = service_admin.pipeline.build_report

    def flaky_build(dataset, threshold):  # 模拟整批重算中途失败
        raise RuntimeError("boom")

    monkeypatch.setattr(service_admin.pipeline, "build_report", flaky_build)
    resp = client.post(
        "/api/service-records",
        json={
            "records": [
                {
                    "device_id": "LINE-A-04",
                    "start_ts": "2026-05-12T01:00:00+08:00",
                    "end_ts": "2026-05-12T05:00:00+08:00",
                }
            ]
        },
    )
    monkeypatch.setattr(service_admin.pipeline, "build_report", real_build)
    assert resp.status_code == 422
    # 事务回滚：active 仍是旧版，没有残留新版本
    after = client.get("/api/service-records").json()
    assert after["active_version_id"] == before["active_version_id"]
    assert len(after["versions"]) == len(before["versions"])


def test_page_json_and_csv_share_service_scope_and_coverage(client: TestClient):
    report = client.get("/api/report").json()
    csv_text = client.get("/api/report.csv").text
    review_html = client.get("/review").text

    row = next(r for r in report["rows"] if r["device_id"] == "LINE-A-04")
    expected = row["coverage"]["expected_cells"]
    coverage_text = f"{row['coverage']['coverage']:.4f}"
    assert expected == 210
    # CSV 同一行带出相同在役范围与覆盖率
    csv_line = next(line for line in csv_text.splitlines() if line.startswith("LINE-A-04,"))
    csv_fields = csv_line.split(",")
    assert int(csv_fields[3]) == 210          # in_service_cells
    assert int(csv_fields[4]) == 210          # expected_cells
    assert csv_fields[12] == coverage_text    # coverage
    assert "T02:00:00+08:00~" in csv_fields[15]      # service_ranges
    # 复核页展示同一在役口径
    assert "02:00:00" in review_html and "05:30:00" in review_html
    assert f"{row['coverage']['coverage'] * 100:.2f}%" in review_html
