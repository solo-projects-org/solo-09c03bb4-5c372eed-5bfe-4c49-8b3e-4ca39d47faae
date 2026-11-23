"""pytest 自测：引擎手算样例、预算边界、CSV 解析、FastAPI 接口。"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.optimizer import build_day_stats, files_for_rows, optimize
from app.parser import parse_batches, parse_queries

OPEN_COST = 1000
MAX_ROWS = 100


def _sample_batches():
    raw = (
        "入仓批次号,运营日,线路,行数\n"
        "B1,2026-09-01,R1,200\n"
        "B1,2026-09-01,R2,150\n"
        "B2,2026-09-01,R1,80\n"
        "B1,2026-09-02,R1,120\n"
        "B1,2026-09-02,R2,90\n"
        "B2,2026-09-02,R2,70\n"
        "B1,2026-09-03,R1,60\n"
        "B1,2026-09-03,R2,50\n"
    ).encode("utf-8")
    return parse_batches(raw)[0]


def _sample_queries():
    raw = (
        "起始运营日,截止运营日,线路,执行次数\n"
        "2026-09-01,2026-09-01,R1,100\n"
        "2026-09-02,2026-09-03,R2,2\n"
        "2026-09-03,2026-09-03,,1\n"
    ).encode("utf-8")
    return parse_queries(raw)[0]


# ---------- 文件切分 ----------

@pytest.mark.parametrize(
    "rows,expected",
    [(0, 0), (1, 1), (100, 1), (101, 2), (250, 3)],
)
def test_files_for_rows(rows, expected):
    assert files_for_rows(rows, MAX_ROWS) == expected


def test_day_stats_file_counts():
    stats = build_day_stats(_sample_batches(), MAX_ROWS)
    d1, d2, d3 = stats[date(2026, 9, 1)], stats[date(2026, 9, 2)], stats[date(2026, 9, 3)]
    # 9-01：B1=350 行→4 文件，B2=80 行→1 文件，共 5；拆分后 2+2+1=5（额外 0）
    assert (d1.day_only_files, d1.split_files) == (5, 5)
    assert d1.route_split_files == {"R1": 3, "R2": 2}
    # 9-02：B1=210→3，B2=70→1，共 4；拆分后 R1=2, R2=1+1=2，共 4（额外 0）
    assert (d2.day_only_files, d2.split_files) == (4, 4)
    assert d2.route_split_files == {"R1": 2, "R2": 2}
    # 9-03：单批次共 110→2；拆分后 1+1=2（额外 0）
    assert (d3.day_only_files, d3.split_files) == (2, 2)
    assert d3.route_split_files == {"R1": 1, "R2": 1}


def test_split_can_cost_more_files_than_day_only():
    # 同批次两条小线路：日布局 1 个文件，拆分后 2 个文件
    raw = (
        "入仓批次号,运营日,线路,行数\n"
        "B1,2026-09-01,R1,40\n"
        "B1,2026-09-01,R2,40\n"
    ).encode()
    stats = build_day_stats(parse_batches(raw)[0], MAX_ROWS)
    stat = stats[date(2026, 9, 1)]
    assert (stat.day_only_files, stat.split_files, stat.extra_files) == (1, 2, 1)


# ---------- 手算样例整体优化 ----------

def test_optimize_hand_calculated_sample():
    result = optimize(
        _sample_batches(), _sample_queries(),
        max_rows_per_file=MAX_ROWS, file_budget=1000, open_cost_rows=OPEN_COST,
    )
    assert result["feasible"] is True
    s = result["summary"]
    # 三天额外文件数都是 0，且拆分对各自查询有益 → 全部拆分，共 11 个文件
    assert s["min_files"] == 11
    assert s["max_split_files"] == 11
    assert s["chosen_files"] == 11
    assert s["split_day_count"] == 3
    # 手算总代价（打开成本 1000）：
    # Q1 = 3×1000×100 = 300000；Q2 = 3×1000×2 = 6000；Q3 = 2×1000×1 = 2000
    assert s["total_cost"] == 308000
    # 基线（全部仅按日）：Q1=5×1000×100=500000；Q2=6×1000×2=12000；Q3=2000
    assert s["baseline_total_cost"] == 514000
    assert s["saved_cost"] == 206000

    q1, q2, q3 = result["queries"]
    assert (q1["files_open"], q1["scanned_rows"], q1["cost"]) == (3, 280, 300000)
    assert (q2["files_open"], q2["scanned_rows"], q2["cost"]) == (3, 210, 6000)
    # 全线查询：split 下也要扫当天全部文件/全部行数；Q3 执行 1 次 → 2×1000×1
    assert (q3["files_open"], q3["scanned_rows"], q3["cost"]) == (2, 110, 2000)
    # 逐日核对
    q1_day = q1["per_day"][0]
    assert q1_day["day"] == "2026-09-01" and q1_day["layout"] == "split"
    assert (q1_day["files"], q1_day["scanned_rows"]) == (3, 280)
    assert (q1_day["day_only_files"], q1_day["day_only_scanned_rows"]) == (5, 430)


def test_budget_below_minimum_reports_only_required_files():
    result = optimize(
        _sample_batches(), _sample_queries(),
        max_rows_per_file=MAX_ROWS, file_budget=8, open_cost_rows=OPEN_COST,
    )
    assert result["feasible"] is False
    assert result["min_files"] == 11
    assert result["file_budget"] == 8
    assert "queries" not in result and "day_layouts" not in result


def test_knapsack_trades_extra_files_under_budget():
    # 两天结构相同：两个批次各含 R1/R2 两条小线路（每格 40 行）。
    # 日布局：每批次 80 行 → 1 文件，每天 2 文件；
    # split：每个 (批次, 线路) 1 文件，每天 4 文件 → 拆分额外 +2。
    # 指定线路查询：日布局命中 2 文件，split 只命中 2 个该线路文件中的…
    # 实际 split 下 R1 有 B1/B2 两个文件 → 仍 2 文件，无收益；
    # 故改用单批次但行数越过上限的构造，确保 split 严格减少命中文件。
    batches = parse_batches(
        (
            "入仓批次号,运营日,线路,行数\n"
            # d1：B1 共 180 行 → 日布局 2 文件；split 下 R1=1 文件、R2=1 文件，共 2（额外 0）
            "B1,2026-09-01,R1,90\n"
            "B1,2026-09-01,R2,90\n"
            # d2：两个批次，每批 80 行 → 日布局 2 文件；split 每格 1 文件，共 4，额外 +2
            "B1,2026-09-02,R1,40\n"
            "B1,2026-09-02,R2,40\n"
            "B2,2026-09-02,R1,40\n"
            "B2,2026-09-02,R2,40\n"
        ).encode()
    )[0]
    queries = parse_queries(
        (
            "起始运营日,截止运营日,线路,执行次数\n"
            # d1 指定 R1：日布局 2 文件 → split 1 文件，额外 0 文件、节省 100000
            "2026-09-01,2026-09-01,R1,100\n"
            # d2 指定 R1：日布局 2 文件 → split 2 文件（B1/B2 各一），无收益；
            # 改成只查 B1 无法区分，这里用 R2 且执行 1 次，同样 2→2 无收益。
            # 为制造正权重收益，d2 改为下面单独追加的单日场景。
            "2026-09-02,2026-09-02,R2,1\n"
        ).encode()
    )[0]
    # 直接校验文件数事实
    stats = build_day_stats(batches, MAX_ROWS)
    assert (stats[date(2026, 9, 1)].day_only_files,
            stats[date(2026, 9, 1)].split_files) == (2, 2)
    assert (stats[date(2026, 9, 2)].day_only_files,
            stats[date(2026, 9, 2)].split_files) == (2, 4)

    # d1 零额外文件且有益 → 免费拆分；d2 对 R2 查询 2→2 无收益 → 不拆
    result = optimize(batches, queries, MAX_ROWS, file_budget=4, open_cost_rows=OPEN_COST)
    layouts = {d["day"]: d["layout"] for d in result["day_layouts"]}
    assert layouts["2026-09-01"] == "split"
    assert layouts["2026-09-02"] == "day_only"
    assert result["summary"]["chosen_files"] == 4  # d1 用 2 + d2 用 2
    # Q1：split 后 1×1000×100=100000；Q2：日布局 2×1000×1=2000
    assert result["summary"]["total_cost"] == 102000
    assert result["summary"]["baseline_total_cost"] == 202000


def test_knapsack_positive_weight_choice_and_budget_cap():
    # 正权重候选：单日，B1/B2 各含 R1、R2，每格 60 行（上限 100）。
    # 日布局：每批次 120 行 → 2 文件，共 4；split：4 格各 1 文件，共 4，额外 0……
    # 要让 split 多开文件，需每格行数小到日布局能合并：每格 40，批次合计 80 → 日 2 文件，
    # split 4 文件，额外 +2；指定 R1 查询命中文件 2 → split 下仍 2（B1/B2 各一），无收益。
    # 正收益且正权重的最小可复现构造：单批次三线路，两条 40 + 一条 30。
    batches = parse_batches(
        (
            "入仓批次号,运营日,线路,行数\n"
            "B1,2026-09-01,R1,40\n"
            "B1,2026-09-01,R2,40\n"
            "B1,2026-09-01,R3,30\n"     # 日布局：110 行 → 2 文件；split：3 文件，额外 +1
            "B1,2026-09-02,R1,40\n"
            "B1,2026-09-02,R2,40\n"
            "B1,2026-09-02,R3,30\n"
        ).encode()
    )[0]
    queries = parse_queries(
        (
            "起始运营日,截止运营日,线路,执行次数\n"
            "2026-09-01,2026-09-01,R1,100\n"   # 日 2 文件 → split 1 文件，省 100000，+1 文件
            "2026-09-02,2026-09-02,R2,1\n"     # 日 2 → split 1，省 1000，+1 文件
        ).encode()
    )[0]
    stats = build_day_stats(batches, MAX_ROWS)
    assert stats[date(2026, 9, 1)].extra_files == 1

    # min_files=4；预算 5 只够一个额外文件 → 选节省大的 d1
    result = optimize(batches, queries, MAX_ROWS, file_budget=5, open_cost_rows=OPEN_COST)
    layouts = {d["day"]: d["layout"] for d in result["day_layouts"]}
    assert layouts["2026-09-01"] == "split"
    assert layouts["2026-09-02"] == "day_only"
    assert result["summary"]["chosen_files"] == 5
    # Q1 拆后 1×1000×100=100000；Q2 日布局 2×1000×1=2000
    assert result["summary"]["total_cost"] == 102000

    # 预算 6 → 两天都拆：100000 + 1000
    result2 = optimize(batches, queries, MAX_ROWS, file_budget=6, open_cost_rows=OPEN_COST)
    assert result2["summary"]["chosen_files"] == 6
    assert result2["summary"]["total_cost"] == 101000
    assert result2["summary"]["saved_cost"] == 202000 - 101000


def test_negative_value_split_is_never_chosen():
    # 全线查询在 split 下文件更多（同批次两条小线路），拆分价值为负 → 永远不拆
    batches = parse_batches(
        (
            "入仓批次号,运营日,线路,行数\n"
            "B1,2026-09-01,R1,40\n"
            "B1,2026-09-01,R2,40\n"
        ).encode()
    )[0]
    queries = parse_queries(
        "起始运营日,截止运营日,线路,执行次数\n2026-09-01,2026-09-01,,5\n".encode()
    )[0]
    result = optimize(batches, queries, MAX_ROWS, file_budget=100, open_cost_rows=OPEN_COST)
    assert result["day_layouts"][0]["layout"] == "day_only"
    assert result["summary"]["chosen_files"] == 1
    assert result["summary"]["total_cost"] == 1 * 1000 * 5


def test_query_without_data_in_range_warns():
    queries = parse_queries(
        (
            "起始运营日,截止运营日,线路,执行次数\n"
            "2026-08-01,2026-08-02,R1,3\n"
        ).encode()
    )[0]
    result = optimize(_sample_batches(), queries, MAX_ROWS, 1000, OPEN_COST)
    assert result["feasible"] is True
    assert result["queries"][0]["files_open"] == 0
    assert result["queries"][0]["cost"] == 0
    assert any("没有任何明细数据" in w for w in result["warnings"])


# ---------- 解析器 ----------

def test_parser_accepts_english_headers_and_blank_route():
    raw = (
        "batch_id,date,route,rows\n"
        "X1,2026/09/01,K2,123\n"
    ).encode()
    records, _ = parse_batches(raw)
    assert records[0].batch_id == "X1"
    assert records[0].day == date(2026, 9, 1)
    assert records[0].route == "K2" and records[0].rows == 123

    qraw = (
        "start_day,end_day,route,executions\n"
        "2026-09-01,2026-09-02,,7\n"
    ).encode()
    qs, _ = parse_queries(qraw)
    assert qs[0].route is None and qs[0].executions == 7


def test_parser_rejects_bad_rows_and_reversed_dates():
    with pytest.raises(ValueError, match="行数"):
        parse_batches("入仓批次号,运营日,线路,行数\nB1,2026-09-01,R1,0\n".encode())
    with pytest.raises(ValueError, match="起始运营日"):
        parse_queries(
            "起始运营日,截止运营日,线路,执行次数\n2026-09-03,2026-09-01,R1,1\n".encode()
        )
    with pytest.raises(ValueError, match="缺少必需列"):
        parse_batches("运营日,线路,行数\n2026-09-01,R1,10\n".encode())


def test_parser_merges_duplicate_keys():
    raw = (
        "入仓批次号,运营日,线路,行数\n"
        "B1,2026-09-01,R1,60\n"
        "B1,2026-09-01,R1,40\n"
    ).encode()
    records, warnings = parse_batches(raw)
    assert len(records) == 1 and records[0].rows == 100
    assert warnings and "重复" in warnings[0]


# ---------- API ----------

def test_api_end_to_end():
    client = TestClient(app)
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/").status_code == 200

    with open("app/static/sample_batches.csv", "rb") as fb, \
         open("app/static/sample_queries.csv", "rb") as fq:
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
    assert len(body["queries"]) == 3


def test_api_infeasible_budget():
    client = TestClient(app)
    with open("app/static/sample_batches.csv", "rb") as fb, \
         open("app/static/sample_queries.csv", "rb") as fq:
        resp = client.post(
            "/api/analyze",
            files={
                "batches_file": ("b.csv", fb, "text/csv"),
                "queries_file": ("q.csv", fq, "text/csv"),
            },
            data={"maxRowsPerFile": "100", "fileBudget": "1", "openCostRows": "1000"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is False and body["min_files"] == 11


def test_api_bad_input_returns_400():
    client = TestClient(app)
    resp = client.post(
        "/api/analyze",
        files={
            "batches_file": ("b.csv", "a,b,c\n1,2,3\n".encode(), "text/csv"),
            "queries_file": ("q.csv", "起始运营日,截止运营日,线路,执行次数\n2026-09-01,2026-09-01,R1,1\n".encode(), "text/csv"),
        },
        data={"maxRowsPerFile": "100", "fileBudget": "10", "openCostRows": "1000"},
    )
    assert resp.status_code == 400
    assert "缺少必需列" in resp.json()["detail"]
