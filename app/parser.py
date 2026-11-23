"""两张 CSV 上传文件的解析与校验。

批次明细列：入仓批次号、运营日、线路、行数
历史查询列：起始运营日、截止运营日、线路（留空=全线）、执行次数

表头同时接受常见英文别名，文件编码按 UTF-8（含 BOM）解析，失败时回退 GBK。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

BATCH_ALIASES = {"入仓批次号", "批次号", "批次", "batch", "batch_id", "batchid"}
DAY_ALIASES = {"运营日", "日期", "日", "day", "date", "op_day", "opdate"}
ROUTE_ALIASES = {"线路", "线路号", "线", "route", "route_id", "line", "line_id"}
ROWS_ALIASES = {"行数", "记录行数", "条数", "rows", "row_count", "rowcount", "cnt"}
SEQ_ALIASES = {
    "流水号", "交易流水号", "流水", "交易序号", "序号",
    "seq", "serial", "serial_no", "serialno", "transaction_id", "txn_id", "txnid",
}
ORIG_ROUTE_ALIASES = {
    "原线路", "原线路号", "更正前线路", "旧线路",
    "original_route", "old_route", "from_route", "route_before",
}
CORRECTED_ROUTE_ALIASES = {
    "正确线路", "更正线路", "更正后线路", "新线路", "目标线路",
    "corrected_route", "new_route", "to_route", "target_route", "route_after",
}
START_ALIASES = {
    "起始运营日", "开始运营日", "起始日期", "开始日期",
    "start", "start_day", "begin", "begin_day",
}
END_ALIASES = {
    "截止运营日", "结束运营日", "截止日期", "结束日期",
    "end", "end_day",
}
EXEC_ALIASES = {"执行次数", "次数", "executions", "exec_count", "times", "count"}


@dataclass(frozen=True)
class BatchRecord:
    batch_id: str
    day: date
    route: str
    rows: int


@dataclass(frozen=True)
class QueryRecord:
    start_day: date
    end_day: date
    route: Optional[str]  # None 表示查全线
    executions: int


@dataclass(frozen=True)
class CorrectionRow:
    """一行线路更正：把（批次号, 流水号）对应的流水从原线路改到正确线路。"""

    batch_id: str
    seq: str
    original_route: str
    corrected_route: str


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError as exc:
            raise ValueError("CSV 编码无法识别，请另存为 UTF-8 后再上传") from exc


def _normalize_header(name: str) -> str:
    return (name or "").strip().lstrip("﻿").lower()


def _map_headers(fieldnames, aliases: dict[str, set[str]], kind: str) -> dict[str, str]:
    """返回 逻辑字段 -> 实际列名 的映射。"""
    if not fieldnames:
        raise ValueError(f"{kind}CSV 缺少表头行")
    lowered = {_normalize_header(h): h for h in fieldnames}
    mapping: dict[str, str] = {}
    for logical, names in aliases.items():
        for name in names:
            key = name.strip().lstrip("﻿").lower()
            if key in lowered:
                mapping[logical] = lowered[key]
                break
    return mapping


def _parse_date(value: str, field: str) -> date:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field}不能为空")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"无法识别的{field}：{text!r}，应为 YYYY-MM-DD 格式")


def _parse_int(value: str, field: str, *, minimum: int = 1) -> int:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field}不能为空")
    try:
        number = int(text)
    except ValueError as exc:
        raise ValueError(f"{field}必须是整数，实际为 {text!r}") from exc
    if number < minimum:
        raise ValueError(f"{field}必须 ≥ {minimum}，实际为 {number}")
    return number


def _read_dict_rows(raw: bytes, kind: str) -> tuple[list[dict[str, str]], list[str]]:
    text = _decode(raw)
    reader = csv.DictReader(io.StringIO(text))
    rows = [row for row in reader if any((v or "").strip() for v in row.values())]
    if not rows:
        raise ValueError(f"{kind}CSV 没有数据行")
    return rows, reader.fieldnames or []


def parse_batches(raw: bytes) -> tuple[list[BatchRecord], list[str]]:
    warnings: list[str] = []
    rows, fieldnames = _read_dict_rows(raw, "批次明细")
    cols = _map_headers(
        fieldnames,
        {
            "batch_id": BATCH_ALIASES,
            "day": DAY_ALIASES,
            "route": ROUTE_ALIASES,
            "rows": ROWS_ALIASES,
        },
        "批次明细",
    )
    missing = [k for k in ("batch_id", "day", "route", "rows") if k not in cols]
    if missing:
        raise ValueError(
            "批次明细CSV缺少必需列：" + "、".join(missing)
            + f"；实际表头：{', '.join(fieldnames)}"
        )

    merged: dict[tuple[str, date, str], int] = {}
    duplicates = 0
    for line_no, row in enumerate(rows, start=2):
        batch_id = (row.get(cols["batch_id"]) or "").strip()
        if not batch_id:
            raise ValueError(f"批次明细第 {line_no} 行入仓批次号为空")
        day = _parse_date(row.get(cols["day"], ""), f"第 {line_no} 行运营日")
        route = (row.get(cols["route"]) or "").strip()
        if not route:
            raise ValueError(f"批次明细第 {line_no} 行线路为空（明细数据必须归属具体线路）")
        row_count = _parse_int(row.get(cols["rows"], ""), f"第 {line_no} 行行数")
        key = (batch_id, day, route)
        if key in merged:
            duplicates += 1
        merged[key] = merged.get(key, 0) + row_count

    if duplicates:
        warnings.append(f"批次明细存在 {duplicates} 条重复的（批次/运营日/线路）记录，已自动合并行数")
    records = [
        BatchRecord(batch_id=k[0], day=k[1], route=k[2], rows=v)
        for k, v in sorted(merged.items(), key=lambda x: (x[0][1], x[0][0], x[0][2]))
    ]
    return records, warnings


def parse_queries(raw: bytes) -> tuple[list[QueryRecord], list[str]]:
    warnings: list[str] = []
    rows, fieldnames = _read_dict_rows(raw, "历史查询")
    cols = _map_headers(
        fieldnames,
        {
            "start_day": START_ALIASES,
            "end_day": END_ALIASES,
            "route": ROUTE_ALIASES,
            "executions": EXEC_ALIASES,
        },
        "历史查询",
    )
    missing = [k for k in ("start_day", "end_day", "executions") if k not in cols]
    if missing:
        raise ValueError(
            "历史查询CSV缺少必需列：" + "、".join(missing)
            + f"；实际表头：{', '.join(fieldnames)}"
        )

    records: list[QueryRecord] = []
    for line_no, row in enumerate(rows, start=2):
        start_day = _parse_date(row.get(cols["start_day"], ""), f"第 {line_no} 行起始运营日")
        end_day = _parse_date(row.get(cols["end_day"], ""), f"第 {line_no} 行截止运营日")
        if start_day > end_day:
            raise ValueError(
                f"第 {line_no} 行起始运营日 {start_day} 晚于截止运营日 {end_day}"
            )
        route_text = (row.get(cols["route"]) or "").strip()
        route = route_text or None
        executions = _parse_int(
            row.get(cols["executions"], ""), f"第 {line_no} 行执行次数"
        )
        records.append(
            QueryRecord(
                start_day=start_day, end_day=end_day, route=route, executions=executions
            )
        )

    records.sort(key=lambda q: (q.start_day, q.end_day, q.route or ""))
    return records, warnings


def parse_corrections(raw: bytes) -> tuple[list[CorrectionRow], list[str]]:
    """解析整份更正 CSV（列：批次号、流水号、原线路、正确线路）。

    与“整份退回”规则对应的硬校验（任一不过即抛 ValueError）：
      * 缺列 / 无数据行 / 任一字段为空；
      * 同（批次号, 流水号）出现多条更正 —— 归档里的一条流水只能有一个正确线路。
    原线路 == 正确线路的行允许上传，仅产生提示（最终是否有效由归档校验决定）。
    """
    warnings: list[str] = []
    rows, fieldnames = _read_dict_rows(raw, "更正")
    cols = _map_headers(
        fieldnames,
        {
            "batch_id": BATCH_ALIASES,
            "seq": SEQ_ALIASES,
            "original_route": ORIG_ROUTE_ALIASES,
            "corrected_route": CORRECTED_ROUTE_ALIASES,
        },
        "更正",
    )
    missing = [
        k for k in ("batch_id", "seq", "original_route", "corrected_route")
        if k not in cols
    ]
    if missing:
        raise ValueError(
            "更正CSV缺少必需列：" + "、".join(missing)
            + f"；实际表头：{', '.join(fieldnames)}"
        )

    records: list[CorrectionRow] = []
    seen: dict[tuple[str, str], int] = {}
    noop_count = 0
    for line_no, row in enumerate(rows, start=2):
        batch_id = (row.get(cols["batch_id"]) or "").strip()
        if not batch_id:
            raise ValueError(f"更正CSV第 {line_no} 行批次号为空")
        seq = (row.get(cols["seq"]) or "").strip()
        if not seq:
            raise ValueError(f"更正CSV第 {line_no} 行流水号为空")
        original_route = (row.get(cols["original_route"]) or "").strip()
        if not original_route:
            raise ValueError(f"更正CSV第 {line_no} 行原线路为空")
        corrected_route = (row.get(cols["corrected_route"]) or "").strip()
        if not corrected_route:
            raise ValueError(f"更正CSV第 {line_no} 行正确线路为空")
        key = (batch_id, seq)
        if key in seen:
            raise ValueError(
                f"更正CSV存在同批同号的多条更正：批次 {batch_id}、流水 {seq}"
                f"（第 {seen[key]} 行与第 {line_no} 行），整份退回"
            )
        seen[key] = line_no
        if original_route == corrected_route:
            noop_count += 1
        records.append(
            CorrectionRow(
                batch_id=batch_id,
                seq=seq,
                original_route=original_route,
                corrected_route=corrected_route,
            )
        )

    if noop_count:
        warnings.append(
            f"更正CSV中有 {noop_count} 行原线路与正确线路相同，不改变取数归属"
        )
    return records, warnings


def parse_day(raw_text: str, field: str = "运营日") -> date:
    """对外的日期解析入口，复用与 CSV 列一致的格式与报错规则。"""
    return _parse_date(raw_text, field)
