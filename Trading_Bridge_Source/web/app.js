/* TradingBridge signal board.
 *
 * Plain HTML/CSS/JS with no build step: the whole dashboard is three static
 * files the Flask server hands out, so an operator can read or patch what they
 * are running. Updates arrive over Server-Sent Events; if the stream is refused
 * or stalls, the same data is fetched by polling /api/v1/events with a cursor,
 * because every event is in the SQLite ledger either way.
 *
 * Nothing from the server is ever inserted as HTML. Every dynamic string goes
 * through textContent, so a symbol or result string containing markup cannot
 * render as markup in a page that can place trades.
 */
"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const state = {
  contract: null,
  snapshot: null,
  eventCursor: 0,
  events: [],
  signals: [],
  filters: { status: "", symbol: "", action: "", search: "" },
  offset: 0,
  limit: 100,
  stream: null,
  streamMode: "connecting",
  lastEventAt: 0,
  pollTimer: null,
  refreshTimer: null,
  drawerId: null,
};

const POLL_INTERVAL_MS = 1500;
const REFRESH_INTERVAL_MS = 5000;
const STREAM_STALL_MS = 45000;

/* ------------------------------------------------------------------ helpers */

function h(tag, props, ...children) {
  const node = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === "string" || typeof child === "number"
      ? document.createTextNode(String(child))
      : child);
  }
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function toast(message, kind = "info", ms = 4200) {
  const box = $("#toasts");
  const node = h("div", { class: `toast ${kind}`, text: message });
  box.appendChild(node);
  setTimeout(() => node.remove(), ms);
}

async function api(path, options = {}) {
  const init = { headers: { "Content-Type": "application/json" }, ...options };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  const response = await fetch(path, init);
  const text = await response.text();
  let body;
  try { body = text ? JSON.parse(text) : {}; } catch { body = { error: text.slice(0, 300) }; }
  if (!response.ok) {
    const error = new Error(body.error || body.errors && JSON.stringify(body.errors) || `HTTP ${response.status}`);
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return body;
}

function parseDetail(detail) {
  if (detail && typeof detail === "object") return detail;
  if (typeof detail === "string" && detail.startsWith("{")) {
    try { return JSON.parse(detail); } catch { return { text: detail }; }
  }
  return detail ? { text: detail } : {};
}

function clock(iso) {
  if (!iso) return "—";
  // SQLite stores "YYYY-MM-DD HH:MM:SS" in UTC; treat it as such rather than as
  // local time, or every timestamp on the board would be shifted.
  const text = iso.includes("T") ? iso : iso.replace(" ", "T") + "Z";
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleTimeString([], { hour12: false });
}

function ago(seconds) {
  if (seconds === null || seconds === undefined) return "never";
  if (seconds < 2) return "just now";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

function number(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

/* -------------------------------------------------------------------- tabs */

function showView(name) {
  $$(".view").forEach((view) => view.classList.toggle("is-active", view.id === `view-${name}`));
  $$(".tab").forEach((tab) => tab.classList.toggle("is-active", tab.dataset.view === name));
  if (name === "signals") loadSignals();
  if (name === "events") renderEventsFeed();
  if (name === "settings") renderSettings();
  if (name === "board") renderBoard();
}

/* --------------------------------------------------------------- top bar */

function setPill(id, text, kind, title) {
  const node = $(id);
  node.textContent = text;
  node.className = `pill${kind ? ` ${kind}` : ""}`;
  if (title) node.title = title;
}

function renderTopBar() {
  const snap = state.snapshot;
  if (!snap) return;
  const ea = snap.ea || {};
  const mode = state.streamMode;

  setPill("#pill-connection",
    mode === "live" ? "live" : mode === "polling" ? "polling" : mode === "connecting" ? "connecting" : "offline",
    mode === "live" ? "ok" : mode === "polling" ? "info" : mode === "connecting" ? "" : "bad",
    mode === "live"
      ? "Server-sent events. Events are also in the ledger, so a reconnect never loses one."
      : `Falling back to polling every ${POLL_INTERVAL_MS} ms.`);

  setPill("#pill-ea",
    ea.online ? `EA online · ${ago(ea.seconds_since_poll)}` : `EA OFFLINE · ${ago(ea.seconds_since_poll)}`,
    ea.online ? "ok" : "bad",
    ea.online
      ? `Bot_Trader polled ${ea.last_seen_at || "recently"}.`
      : `Bot_Trader has not polled for ${ago(ea.seconds_since_poll)}. Check that the EA is attached, Algo Trading is on, and ${location.origin}/signal is whitelisted for WebRequest.`);

  setPill("#pill-queue", `queue ${snap.queue_depth}`, snap.queue_depth > 0 ? "info" : "",
    "Commands accepted but not yet handed to the EA.");

  const watcher = snap.watcher || {};
  setPill("#pill-folder", `inbox ${watcher.pending_files ?? 0}`,
    watcher.last_error ? "warn" : "",
    watcher.last_error
      ? `Watcher error: ${watcher.last_error}`
      : `${watcher.folder || "no inbox"} (read-only) · scanned ${watcher.scans || 0}× · consumed ${watcher.claimed || 0} · copies in ${watcher.data_folder || "?"}`);

  const settings = snap.settings || {};
  setPill("#pill-mode", settings.paused ? "PAUSED" : `${settings.validation_mode} mode`,
    settings.paused ? "warn" : "",
    settings.paused
      ? "Dispatch is paused. Intake still records, but nothing is sent to the EA."
      : "strict requires a command to state its own symbol and size; permissive lets the EA's inputs fill them in.");

  $("#btn-pause").textContent = settings.paused ? "Resume dispatch" : "Pause dispatch";
  $("#btn-pause").classList.toggle("btn-primary", !settings.paused);
  $("#contract-version").textContent = state.contract
    ? `EA v${state.contract.ea_version} · schema v${state.contract.schema_version}`
    : "…";
}

/* ------------------------------------------------------------------ board */

const KPI_DEFS = [
  { key: "executed", label: "Executed", cls: "executed", help: "The broker accepted the order (retcode 0)." },
  { key: "failed", label: "Failed", cls: "failed", help: "The broker refused it. Hover the return-code table for why." },
  { key: "skipped", label: "Skipped", cls: "skipped", help: "An EA guard fired first: market closed, trading disabled, max positions, unknown action." },
  { key: "queued", label: "Waiting", cls: "queued", help: "Accepted and validated, not yet handed to the EA." },
  { key: "rejected", label: "Rejected", cls: "rejected", help: "Failed validation. Never sent, and recorded so you can see what was refused." },
  { key: "expired", label: "Expired", cls: "", help: "Aged out before dispatch, or dispatched and never acknowledged. Not re-sent." },
];

function renderBoard() {
  const snap = state.snapshot;
  if (!snap) return;
  const metrics = snap.metrics || {};
  const totals = metrics.totals || {};
  const byStatus = totals.by_status || {};

  const kpis = $("#kpis");
  clear(kpis);
  const cards = [
    h("div", { class: "kpi" },
      h("div", { class: "kpi-value", text: number(totals.all_time) }),
      h("div", { class: "kpi-label", text: "Signals total" }),
      h("div", { class: "kpi-sub", text: `${number(totals.recent)} in the last hour` })),
  ];
  for (const def of KPI_DEFS) {
    cards.push(h("div", { class: `kpi ${def.cls}`, title: def.help },
      h("div", { class: "kpi-value", text: number(byStatus[def.key] || 0) }),
      h("div", { class: "kpi-label", text: def.label })));
  }
  cards.forEach((card) => kpis.appendChild(card));

  $("#throughput-note").textContent = `${(metrics.throughput || []).length} one-minute buckets`;
  drawThroughput(metrics.throughput || []);
  renderBars("#status-bars", Object.entries(byStatus).map(([k, v]) => ({ name: k, count: v, cls: k })));
  renderBars("#symbol-bars", (metrics.by_symbol || []).map((r) => ({ name: r.symbol, count: r.count })));
  renderBars("#action-bars", (metrics.by_action || []).map((r) => ({ name: r.action, count: r.count })));

  const latency = metrics.latency_ms || {};
  clear($("#latency"));
  [
    ["Samples dispatched", number(latency.samples)],
    ["Avg queue → dispatch", `${number(latency.avg_queue, 1)} ms`],
    ["Avg dispatch → ack", `${number(latency.avg_exec, 1)} ms`],
    ["Slowest queue wait", latency.max_queue === null ? "—" : `${number(latency.max_queue)} ms`],
    ["Slowest execution", latency.max_exec === null ? "—" : `${number(latency.max_exec)} ms`],
    ["First signal", clock(metrics.first_signal_at)],
    ["Last signal", clock(metrics.last_signal_at)],
  ].forEach(([label, value]) => {
    $("#latency").appendChild(h("div", { class: "stat-row" }, h("span", { text: label }), h("span", { text: value })));
  });

  const retcodes = metrics.by_retcode || [];
  const tbody = $("#retcode-table tbody");
  clear(tbody);
  if (!retcodes.length) {
    tbody.appendChild(h("tr", {}, h("td", { colspan: "3", class: "empty", text: "No acknowledgements recorded yet." })));
  }
  for (const row of retcodes) {
    // MT5 success codes (10008/10009/10010) are positive but are not rejections;
    // painting them red would read as a failed trade.
    const isSuccess = row.retcode === 0 || (state.contract.success_retcodes || {})[String(row.retcode)] !== undefined;
    const cls = isSuccess ? "executed" : row.retcode < 0 ? "skipped" : "failed";
    tbody.appendChild(h("tr", {},
      h("td", {}, h("span", { class: `tag ${cls}`, text: String(row.retcode) })),
      h("td", { class: "mono", text: row.label }),
      h("td", { class: "num", text: number(row.count) })));
  }

  renderBoardFeed();
}

function renderBars(selector, rows) {
  const box = $(selector);
  clear(box);
  const meaningful = rows.filter((r) => r.count > 0).sort((a, b) => b.count - a.count).slice(0, 10);
  if (!meaningful.length) {
    box.appendChild(h("div", { class: "empty", text: "Nothing recorded yet." }));
    return;
  }
  const max = Math.max(...meaningful.map((r) => r.count), 1);
  for (const row of meaningful) {
    box.appendChild(h("div", { class: "bar-row" },
      h("div", { class: "bar-name", text: row.name, title: row.name }),
      h("div", { class: "bar-track" }, h("div", { class: `bar-fill ${row.cls || ""}`, style: `width:${(row.count / max) * 100}%` })),
      h("div", { class: "bar-count", text: number(row.count) })));
  }
}

function drawThroughput(buckets) {
  const canvas = $("#chart-throughput");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || canvas.parentElement.clientWidth || 600;
  const height = 150;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);

  if (!buckets.length) {
    ctx.fillStyle = "#5f6b78";
    ctx.font = "12px Segoe UI, sans-serif";
    ctx.fillText("No signals in this window yet.", 12, height / 2);
    return;
  }

  const pad = { top: 12, right: 8, bottom: 22, left: 30 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const max = Math.max(...buckets.map((b) => b.count), 1);
  const slot = plotW / Math.max(buckets.length, 1);
  const barW = Math.max(1.5, Math.min(slot - 1.5, 22));

  ctx.strokeStyle = "#232c37";
  ctx.fillStyle = "#5f6b78";
  ctx.font = "10px Cascadia Mono, Consolas, monospace";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 2; i++) {
    const y = pad.top + (plotH / 2) * i;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    ctx.fillText(String(Math.round(max - (max / 2) * i)), 4, y + 3);
  }

  buckets.forEach((bucket, index) => {
    const barH = (bucket.count / max) * plotH;
    const x = pad.left + index * slot + (slot - barW) / 2;
    ctx.fillStyle = "#3ddc84";
    ctx.fillRect(x, pad.top + plotH - barH, barW, barH);
  });

  const first = buckets[0].bucket.slice(11) || "";
  const last = buckets[buckets.length - 1].bucket.slice(11) || "";
  ctx.fillStyle = "#5f6b78";
  ctx.fillText(first, pad.left, height - 6);
  const lastLabel = ctx.measureText(last).width;
  ctx.fillText(last, width - pad.right - lastLabel, height - 6);
}

function renderBoardFeed() {
  const list = $("#board-feed");
  clear(list);
  for (const event of state.events.slice(-24).reverse()) list.appendChild(eventRow(event));
}

/* ----------------------------------------------------------------- events */

function eventRow(event) {
  const detail = parseDetail(event.detail);
  const text = detail.summary || detail.error || detail.reason || detail.result
    || detail.file || detail.text || JSON.stringify(detail);
  const extra = detail.retcode_label ? ` · ${detail.retcode_label}` : "";
  const li = h("li", { class: event.type },
    h("span", { class: "t", text: clock(event.created_at) }),
    h("span", { class: "k", text: event.type }),
    h("span", { class: "d", text: `${text}${extra}`, title: `${event.request_id || ""}\n${text}${extra}` }));
  if (event.request_id) {
    li.classList.add("clickable");
    li.addEventListener("click", () => openDrawer(event.request_id));
  }
  return li;
}

function renderEventsFeed() {
  const list = $("#events-feed");
  clear(list);
  const rows = state.events.slice(-400).reverse();
  if (!rows.length) {
    list.appendChild(h("li", { class: "empty", text: "No events yet." }));
    return;
  }
  for (const event of rows) list.appendChild(eventRow(event));
}

function pushEvents(incoming) {
  let added = 0;
  for (const event of incoming) {
    const id = Number(event.id);
    if (id <= state.eventCursor) continue;
    state.eventCursor = id;
    state.events.push(event);
    added++;
  }
  if (state.events.length > 1200) state.events.splice(0, state.events.length - 1200);
  if (added) {
    state.lastEventAt = Date.now();
    $("#badge-events").textContent = String(state.events.length);
    if ($("#view-events").classList.contains("is-active") && $("#events-follow").checked) renderEventsFeed();
    if ($("#view-board").classList.contains("is-active")) renderBoardFeed();
  }
  return added;
}

/* ---------------------------------------------------------------- signals */

async function loadSignals() {
  const params = new URLSearchParams({ limit: String(state.limit), offset: String(state.offset) });
  for (const [key, value] of Object.entries(state.filters)) if (value) params.set(key, value);
  try {
    const data = await api(`/api/v1/signals?${params}`);
    state.signals = data.signals;
    renderSignalsTable();
    populateFilters(data.filters || {});
  } catch (error) {
    toast(`Could not load signals: ${error.message}`, "bad");
  }
}

function populateFilters(filters) {
  for (const [key, select, label] of [
    ["statuses", "#filter-status", "all statuses"],
    ["symbols", "#filter-symbol", "all symbols"],
    ["actions", "#filter-action", "all actions"],
  ]) {
    const node = $(select);
    const current = node.value;
    clear(node);
    node.appendChild(h("option", { value: "", text: label }));
    for (const value of filters[key] || []) node.appendChild(h("option", { value, text: value }));
    node.value = current;
  }
}

function renderSignalsTable() {
  const tbody = $("#signals-table tbody");
  clear(tbody);
  if (!state.signals.length) {
    tbody.appendChild(h("tr", {}, h("td", { colspan: "12", class: "empty", text: "No signals match." })));
  }
  for (const signal of state.signals) {
    const size = signal.size_value === null || signal.size_value === undefined
      ? "—" : `${number(signal.size_value, 2)} ${signal.size_unit || "lots"}`;
    const warnings = Array.isArray(signal.warnings) ? signal.warnings.length : 0;
    const row = h("tr", {},
      h("td", { class: "mono", text: clock(signal.created_at) }),
      h("td", { class: "mono", text: signal.request_id }),
      h("td", {}, h("span", { class: "tag", text: signal.source })),
      h("td", {}, h("span", { class: `tag ${signal.action}`, text: signal.action })),
      h("td", { class: "mono", text: signal.symbol || "—" }),
      h("td", { class: "num", text: size }),
      h("td", {}, h("span", { class: `tag ${signal.status}`, text: signal.status }),
        warnings ? h("span", { class: "warn-dot", text: " ▲", title: signal.warnings.join("\n") }) : null),
      h("td", { class: "num", text: signal.retcode === null || signal.retcode === undefined ? "—" : String(signal.retcode) }),
      h("td", { class: "mono", text: signal.result_text || signal.error || "" , title: signal.result_text || signal.error || ""}),
      h("td", { class: "num", text: signal.queue_ms === null ? "—" : number(signal.queue_ms) }),
      h("td", { class: "num", text: signal.exec_ms === null ? "—" : number(signal.exec_ms) }),
      h("td", {}, h("button", { class: "btn btn-small", text: "Open", onclick: () => openDrawer(signal.request_id) })));
    tbody.appendChild(row);
  }
  $("#pager-label").textContent = `${state.offset + 1}–${state.offset + state.signals.length}`;
  $("#badge-signals").textContent = String((state.snapshot?.metrics?.totals?.all_time) ?? state.signals.length);
}

/* ----------------------------------------------------------------- drawer */

async function openDrawer(requestId) {
  state.drawerId = requestId;
  const body = $("#drawer-body");
  clear(body);
  body.appendChild(h("p", { class: "empty", text: "Loading…" }));
  $("#drawer").classList.add("is-open");
  $("#scrim").classList.add("is-open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#drawer-title").textContent = requestId;

  let signal;
  try {
    signal = await api(`/api/v1/signals/${encodeURIComponent(requestId)}`);
  } catch (error) {
    clear(body);
    body.appendChild(h("p", { class: "empty", text: `Could not load: ${error.message}` }));
    return;
  }

  clear(body);
  const facts = [
    ["Status", signal.status],
    ["Action", signal.action],
    ["Symbol", signal.symbol || "(EA chart symbol)"],
    ["Size", signal.size_value === null || signal.size_value === undefined ? "(EA LotSize input)" : `${signal.size_value} ${signal.size_unit || "lots"}`],
    ["Source", `${signal.source}${signal.source_file ? ` · ${signal.source_file}` : ""}`],
    ["Retcode", signal.retcode === null || signal.retcode === undefined ? "—" : String(signal.retcode)],
    ["Result", signal.result_text || "—"],
    ["Error", signal.error || "—"],
    ["Received", signal.created_at || "—"],
    ["Dispatched", signal.dispatched_at || "never"],
    ["Finished", signal.finished_at || "—"],
    ["Queue wait", signal.queue_ms === null ? "—" : `${signal.queue_ms} ms`],
    ["Execution", signal.exec_ms === null ? "—" : `${signal.exec_ms} ms`],
  ];
  const list = h("div", { class: "stat-list" });
  for (const [label, value] of facts) {
    list.appendChild(h("div", { class: "stat-row" }, h("span", { text: label }), h("span", { text: String(value) })));
  }
  body.appendChild(list);

  const actions = h("div", { class: "row" });
  const canCancel = signal.status === "queued";
  const canRetry = !["queued", "dispatched"].includes(signal.status);
  const canRequeue = ["expired", "cancelled"].includes(signal.status);
  actions.appendChild(h("button", {
    class: "btn btn-small", text: "Cancel", disabled: !canCancel,
    title: canCancel ? "Remove it from the queue before the EA sees it." : "Only a queued signal can still be cancelled.",
    onclick: () => act(`/api/v1/signals/${encodeURIComponent(requestId)}/cancel`, "cancelled"),
  }));
  actions.appendChild(h("button", {
    class: "btn btn-small", text: "Retry as new", disabled: !canRetry,
    title: "Submits the same command under a fresh request_id. A new id is required: the EA skips a body identical to the last one it processed.",
    onclick: () => act(`/api/v1/signals/${encodeURIComponent(requestId)}/retry`, "re-submitted"),
  }));
  actions.appendChild(h("button", {
    class: "btn btn-small", text: "Requeue", disabled: !canRequeue,
    title: "Puts this exact signal back on the queue.",
    onclick: () => act(`/api/v1/signals/${encodeURIComponent(requestId)}/requeue`, "requeued"),
  }));
  body.appendChild(actions);

  if (Array.isArray(signal.warnings) && signal.warnings.length) {
    body.appendChild(h("h2", { class: "subhead", text: "Warnings" }));
    body.appendChild(h("ul", { class: "result-list warn" },
      signal.warnings.map((w) => h("li", { text: w }))));
  }

  body.appendChild(h("h2", { class: "subhead", text: "Normalized command sent to the EA" }));
  body.appendChild(h("pre", { class: "code", text: JSON.stringify(signal.command, null, 2) }));

  body.appendChild(h("h2", { class: "subhead", text: "Original payload as received" }));
  body.appendChild(h("pre", { class: "code", text: pretty(signal.raw_payload) }));

  body.appendChild(h("h2", { class: "subhead", text: `Timeline (${(signal.events || []).length})` }));
  const feed = h("ul", { class: "feed feed-large" });
  for (const event of signal.events || []) feed.appendChild(eventRow(event));
  body.appendChild(feed);
}

function pretty(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") {
    try { return JSON.stringify(JSON.parse(value), null, 2); } catch { return value; }
  }
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

function closeDrawer() {
  state.drawerId = null;
  $("#drawer").classList.remove("is-open");
  $("#scrim").classList.remove("is-open");
  $("#drawer").setAttribute("aria-hidden", "true");
}

async function act(path, verb) {
  try {
    const result = await api(path, { method: "POST" });
    toast(`${verb}: ${result.status || "ok"}${result.request_id && result.request_id !== $("#drawer-title").textContent ? ` → ${result.request_id}` : ""}`, "ok");
    closeDrawer();
    await refreshState();
    loadSignals();
  } catch (error) {
    toast(`${verb} failed: ${error.message}`, "bad");
  }
}

/* ------------------------------------------------------------------- send */

function templates() {
  const points = { type: "distance", unit: "points", value: 200 };
  return {
    buy: { action: "buy", symbol: "EURUSD", size: { value: 0.01, unit: "lots" }, stop_loss: points, take_profit: { type: "distance", unit: "points", value: 400 } },
    sell: { action: "sell", symbol: "XAUUSD", size: { value: 0.05, unit: "lots" }, stop_loss: 500, take_profit: 1000 },
    risk_sized_buy: {
      action: "buy", symbol: "EURUSD", risk_percent: 1.0,
      size: { value: 1.0, unit: "risk" },
      stop_loss: { type: "distance", unit: "atr", period: 14, multiplier: 1.5 },
      take_profit: { type: "distance", unit: "atr", period: 14, multiplier: 3.0 },
    },
    close_symbol: { action: "close", symbol: "EURUSD" },
    close_all: { action: "close_all" },
    partial_close: { action: "partial_close", symbol: "EURUSD", close_percent: 50 },
    modify: { action: "modify", symbol: "EURUSD", stop_loss: { unit: "points", value: 250 } },
    breakeven: { action: "breakeven", symbol: "EURUSD", breakeven: { lock_in: { unit: "points", value: 20 } } },
    hold: { action: "hold", symbol: "EURUSD" },
    legacy_text: { signal: "BUY SL=50 TP=100 LOT=0.01" },
  };
}

function buildSendTab() {
  const builder = $("#builder");
  clear(builder);
  for (const [name, template] of Object.entries(templates())) {
    builder.appendChild(h("button", {
      class: "btn btn-small", text: name.replace(/_/g, " "),
      onclick: () => { $("#send-json").value = JSON.stringify(template, null, 2); validateDraft(); },
    }));
  }
  const select = $("#send-examples");
  clear(select);
  select.appendChild(h("option", { value: "", text: "load an example…" }));
  for (const name of Object.keys(templates())) select.appendChild(h("option", { value: name, text: name }));
  select.addEventListener("change", () => {
    const template = templates()[select.value];
    if (template) {
      $("#send-json").value = JSON.stringify(template, null, 2);
      validateDraft();
    }
    select.value = "";
  });
  $("#send-json").value = JSON.stringify(templates().buy, null, 2);
}

function readDraft() {
  const text = $("#send-json").value.trim();
  if (!text) throw new Error("the command box is empty");
  return JSON.parse(text);
}

function renderValidation(result, box, envelopeBox) {
  clear(box);
  box.appendChild(h("div", {
    class: `result-banner ${result.valid ? "ok" : "bad"}`,
    text: result.valid
      ? `Valid in ${result.mode || "strict"} mode — would be queued as: ${result.summary}`
      : `Rejected — ${result.status}`,
  }));
  if (result.errors && result.errors.length) {
    box.appendChild(h("div", { class: "field-label", text: "Errors" }));
    box.appendChild(h("ul", { class: "result-list err" }, result.errors.map((e) => h("li", { text: e }))));
  }
  if (result.warnings && result.warnings.length) {
    box.appendChild(h("div", { class: "field-label", text: "Warnings — the trade still goes through" }));
    box.appendChild(h("ul", { class: "result-list warn" }, result.warnings.map((w) => h("li", { text: w }))));
  }
  if (result.command) {
    box.appendChild(h("div", { class: "field-label", text: "Normalized command" }));
    box.appendChild(h("pre", { class: "code", text: JSON.stringify(result.command, null, 2) }));
  }
  if (envelopeBox) envelopeBox.textContent = result.envelope ? JSON.stringify(result.envelope, null, 2) : "—";
}

let validateTimer = null;
function validateDraft() {
  clearTimeout(validateTimer);
  validateTimer = setTimeout(async () => {
    let payload;
    try { payload = readDraft(); } catch (error) {
      const box = $("#send-result");
      clear(box);
      box.appendChild(h("div", { class: "result-banner bad", text: `Not JSON: ${error.message}` }));
      $("#send-envelope").textContent = "—";
      return;
    }
    try {
      const result = await api("/api/v1/validate", { method: "POST", body: payload });
      renderValidation(result, $("#send-result"), $("#send-envelope"));
    } catch (error) {
      toast(`Validate failed: ${error.message}`, "bad");
    }
  }, 350);
}

async function sendDraft() {
  let payload;
  try { payload = readDraft(); } catch (error) { toast(error.message, "bad"); return; }
  const button = $("#btn-send");
  button.disabled = true;
  try {
    const result = await api("/api/v1/signals", { method: "POST", body: { command: payload } });
    toast(`Queued ${result.request_id} — ${result.summary}`, "ok");
    await refreshState();
    openDrawer(result.request_id);
  } catch (error) {
    const body = error.body || {};
    toast(`Not sent: ${error.message}`, "bad", 7000);
    if (body.command || body.errors) {
      renderValidation({
        valid: false, status: body.status || "rejected",
        errors: body.errors || [error.message], warnings: body.warnings || [], command: body.command,
      }, $("#send-result"), null);
    }
  } finally {
    button.disabled = false;
  }
}

/* --------------------------------------------------------------- settings */

function renderSettings() {
  const snap = state.snapshot;
  if (!snap) return;
  const form = $("#settings-form");
  const editable = snap.editable_settings || {};
  const current = snap.settings || {};
  clear(form);

  for (const [name, spec] of Object.entries(editable)) {
    const field = h("div", { class: "field" });
    field.appendChild(h("div", { class: "field-head" },
      h("label", { for: `setting-${name}`, text: spec.label }),
      h("span", { class: "field-value", id: `current-${name}`, text: String(current[name]) })));
    field.appendChild(h("div", { class: "field-help", text: spec.help }));

    let input;
    if (spec.type === "bool") {
      input = h("select", { id: `setting-${name}`, name },
        h("option", { value: "true", text: "on" }), h("option", { value: "false", text: "off" }));
      input.value = current[name] ? "true" : "false";
    } else if (spec.choices) {
      input = h("select", { id: `setting-${name}`, name },
        spec.choices.map((choice) => h("option", { value: choice, text: choice })));
      input.value = String(current[name]);
    } else {
      input = h("input", {
        type: spec.type === "string" ? "text" : "number",
        id: `setting-${name}`, name,
        value: String(current[name]),
        step: spec.type === "float" ? "0.1" : "1",
        min: spec.min === null ? undefined : spec.min,
        max: spec.max === null ? undefined : spec.max,
      });
      if (spec.path) {
        // A folder path is not prose: keep the browser from "correcting" it.
        input.setAttribute("spellcheck", "false");
        input.setAttribute("autocomplete", "off");
        input.classList.add("field-path");
      }
    }
    input.addEventListener("change", () => { $(`#current-${name}`).textContent = input.value; });
    field.appendChild(input);
    field.appendChild(h("div", { class: "field-error", id: `error-${name}` }));
    form.appendChild(field);
  }

  const info = [
    ["Inbox (read-only)", snap.watcher ? snap.watcher.folder : current.signals_folder],
    ["Inbox exists", snap.watcher ? String(snap.watcher.folder_exists) : "—"],
    ["Scans", snap.watcher ? String(snap.watcher.scans) : "—"],
    ["Files consumed", snap.watcher ? String(snap.watcher.claimed) : "—"],
    ["Unhandled .json", snap.watcher ? String(snap.watcher.pending_files) : "—"],
    ["Already-known skipped", snap.watcher ? String(snap.watcher.duplicates_skipped ?? 0) : "—"],
    ["Adopted on switch", adoptedTitle(snap)],
    ["Read failures", snap.watcher ? String(snap.watcher.read_failed ?? 0) : "—"],
    ["Intake failures", snap.watcher ? String(snap.watcher.submit_failed) : "—"],
    ["Interrupted", snap.watcher ? String(snap.watcher.interrupted_files) : "—"],
    ["Last scan", snap.watcher && snap.watcher.last_scan_at ? snap.watcher.last_scan_at : "—"],
    ["Last watcher error", snap.watcher && snap.watcher.last_error ? snap.watcher.last_error : "none"],
  ];
  fillStatList("#intake-info", info);
  fillStatList("#fixed-info", [
    ["Data folder", snap.watcher ? snap.watcher.data_folder : current.data_folder],
    ["Intake copies", snap.watcher ? snap.watcher.intake_dir : `${current.data_folder}\\intake`],
    ["Ledger", current.db_path],
    ["Host", current.host],
    ["Port", String(current.port)],
    ["Origin check", current.require_origin ? "on" : "off"],
    ["Allowed origins", (current.allowed_origins || []).join(", ") || "none"],
    ["Token auth", current.token_auth_enabled ? "on" : "off"],
    ["Live streams", String(snap.sse_clients ?? 0)],
    ["Server time", snap.now],
  ]);
}

/* Files that were already in an inbox when the bridge started watching it. They
   are recorded by path and never traded, so this number is reassurance, not a
   problem - but it should be visible, because it explains a signal that did not
   fire. */
function adoptedTitle(snap) {
  const w = snap.watcher;
  if (!w) return "—";
  const total = w.adopted_total ?? 0;
  if (!total) return "0";
  const here = w.adopted_files ?? 0;
  return here === total ? String(total) : `${total} (${here} in this inbox)`;
}

function fillStatList(selector, rows) {  const box = $(selector);
  clear(box);
  for (const [label, value] of rows) {
    box.appendChild(h("div", { class: "stat-row" },
      h("span", { text: label }),
      h("span", { text: String(value), title: String(value) })));
  }
}

async function saveSettings() {
  const updates = {};
  for (const name of Object.keys(state.snapshot.editable_settings || {})) {
    const input = $(`#setting-${name}`);
    if (!input) continue;
    updates[name] = input.value;
  }
  const box = $("#settings-result");
  clear(box);
  let result;
  try {
    result = await api("/api/v1/settings", { method: "PATCH", body: updates });
  } catch (error) {
    // A rejected batch arrives as 422 carrying the per-field reasons, which is
    // the normal outcome for a typo rather than a failure to talk to the bridge.
    result = error.body && error.body.errors ? error.body : null;
    if (!result) {
      box.appendChild(h("div", { class: "result-banner bad", text: error.message }));
      return;
    }
  }
  if (result.errors && Object.keys(result.errors).length) {
    const specs = state.snapshot.editable_settings || {};
    box.appendChild(h("div", { class: "result-banner bad", text: "Nothing was changed — fix these:" }));
    box.appendChild(h("ul", { class: "result-list err" },
      Object.entries(result.errors).map(([k, v]) =>
        h("li", { text: `${(specs[k] && specs[k].label) || k}: ${v}` }))));
    for (const [key, message] of Object.entries(result.errors)) {
      const node = $(`#error-${key}`);
      if (node) node.textContent = message;
    }
    return;
  }
  box.appendChild(h("div", { class: "result-banner ok", text: "Saved. Effective immediately and kept across restarts." }));
  toast("Settings saved", "ok");
  await refreshState();
  renderSettings();
}

/* --------------------------------------------------------------- contract */

function renderContract() {
  const contract = state.contract;
  if (!contract) return;

  const box = $("#contract-actions");
  clear(box);
  const unsupported = contract.unsupported_actions || {};
  for (const [name, aliases] of Object.entries(contract.actions)) {
    const chip = h("details", { class: "action-chip" },
      h("summary", { text: name }),
      h("div", { class: "aliases", text: aliases.join("  ·  ") }));
    if (unsupported[name]) {
      chip.appendChild(h("div", { class: "aliases", text: `not executed by this EA: ${unsupported[name]}` }));
    }
    box.appendChild(chip);
  }

  fillStatList("#contract-units", [
    ["Size units", (contract.size_units || []).join(", ")],
    ["Approximate sizing", (contract.approximate_size_units || []).join(", ")],
    ["Distance units", (contract.distance_units || []).join(", ")],
    ["Unsupported distance units", Object.keys(contract.unsupported_distance_units || {}).join(", ") || "none"],
    ["ATR timeframe honoured", contract.atr_timeframe_honoured ? "yes" : "no — ATR always uses M15"],
    ["Envelope keys", (contract.envelope_keys || []).join(", ")],
    ["Ack fields", (contract.result_fields || []).join(", ")],
    ["EA inputs", (contract.ea_inputs || []).join(", ")],
  ]);

  const statusBox = $("#contract-statuses");
  clear(statusBox);
  const meaning = {
    queued: "Accepted and validated, waiting for the EA's next poll.",
    dispatched: "Handed to the EA. Waiting for its acknowledgement.",
    executed: "The broker accepted it (retcode 0).",
    failed: "The broker refused it (retcode > 0).",
    skipped: "An EA guard fired before the broker was asked (retcode < 0).",
    rejected: "Failed validation. Never sent.",
    invalid: "Could not be read as a command at all. Never sent.",
    expired: "Aged out before dispatch, or dispatched and never acknowledged. Never re-sent automatically.",
    cancelled: "Removed from the queue by an operator.",
  };
  for (const status of contract.signal_statuses || []) {
    statusBox.appendChild(h("div", { class: "stat-row" },
      h("span", {}, h("span", { class: `tag ${status}`, text: status })),
      h("span", { text: meaning[status] || "" })));
  }

  fillRetcodes("#contract-local tbody", contract.local_retcodes);
  fillRetcodes("#contract-broker tbody", contract.broker_retcodes);
  fillRetcodes("#contract-success tbody", contract.success_retcodes);
}

function fillRetcodes(selector, codes) {
  const tbody = $(selector);
  clear(tbody);
  for (const [code, label] of Object.entries(codes || {})) {
    tbody.appendChild(h("tr", {}, h("td", { class: "mono", text: code }), h("td", { text: label })));
  }
}

/* ------------------------------------------------------------ live update */

function connectStream() {
  if (state.stream) {
    state.stream.close();
    state.stream = null;
  }
  if (!("EventSource" in window)) {
    startPolling();
    return;
  }
  state.streamMode = "connecting";
  renderTopBar();

  const source = new EventSource(`/api/v1/stream?since=${state.eventCursor}`);
  state.stream = source;

  source.onopen = () => {
    state.streamMode = "live";
    state.lastEventAt = Date.now();
    stopPolling();
    renderTopBar();
  };

  // The server sends each ledger event type under its own name, so listen for
  // all of them rather than the unnamed default.
  const types = ["signal.received", "signal.rejected", "signal.invalid", "signal.duplicate",
    "signal.dispatched", "signal.result", "signal.expired", "signal.cancelled", "signal.requeued",
    "ea.online", "ea.offline", "settings.changed", "watcher", "server.error", "heartbeat", "message"];
  for (const type of types) {
    source.addEventListener(type, (message) => {
      state.lastEventAt = Date.now();
      try {
        const event = JSON.parse(message.data);
        // The heartbeat proves the stream is alive between ledger events; it is
        // not stored server-side, so feeding it to pushEvents would poison the
        // event cursor with an id that does not exist.
        if (event.type === "heartbeat") return;
        if (pushEvents([event])) onNewEvents([event]);
      } catch { /* ignore a malformed frame; the next poll resynchronises */ }
    });
  }

  source.onerror = () => {
    state.streamMode = "reconnecting";
    renderTopBar();
    // EventSource retries on its own, but Waitress may have refused the stream
    // outright (too many clients), in which case it never recovers. Poll too, so
    // the board keeps working either way; duplicates are dropped by the cursor.
    startPolling();
  };
}

function startPolling() {
  if (state.pollTimer) return;
  state.streamMode = "polling";
  renderTopBar();
  state.pollTimer = setInterval(pollEvents, POLL_INTERVAL_MS);
  pollEvents();
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

async function pollEvents() {
  try {
    const data = await api(`/api/v1/events?since=${state.eventCursor}&limit=500`);
    const events = data.events || [];
    if (pushEvents(events)) onNewEvents(events);
  } catch {
    state.streamMode = "offline";
    renderTopBar();
  }
}

function onNewEvents(events) {
  for (const event of events) {
    const detail = parseDetail(event.detail);
    if (event.type === "signal.result") {
      const tone = detail.status === "executed" ? "ok" : detail.status === "failed" ? "bad" : "warn";
      toast(`${detail.action || ""} ${detail.symbol || ""} → ${detail.status}` +
        `${detail.retcode_label ? ` (${detail.retcode_label})` : ""}`, tone);
    } else if (event.type === "signal.rejected" || event.type === "signal.invalid") {
      toast(`Signal ${event.type.split(".")[1]}: ${detail.error || ""}`, "bad", 7000);
    } else if (event.type === "ea.offline") {
      toast("Bot_Trader stopped polling — check the EA and its WebRequest allow-list", "bad", 9000);
    } else if (event.type === "signal.expired") {
      toast(`Signal expired: ${detail.reason || ""}`, "warn", 7000);
    }
  }
  // A result changes the ledger row, so refresh the table the operator is looking at.
  if ($("#view-signals").classList.contains("is-active")) loadSignals();
  // The drawer is a snapshot of one signal; without this it would keep showing
  // "queued" while the trade it describes is already executed.
  if (state.drawerId && events.some((event) => event.request_id === state.drawerId)) {
    openDrawer(state.drawerId);
  }
  refreshState();
}

let refreshInFlight = false;
let refreshQueued = false;

/* Coalesced: a burst of ledger events would otherwise produce a burst of
 * /api/v1/state round trips, each of which re-reads the metrics aggregates. */
function refreshState() {
  if (refreshInFlight) { refreshQueued = true; return Promise.resolve(); }
  refreshInFlight = true;
  return doRefreshState().finally(() => {
    refreshInFlight = false;
    if (refreshQueued) { refreshQueued = false; refreshState(); }
  });
}

async function doRefreshState() {
  try {
    state.snapshot = await api(`/api/v1/state?limit=1&since_event=${state.eventCursor}&window=3600`);
    pushEvents(state.snapshot.events || []);
    renderTopBar();
    if ($("#view-board").classList.contains("is-active")) renderBoard();
    if ($("#view-settings").classList.contains("is-active")) renderSettings();
  } catch (error) {
    setPill("#pill-connection", "server unreachable", "bad", error.message);
  }
}

/* A live stream that goes quiet is indistinguishable from a quiet market, so
 * the periodic refresh is what proves the server is still answering. */
function watchStreamHealth() {
  setInterval(() => {
    if (state.streamMode !== "live") return;
    if (Date.now() - state.lastEventAt > STREAM_STALL_MS) startPolling();
  }, 10000);
}

/* ------------------------------------------------------------------ wiring */

async function togglePause() {
  const paused = !(state.snapshot && state.snapshot.settings && state.snapshot.settings.paused);
  try {
    await api("/api/v1/settings", { method: "PATCH", body: { paused } });
    toast(paused ? "Dispatch paused — intake keeps recording, nothing reaches the EA" : "Dispatch resumed", paused ? "warn" : "ok");
    await refreshState();
    renderSettings();
  } catch (error) {
    toast(`Could not change pause state: ${error.message}`, "bad");
  }
}

async function rescan() {
  try {
    const result = await api("/api/v1/rescan", { method: "POST" });
    toast(`Scanned the inbox — ${result.scanned} new file(s) consumed`, result.scanned ? "ok" : "info");
    await refreshState();
    loadSignals();
  } catch (error) {
    toast(`Scan failed: ${error.message}`, "bad");
  }
}

async function clearQueue() {
  const depth = state.snapshot ? state.snapshot.queue_depth : 0;
  if (!depth) { toast("The queue is already empty.", "info"); return; }
  if (!window.confirm(`Cancel ${depth} queued signal(s)? Anything already sent to the EA cannot be recalled.`)) return;
  try {
    const result = await api("/signal/clear", { method: "POST" });
    toast(`Cancelled ${result.count}. ${result.still_dispatched} already with the EA cannot be recalled.`, "warn");
    await refreshState();
    loadSignals();
  } catch (error) {
    toast(`Could not clear the queue: ${error.message}`, "bad");
  }
}

function wire() {
  $("#tabs").addEventListener("click", (event) => {
    const tab = event.target.closest(".tab");
    if (tab) showView(tab.dataset.view);
  });
  $("#btn-pause").addEventListener("click", togglePause);
  $("#btn-rescan").addEventListener("click", rescan);
  $("#btn-clear").addEventListener("click", clearQueue);
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });

  $("#btn-refresh-signals").addEventListener("click", loadSignals);
  $("#filter-search").addEventListener("input", (event) => {
    state.filters.search = event.target.value;
    state.offset = 0;
    clearTimeout(state.filterTimer);
    state.filterTimer = setTimeout(loadSignals, 300);
  });
  for (const [selector, key] of [["#filter-status", "status"], ["#filter-symbol", "symbol"], ["#filter-action", "action"]]) {
    $(selector).addEventListener("change", (event) => {
      state.filters[key] = event.target.value;
      state.offset = 0;
      loadSignals();
    });
  }
  $("#btn-prev").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - state.limit);
    loadSignals();
  });
  $("#btn-next").addEventListener("click", () => { state.offset += state.limit; loadSignals(); });

  $("#send-json").addEventListener("input", validateDraft);
  $("#btn-validate").addEventListener("click", validateDraft);
  $("#btn-send").addEventListener("click", sendDraft);
  $("#btn-format").addEventListener("click", () => {
    try { $("#send-json").value = JSON.stringify(readDraft(), null, 2); }
    catch (error) { toast(error.message, "bad"); }
  });

  $("#btn-save-settings").addEventListener("click", saveSettings);
  $("#btn-reload-settings").addEventListener("click", () => { renderSettings(); clear($("#settings-result")); });

  window.addEventListener("resize", () => {
    if ($("#view-board").classList.contains("is-active") && state.snapshot) {
      drawThroughput(state.snapshot.metrics.throughput || []);
    }
  });
}

async function boot() {
  wire();
  buildSendTab();
  try {
    state.contract = await api("/api/v1/contract");
    renderContract();
  } catch (error) {
    toast(`Could not load the EA contract: ${error.message}`, "bad");
  }
  await refreshState();
  renderTopBar();
  showView("board");
  connectStream();
  watchStreamHealth();
  state.refreshTimer = setInterval(refreshState, REFRESH_INTERVAL_MS);
}

boot();
