"""FastAPI 入口：网页 + /api/analyze 试算接口。只做试算，不写任何数据文件。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .optimizer import optimize
from .parser import parse_batches, parse_queries

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="公交刷卡 Parquet 分区布局试算",
    description="上传批次明细与历史查询 CSV，在文件数预算内寻找总代价最低的逐日分区布局",
    version="1.0.0",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


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
