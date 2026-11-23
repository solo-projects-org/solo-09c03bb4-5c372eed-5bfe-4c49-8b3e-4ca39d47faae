"""更正 CSV 与当日原档的对账校验。

整份退回规则：只要出现下列任一情况，整份更正不生效，继续使用前一份更正表。

1. 找不到流水：（批次号, 流水号）在当日原档中不存在；
2. 原线路对不上：更正 CSV 声明的原线路与原档里该流水的线路不一致；
3. 同批同号多条更正：在解析阶段即拦截（见 ``parser.parse_corrections``）。

校验一次性返回全部错误，前端可一次展示，避免用户逐行试错。
"""

from __future__ import annotations

from dataclasses import dataclass

from .archive import ParquetArchive
from .parser import CorrectionRow


@dataclass
class ValidationReport:
    ok: bool
    errors: list[str]
    warnings: list[str]
    matched_rows: int
    noop_rows: int


def validate_corrections(
    rows: list[CorrectionRow],
    archive: ParquetArchive,
    day,
    parse_warnings: list[str] | None = None,
) -> ValidationReport:
    index = archive.load_day_index(day)
    errors: list[str] = []
    noop = 0
    for row in rows:
        key = (row.batch_id, row.seq)
        archive_routes = index.get(key)
        if archive_routes is None:
            errors.append(
                f"批次 {row.batch_id} 流水 {row.seq}：当日原档找不到该流水，整份退回"
            )
            continue
        if row.original_route not in archive_routes:
            errors.append(
                f"批次 {row.batch_id} 流水 {row.seq}：原线路对不上"
                f"（更正CSV写 {row.original_route}，原档为"
                f" {'/'.join(sorted(archive_routes))}），整份退回"
            )
            continue
        if row.original_route == row.corrected_route:
            noop += 1

    warnings = list(parse_warnings or [])
    return ValidationReport(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        matched_rows=len(rows) - sum(1 for _ in errors),
        noop_rows=noop,
    )
