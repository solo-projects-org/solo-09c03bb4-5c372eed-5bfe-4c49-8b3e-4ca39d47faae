/* 全局：试算结果，供查询详情展开时读取 */
let lastResult = null;

const form = document.getElementById("analyze-form");
const resultArea = document.getElementById("result-area");
const submitBtn = document.getElementById("submit-btn");
const formStatus = document.getElementById("form-status");
const sampleBtn = document.getElementById("sample-btn");

const SAMPLE_BATCHES_URL = "/static/sample_batches.csv";
const SAMPLE_QUERIES_URL = "/static/sample_queries.csv";

function fmt(n) {
  if (n === null || n === undefined) return "-";
  return Number(n).toLocaleString("zh-CN");
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function layoutBadge(layout) {
  if (layout === "split") {
    return '<span class="badge split">日 + 线路</span>';
  }
  return '<span class="badge day_only">仅按日</span>';
}

function routeBadge(route) {
  if (!route) return '<span class="badge all-route">全线</span>';
  return `<span class="badge all-route">线路 ${escapeHtml(route)}</span>`;
}

async function fetchAsFile(url, filename) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`示例文件 ${filename} 加载失败：HTTP ${resp.status}`);
  const text = await resp.text();
  return new File([text], filename, { type: "text/csv" });
}

sampleBtn.addEventListener("click", async () => {
  try {
    sampleBtn.disabled = true;
    formStatus.textContent = "正在载入示例…";
    const [batchFile, queryFile] = await Promise.all([
      fetchAsFile(SAMPLE_BATCHES_URL, "sample_batches.csv"),
      fetchAsFile(SAMPLE_QUERIES_URL, "sample_queries.csv"),
    ]);
    const dtB = new DataTransfer();
    dtB.items.add(batchFile);
    document.getElementById("batchesFile").files = dtB.files;
    const dtQ = new DataTransfer();
    dtQ.items.add(queryFile);
    document.getElementById("queriesFile").files = dtQ.files;
    document.getElementById("maxRowsPerFile").value = 100;
    document.getElementById("fileBudget").value = 1000;
    document.getElementById("openCostRows").value = 1000;
    formStatus.textContent = "示例已载入，可直接点击“开始试算”。";
  } catch (err) {
    formStatus.textContent = err.message;
  } finally {
    sampleBtn.disabled = false;
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(form);
  submitBtn.disabled = true;
  formStatus.textContent = "试算中…";
  resultArea.classList.add("hidden");

  try {
    const resp = await fetch("/api/analyze", { method: "POST", body: data });
    const payload = await resp.json();
    if (!resp.ok) {
      throw new Error(payload.detail || `HTTP ${resp.status}`);
    }
    lastResult = payload;
    renderResult(payload);
    formStatus.textContent = "试算完成。";
  } catch (err) {
    resultArea.classList.remove("hidden");
    resultArea.innerHTML = `<div class="alert error"><b>输入有误：</b>${escapeHtml(err.message)}</div>`;
    formStatus.textContent = "试算失败。";
  } finally {
    submitBtn.disabled = false;
  }
});

function renderWarnings(warnings) {
  if (!warnings || warnings.length === 0) return "";
  const items = warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("");
  return `<div class="alert warn"><b>提示：</b><ul style="margin:6px 0 0 18px;padding:0">${items}</ul></div>`;
}

function renderInfeasible(data) {
  return `
    <div class="card">
      <div class="alert error">
        <b>预算不足，无法试算布局。</b><br/>
        所有运营日都只按日分区（文件数最少的可行布局）也至少需要
        <b class="mono">${fmt(data.min_files)}</b> 个文件，
        当前预算只有 <b class="mono">${fmt(data.file_budget)}</b> 个。
        若全部运营日再按线路拆分，最多会到
        <b class="mono">${fmt(data.max_split_files)}</b> 个文件。
      </div>
      ${renderWarnings(data.warnings)}
    </div>`;
}

function renderSummary(data) {
  const s = data.summary;
  const savedCls = s.saved_cost >= 0 ? "saved" : "loss";
  return `
    <div class="card">
      <div class="alert ok">
        <b>试算完成：</b>在文件数预算
        <b class="mono">${fmt(data.params.file_budget)}</b> 内，
        选定布局共需 <b class="mono">${fmt(s.chosen_files)}</b> 个文件
        （剩余预算 <b class="mono">${fmt(s.budget_remaining)}</b>），
        其中 <b class="mono">${fmt(s.split_day_count)}</b> 个运营日采用「日+线路」拆分。
      </div>
      <div class="metric-grid">
        <div class="metric"><div class="k">总代价（全部查询加权）</div><div class="v mono">${fmt(s.total_cost)}</div></div>
        <div class="metric"><div class="k">全部仅按日的基线代价</div><div class="v small mono">${fmt(s.baseline_total_cost)}</div></div>
        <div class="metric"><div class="k">相对基线节省</div><div class="v small mono ${savedCls}">${fmt(s.saved_cost)}</div></div>
        <div class="metric"><div class="k">最少文件数 / 最多文件数</div><div class="v small mono">${fmt(s.min_files)} / ${fmt(s.max_split_files)}</div></div>
        <div class="metric"><div class="k">选定文件数</div><div class="v mono">${fmt(s.chosen_files)}</div></div>
        <div class="metric"><div class="k">覆盖运营日 / 查询数</div><div class="v small mono">${fmt(s.num_days)} / ${fmt(s.num_queries)}</div></div>
        <div class="metric"><div class="k">明细总行数</div><div class="v small mono">${fmt(s.total_rows)}</div></div>
      </div>
      ${renderWarnings(data.input_warnings || [])}
      ${renderWarnings(data.warnings)}
    </div>`;
}

function renderDayTable(data) {
  const rows = data.day_layouts.map((d) => `
    <tr>
      <td class="l mono">${escapeHtml(d.day)}</td>
      <td>${layoutBadge(d.layout)}</td>
      <td class="mono">${fmt(d.day_only_files)}</td>
      <td class="mono">${fmt(d.split_files)}</td>
      <td class="mono"><b>${fmt(d.chosen_files)}</b></td>
      <td class="mono">${fmt(d.extra_files_if_split)}</td>
      <td class="mono">${fmt(d.total_rows)}</td>
    </tr>`).join("");
  const total = data.summary;
  return `
    <div class="card">
      <h2>2. 各运营日文件数与选定布局</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th class="l">运营日</th><th>选定布局</th>
              <th>仅按日文件数</th><th>日+线路文件数</th>
              <th>选定文件数</th><th>拆分多开文件</th><th>当天行数</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
          <tfoot>
            <tr>
              <td class="l"><b>合计</b></td><td></td>
              <td class="mono"><b>${fmt(total.min_files)}</b></td>
              <td class="mono"><b>${fmt(total.max_split_files)}</b></td>
              <td class="mono"><b>${fmt(total.chosen_files)}</b></td>
              <td class="mono"><b>${fmt(total.max_split_files - total.min_files)}</b></td>
              <td class="mono"><b>${fmt(total.total_rows)}</b></td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>`;
}

function renderQueryBlock(data) {
  const items = data.queries.map((q) => {
    const perDayRows = q.per_day.map((p) => `
      <tr>
        <td class="l mono">${escapeHtml(p.day)}</td>
        <td>${layoutBadge(p.layout)}</td>
        <td class="mono">${fmt(p.files)}</td>
        <td class="mono">${fmt(p.scanned_rows)}</td>
        <td class="mono">${fmt(p.day_only_files)}</td>
        <td class="mono">${fmt(p.day_only_scanned_rows)}</td>
      </tr>`).join("");
    return `
      <div class="query-item" data-query-index="${q.index}">
        <button type="button" class="query-head">
          <span>
            查询 #${q.index}：${escapeHtml(q.start_day)} ~ ${escapeHtml(q.end_day)}
            ${routeBadge(q.route)}
            <span class="meta">执行 ${fmt(q.executions)} 次</span>
          </span>
          <span>
            <span class="meta">文件 <b class="mono">${fmt(q.files_open)}</b> ·
            扫描行数 <b class="mono">${fmt(q.scanned_rows)}</b> ·
            代价 <b class="mono">${fmt(q.cost)}</b></span>
            <span class="arrow">▶</span>
          </span>
        </button>
        <div class="query-body">
          <div class="kv">
            <span>命中文件数：<b class="mono">${fmt(q.files_open)}</b></span>
            <span>扫描行数（命中文件整份计入）：<b class="mono">${fmt(q.scanned_rows)}</b></span>
            <span>执行次数：<b class="mono">${fmt(q.executions)}</b></span>
            <span>单次打开折合行数：<b class="mono">${fmt(data.params.open_cost_rows)}</b></span>
            <span>代价 = ${fmt(q.files_open)} × ${fmt(data.params.open_cost_rows)} × ${fmt(q.executions)}
              = <b class="mono">${fmt(q.cost)}</b></span>
          </div>
          <div class="kv">
            <span>对照（全部仅按日）：文件 <b class="mono">${fmt(q.baseline_files_open)}</b> ·
              扫描行数 <b class="mono">${fmt(q.baseline_scanned_rows)}</b> ·
              代价 <b class="mono">${fmt(q.baseline_cost)}</b></span>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th class="l">运营日</th><th>当天布局</th>
                  <th>命中文件数</th><th>当天扫描行数</th>
                  <th>仅按日命中文件</th><th>仅按日扫描行数</th>
                </tr>
              </thead>
              <tbody>${perDayRows || '<tr><td colspan="6" class="l">日期范围内无明细数据</td></tr>'}</tbody>
            </table>
          </div>
        </div>
      </div>`;
  }).join("");

  return `
    <div class="card">
      <h2>3. 查询核对（点击任一查询展开逐日文件数与扫描行数）</h2>
      ${items}
    </div>`;
}

function renderResult(data) {
  resultArea.classList.remove("hidden");
  if (!data.feasible) {
    resultArea.innerHTML = renderInfeasible(data);
    return;
  }
  resultArea.innerHTML =
    renderSummary(data) + renderDayTable(data) + renderQueryBlock(data);

  resultArea.querySelectorAll(".query-head").forEach((head) => {
    head.addEventListener("click", () => {
      head.closest(".query-item").classList.toggle("open");
    });
  });
}
