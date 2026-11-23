#!/usr/bin/env python3
"""生成两种分区布局的本地样例归档（真实 Parquet 文件）。

布局一  data/sample_archive/day_only/   按日分区：day=YYYY-MM-DD/part-*.parquet
布局二  data/sample_archive/route_partitioned/
        日 + 线路分区：day=YYYY-MM-DD/route=R1/part-*.parquet

两份归档的数据行完全一致，区别只在物理分区方式，便于对照查询时的文件裁剪差异。
样例更正 CSV 同时写入 app/static 供网页“载入样例”下载体验。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_ROOT = ROOT / "data" / "sample_archive"
STATIC_DIR = ROOT / "app" / "static"

# (运营日, 批次, 流水号, 原线路)
TRANSACTIONS = [
    # ---- 2026-11-20：两个批次、三条线路，4 个 (批次,线路) 分组 ----
    ("2026-11-20", "B1", "T1001", "R1"),
    ("2026-11-20", "B1", "T1002", "R1"),
    ("2026-11-20", "B1", "T1003", "R2"),
    ("2026-11-20", "B1", "T1004", "R2"),
    ("2026-11-20", "B1", "T1005", "R3"),
    ("2026-11-20", "B2", "T2001", "R1"),
    ("2026-11-20", "B2", "T2002", "R2"),
    ("2026-11-20", "B2", "T2003", "R2"),
    # ---- 2026-11-21：单批次两条线路，用于无更正当日的对照 ----
    ("2026-11-21", "B1", "T3001", "R1"),
    ("2026-11-21", "B1", "T3002", "R2"),
    ("2026-11-21", "B1", "T3003", "R2"),
]

SCHEMA = pa.schema(
    [
        pa.field("batch_id", pa.string()),
        pa.field("seq", pa.string()),
        pa.field("route", pa.string()),
    ]
)

# 与更正样例一致：T1003 R2->R1、T2002 R2->R1（改入 R1 / 改出 R2），T1005 R3->R2（改入 R2）
VALID_CORRECTION_CSV = """批次号,流水号,原线路,正确线路
B1,T1003,R2,R1
B2,T2002,R2,R1
B1,T1005,R3,R2
"""

MISSING_CSV = """批次号,流水号,原线路,正确线路
B1,T9999,R1,R2
"""

MISMATCH_CSV = """批次号,流水号,原线路,正确线路
B1,T1001,R9,R2
"""

DUP_CSV = """批次号,流水号,原线路,正确线路
B1,T1003,R2,R1
B1,T1003,R2,R3
"""


def _write_table(rows: list[tuple[str, str, str]], path: Path) -> None:
    table = pa.Table.from_pylist(
        [{"batch_id": b, "seq": s, "route": r} for (_d, b, s, r) in rows],
        schema=SCHEMA,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def build() -> None:
    if ARCHIVE_ROOT.exists():
        shutil.rmtree(ARCHIVE_ROOT)

    by_day: dict[str, list[tuple[str, str, str]]] = {}
    for row in TRANSACTIONS:
        by_day.setdefault(row[0], []).append(row)

    for day, rows in sorted(by_day.items()):
        # 布局一：按日分区，按批次切文件（同批次的全部线路写在一个文件里）
        day_dir = ARCHIVE_ROOT / "day_only" / f"day={day}"
        part = 1
        for batch in sorted({r[1] for r in rows}):
            batch_rows = [r for r in rows if r[1] == batch]
            _write_table(batch_rows, day_dir / f"part-{part:05d}.parquet")
            part += 1

        # 布局二：日 + 线路分区，每个 (批次, 线路) 一个文件
        route_dir = ARCHIVE_ROOT / "route_partitioned" / f"day={day}"
        groups = sorted({(r[1], r[3]) for r in rows})
        per_route_part: dict[str, int] = {}
        for batch, route in groups:
            group_rows = [r for r in rows if r[1] == batch and r[3] == route]
            idx = per_route_part.get(route, 0) + 1
            per_route_part[route] = idx
            _write_table(
                group_rows,
                route_dir / f"route={route}" / f"part-{idx:05d}.parquet",
            )

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    (STATIC_DIR / "sample_corrections_valid.csv").write_text(
        VALID_CORRECTION_CSV, encoding="utf-8"
    )
    (STATIC_DIR / "sample_corrections_missing.csv").write_text(MISSING_CSV, encoding="utf-8")
    (STATIC_DIR / "sample_corrections_mismatch.csv").write_text(MISMATCH_CSV, encoding="utf-8")
    (STATIC_DIR / "sample_corrections_duplicate.csv").write_text(DUP_CSV, encoding="utf-8")

    total = sum(1 for _ in ARCHIVE_ROOT.rglob("*.parquet"))
    print(f"样例归档已生成：{ARCHIVE_ROOT}（{total} 个 Parquet 文件，两种布局各一份）")


if __name__ == "__main__":
    build()
