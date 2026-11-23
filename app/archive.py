"""本地 Parquet 归档的读取与分区裁剪。

归档只代表入仓后的原始流水，**永不改写**；线路更正只发生在查询时的内存投影里。

支持两种物理分区布局（样例与生产各自选一种，目录互不相干）：

``day_only``（按日分区，当天文件全在一个目录）::

    <root>/day=YYYY-MM-DD/part-00001.parquet

``route_partitioned``（日 + 线路分区，目录里带线路值）::

    <root>/day=YYYY-MM-DD/route=R1/part-00001.parquet

两种文件内部都带完整列：批次号 ``batch_id``、流水号 ``seq``、线路 ``route``。
查询按“更正后线路”取数：

* day_only：只打开当日目录下的文件（按日分区就读当日文件），读入后再按更正后线路筛；
* route_partitioned：打开“所查线路目录”以及“有流水改入该线路的原线路目录”
  （即更正前线路目录），其余线路目录直接裁剪掉。

单次查询拿到的文件清单与逐流水筛选使用同一个 :class:`~app.store.DayCorrection`
不可变快照，保证选文件和筛流水基于同一份生效更正。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

import pyarrow.parquet as pq

from .store import DayCorrection

BATCH_COL = "batch_id"
SEQ_COL = "seq"
ROUTE_COL = "route"
REQUIRED_COLUMNS = (BATCH_COL, SEQ_COL, ROUTE_COL)

LAYOUT_DAY_ONLY = "day_only"
LAYOUT_ROUTE = "route_partitioned"
LAYOUTS = (LAYOUT_DAY_ONLY, LAYOUT_ROUTE)


class ArchiveError(ValueError):
    """归档目录缺失或结构不符合约定。"""


@dataclass(frozen=True)
class Transaction:
    batch_id: str
    seq: str
    original_route: str
    corrected_route: str
    source_file: str
    changed: bool


@dataclass(frozen=True)
class QueryResult:
    day: date
    route: str
    layout: str
    transactions: tuple[Transaction, ...]
    files_opened: tuple[str, ...]
    files_skipped_by_pruning: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.transactions)

    def to_payload(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "route": self.route,
            "layout": self.layout,
            "count": self.count,
            "files_opened": list(self.files_opened),
            "files_skipped_by_pruning": list(self.files_skipped_by_pruning),
            "transactions": [
                {
                    "batch_id": t.batch_id,
                    "seq": t.seq,
                    "original_route": t.original_route,
                    "corrected_route": t.corrected_route,
                    "changed": t.changed,
                    "source_file": t.source_file,
                }
                for t in self.transactions
            ],
        }


class ParquetArchive:
    def __init__(self, root: str | os.PathLike, layout: str) -> None:
        if layout not in LAYOUTS:
            raise ArchiveError(f"未知归档布局：{layout}，支持 {LAYOUTS}")
        self.root = Path(root)
        self.layout = layout

    # ---------- 目录结构 ----------

    def _day_dir(self, day: date) -> Path:
        return self.root / f"day={day.isoformat()}"

    def list_days(self) -> list[date]:
        if not self.root.exists():
            return []
        days = []
        for path in self.root.glob("day=*"):
            if not path.is_dir():
                continue
            try:
                days.append(date.fromisoformat(path.name.removeprefix("day=")))
            except ValueError:
                continue
        return sorted(days)

    def _all_day_files(self, day_dir: Path) -> list[Path]:
        return sorted(p for p in day_dir.rglob("*.parquet") if p.is_file())

    def _route_dirs(self, day_dir: Path) -> dict[str, Path]:
        """route_partitioned 布局：route=线路 -> 目录。"""
        out: dict[str, Path] = {}
        for path in sorted(day_dir.glob("route=*")):
            if path.is_dir():
                out[path.name.removeprefix("route=")] = path
        return out

    # ---------- 校验用：读取当日全部流水 ----------

    def load_day_index(self, day: date) -> dict[tuple[str, str], set[str]]:
        """读当日全部 Parquet，返回 (批次号, 流水号) -> 原线路集合。

        更正上传校验用：必须能按批次号 + 流水号在“当日原档”里找到流水，
        且原线路与更正 CSV 声明的对得上。
        """
        day_dir = self._day_dir(day)
        if not day_dir.is_dir():
            raise ArchiveError(f"归档中找不到运营日 {day.isoformat()} 的分区目录")
        files = self._all_day_files(day_dir)
        if not files:
            raise ArchiveError(f"运营日 {day.isoformat()} 的归档目录下没有 Parquet 文件")
        index: dict[tuple[str, str], set[str]] = {}
        for path in files:
            table = pq.read_table(path, columns=list(REQUIRED_COLUMNS))
            batch_ids = table.column(BATCH_COL).to_pylist()
            seqs = table.column(SEQ_COL).to_pylist()
            routes = table.column(ROUTE_COL).to_pylist()
            for batch_id, seq, route in zip(batch_ids, seqs, routes):
                index.setdefault((str(batch_id), str(seq)), set()).add(str(route))
        return index

    # ---------- 查询用：分区裁剪 + 更正投影 ----------

    def _select_files(
        self, day: date, route: str, snapshot: Optional[DayCorrection]
    ) -> tuple[list[Path], list[Path]]:
        """按布局与生效更正决定本次查询实际打开 / 被裁剪跳过的文件。"""
        day_dir = self._day_dir(day)
        all_files = self._all_day_files(day_dir)
        if self.layout == LAYOUT_DAY_ONLY:
            # 按日分区：当日文件都要打开，靠读入后的投影筛线路
            return all_files, []

        # route_partitioned：只开所查线路目录 + 有流水改入的原线路目录
        route_dirs = self._route_dirs(day_dir)
        wanted_routes = {route}
        if snapshot is not None:
            for entry in snapshot.entries:
                if entry.corrected_route == route:
                    wanted_routes.add(entry.original_route)
        opened: list[Path] = []
        skipped: list[Path] = []
        for path in all_files:
            parent_route = path.parent.name.removeprefix("route=")
            if parent_route in wanted_routes and parent_route in route_dirs:
                opened.append(path)
            else:
                skipped.append(path)
        return opened, skipped

    def query_day_route(
        self,
        day: date,
        route: str,
        snapshot: Optional[DayCorrection],
    ) -> QueryResult:
        """按更正后线路取当日明细；返回逐流水（含实读文件）与文件裁剪清单。"""
        day_dir = self._day_dir(day)
        if not day_dir.is_dir():
            raise ArchiveError(f"归档中找不到运营日 {day.isoformat()} 的分区目录")
        opened, skipped = self._select_files(day, route, snapshot)

        txns: list[Transaction] = []
        for path in opened:
            table = pq.read_table(path, columns=list(REQUIRED_COLUMNS))
            batch_ids = table.column(BATCH_COL).to_pylist()
            seqs = table.column(SEQ_COL).to_pylist()
            routes = table.column(ROUTE_COL).to_pylist()
            rel_file = str(path.relative_to(self.root))
            for batch_id, seq, original_route in zip(batch_ids, seqs, routes):
                batch_id, seq, original_route = (
                    str(batch_id),
                    str(seq),
                    str(original_route),
                )
                entry = snapshot.get(batch_id, seq) if snapshot is not None else None
                if entry is None:
                    # 未列出的流水沿用原线路
                    corrected_route = original_route
                else:
                    corrected_route = entry.corrected_route
                if corrected_route != route:
                    continue  # 按更正后线路取数
                txns.append(
                    Transaction(
                        batch_id=batch_id,
                        seq=seq,
                        original_route=original_route,
                        corrected_route=corrected_route,
                        source_file=rel_file,
                        changed=original_route != corrected_route,
                    )
                )

        def rel(paths: Iterable[Path]) -> tuple[str, ...]:
            return tuple(str(p.relative_to(self.root)) for p in paths)

        return QueryResult(
            day=day,
            route=route,
            layout=self.layout,
            transactions=tuple(txns),
            files_opened=rel(opened),
            files_skipped_by_pruning=rel(skipped),
        )
