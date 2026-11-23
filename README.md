# 公交刷卡明细 · Parquet 分区试算与线路更正取数

两个互不干扰的能力，共用一个 FastAPI 服务：

1. **分区布局试算**（原有，完整保留）：上传两张 CSV，在“全表文件数上限”预算内，
   找出每个运营日应选择「仅按日分区」还是「日 + 线路分区」，使历史查询的总扫描代价最低。
2. **线路取数更正**（新增）：清分发现刷卡流水挂错线路时，**不改写归档 Parquet**，
   按运营日上传全量更正 CSV；校验、保存都成功才原子换表，网页按更正后线路查明细，
   并展示每次查询实际打开 / 被分区裁剪跳过的文件。

---

## 一、分区布局试算（原功能，规则不变）

### 试算规则（与需求逐条对应）

1. 每个运营日只有两种候选布局：
   - **仅按日（day_only）**：当天数据只按日分区；
   - **日 + 线路（split）**：日分区下再按线路拆分。
2. 文件按**单文件行数上限**装满一份再开下一份：对每个 `(入仓批次, 线路)` 的行数
   独立做 `ceil(行数 / 上限)` 切分；**不跨批次拼文件**。
   - day_only 的文件数 = 各批次当天合计行数各自切分后求和；
   - split 的文件数 = 各 `(批次, 线路)` 切分后求和（因此可能多开小文件）。
3. 查询只按分区裁剪，**命中的文件整份计入扫描**，扫描行数按文件承载的全部行数计：
   - day_only：任何查询都扫当天全部文件、全部行数；
   - split + 指定线路：只扫该线路文件；
   - split + 线路留空（全线查询）：仍扫当天全部文件。
4. 查询成本 = 命中文件数 × 每次打开折合扫描行数 × 执行次数，按执行次数加权汇总。
5. 布局选择建模为 **0/1 背包**：每个运营日拆分布局的“重量 = 相对日布局多开的文件数”，
   “价值 = 全部覆盖查询因此节省的代价”，在 `文件数预算 − 最少文件数` 的容量内求最大节省。
   - 多开文件数为 0 且拆分有益的日子免费拆分；
   - 拆分反而增加代价的日子永远不拆；
   - **预算 < 最少文件数（全部仅按日）时，不返回布局，只报至少需要多少文件**。

### 试算输入 CSV 格式

| 文件 | 必需列（支持常见英文别名） |
| --- | --- |
| 批次明细 | 入仓批次号、运营日、线路、行数 |
| 历史查询 | 起始运营日、截止运营日、线路（**留空 = 查全线**）、执行次数 |

- 编码：UTF-8（带 BOM 亦可），失败自动回退 GBK；日期支持 `YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD`。
- 同 `(批次, 运营日, 线路)` 的重复行会自动合并行数并给出提示。
- 网页点“载入示例 CSV”可直接用内置样例体验（`app/static/sample_*.csv`）。

### 试算接口

- `GET /`：试算网页（上传两张 CSV + 三个参数），与新增的线路取数页互相导航。
- `POST /api/analyze`：`multipart/form-data`，字段
  `batches_file`、`queries_file`、`maxRowsPerFile`、`fileBudget`、`openCostRows`，
  返回完整试算 JSON；输入错误返回 HTTP 400 与中文原因。

---

## 二、线路取数更正（新增）

### 业务规则（与需求逐条对应）

1. **归档 Parquet 永不改写**：更正只保存在独立的更正表里，查询时做内存投影。
2. 网页 `GET /corrections`「线路取数」页：选运营日，上传该日**全量**更正 CSV，
   必需列：**批次号、流水号、原线路、正确线路**（同样支持英文表头别名与 GBK 回退）。
3. 按 `(批次号, 流水号)` 对**当日原档**逐条对账，出现任一情况即**整份退回、继续用前一份**：
   - 找不到流水；
   - 更正 CSV 声明的原线路与原档线路对不上；
   - 同批同号在 CSV 里出现多条更正（解析阶段直接拦截）。
4. **校验、保存都成功才换表**：落盘走“临时文件 + `os.replace`”原子替换，
   失败时内存与磁盘上的旧表都不受影响。
5. **重传不叠加**：新 CSV 整份替换该运营日的旧更正；
   **未列出的流水不建条目，查询时沿用原线路**。
6. **重启后仍用已生效更正**：每个运营日一份 JSON 落在数据目录
   （`CORRECTION_DATA_DIR`，Docker 里是命名卷），服务启动自动加载。
7. 网页按运营日 + 线路查明细，**按更正后线路取数**，逐行列出
   批次号、流水号、更正前线路、更正后线路、归属（原档/更正改入）及**实读文件**。
8. 分区裁剪：
   - **按日分区（day_only）**：查询只读当日目录下的文件，读入后按更正后线路筛；
   - **日 + 线路分区（route_partitioned）**：只打开「所查线路目录」+
     「有流水改入该线路的原线路目录」，其余线路目录整目录裁剪，页面同时给出跳过清单。
9. **单次查询选文件与筛流水共用同一份不可变更正快照**（`DayCorrection` 冻结对象）：
   查询进行中即使有人重传换表，本次结果也不会前后混用两份更正。

### 接口

| 方法 路径 | 作用 |
| --- | --- |
| `GET /corrections` | 线路取数更正网页 |
| `POST /api/corrections` | 表单字段 `day` + `corrections_file`；成功返回 `accepted=true` 与条目；整份退回返回 400、`accepted=false`、全部错误及 `previous_kept` |
| `GET /api/corrections` | 列出全部已生效更正（按运营日） |
| `GET /api/corrections/{day}` | 查单日生效表（无表返回 `active=false`） |
| `POST /api/query` | 表单字段 `day` + `route`，返回逐流水明细、实读文件、裁剪跳过文件 |
| `GET /api/archive/days` | 归档布局、根目录与有数据的运营日 |
| `GET /healthz` | 健康检查 |

### 本地样例（两种分区布局，真实 Parquet）

`scripts/build_sample_archive.py` 用 pyarrow 生成数据行完全相同、物理布局不同的两份归档：

```
data/sample_archive/
  day_only/day=2026-11-20/part-0000x.parquet            # 按日分区
  route_partitioned/day=2026-11-20/route=R1/part-*.parquet  # 日+线路分区
```

配套静态样例（网页按钮可直接载入）：

- `app/static/sample_corrections_valid.csv`：3 条有效更正
  （T1003、T2002 从 R2 改入 R1；T1005 从 R3 改入 R2，覆盖改入/改出两个方向）；
- `sample_corrections_missing.csv`（找不到流水）、
  `sample_corrections_mismatch.csv`（原线路对不上）、
  `sample_corrections_duplicate.csv`（同批同号多条）三份必退回样例。

重新生成样例：

```bash
python scripts/build_sample_archive.py
```

---

## 三、启动方式（Docker Compose）

```bash
# 默认按日分区样例
docker compose up --build
# 浏览器打开 http://localhost:8000 （试算页 /），线路取数页 /corrections

# 切换为日 + 线路分区样例
ARCHIVE_LAYOUT=route_partitioned docker compose up --build
```

- 容器以非 root 用户运行，带 `/healthz` 健康检查；
- 生效更正写入命名卷 `correction-data`（容器内 `/data/corrections`），
  容器重启 / 重建后自动恢复；
- 环境变量：`ARCHIVE_LAYOUT`（`day_only` / `route_partitioned`）、
  `ARCHIVE_ROOT`（具体布局目录，其直接子目录为 `day=*`）、`CORRECTION_DATA_DIR`。

本地裸跑（无 Docker 时）：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python scripts/build_sample_archive.py     # 首次需要，生成样例 Parquet
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 切换布局：ARCHIVE_LAYOUT=route_partitioned \
#   ARCHIVE_ROOT=$(pwd)/data/sample_archive/route_partitioned uvicorn app.main:app
```

---

## 四、自测结果

`pytest tests/ -q` → **42 passed**：

- 原试算 19 个测试全部保留通过（文件切分、背包、CSV 解析、`/api/analyze` 全链路）；
- 新增 23 个测试（`tests/test_corrections.py`）覆盖：
  - 更正 CSV 解析：中英文表头、缺列/空字段、同批同号重复拦截、无变化行提示；
  - 对账校验：找不到流水、原线路对不上聚合报错，未知运营日报错；
  - 存储：原子换表、重传整份替换不叠加、重开存储（模拟重启）后内容一致、
    旧快照不受换表影响；
  - 两种布局取数：day_only 当日文件全读；route_partitioned 只开所查线路 +
    改入来源线路目录并跳过其余目录；改入/改出/改空三类结果；未列流水沿用原线路；
  - API：有效上传、三类 400 退回且 `previous_kept` 正确、重传替换、
    重启后生效表恢复、两种布局端到端、原 `/api/analyze` 数值不变（11 文件 / 308000）。

另做了真实 HTTP 冒烟（uvicorn + curl，两种布局各起一个实例、共用同一更正目录）：

- 三份坏样例分别返回“找不到流水 / 原线路对不上 / 同批同号多条”，均整份退回；
- 有效样例生效后，查 R1 得到 5 条（含 T1003、T2002 两条改入，附实读文件）；
  查 R2 改出两条但收到 R3 改入的 T1005；查 R3 为空；
- 坏文件在有效表之后上传：`previous_kept=true`，前一份 3 条仍生效；
- 重传单条新文件：`count=1`、`replaced_previous=true`、磁盘当日仅一份 JSON 无 tmp 残留；
- 杀掉进程后用同一数据目录重启：生效表与查询结果自动恢复；
- route_partitioned 实例查 R1 实开 `route=R1`+`route=R2` 四个文件、跳过 `route=R3`；
- 原试算接口 HTTP 实测仍返回 11 文件 / 代价 308000。

## 五、目录结构

```
app/
  main.py               FastAPI 应用工厂与路由（试算 + 更正 + 取数 + 健康检查）
  parser.py             三类 CSV 的解析、表头别名、编码回退与硬校验
  optimizer.py          分区文件数、查询裁剪成本、0/1 背包选布局（未改动）
  archive.py            ParquetArchive：两种布局的目录裁剪、当日索引、更正投影取数
  store.py              CorrectionStore：按运营日原子换表、JSON 持久化、不可变快照
  correction_service.py 更正 CSV 与当日原档对账（整份退回规则）
  templates/            index.html（试算）、corrections.html（线路取数）
  static/               样式、前端逻辑、试算样例与 4 份更正样例 CSV
scripts/build_sample_archive.py  生成两种布局的真实 Parquet 样例归档
data/sample_archive/    生成产物：day_only 与 route_partitioned 两份样例
tests/                  test_app.py（原 19 项）+ test_corrections.py（新 23 项）
Dockerfile / docker-compose.yml / requirements.txt
```

## 六、待办 / 已知边界

- 本机环境未安装 Docker，Compose 文件仅做了 YAML 语法与镜像内依赖（PyPI 200）校验，
  **未实际 `docker compose up` 跑过**，需在有 Docker 的环境再验一次；
  应用本身已用 uvicorn + curl 做过两种布局的真实 HTTP 冒烟。
- 样例归档为便于观察裁剪，每个 `(批次,线路)` 只写一个小 Parquet；
  生产环境多文件场景下裁剪逻辑相同（按目录聚合全部 `*.parquet`）。
- 更正表按运营日整体加载到内存，面向“单日全量更正”的规模设计；
  超大规模（数十万行/日）建议改为 SQLite 等嵌入式存储，接口层无需变动。
- 上传文件大小未做显式上限，依赖反代/网关限制；如需公网部署建议加请求体限制与鉴权。
- 试算背包数组 DP 容量阈值固定为 5 万，更大预算自动切稀疏字典 DP（已单测一致性）。
