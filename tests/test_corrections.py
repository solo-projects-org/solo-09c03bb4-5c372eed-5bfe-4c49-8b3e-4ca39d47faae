"""pytest：线路更正取数功能（解析 / 存储 / 两种分区布局查询 / 整份退回 / API）。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.archive import (
    LAYOUT_DAY_ONLY,
    LAYOUT_ROUTE,
    ParquetArchive,
)
from app.correction_service import validate_corrections
from app.main import create_app
from app.parser import parse_corrections, parse_day
from app.store import CorrectionStore

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_ROOT = ROOT / "data" / "sample_archive"
DAY = date(2026, 11, 20)
DAY2 = date(2026, 11, 21)

VALID_CSV = (
    "批次号,流水号,原线路,正确线路\n"
    "B1,T1003,R2,R1\n"
    "B2,T2002,R2,R1\n"
    "B1,T1005,R3,R2\n"
).encode("utf-8")


@pytest.fixture()
def tmp_paths(tmp_path):
    return tmp_path / "corrections", tmp_path / "app-state"


# ---------- 解析 ----------

def test_parse_corrections_basic_and_english_headers():
    rows, warnings = parse_corrections(
        b"batch_id,seq,original_route,corrected_route\nB1,T1,R1,R2\n"
    )
    assert len(rows) == 1
    assert rows[0].batch_id == "B1" and rows[0].seq == "T1"
    assert rows[0].original_route == "R1" and rows[0].corrected_route == "R2"
    assert warnings == []


def test_parse_corrections_duplicate_batch_seq_rejected():
    raw = (
        "批次号,流水号,原线路,正确线路\n"
        "B1,T1003,R2,R1\n"
        "B1,T1003,R2,R3\n"
    ).encode()
    with pytest.raises(ValueError, match="同批同号"):
        parse_corrections(raw)


def test_parse_corrections_missing_column_and_blank_field():
    with pytest.raises(ValueError, match="缺少必需列"):
        parse_corrections("批次号,流水号,原线路\nB1,T1,R1\n".encode())
    with pytest.raises(ValueError, match="正确线路为空"):
        parse_corrections(
            "批次号,流水号,原线路,正确线路\nB1,T1,R1,\n".encode()
        )


def test_parse_corrections_noop_warning():
    rows, warnings = parse_corrections(
        "批次号,流水号,原线路,正确线路\nB1,T1,R1,R1\n".encode()
    )
    assert len(rows) == 1 and "原线路与正确线路相同" in warnings[0]


# ---------- 对账校验 ----------

def test_validate_ok_against_archive():
    archive = ParquetArchive(SAMPLE_ROOT / LAYOUT_DAY_ONLY, LAYOUT_DAY_ONLY)
    rows, warnings = parse_corrections(VALID_CSV)
    report = validate_corrections(rows, archive, DAY, parse_warnings=warnings)
    assert report.ok is True
    assert report.errors == []
    assert report.matched_rows == 3


def test_validate_missing_transaction_and_wrong_route():
    archive = ParquetArchive(SAMPLE_ROOT / LAYOUT_DAY_ONLY, LAYOUT_DAY_ONLY)
    bad = (
        "批次号,流水号,原线路,正确线路\n"
        "B1,T9999,R1,R2\n"      # 找不到流水
        "B1,T1001,R9,R2\n"      # 原线路对不上（T1001 原档是 R1）
    ).encode()
    rows, _ = parse_corrections(bad)
    report = validate_corrections(rows, archive, DAY)
    assert report.ok is False
    assert len(report.errors) == 2
    assert any("找不到该流水" in e for e in report.errors)
    assert any("原线路对不上" in e for e in report.errors)


def test_validate_unknown_day_raises():
    archive = ParquetArchive(SAMPLE_ROOT / LAYOUT_DAY_ONLY, LAYOUT_DAY_ONLY)
    rows, _ = parse_corrections(VALID_CSV)
    with pytest.raises(Exception, match="找不到运营日"):
        validate_corrections(rows, archive, date(2020, 1, 1))


# ---------- 存储：原子换表 / 重传不叠加 / 重启加载 ----------

def test_store_replace_is_full_swap_and_persists(tmp_paths):
    data_dir, _ = tmp_paths
    store = CorrectionStore(data_dir)
    rows1, _ = parse_corrections(
        "批次号,流水号,原线路,正确线路\nB1,T1003,R2,R1\n".encode()
    )
    table1 = store.replace_day(DAY, rows1, source_file="v1.csv", applied_at="t1")
    assert table1.count == 1 and store.snapshot(DAY).count == 1

    # 重传只带新条目：整份替换，不叠加
    rows2, _ = parse_corrections(
        "批次号,流水号,原线路,正确线路\nB2,T2002,R2,R1\n".encode()
    )
    store.replace_day(DAY, rows2, source_file="v2.csv", applied_at="t2")
    snap = store.snapshot(DAY)
    assert snap.count == 1
    assert snap.entries[0].seq == "T2002"
    assert snap.source_file == "v2.csv"

    # 磁盘只有一份当日 JSON，且重新实例化（模拟重启）后内容一致
    files = list(Path(data_dir).glob("*.json"))
    assert len(files) == 1
    reloaded = CorrectionStore(data_dir)
    assert reloaded.snapshot(DAY).count == 1
    assert reloaded.snapshot(DAY).entries[0].seq == "T2002"


def test_store_snapshot_is_stable_across_replace(tmp_paths):
    data_dir, _ = tmp_paths
    store = CorrectionStore(data_dir)
    rows1, _ = parse_corrections(
        "批次号,流水号,原线路,正确线路\nB1,T1003,R2,R1\n".encode()
    )
    store.replace_day(DAY, rows1, source_file="v1.csv", applied_at="t1")
    snapshot = store.snapshot(DAY)
    rows2, _ = parse_corrections(
        "批次号,流水号,原线路,正确线路\nB2,T2002,R2,R1\n".encode()
    )
    store.replace_day(DAY, rows2, source_file="v2.csv", applied_at="t2")
    # 查询期间持有的旧快照不受换表影响
    assert snapshot.count == 1 and snapshot.entries[0].seq == "T1003"
    assert store.snapshot(DAY).entries[0].seq == "T2002"


# ---------- 两种布局的取数与文件裁剪 ----------

def _store_with_valid_correction(data_dir):
    store = CorrectionStore(data_dir)
    rows, _ = parse_corrections(VALID_CSV)
    store.replace_day(DAY, rows, source_file="valid.csv", applied_at="t")
    return store


def test_day_only_query_opens_all_day_files_and_applies_correction(tmp_paths):
    data_dir, _ = tmp_paths
    store = _store_with_valid_correction(data_dir)
    archive = ParquetArchive(SAMPLE_ROOT / LAYOUT_DAY_ONLY, LAYOUT_DAY_ONLY)
    snap = store.snapshot(DAY)

    # 查 R1（更正后）：T1001、T1002、T2001 原生 R1 + T1003、T2002 改入
    result = archive.query_day_route(DAY, "R1", snap)
    seqs = {t.seq for t in result.transactions}
    assert seqs == {"T1001", "T1002", "T2001", "T1003", "T2002"}
    changed_in = {t.seq for t in result.transactions if t.changed}
    assert changed_in == {"T1003", "T2002"}
    # day_only：当日两个文件都打开，无裁剪
    assert len(result.files_opened) == 2
    assert result.files_skipped_by_pruning == ()
    # 每条流水带实读文件
    assert all(t.source_file.endswith(".parquet") for t in result.transactions)

    # 查 R2：T1003/T2002 已改出，不再出现；T1005 从 R3 改入
    r2 = archive.query_day_route(DAY, "R2", snap)
    r2_seqs = {t.seq for t in r2.transactions}
    assert "T1003" not in r2_seqs and "T2002" not in r2_seqs
    assert r2_seqs == {"T1004", "T2003", "T1005"}
    assert {t.seq for t in r2.transactions if t.changed} == {"T1005"}


def test_route_partition_prunes_to_queried_and_inbound_routes(tmp_paths):
    data_dir, _ = tmp_paths
    store = _store_with_valid_correction(data_dir)
    archive = ParquetArchive(SAMPLE_ROOT / LAYOUT_ROUTE, LAYOUT_ROUTE)
    snap = store.snapshot(DAY)

    # 查 R1：只开 route=R1 目录 + 有改入的原线路 R2 目录；R3 目录被裁剪
    r1 = archive.query_day_route(DAY, "R1", snap)
    opened_routes = {Path(f).parent.name for f in r1.files_opened}
    assert opened_routes == {"route=R1", "route=R2"}
    skipped_routes = {Path(f).parent.name for f in r1.files_skipped_by_pruning}
    assert skipped_routes == {"route=R3"}
    assert {t.seq for t in r1.transactions} == {
        "T1001", "T1002", "T2001", "T1003", "T2002"
    }

    # 查 R2：开 R2 + 改入来源 R3；R1 目录被裁剪
    r2 = archive.query_day_route(DAY, "R2", snap)
    opened_routes2 = {Path(f).parent.name for f in r2.files_opened}
    assert opened_routes2 == {"route=R2", "route=R3"}
    assert {Path(f).parent.name for f in r2.files_skipped_by_pruning} == {"route=R1"}
    assert {t.seq for t in r2.transactions} == {"T1004", "T2003", "T1005"}

    # 查 R3：无改入来源，只开 R3 目录；T1005 已改出，结果为空
    r3 = archive.query_day_route(DAY, "R3", snap)
    assert {Path(f).parent.name for f in r3.files_opened} == {"route=R3"}
    assert r3.count == 0


def test_unlisted_transactions_keep_original_route(tmp_paths):
    data_dir, _ = tmp_paths
    store = CorrectionStore(data_dir)  # 无任何更正
    archive = ParquetArchive(SAMPLE_ROOT / LAYOUT_ROUTE, LAYOUT_ROUTE)
    result = archive.query_day_route(DAY2, "R2", store.snapshot(DAY2))
    assert {t.seq for t in result.transactions} == {"T3002", "T3003"}
    assert all(not t.changed for t in result.transactions)


def test_same_snapshot_used_for_file_selection_and_filtering(tmp_paths):
    """换表后新查询用新快照；旧查询对象的结果不被后续重传改变。"""
    data_dir, _ = tmp_paths
    store = _store_with_valid_correction(data_dir)
    archive = ParquetArchive(SAMPLE_ROOT / LAYOUT_ROUTE, LAYOUT_ROUTE)
    snap_before = store.snapshot(DAY)
    result_before = archive.query_day_route(DAY, "R1", snap_before)

    # 换成只保留一条更正的新表（重传不叠加）
    rows, _ = parse_corrections(
        "批次号,流水号,原线路,正确线路\nB1,T1003,R2,R1\n".encode()
    )
    store.replace_day(DAY, rows, source_file="v2.csv", applied_at="t2")
    result_after = archive.query_day_route(DAY, "R1", store.snapshot(DAY))

    assert {t.seq for t in result_before.transactions} == {
        "T1001", "T1002", "T2001", "T1003", "T2002"
    }
    assert {t.seq for t in result_after.transactions} == {
        "T1001", "T1002", "T2001", "T1003"
    }


# ---------- API 全链路 ----------

@pytest.fixture()
def client(tmp_paths):
    data_dir, _ = tmp_paths
    app = create_app(
        data_dir=data_dir,
        archive_root=SAMPLE_ROOT / LAYOUT_DAY_ONLY,
        archive_layout=LAYOUT_DAY_ONLY,
    )
    return TestClient(app)


def test_api_pages_and_archive_days(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/").status_code == 200
    assert client.get("/corrections").status_code == 200
    body = client.get("/api/archive/days").json()
    assert body["layout"] == LAYOUT_DAY_ONLY
    assert body["days"] == ["2026-11-20", "2026-11-21"]


def test_api_upload_valid_then_query_changed_in_and_out(client):
    resp = client.post(
        "/api/corrections",
        data={"day": "2026-11-20"},
        files={"corrections_file": ("c.csv", VALID_CSV, "text/csv")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is True and body["count"] == 3
    assert body["changed_count"] == 3 and body["replaced_previous"] is False

    # 生效表可查
    listed = client.get("/api/corrections").json()
    assert len(listed["days"]) == 1 and listed["days"][0]["day"] == "2026-11-20"
    one = client.get("/api/corrections/2026-11-20").json()
    assert one["active"] is True and one["count"] == 3

    # 改入：R1 收到 T1003/T2002
    q1 = client.post(
        "/api/query", data={"day": "2026-11-20", "route": "R1"}
    ).json()
    assert {t["seq"] for t in q1["transactions"]} == {
        "T1001", "T1002", "T2001", "T1003", "T2002"
    }
    assert q1["correction_active"] is True and len(q1["files_opened"]) == 2

    # 改出：R2 不再有 T1003/T2002，但收到 R3 改入的 T1005
    q2 = client.post(
        "/api/query", data={"day": "2026-11-20", "route": "R2"}
    ).json()
    seqs2 = {t["seq"] for t in q2["transactions"]}
    assert "T1003" not in seqs2 and "T2002" not in seqs2
    assert seqs2 == {"T1004", "T2003", "T1005"}


def test_api_rejected_upload_keeps_previous_table(client):
    # 先生效一份
    client.post(
        "/api/corrections",
        data={"day": "2026-11-20"},
        files={"corrections_file": ("ok.csv", VALID_CSV, "text/csv")},
    )
    # 再传找不到流水的坏文件 → 400 整份退回
    bad = "批次号,流水号,原线路,正确线路\nB1,T9999,R1,R2\n".encode()
    resp = client.post(
        "/api/corrections",
        data={"day": "2026-11-20"},
        files={"corrections_file": ("bad.csv", bad, "text/csv")},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["accepted"] is False and body["previous_kept"] is True
    assert any("找不到该流水" in e for e in body["errors"])

    # 前一份仍然生效
    active = client.get("/api/corrections/2026-11-20").json()
    assert active["active"] is True and active["count"] == 3


def test_api_reject_duplicate_and_mismatch(client):
    dup = (
        "批次号,流水号,原线路,正确线路\n"
        "B1,T1003,R2,R1\nB1,T1003,R2,R3\n"
    ).encode()
    resp = client.post(
        "/api/corrections",
        data={"day": "2026-11-20"},
        files={"corrections_file": ("dup.csv", dup, "text/csv")},
    )
    assert resp.status_code == 400 and "同批同号" in resp.json()["errors"][0]

    mismatch = "批次号,流水号,原线路,正确线路\nB1,T1001,R9,R2\n".encode()
    resp2 = client.post(
        "/api/corrections",
        data={"day": "2026-11-20"},
        files={"corrections_file": ("mm.csv", mismatch, "text/csv")},
    )
    assert resp2.status_code == 400 and "原线路对不上" in resp2.json()["errors"][0]
    # 从未生效过任何更正
    assert client.get("/api/corrections/2026-11-20").json()["active"] is False


def test_api_reupload_replaces_not_stacks(client):
    client.post(
        "/api/corrections",
        data={"day": "2026-11-20"},
        files={"corrections_file": ("v1.csv", VALID_CSV, "text/csv")},
    )
    v2 = "批次号,流水号,原线路,正确线路\nB1,T1003,R2,R1\n".encode()
    resp = client.post(
        "/api/corrections",
        data={"day": "2026-11-20"},
        files={"corrections_file": ("v2.csv", v2, "text/csv")},
    )
    body = resp.json()
    assert body["accepted"] is True and body["count"] == 1
    assert body["replaced_previous"] is True and body["previous_count"] == 3
    active = client.get("/api/corrections/2026-11-20").json()
    assert active["count"] == 1 and active["entries"][0]["seq"] == "T1003"


def test_api_query_unknown_day_and_blank_route_400(client):
    resp = client.post(
        "/api/query", data={"day": "2020-01-01", "route": "R1"}
    )
    assert resp.status_code == 400 and "找不到运营日" in resp.json()["detail"]
    resp2 = client.post("/api/query", data={"day": "2026-11-20", "route": "  "})
    assert resp2.status_code == 400 and "查询线路不能为空" in resp2.json()["detail"]


def test_api_analyze_still_works(client):
    """原试算能力保留。"""
    with open(ROOT / "app/static/sample_batches.csv", "rb") as fb, \
         open(ROOT / "app/static/sample_queries.csv", "rb") as fq:
        resp = client.post(
            "/api/analyze",
            files={
                "batches_file": ("b.csv", fb, "text/csv"),
                "queries_file": ("q.csv", fq, "text/csv"),
            },
            data={
                "maxRowsPerFile": "100",
                "fileBudget": "1000",
                "openCostRows": "1000",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is True
    assert body["summary"]["chosen_files"] == 11
    assert body["summary"]["total_cost"] == 308000


def test_restart_reloads_effective_corrections(tmp_paths):
    data_dir, _ = tmp_paths
    app1 = create_app(
        data_dir=data_dir,
        archive_root=SAMPLE_ROOT / LAYOUT_DAY_ONLY,
        archive_layout=LAYOUT_DAY_ONLY,
    )
    TestClient(app1).post(
        "/api/corrections",
        data={"day": "2026-11-20"},
        files={"corrections_file": ("c.csv", VALID_CSV, "text/csv")},
    )
    # 重新 create_app 模拟进程重启
    app2 = create_app(
        data_dir=data_dir,
        archive_root=SAMPLE_ROOT / LAYOUT_DAY_ONLY,
        archive_layout=LAYOUT_DAY_ONLY,
    )
    active = TestClient(app2).get("/api/corrections/2026-11-20").json()
    assert active["active"] is True and active["count"] == 3


def test_route_partitioned_app_pruning_end_to_end(tmp_paths):
    data_dir, _ = tmp_paths
    app = create_app(
        data_dir=data_dir,
        archive_root=SAMPLE_ROOT / LAYOUT_ROUTE,
        archive_layout=LAYOUT_ROUTE,
    )
    client = TestClient(app)
    client.post(
        "/api/corrections",
        data={"day": "2026-11-20"},
        files={"corrections_file": ("c.csv", VALID_CSV, "text/csv")},
    )
    q1 = client.post(
        "/api/query", data={"day": "2026-11-20", "route": "R1"}
    ).json()
    opened = {Path(f).parent.name for f in q1["files_opened"]}
    assert opened == {"route=R1", "route=R2"}
    skipped = {Path(f).parent.name for f in q1["files_skipped_by_pruning"]}
    assert skipped == {"route=R3"}


def test_parse_day_helper():
    assert parse_day("2026-11-20") == date(2026, 11, 20)
    with pytest.raises(ValueError):
        parse_day("not-a-date")
