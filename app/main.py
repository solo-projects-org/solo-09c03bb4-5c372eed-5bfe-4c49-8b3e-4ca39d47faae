"""FastAPI 入口。

保留原有只读试算能力（``/api/analyze``），新增线路取数更正能力：

* ``POST /api/corrections`` 上传某运营日的全量更正 CSV，整份校验通过后原子换表；
* ``GET  /api/corrections`` / ``GET /api/corrections/{day}`` 查看已生效更正；
* ``POST /api/query`` 按运营日 + 更正后线路查明细，返回实读文件与裁剪清单；
* ``GET  /api/archive/days`` 列出归档中有数据的运营日。

数据目录（生效更正 JSON）通过环境变量 ``CORRECTION_DATA_DIR`` 配置；
归档根目录与布局通过 ``ARCHIVE_ROOT``、``ARCHIVE_LAYOUT`` 配置。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .archive import LAYOUTS, ArchiveError, ParquetArchive
from .correction_service import validate_corrections
from .optimizer import optimize
from .parser import parse_batches, parse_corrections, parse_day, parse_queries
from .store import CorrectionStore

BASE_DIR = Path(__file__).resolve().parent
# ARCHIVE_ROOT 指向具体布局目录（其直接子目录为 day=YYYY-MM-DD）
DEFAULT_ARCHIVE_ROOT = BASE_DIR.parent / "data" / "sample_archive" / "day_only"
DEFAULT_DATA_DIR = BASE_DIR.parent / "data" / "corrections"


def create_app(
    *,
    data_dir: str | os.PathLike | None = None,
    archive_root: str | os.PathLike | None = None,
    archive_layout: str | None = None,
) -> FastAPI:
    layout = archive_layout or os.getenv("ARCHIVE_LAYOUT", "day_only")
    if layout not in LAYOUTS:
        raise ValueError(f"ARCHIVE_LAYOUT 只支持 {LAYOUTS}，实际为 {layout!r}")
    root = archive_root or os.getenv("ARCHIVE_ROOT", str(DEFAULT_ARCHIVE_ROOT))
    store = CorrectionStore(data_dir or os.getenv("CORRECTION_DATA_DIR", str(DEFAULT_DATA_DIR)))
    archive = ParquetArchive(root, layout)

    app = FastAPI(
        title="公交刷卡 Parquet 分区试算与线路更正取数",
        description="保留原分区布局试算；新增按运营日上传全量线路更正 CSV、按更正后线路取数",
        version="2.0.0",
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "templates")
    app.state.correction_store = store
    app.state.archive = archive
    app.state.archive_layout = layout
    app.state.archive_root = str(Path(root).resolve())

    # ---------- 页面 ----------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html")

    @app.get("/corrections", response_class=HTMLResponse)
    async def corrections_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "corrections.html")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    # ---------- 原试算接口（保持不变） ----------

    @app.post("/api/analyze")
    async def analyze(
        batches_file: UploadFile = File(..., description="批次明细 CSV"),
        queries_file: UploadFile = File(..., description="历史查询 CSV"),
        max_rows_per_file: int = Form(..., alias="maxRowsPerFile"),
        file_budget: int = Form(..., alias="fileBudget"),
        open_cost_rows: int = Form(..., alias="openCostRows"),
    ):
        if max_rows_per_file <= 0:
            raise HTTPException(status_code=400, detail="单文件行数上限必须为正整数")
        if file_budget <= 0:
            raise HTTPException(status_code=400, detail="全表文件数上限必须为正整数")
        if open_cost_rows <= 0:
            raise HTTPException(status_code=400, detail="每次打开折合扫描行数必须为正整数")

        batches_raw = await batches_file.read()
        queries_raw = await queries_file.read()
        if not batches_raw.strip():
            raise HTTPException(status_code=400, detail="批次明细 CSV 为空")
        if not queries_raw.strip():
            raise HTTPException(status_code=400, detail="历史查询 CSV 为空")

        try:
            batches, batch_warnings = parse_batches(batches_raw)
            queries, query_warnings = parse_queries(queries_raw)
            result = optimize(
                batches=batches,
                queries=queries,
                max_rows_per_file=max_rows_per_file,
                file_budget=file_budget,
                open_cost_rows=open_cost_rows,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        result["input_warnings"] = batch_warnings + query_warnings
        return JSONResponse(result)

    # ---------- 线路更正 ----------

    @app.get("/api/archive/days")
    async def archive_days() -> dict:
        days = archive.list_days()
        return {
            "layout": archive.layout,
            "archive_root": app.state.archive_root,
            "days": [d.isoformat() for d in days],
        }

    @app.get("/api/corrections")
    async def list_corrections() -> dict:
        tables = store.list_days()
        return {
            "layout": archive.layout,
            "days": [t.to_payload() for t in tables],
        }

    @app.get("/api/corrections/{day}")
    async def get_correction(day: str) -> dict:
        try:
            day_obj = parse_day(day)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        table = store.snapshot(day_obj)
        if table is None:
            return {
                "day": day_obj.isoformat(),
                "active": False,
                "count": 0,
                "entries": [],
            }
        payload = table.to_payload()
        payload["active"] = True
        return payload

    @app.post("/api/corrections")
    async def upload_correction(
        day: str = Form(..., alias="day"),
        corrections_file: UploadFile = File(..., alias="corrections_file"),
    ):
        try:
            day_obj = parse_day(day)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        raw = await corrections_file.read()
        if not raw.strip():
            raise HTTPException(status_code=400, detail="更正 CSV 为空")

        # 1) 解析（含同批同号多条更正的硬校验，失败即整份退回）
        try:
            rows, parse_warnings = parse_corrections(raw)
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "accepted": False,
                    "day": day_obj.isoformat(),
                    "errors": [str(exc)],
                    "warnings": [],
                    "previous_kept": store.snapshot(day_obj) is not None,
                },
            )

        # 2) 与当日原档对账（找不到流水 / 原线路对不上 → 整份退回，不动旧表）
        try:
            report = validate_corrections(
                rows, archive, day_obj, parse_warnings=parse_warnings
            )
        except ArchiveError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "accepted": False,
                    "day": day_obj.isoformat(),
                    "errors": [str(exc)],
                    "warnings": parse_warnings,
                    "previous_kept": store.snapshot(day_obj) is not None,
                },
            )
        if not report.ok:
            return JSONResponse(
                status_code=400,
                content={
                    "accepted": False,
                    "day": day_obj.isoformat(),
                    "errors": report.errors,
                    "warnings": report.warnings,
                    "previous_kept": store.snapshot(day_obj) is not None,
                },
            )

        # 3) 校验成功：原子落盘 + 换表（重传整份替换，不叠加）
        previous = store.snapshot(day_obj)
        table = store.replace_day(
            day_obj,
            rows,
            source_file=corrections_file.filename or "",
            applied_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        return JSONResponse(
            {
                "accepted": True,
                "day": day_obj.isoformat(),
                "count": table.count,
                "changed_count": table.changed_count,
                "replaced_previous": previous is not None,
                "previous_count": previous.count if previous else 0,
                "source_file": table.source_file,
                "applied_at": table.applied_at,
                "warnings": report.warnings,
                "entries": [e.to_dict() for e in table.entries],
            }
        )

    @app.post("/api/query")
    async def query_detail(
        day: str = Form(..., alias="day"),
        route: str = Form(..., alias="route"),
    ):
        try:
            day_obj = parse_day(day)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        route = (route or "").strip()
        if not route:
            raise HTTPException(status_code=400, detail="查询线路不能为空")

        # 单次查询：选文件与筛流水共用这一份不可变快照
        snapshot = store.snapshot(day_obj)
        try:
            result = archive.query_day_route(day_obj, route, snapshot)
        except ArchiveError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        payload = result.to_payload()
        payload["correction_active"] = snapshot is not None
        payload["correction_count"] = snapshot.count if snapshot else 0
        return JSONResponse(payload)

    return app


app = create_app()
