/* 线路取数更正页：上传整份更正、查看生效表、按更正后线路查询明细。 */

const uploadForm = document.getElementById("upload-form");
const uploadBtn = document.getElementById("upload-btn");
const uploadStatus = document.getElementById("upload-status");
const uploadResult = document.getElementById("upload-result");
const activeArea = document.getElementById("active-area");
const archiveInfo = document.getElementById("archive-info");
const queryForm = document.getElementById("query-form");
const queryBtn = document.getElementById("query-btn");
const queryStatus = document.getElementById("query-status");
const queryResult = document.getElementById("query-result");

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function fmt(n) {
  if (n === null || n === undefined) return "-";
  return Number(n).toLocaleString("zh-CN");
}

async function fetchAsFile(url, filename) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`样例文件加载失败：HTTP ${resp.status}`);
  const text = await resp.text();
  return new File([text], filename, { type: "text/csv" });
}

async function loadSample(button, url, filename) {
  try {
    button.disabled = true;
    const file = await fetchAsFile(url, filename);
    const dt = new DataTransfer();
    dt.items.add(file);
    document.getElementById("correctionsFile").files = dt.files;
    if (!document.getElementById("uploadDay").value) {
      document.getElementById("uploadDay").value = "2026-11-20";
    }
    uploadStatus.textContent = `已载入样例 ${filename}。`;
  } catch (err) {
    uploadStatus.textContent = err.message;
  } finally {
    button.disabled = false;
  }
}

document.getElementById("sample-valid-btn").addEventListener("click", (e) =>
  loadSample(e.currentTarget, "/static/sample_corrections_valid.csv", "sample_corrections_valid.csv")
);
document.getElementById("sample-bad-btn").addEventListener("click", (e) =>
  loadSample(e.currentTarget, "/static/sample_corrections_missing.csv", "sample_corrections_missing.csv")
);

// ---------- 上传 ----------

uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(uploadForm);
  uploadBtn.disabled = true;
  uploadStatus.textContent = "校验并保存中…";
  uploadResult.innerHTML = "";
  try {
    const resp = await fetch("/api/corrections", { method: "POST", body: data });
    const payload = await resp.json();
    if (!resp.ok || payload.accepted === false) {
      renderUploadRejection(payload);
      uploadStatus.textContent = "整份退回，未替换任何已生效更正。";
    } else {
      renderUploadAccepted(payload);
      uploadStatus.textContent = "更正已生效。";
      await refreshActive();
    }
  } catch (err) {
    uploadResult.innerHTML = `<div class="alert error"><b>请求失败：</b>${escapeHtml(err.message)}</div>`;
    uploadStatus.textContent = "上传失败。";
  } finally {
    uploadBtn.disabled = false;
  }
});

function renderUploadRejection(payload) {
  const errors = (payload.errors || []).map((e) => `<li>${escapeHtml(e)}</li>`).join("");
  const warnings = (payload.warnings || []).map((w) => `<li>${escapeHtml(w)}</li>`).join("");
  uploadResult.innerHTML = `
    <div class="alert error">
      <b>整份退回：</b>运营日 ${escapeHtml(payload.day || "-")} 的更正未生效，
      ${payload.previous_kept ? "继续使用前一份生效更正。" : "该运营日此前没有生效更正。"}
      <ul style="margin:6px 0 0 18px;padding:0">${errors}</ul>
      ${warnings ? `<div style="margin-top:6px">提示：<ul style="margin:4px 0 0 18px;padding:0">${warnings}</ul></div>` : ""}
    </div>`;
}

function renderUploadAccepted(payload) {
  const warnings = (payload.warnings || []).map((w) => `<li>${escapeHtml(w)}</li>`).join("");
  uploadResult.innerHTML = `
    <div class="alert ok">
      <b>已生效：</b>运营日 ${escapeHtml(payload.day)} 共 ${fmt(payload.count)} 条更正
      （实际改线 ${fmt(payload.changed_count)} 条），
      ${payload.replaced_previous
        ? `已整份替换前一份（前一份 ${fmt(payload.previous_count)} 条，重传不叠加）。`
        : "该运营日此前无生效更正。"}
      来源文件：${escapeHtml(payload.source_file || "-")}，生效时间：${escapeHtml(payload.applied_at || "-")}
      ${warnings ? `<ul style="margin:6px 0 0 18px;padding:0">${warnings}</ul>` : ""}
    </div>`;
}

// ---------- 生效表 ----------

async function refreshActive() {
  const [corrResp, daysResp] = await Promise.all([
    fetch("/api/corrections"),
    fetch("/api/archive/days"),
  ]);
  const corr = await corrResp.json();
  const days = await daysResp.json();
  archiveInfo.textContent =
    `归档布局：${corr.layout === "route_partitioned" ? "日 + 线路分区" : "仅按日分区"}` +
    `（${escapeHtml(days.archive_root)}）；归档运营日：${days.days.map(escapeHtml).join("、") || "无"}`;
  if (!corr.days.length) {
    activeArea.innerHTML = '<div class="alert warn">当前没有任何已生效更正。</div>';
    return;
  }
  activeArea.innerHTML = corr.days
    .map((t) => {
      const rows = t.entries
        .map(
          (e) => `
        <tr>
          <td class="mono">${escapeHtml(e.batch_id)}</td>
          <td class="mono">${escapeHtml(e.seq)}</td>
          <td class="mono">${escapeHtml(e.original_route)}</td>
          <td class="mono">${escapeHtml(e.corrected_route)}</td>
          <td>${e.original_route === e.corrected_route
            ? '<span class="badge all-route">无变化</span>'
            : '<span class="badge split">已改线</span>'}</td>
        </tr>`
        )
        .join("");
      return `
      <div class="query-item open">
        <button type="button" class="query-head">
          <span>运营日 <b class="mono">${escapeHtml(t.day)}</b>
            <span class="meta">${fmt(t.count)} 条（改线 ${fmt(t.changed_count)} 条）·
            来源 ${escapeHtml(t.source_file || "-")} · ${escapeHtml(t.applied_at || "-")}</span>
          </span>
          <span class="arrow">▶</span>
        </button>
        <div class="query-body">
          <div class="table-wrap">
            <table>
              <thead><tr>
                <th class="l">批次号</th><th class="l">流水号</th>
                <th class="l">原线路</th><th class="l">正确线路</th><th class="l">状态</th>
              </tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>
        </div>
      </div>`;
    })
    .join("");
  activeArea.querySelectorAll(".query-head").forEach((head) => {
    head.addEventListener("click", () => head.closest(".query-item").classList.toggle("open"));
  });
}

document.getElementById("refresh-btn").addEventListener("click", () => {
  refreshActive().catch((err) => {
    activeArea.innerHTML = `<div class="alert error">${escapeHtml(err.message)}</div>`;
  });
});

// ---------- 查询 ----------

queryForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(queryForm);
  queryBtn.disabled = true;
  queryStatus.textContent = "查询中…";
  queryResult.innerHTML = "";
  try {
    const resp = await fetch("/api/query", { method: "POST", body: data });
    const payload = await resp.json();
    if (!resp.ok) throw new Error(payload.detail || `HTTP ${resp.status}`);
    renderQuery(payload);
    queryStatus.textContent = "查询完成。";
  } catch (err) {
    queryResult.innerHTML = `<div class="alert error"><b>查询失败：</b>${escapeHtml(err.message)}</div>`;
    queryStatus.textContent = "查询失败。";
  } finally {
    queryBtn.disabled = false;
  }
});

function renderQuery(data) {
  const txnRows = data.transactions
    .map((t) => `
      <tr>
        <td class="mono">${escapeHtml(t.batch_id)}</td>
        <td class="mono">${escapeHtml(t.seq)}</td>
        <td class="mono">${escapeHtml(t.original_route)}</td>
        <td class="mono"><b>${escapeHtml(t.corrected_route)}</b></td>
        <td>${t.changed
          ? '<span class="badge split">更正改入</span>'
          : '<span class="badge day_only">原档线路</span>'}</td>
        <td class="mono" style="white-space:normal;text-align:left">${escapeHtml(t.source_file)}</td>
      </tr>`)
    .join("");
  const opened = data.files_opened.map((f) => `<li class="mono">${escapeHtml(f)}</li>`).join("");
  const skipped = data.files_skipped_by_pruning.length
    ? data.files_skipped_by_pruning.map((f) => `<li class="mono">${escapeHtml(f)}</li>`).join("")
    : "<li>无（当日文件全部打开）</li>";
  queryResult.innerHTML = `
    <div class="alert ok">
      <b>查询结果：</b>运营日 ${escapeHtml(data.day)}、更正后线路
      <b class="mono">${escapeHtml(data.route)}</b> 共 <b class="mono">${fmt(data.count)}</b> 条流水；
      布局：${data.layout === "route_partitioned" ? "日 + 线路分区" : "仅按日分区"}；
      ${data.correction_active
        ? `本次查询基于当日生效更正（${fmt(data.correction_count)} 条）的同一份快照选文件、筛流水。`
        : "当日无生效更正，全部按原档线路取数。"}
    </div>
    <div class="kv">
      <div>
        <b>实际打开的文件（${data.files_opened.length}）：</b>
        <ul style="margin:6px 0 0 18px;padding:0">${opened || "<li>无</li>"}</ul>
      </div>
      <div>
        <b>分区裁剪跳过的文件（${data.files_skipped_by_pruning.length}）：</b>
        <ul style="margin:6px 0 0 18px;padding:0">${skipped}</ul>
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th class="l">批次号</th><th class="l">流水号</th>
          <th class="l">更正前线路</th><th class="l">更正后线路</th>
          <th class="l">归属</th><th class="l">实读文件</th>
        </tr></thead>
        <tbody>${txnRows || '<tr><td colspan="6" class="l">该线路下没有流水</td></tr>'}</tbody>
      </table>
    </div>`;
}

refreshActive().catch((err) => {
  activeArea.innerHTML = `<div class="alert error">${escapeHtml(err.message)}</div>`;
});
