"""线路更正表的持久化存储。

设计要点（对应需求）：

* 每个运营日一份生效更正，落盘为数据目录下的单个 JSON 文件
  （``corrections-YYYY-MM-DD.json``）；写入走“临时文件 + os.replace”原子换表，
  校验、保存都成功后才替换内存与磁盘上的旧表，任何一步失败都继续用前一份。
* 重传不叠加：新文件整份替换该运营日的旧更正，不做合并；
  未在更正 CSV 中列出的流水不建条目，查询时自然沿用原线路。
* 服务重启后从数据目录重新加载全部已生效更正。
* 查询接口通过 :meth:`CorrectionStore.snapshot` 拿到不可变快照，
  单次查询选文件、筛流水始终基于同一份生效更正。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .parser import CorrectionRow

FILE_PREFIX = "corrections-"
FILE_SUFFIX = ".json"
ENCODING = "utf-8"


@dataclass(frozen=True)
class CorrectionEntry:
    batch_id: str
    seq: str
    original_route: str
    corrected_route: str

    @property
    def key(self) -> tuple[str, str]:
        return self.batch_id, self.seq

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "seq": self.seq,
            "original_route": self.original_route,
            "corrected_route": self.corrected_route,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CorrectionEntry":
        return cls(
            batch_id=str(data["batch_id"]),
            seq=str(data["seq"]),
            original_route=str(data["original_route"]),
            corrected_route=str(data["corrected_route"]),
        )


@dataclass(frozen=True)
class DayCorrection:
    """某运营日的一份不可变生效更正表。"""

    day: date
    entries: tuple[CorrectionEntry, ...]
    source_file: str = ""
    applied_at: str = ""
    # (批次号, 流水号) -> 条目；frozen dataclass 的缓存用 object.__setattr__ 初始化
    _index: dict[tuple[str, str], CorrectionEntry] = field(
        default_factory=dict, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        index = {e.key: e for e in self.entries}
        object.__setattr__(self, "_index", index)

    def get(self, batch_id: str, seq: str) -> CorrectionEntry | None:
        return self._index.get((batch_id, seq))

    @property
    def count(self) -> int:
        return len(self.entries)

    @property
    def changed_count(self) -> int:
        """实际改变线路的条目数（原线路 == 正确线路的不计）。"""
        return sum(1 for e in self.entries if e.original_route != e.corrected_route)

    def to_payload(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "count": self.count,
            "changed_count": self.changed_count,
            "source_file": self.source_file,
            "applied_at": self.applied_at,
            "entries": [e.to_dict() for e in self.entries],
        }


class CorrectionStore:
    """线程安全的按运营日更正表存储；内存索引 + JSON 落盘。"""

    def __init__(self, data_dir: str | os.PathLike) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tables: dict[date, DayCorrection] = {}
        self._load_all()

    # ---------- 持久化 ----------

    def _path_for(self, day: date) -> Path:
        return self.data_dir / f"{FILE_PREFIX}{day.isoformat()}{FILE_SUFFIX}"

    def _load_all(self) -> None:
        loaded: dict[date, DayCorrection] = {}
        for path in sorted(self.data_dir.glob(f"{FILE_PREFIX}*{FILE_SUFFIX}")):
            try:
                payload = json.loads(path.read_text(encoding=ENCODING))
                day = date.fromisoformat(payload["day"])
                entries = tuple(
                    CorrectionEntry.from_dict(item) for item in payload["entries"]
                )
                loaded[day] = DayCorrection(
                    day=day,
                    entries=entries,
                    source_file=payload.get("source_file", ""),
                    applied_at=payload.get("applied_at", ""),
                )
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                # 损坏文件不阻断服务启动，跳过并保留在磁盘上供人工排查
                print(f"[correction-store] 跳过无法解析的更正文件 {path.name}: {exc}")
        self._tables = loaded

    def _write_atomic(self, table: DayCorrection) -> None:
        target = self._path_for(table.day)
        tmp = target.with_suffix(".json.tmp")
        payload = {
            "day": table.day.isoformat(),
            "source_file": table.source_file,
            "applied_at": table.applied_at,
            "entries": [e.to_dict() for e in table.entries],
        }
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding=ENCODING
        )
        os.replace(tmp, target)  # 同目录原子换表

    # ---------- 业务操作 ----------

    def replace_day(
        self,
        day: date,
        rows: list[CorrectionRow],
        *,
        source_file: str,
        applied_at: str,
    ) -> DayCorrection:
        """校验通过后调用：整份替换该运营日更正（重传不叠加），先落盘再换内存表。"""
        entries = tuple(
            CorrectionEntry(
                batch_id=r.batch_id,
                seq=r.seq,
                original_route=r.original_route,
                corrected_route=r.corrected_route,
            )
            for r in rows
        )
        table = DayCorrection(
            day=day,
            entries=entries,
            source_file=source_file,
            applied_at=applied_at,
        )
        with self._lock:
            self._write_atomic(table)
            self._tables[day] = table
        return table

    def snapshot(self, day: date) -> DayCorrection | None:
        """取某运营日当前生效更正的不可变快照；查询期间即使重传也不影响本对象。"""
        with self._lock:
            return self._tables.get(day)

    def list_days(self) -> list[DayCorrection]:
        with self._lock:
            return [self._tables[d] for d in sorted(self._tables)]

    def drop_day(self, day: date) -> bool:
        """删除某运营日的生效更正（管理用；失败不影响内存）。"""
        with self._lock:
            path = self._path_for(day)
            existed = path.exists()
            if existed:
                path.unlink()
            self._tables.pop(day, None)
            return existed
