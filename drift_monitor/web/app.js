/* Feature drift dashboard — vanilla JS, no dependencies, SVG charts. */
"use strict";

const state = {
  dimensions: null, windows: [], baselines: [], alerts: [], runs: [],
  distribution: null,
};
const $ = (sel) => document.querySelector(sel);
const cssVar = (name) => getComputedStyle(document.body).getPropertyValue(name).trim();
const fmt = (v, digits = 3) =>
  v === null || v === undefined ? "—" : Number(v).toFixed(digits);
const pct = (v) => (v === null || v === undefined ? "—" : (v * 100).toFixed(1) + "%");

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
const post = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body) });
const patch = (path, body) =>
  api(path, { method: "PATCH", headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body) });

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 3200);
}

// ------------------------------------------------------------------ bootstrap
async function init() {
  bindStaticActions();
  await refreshAll();
  $("#f-feature").addEventListener("change", onScopeChange);
  $("#f-segment").addEventListener("change", onScopeChange);
  $("#f-version").addEventListener("change", onScopeChange);
  $("#f-window").addEventListener("change", loadDistribution);
  $("#f-status").addEventListener("change", renderAlerts);
}

async function refreshAll() {
  state.dimensions = await api("/api/dimensions");
  state.windows = await api("/api/windows");
  state.baselines = await api("/api/baselines");
  state.runs = await api("/api/runs");
  await refreshAlerts();
  populateFilters();
  renderKpis();
  renderBaselines();
  renderRuns();
  await loadDistribution();
}

async function refreshAlerts() {
  state.alerts = await api("/api/alerts");
  renderAlerts();
}

// ------------------------------------------------------------------ filters
function populateFilters() {
  const sel = (id, rows, labelKey, current) => {
    const el = $(id);
    const keep = current || el.value;
    el.innerHTML = rows.map((r) =>
      `<option value="${r[labelKey]}"${r[labelKey] === keep ? " selected" : ""}>${r[labelKey]}</option>`
    ).join("");
    if (!el.value && rows.length) el.value = rows[0][labelKey];
  };
  sel("#f-feature", state.dimensions.features, "name");
  sel("#f-segment", state.dimensions.segments, "name");
  sel("#f-version", state.dimensions.business_versions, "name");
  populateWindowFilter();
}

function scopeRows() {
  const feature = $("#f-feature").value;
  const segment = $("#f-segment").value;
  const version = $("#f-version").value;
  return state.windows.filter((w) =>
    w.feature === feature && w.segment === segment &&
    w.business_version === version);
}

function populateWindowFilter() {
  const rows = scopeRows();
  const el = $("#f-window");
  const keep = el.value;
  el.innerHTML = rows.map((w) =>
    `<option value="${w.window_start}"${w.window_start === keep ? " selected" : ""}>${w.window_start.replace("T", " ").slice(0, 16)}</option>`
  ).join("");
  if (!el.value && rows.length) el.value = rows[0].window_start;
}

async function onScopeChange() {
  populateWindowFilter();
  await loadDistribution();
  renderAlerts();
}

function currentScope() {
  return {
    feature: $("#f-feature").value,
    segment: $("#f-segment").value,
    business_version: $("#f-version").value,
    window: $("#f-window").value,
  };
}

// ------------------------------------------------------------------ KPIs
function renderKpis() {
  const open = state.alerts.filter((a) => a.status === "open");
  const critical = state.alerts.filter((a) => a.severity === "critical" && a.status !== "resolved");
  const empty = state.windows.filter((w) => w.empty_window).length;
  const dirty = state.windows.filter((w) => w.dirty).length;
  const tiles = [
    ["告警总数", state.alerts.length, `已解决 ${state.alerts.filter(a => a.status === "resolved").length}`],
    ["未处理 open", open.length, "需要关注"],
    ["critical 告警", critical.length, "最高严重级别"],
    ["空分群窗口", empty, "人群无有效样本"],
    ["待重评脏窗口", dirty, "数据修订后受影响"],
  ];
  $("#kpi-row").innerHTML = tiles.map(([label, value, delta]) =>
    `<div class="kpi"><div class="label">${label}</div>
     <div class="value">${value}</div><div class="delta">${delta}</div></div>`
  ).join("");
}

// ------------------------------------------------------------------ distribution
async function loadDistribution() {
  const scope = currentScope();
  if (!scope.window) {
    state.distribution = null;
    renderDistribution();
    return;
  }
  const qs = new URLSearchParams(scope).toString();
  state.distribution = await api(`/api/distribution?${qs}`);
  renderDistribution();
}

function renderDistribution() {
  const d = state.distribution;
  const chartEl = $("#dist-chart");
  const legendEl = $("#dist-legend");
  const chipsEl = $("#dist-metrics");
  const tableEl = $("#dist-table");
  $("#dist-context").textContent = d
    ? `${d.feature} · ${d.segment} · ${d.business_version} · ${d.window_start.replace("T", " ").slice(0, 16)}`
    : "";

  if (!d) {
    chartEl.innerHTML = `<p class="hint">当前筛选条件下没有窗口数据。</p>`;
    legendEl.innerHTML = ""; chipsEl.innerHTML = ""; tableEl.innerHTML = "";
    $("#empty-badge").hidden = true;
    return;
  }

  if (d.current.empty_window) {
    $("#empty-badge").hidden = false;
    chartEl.innerHTML = `<div style="padding:60px 20px;text-align:center">
      <p style="font-size:15px;font-weight:600;color:var(--status-info)">分群为空</p>
      <p class="hint">该人群在此时间窗口没有任何有效观测，评估引擎会记录 info 级告警而非误报漂移。</p></div>`;
    legendEl.innerHTML = ""; chipsEl.innerHTML = ""; tableEl.innerHTML = "";
    return;
  }
  $("#empty-badge").hidden = true;

  const isNumeric = d.dtype === "numeric";
  legendEl.innerHTML = `
    <span class="item"><span class="swatch" style="background:var(--series-1)"></span>基线${d.baseline ? ` v${d.baseline.version}` : ""}</span>
    <span class="item"><span class="swatch" style="background:var(--series-2)"></span>当前窗口</span>`;

  const pairs = isNumeric ? numericPairs(d) : categoricalPairs(d);
  chartEl.innerHTML = groupedBarSvg(pairs, isNumeric);
  bindChartTooltip(chartEl, pairs);
  renderMetricChips(d);
  renderDistributionTable(pairs, isNumeric);
}

function numericPairs(d) {
  const edges = d.baseline?.histogram_edges || d.current.histogram_edges;
  const baseCounts = d.baseline?.histogram_counts || [];
  const curCounts = d.current.histogram_counts || [];
  const baseTotal = baseCounts.reduce((a, b) => a + b, 0) || 1;
  const curTotal = curCounts.reduce((a, b) => a + b, 0) || 1;
  return edges.slice(0, -1).map((lo, i) => ({
    label: `${fmt(lo, 1)}~${fmt(edges[i + 1], 1)}`,
    baseline: (baseCounts[i] || 0) / baseTotal,
    current: (curCounts[i] || 0) / curTotal,
  }));
}

function categoricalPairs(d) {
  const base = d.baseline?.category_counts || {};
  const cur = d.current.category_counts || {};
  const keys = [...new Set([...Object.keys(base), ...Object.keys(cur)])].sort();
  const bt = Object.values(base).reduce((a, b) => a + b, 0) || 1;
  const ct = Object.values(cur).reduce((a, b) => a + b, 0) || 1;
  return keys.map((k) => ({
    label: k, baseline: (base[k] || 0) / bt, current: (cur[k] || 0) / ct,
  }));
}

// ---------------- grouped bar SVG (hover tooltip + axis + grid)
function groupedBarSvg(pairs) {
  const W = 720, H = 320, M = { top: 18, right: 16, bottom: 64, left: 52 };
  const iw = W - M.left - M.right, ih = H - M.top - M.bottom;
  const maxV = Math.max(0.001, ...pairs.flatMap((p) => [p.baseline, p.current]));
  const groupW = iw / pairs.length;
  const barW = Math.max(3, groupW * 0.34);
  const y = (v) => M.top + ih - (v / maxV) * ih;
  const ticks = 4;
  const grid = Array.from({ length: ticks + 1 }, (_, i) => {
    const val = (maxV / ticks) * i, yy = y(val);
    return `<line class="gridline" x1="${M.left}" x2="${W - M.right}" y1="${yy}" y2="${yy}"/>
            <text x="${M.left - 8}" y="${yy + 4}" text-anchor="end" class="axis" font-size="11">${(val * 100).toFixed(0)}%</text>`;
  }).join("");
  const bars = pairs.map((p, i) => {
    const gx = M.left + groupW * i + groupW / 2;
    const x1 = gx - barW - 1, x2 = gx + 1;
    const h1 = ih - (y(p.baseline) - M.top);
    const h2 = ih - (y(p.current) - M.top);
    return `<g class="bar-group" data-label="${p.label}"
        data-baseline="${(p.baseline * 100).toFixed(1)}%"
        data-current="${(p.current * 100).toFixed(1)}%">
      <rect class="bar-baseline" x="${x1}" y="${y(p.baseline)}" width="${barW}" height="${Math.max(0, h1)}" rx="2"/>
      <rect class="bar-current" x="${x2}" y="${y(p.current)}" width="${barW}" height="${Math.max(0, h2)}" rx="2"/>
      <text x="${gx}" y="${H - M.bottom + 16}" text-anchor="middle" class="axis" font-size="10"
        transform="rotate(-28 ${gx} ${H - M.bottom + 16})">${p.label}</text>
    </g>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H}" role="group" aria-label="分组柱状图">
    ${grid}
    <line x1="${M.left}" x2="${M.left}" y1="${M.top}" y2="${M.top + ih}" class="axis"/>
    <line x1="${M.left}" x2="${W - M.right}" y1="${M.top + ih}" y2="${M.top + ih}" class="axis"/>
    ${bars}
  </svg>
  <div id="chart-tooltip" class="chart-tooltip" hidden
     style="position:fixed;pointer-events:none;background:var(--text-primary);color:var(--surface-1);
            padding:6px 10px;border-radius:8px;font-size:12px;z-index:40"></div>`;
}

function bindChartTooltip(el, pairs) {
  const tip = el.querySelector("#chart-tooltip");
  el.querySelectorAll(".bar-group").forEach((g) => {
    g.addEventListener("mousemove", (ev) => {
      tip.hidden = false;
      tip.innerHTML = `<b>${g.dataset.label}</b><br>
        基线占比 ${g.dataset.baseline}<br>当前占比 ${g.dataset.current}`;
      tip.style.left = `${ev.clientX + 12}px`;
      tip.style.top = `${ev.clientY + 12}px`;
    });
    g.addEventListener("mouseleave", () => { tip.hidden = true; });
  });
}

function renderMetricChips(d) {
  const drift = d.drift || {};
  const chips = [
    ["PSI", fmt(drift.psi), drift.psi >= 1.0],
    ["JS 散度", fmt(drift.js_divergence), drift.js_divergence >= 0.15],
    ["均值 Z 值", fmt(drift.mean_z, 2), Math.abs(drift.mean_z || 0) >= 3],
    ["缺失率", pct(d.current.missing_rate), false],
    ["缺失率 Δ", pct(drift.missing_rate_delta), (drift.missing_rate_delta || 0) >= 0.15],
    ["异常值率", pct(d.current.outlier_rate), false],
    ["异常值率 Δ", pct(drift.outlier_rate_delta), (drift.outlier_rate_delta || 0) >= 0.1],
    ["样本量 n", d.current.n, false],
  ];
  $("#dist-metrics").innerHTML = chips.map(([label, val, breach]) =>
    `<span class="chip ${breach ? "breach" : ""}">${label} <b>${val}</b>${breach ? " ⚠" : ""}</span>`
  ).join("");
}

function renderDistributionTable(pairs, isNumeric) {
  $("#dist-table").innerHTML = `
    <thead><tr><th>${isNumeric ? "数值区间" : "类别"}</th>
      <th class="num">基线占比</th><th class="num">当前占比</th>
      <th class="num">占比差</th></tr></thead>
    <tbody>${pairs.map((p) => `<tr>
      <td>${p.label}</td><td class="num">${(p.baseline * 100).toFixed(1)}%</td>
      <td class="num">${(p.current * 100).toFixed(1)}%</td>
      <td class="num">${((p.current - p.baseline) * 100).toFixed(1)} pp</td></tr>`).join("")}
    </tbody>`;
}

// ------------------------------------------------------------------ baselines & runs
function renderBaselines() {
  const rows = state.baselines;
  $("#baseline-table").innerHTML = `
    <thead><tr><th>特征</th><th>人群</th><th>版本</th><th>业务版本</th>
      <th class="num">n</th><th class="num">缺失率</th><th class="num">异常率</th>
      <th>状态</th><th>说明</th><th>创建时间</th></tr></thead>
    <tbody>${rows.map((b) => `<tr>
      <td>${b.feature}</td><td>${b.segment}</td>
      <td><b>v${b.version}</b></td><td>${b.business_version}</td>
      <td class="num">${b.n}</td><td class="num">${pct(b.missing_rate)}</td>
      <td class="num">${pct(b.outlier_rate)}</td>
      <td>${b.active ? '<span class="pill resolved">active</span>' : '<span class="pill suppressed">archived</span>'}</td>
      <td>${b.note || "—"}</td><td>${b.created_at}</td></tr>`).join("")}
    </tbody>`;
}

function renderRuns() {
  $("#run-table").innerHTML = `
    <thead><tr><th>#</th><th>类型</th><th class="num">评估窗口</th>
      <th class="num">空窗口</th><th class="num">新建</th><th class="num">更新</th>
      <th class="num">解决</th><th>说明</th><th>时间</th></tr></thead>
    <tbody>${state.runs.map((r) => `<tr>
      <td>${r.id}</td><td>${runKind(r.kind)}</td>
      <td class="num">${r.windows_considered}</td>
      <td class="num">${r.windows_empty}</td>
      <td class="num">${r.alerts_created}</td>
      <td class="num">${r.alerts_updated}</td>
      <td class="num">${r.alerts_resolved}</td>
      <td>${r.note || "—"}</td><td>${r.created_at}</td></tr>`).join("")}
    </tbody>`;
}
const runKind = (k) => ({
  scheduled: "定时评估", backfill: "历史回溯", revision: "修订重评",
}[k] || k);

// ------------------------------------------------------------------ alerts
function renderAlerts() {
  const scope = currentScope();
  const statusFilter = $("#f-status").value;
  const rows = state.alerts.filter((a) =>
    (!statusFilter || a.status === statusFilter) &&
    a.feature === scope.feature && a.segment === scope.segment &&
    a.business_version === scope.business_version);
  $("#alert-count").textContent = `${rows.length} 条（当前筛选范围）`;
  $("#alert-table").innerHTML = `
    <thead><tr><th>#</th><th>时间窗口</th><th>严重级别</th><th>状态</th>
      <th>基线</th><th>触发原因</th><th>回溯</th><th>最近出现</th></tr></thead>
    <tbody>${rows.map((a) => `<tr class="clickable" data-alert="${a.id}">
      <td>${a.id}</td><td>${a.window_start.replace("T", " ").slice(0, 16)}</td>
      <td><span class="pill ${a.severity}">${a.severity}</span></td>
      <td><span class="pill ${a.status}">${a.status}</span></td>
      <td>${a.baseline_version ? `v${a.baseline_version}` : "无基线（空人群）"}</td>
      <td>${(a.metrics_snapshot.reasons || ["空分群"]).join("，")}</td>
      <td>${a.is_backfill ? "是" : "否"}</td><td>${a.last_seen_at}</td></tr>`).join("")}
    </tbody>`;
  $("#alert-table").querySelectorAll("tr.clickable").forEach((tr) =>
    tr.addEventListener("click", () => openAlert(Number(tr.dataset.alert))));
}

async function openAlert(id) {
  const alert = await api(`/api/alerts/${id}`);
  const events = await api(`/api/alerts/${id}/events`);
  $("#dlg-title").textContent =
    `告警 #${alert.id} · ${alert.feature} / ${alert.segment} / ${alert.business_version}`;
  const reasons = alert.metrics_snapshot.reasons || ["空分群 info 告警"];
  $("#dlg-body").innerHTML = `
    <p><span class="pill ${alert.severity}">${alert.severity}</span>
       <span class="pill ${alert.status}">${alert.status}</span>
       <span class="hint">窗口 ${alert.window_start} · 基线 ${alert.baseline_version ? "v" + alert.baseline_version : "无"}</span></p>
    <h3 class="section-sub">触发指标</h3>
    <p>${reasons.map((r) => `<code>${r}</code>`).join(" ")}</p>
    <h3 class="section-sub">证据样本（最多 10 条）</h3>
    <table><thead><tr><th>观测 ID</th><th>原因</th><th class="num">数值</th>
      <th>缺失</th><th>标签</th></tr></thead>
    <tbody>${(alert.evidence_samples.length ? alert.evidence_samples : [{ observation_id: "—", reason: "空窗口无样本", value: "—", is_missing: "—", tags: {} }])
      .map((s) => `<tr><td>${s.observation_id}</td><td>${s.reason}</td>
        <td class="num">${s.value ?? s.value_text ?? "—"}</td>
        <td>${s.is_missing ? "是" : "否"}</td>
        <td>${JSON.stringify(s.tags || {})}</td></tr>`).join("")}</tbody></table>
    <h3 class="section-sub">生命周期（只追加，不重写）</h3>
    <ul class="timeline">${events.map((e) => `
      <li><b>${eventLabel(e.event_type)}</b>
        <div class="when">${e.created_at}</div>
        <div class="hint">${e.detail}</div></li>`).join("")}</ul>
    <div class="head-actions">
      <button class="btn btn-small" data-action="ack" data-id="${alert.id}"
        ${alert.status !== "open" ? "disabled" : ""}>确认 acknowledge</button>
      <button class="btn btn-small btn-ghost" data-action="suppress" data-id="${alert.id}">抑制 suppress</button>
    </div>`;
  $("#alert-dialog").showModal();
  $("#dlg-body").querySelectorAll("button[data-action]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const action = btn.dataset.action;
      await patch(`/api/alerts/${btn.dataset.id}`, { action, note: `ui ${action}` });
      $("#alert-dialog").close();
      toast(`告警 #${btn.dataset.id} 已${action === "ack" ? "确认" : "抑制"}`);
      await refreshAlerts();
      renderKpis();
    }));
}
const eventLabel = (t) => ({
  created: "告警创建", updated: "指标更新", severity_changed: "级别变更",
  acknowledged: "人工确认", resolved: "自动恢复关闭", reopened: "再次触发重开",
  suppressed: "人工抑制", backfilled: "回溯产生",
}[t] || t);

// ------------------------------------------------------------------ actions
function bindStaticActions() {
  $("#dlg-close").addEventListener("click", () => $("#alert-dialog").close());
  $("#btn-theme").addEventListener("click", () => {
    const body = document.body;
    body.dataset.theme = body.dataset.theme === "light" ? "dark" : "light";
  });
  $("#btn-evaluate").addEventListener("click", async () => {
    const r = await post("/api/evaluate", { kind: "scheduled", note: "UI 手动评估" });
    toast(`评估完成：评估 ${r.windows_considered} 窗口，新建 ${r.alerts_created}，解决 ${r.alerts_resolved}`);
    await refreshAll();
  });
  $("#btn-revision-eval").addEventListener("click", async () => {
    const r = await post("/api/evaluate", { only_dirty: true, kind: "revision", note: "UI 修订后受影响窗口重评" });
    toast(`仅重评受影响窗口：${r.windows_considered} 个，解决 ${r.alerts_resolved} 条告警`);
    await refreshAll();
  });
  $("#btn-new-baseline").addEventListener("click", async () => {
    const rows = scopeRows();
    if (!rows.length) return toast("当前范围没有可用于基线的窗口");
    const scope = currentScope();
    const b = await post("/api/baselines", {
      ...scope, window_from: rows[0].window_start,
      window_to: rows[rows.length - 1].window_start,
      note: "UI 生成的新版本基线（历史版本保留）",
    });
    toast(`已生成基线 v${b.version}（id=${b.id}），历史版本未被重写`);
    await refreshAll();
  });
  $("#btn-backfill").addEventListener("click", async () => {
    const rows = scopeRows();
    const scope = currentScope();
    const latest = state.baselines.filter((b) =>
      b.feature === scope.feature && b.segment === scope.segment &&
      b.business_version === scope.business_version)
      .sort((a, b2) => b2.version - a.version)[0];
    if (!latest) return toast("请先生成基线");
    const r = await post("/api/backfill", {
      window_from: rows[0].window_start, window_to: rows[rows.length - 1].window_start,
      baseline_id: latest.id, note: "UI 历史回溯",
    });
    toast(`回溯完成：${r.windows_considered} 个窗口，新建回溯告警 ${r.alerts_created} 条（不重复、不重写历史）`);
    await refreshAll();
  });
}

init().catch((err) => toast(`初始化失败: ${err.message}`));
