"""分区布局试算引擎。

规则（与需求一一对应）：

* 每个运营日只有两种布局：
  - day_only：只按日分区；
  - split：日分区下再按线路拆分。
* 文件按“单文件行数上限”装满一份再开下一份；同一 (批次, 线路) 的行数
  独立 ceil 切分，不跨批次拼文件。
* 查询只靠分区裁剪，命中的文件整份计入扫描；扫描行数按文件内全部行数计。
  - day_only 下任何查询都扫当天全部文件；
  - split 下指定线路只扫该线路文件，留空（全线）扫当天全部文件。
* 代价 = 命中文件数 × 每次打开折合扫描行数 × 执行次数。
* 在“全表文件数上限”预算内，用 0/1 背包选择各日布局，使总代价最低。
  额外文件数为 0 的日子若拆分有益则免费拆分；预算连最少文件数都不够时，
  只返回至少需要的文件数。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Optional

from .parser import BatchRecord, QueryRecord


def files_for_rows(rows: int, max_rows_per_file: int) -> int:
    """行数按上限装满切分所需文件数；行数为 0 时不需要文件。"""
    if rows <= 0:
        return 0
    return math.ceil(rows / max_rows_per_file)


@dataclass
class DayStat:
    day: date
    # (批次号, 线路) -> 行数
    batch_route_rows: dict[tuple[str, str], int]
    # 线路 -> 该线路当天总行数（跨批次）
    route_rows: dict[str, int]
    # 线路 -> split 布局下该线路文件数（批次间不拼文件，逐批次 ceil 后求和）
    route_split_files: dict[str, int]
    total_rows: int
    day_only_files: int
    split_files: int

    @property
    def extra_files(self) -> int:
        return self.split_files - self.day_only_files


def build_day_stats(
    batches: list[BatchRecord], max_rows_per_file: int
) -> dict[date, DayStat]:
    grouped: dict[date, dict[tuple[str, str], int]] = {}
    for rec in batches:
        bucket = grouped.setdefault(rec.day, {})
        key = (rec.batch_id, rec.route)
        bucket[key] = bucket.get(key, 0) + rec.rows

    stats: dict[date, DayStat] = {}
    for day, bucket in grouped.items():
        route_rows: dict[str, int] = {}
        route_split_files: dict[str, int] = {}
        split_files = 0
        total_rows = 0
        for (_batch_id, route), rows in sorted(bucket.items()):
            route_rows[route] = route_rows.get(route, 0) + rows
            n_files = files_for_rows(rows, max_rows_per_file)
            route_split_files[route] = route_split_files.get(route, 0) + n_files
            split_files += n_files
            total_rows += rows
        # day_only：逐批次（含其全部线路行数）切分，批次间不拼文件
        batch_totals: dict[str, int] = {}
        for (batch_id, _route), rows in bucket.items():
            batch_totals[batch_id] = batch_totals.get(batch_id, 0) + rows
        day_only_files = sum(
            files_for_rows(rows, max_rows_per_file) for rows in batch_totals.values()
        )
        stats[day] = DayStat(
            day=day,
            batch_route_rows=dict(bucket),
            route_rows=route_rows,
            route_split_files=route_split_files,
            total_rows=total_rows,
            day_only_files=day_only_files,
            split_files=split_files,
        )
    return stats


def _day_query_files(stat: DayStat, layout: str, route: Optional[str]) -> int:
    """某天某布局下，一次查询命中的文件数（整份计入）。"""
    if layout == "day_only":
        return stat.day_only_files
    if route is None:  # 全线查询：split 布局也得扫当天所有文件
        return stat.split_files
    return stat.route_split_files.get(route, 0)


def _day_query_rows(stat: DayStat, layout: str, route: Optional[str]) -> int:
    """命中文件内的总行数（整份扫描，按文件承载行数计）。"""
    if layout == "day_only" or route is None:
        return stat.total_rows
    return stat.route_rows.get(route, 0)


def _knapsack(
    items: list[tuple[int, int]], capacity: int
) -> tuple[int, list[bool]]:
    """0/1 背包：权重=额外文件数，价值=拆分节省的代价。

    返回 (采用的总权重, 每个 item 是否拆分)。等值时偏向不拆分（文件更少）。
    """
    n = len(items)
    chosen = [False] * n
    if n == 0 or capacity < 0:
        return 0, chosen

    if capacity <= 50_000:
        # 数组 DP：take[i][w] 记录容量 w 处理到第 i 件时是否取了第 i 件
        neg = [-1] * (capacity + 1)
        dp = neg[:]
        dp[0] = 0
        take_rows: list[bytearray] = []
        for weight, value in items:
            take = bytearray(capacity + 1)
            new_dp = dp[:]
            if weight <= capacity:
                for w in range(capacity - weight, -1, -1):
                    if dp[w] >= 0:
                        candidate = dp[w] + value
                        if candidate > new_dp[w + weight]:
                            new_dp[w + weight] = candidate
                            take[w + weight] = 1
            dp = new_dp
            take_rows.append(take)
        best_w = max(range(capacity + 1), key=lambda w: dp[w])
        best_value = dp[best_w]
        w = best_w
        for i in range(n - 1, -1, -1):
            if take_rows[i][w]:
                chosen[i] = True
                w -= items[i][0]
        return best_w, chosen

    # 大容量稀疏 DP：每个状态保留不可变前驱链，回溯重建选择
    # state: (value, parent_state_or_None, took_index_or_None)
    dp: dict[int, tuple] = {0: (0, None, None)}
    for idx, (weight, value) in enumerate(items):
        snapshot = dict(dp)  # 仅基于上一轮状态转移
        for w, state in snapshot.items():
            nw = w + weight
            if nw > capacity:
                continue
            candidate = state[0] + value
            if nw not in dp or candidate > dp[nw][0]:
                dp[nw] = (candidate, state, idx)
    best_w = max(dp, key=lambda w: dp[w][0])
    state = dp[best_w]
    while state[1] is not None:
        chosen[state[2]] = True
        state = state[1]
    return best_w, chosen


def optimize(
    batches: list[BatchRecord],
    queries: list[QueryRecord],
    max_rows_per_file: int,
    file_budget: int,
    open_cost_rows: int,
) -> dict:
    if not batches:
        raise ValueError("批次明细CSV没有有效记录，无法试算")
    if max_rows_per_file <= 0:
        raise ValueError("单文件行数上限必须为正整数")
    if file_budget <= 0:
        raise ValueError("全表文件数上限必须为正整数")
    if open_cost_rows <= 0:
        raise ValueError("每次打开折合扫描行数必须为正整数")

    stats = build_day_stats(batches, max_rows_per_file)
    days = sorted(stats)
    min_files = sum(stats[d].day_only_files for d in days)
    max_files = sum(stats[d].split_files for d in days)

    warnings: list[str] = []

    # 预计算每个查询覆盖到的、实际有数据的运营日
    covering: list[list[date]] = []
    for qi, q in enumerate(queries):
        hit_days = [d for d in days if q.start_day <= d <= q.end_day]
        covering.append(hit_days)
        if not hit_days:
            warnings.append(
                f"查询 #{qi + 1}（{q.start_day}~{q.end_day}"
                + (f"，线路 {q.route}" if q.route else "，全线")
                + "）的日期范围内没有任何明细数据，代价计为 0"
            )

    # 每个候选日：拆分相对全日布局的代价节省（所有覆盖查询加权求和）
    day_values: dict[date, int] = {}
    for d in days:
        stat = stats[d]
        value = 0
        for qi, q in enumerate(queries):
            if d not in covering[qi]:
                continue
            base_files = _day_query_files(stat, "day_only", q.route)
            split_files = _day_query_files(stat, "split", q.route)
            value += (base_files - split_files) * open_cost_rows * q.executions
        day_values[d] = value

    # 1) 零额外文件且拆分有益的日子：免费拆分
    forced_split: set[date] = set()
    used_extra = 0
    for d in days:
        stat = stats[d]
        if stat.extra_files == 0 and day_values[d] > 0:
            forced_split.add(d)
            used_extra += 0  # 形式化：额外文件为 0

    # 预算连最少文件数都不够：只报至少需要多少文件
    if file_budget < min_files:
        return {
            "feasible": False,
            "min_files": min_files,
            "file_budget": file_budget,
            "max_split_files": max_files,
            "warnings": warnings,
        }

    # 2) 正权重候选：额外文件 > 0 且拆分严格有益
    candidates = [
        d
        for d in days
        if stats[d].extra_files > 0 and day_values[d] > 0
    ]
    items = [(stats[d].extra_files, day_values[d]) for d in candidates]
    capacity = file_budget - min_files
    used_weight, chosen_flags = _knapsack(items, capacity)

    layout: dict[date, str] = {}
    for d in days:
        if d in forced_split:
            layout[d] = "split"
        else:
            layout[d] = "day_only"
    for d, took in zip(candidates, chosen_flags):
        if took:
            layout[d] = "split"

    chosen_files = sum(
        stats[d].split_files if layout[d] == "split" else stats[d].day_only_files
        for d in days
    )

    # 3) 汇总各查询在选定布局下的命中明细，同时给一份全日基线对照
    query_details = []
    total_cost = 0
    baseline_total_cost = 0
    for qi, q in enumerate(queries):
        per_day = []
        files_open = 0
        rows_scanned = 0
        base_files_open = 0
        base_rows_scanned = 0
        for d in covering[qi]:
            stat = stats[d]
            chosen_layout = layout[d]
            nf = _day_query_files(stat, chosen_layout, q.route)
            nr = _day_query_rows(stat, chosen_layout, q.route)
            bf = _day_query_files(stat, "day_only", q.route)
            br = _day_query_rows(stat, "day_only", q.route)
            files_open += nf
            rows_scanned += nr
            base_files_open += bf
            base_rows_scanned += br
            per_day.append(
                {
                    "day": d.isoformat(),
                    "layout": chosen_layout,
                    "files": nf,
                    "scanned_rows": nr,
                    "day_only_files": bf,
                    "day_only_scanned_rows": br,
                }
            )
        cost = files_open * open_cost_rows * q.executions
        base_cost = base_files_open * open_cost_rows * q.executions
        total_cost += cost
        baseline_total_cost += base_cost
        query_details.append(
            {
                "index": qi + 1,
                "start_day": q.start_day.isoformat(),
                "end_day": q.end_day.isoformat(),
                "route": q.route,  # null 表示全线
                "executions": q.executions,
                "files_open": files_open,
                "scanned_rows": rows_scanned,
                "cost": cost,
                "baseline_files_open": base_files_open,
                "baseline_scanned_rows": base_rows_scanned,
                "baseline_cost": base_cost,
                "per_day": per_day,
            }
        )

    day_layouts = [
        {
            "day": d.isoformat(),
            "layout": layout[d],
            "day_only_files": stats[d].day_only_files,
            "split_files": stats[d].split_files,
            "chosen_files": (
                stats[d].split_files if layout[d] == "split"
                else stats[d].day_only_files
            ),
            "total_rows": stats[d].total_rows,
            "extra_files_if_split": stats[d].extra_files,
        }
        for d in days
    ]

    return {
        "feasible": True,
        "params": {
            "max_rows_per_file": max_rows_per_file,
            "file_budget": file_budget,
            "open_cost_rows": open_cost_rows,
        },
        "summary": {
            "num_days": len(days),
            "num_queries": len(queries),
            "total_rows": sum(s.total_rows for s in stats.values()),
            "min_files": min_files,
            "max_split_files": max_files,
            "chosen_files": chosen_files,
            "budget_remaining": file_budget - chosen_files,
            "total_cost": total_cost,
            "baseline_total_cost": baseline_total_cost,
            "saved_cost": baseline_total_cost - total_cost,
            "split_day_count": sum(1 for d in days if layout[d] == "split"),
        },
        "day_layouts": day_layouts,
        "queries": query_details,
        "warnings": warnings,
    }
