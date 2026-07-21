const state = {
  data: null,
  reviewFilter: "priority",
  reviewIndex: 0,
  searchTimer: null,
  toastTimer: null,
  eventTimer: null,
  undo: null,
  dragging: null,
};

const KEY_ACTIONS = { "1": "knowledge", "2": "writing", "3": "later", "4": "cleanup" };

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const ACTION_NAMES = { knowledge: "沉淀为知识", writing: "进入写作", later: "稍后再看", cleanup: "移入待清理" };

const BOARD_COLUMNS = [
  { key: "pending", name: "待判断", dot: "red" },
  { key: "queued", name: "等待自动处理", dot: "yellow" },
  { key: "writable", name: "可写作", dot: "blue" },
  { key: "drafts", name: "写作中", dot: "purple" },
  { key: "published", name: "已发布", dot: "green" },
];

function escapeHtml(value = "") {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safeHttpUrl(value = "") {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch {
    return "";
  }
}

function formatDay(value) {
  if (!value) return "尚无记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "尚无记录";
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}

function formatRecent(value) {
  if (!value) return "";
  const date = new Date(value);
  const diff = Date.now() - date.getTime();
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(date);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json", ...(options.headers || {}) } : options.headers,
  });
  let body;
  try {
    body = await response.json();
  } catch {
    body = {};
  }
  if (!response.ok) throw new Error(body.error || "请求没有完成");
  return body;
}

async function loadDashboard({ quiet = false } = {}) {
  const refresh = $("#refresh");
  refresh.classList.add("spinning");
  try {
    state.data = await api("/api/dashboard");
    state.reviewIndex = Math.max(0, state.reviewIndex);
    renderDashboard();
    $("#loading-state").hidden = true;
    $("#dashboard").hidden = false;
    if (!quiet) showToast("工作台已刷新");
  } catch (error) {
    $("#loading-state").innerHTML = `<p>工作台没有读出来：${escapeHtml(error.message)}。请刷新重试。</p>`;
  } finally {
    refresh.classList.remove("spinning");
  }
}

function renderDashboard() {
  const data = state.data;
  const counts = data.counts;
  const now = new Date();
  $("#today-label").textContent = new Intl.DateTimeFormat("zh-CN", {
    weekday: "long", year: "numeric", month: "long", day: "numeric",
  }).format(now);

  const healthNames = { green: "运行正常", yellow: "需要关注", red: "出现异常", unknown: "等待日报" };
  $("#health-label").textContent = healthNames[data.health] || "状态未知";
  $("#health-dot").className = `health-dot ${data.health}`;
  $("#health-time").textContent = data.latest_health_at ? `健康记录 ${formatDay(data.latest_health_at)}` : "尚无健康记录";
  $("#focus-text").textContent = data.focus;
  $("#focus-note").textContent = data.health_reasons[0]
    ? `${data.health_reasons[0]}。这里的选择会进入原有自动处理流程。`
    : "当前流程通畅，可以把注意力放在真正值得推进的材料上。";
  $("#hero-count").textContent = String(counts.decision_pending).padStart(2, "0");
  $("#open-inbox").href = data.inbox_uri;

  renderPipeline(counts);
  renderReview();
  renderQueued();
  renderBoard();
  renderTrend();
  renderKnowledgeMix();
  renderRecent();
}

function renderPipeline(counts) {
  const steps = [
    ["资料池", counts.captured, "所有外部输入"],
    ["待提炼", counts.pipeline_pending, "机器队列"],
    ["待判断", counts.decision_pending, "今天的阻力"],
    ["可写作", counts.writable, "已经确认"],
    ["长期知识", counts.knowledge, "可复用资产"],
  ];
  $("#pipeline").innerHTML = steps.map(([label, value, note], index) => `
    <div class="pipeline-step ${index === 2 && value > 0 ? "attention" : ""}">
      <span>${escapeHtml(label)}</span>
      <strong>${value}</strong>
      <small>${escapeHtml(note)}</small>
    </div>
  `).join("");

  const ledger = [
    ["高价值待判断", counts.high_value],
    ["低成本可清理", counts.low_value],
    ["正在写的草稿", counts.drafts],
    ["正式发布", counts.published],
  ];
  $("#metric-ledger").innerHTML = ledger.map(([label, value]) => `
    <div class="ledger-item"><span>${escapeHtml(label)}</span><strong>${value}</strong></div>
  `).join("");
}

function filteredReviews() {
  const reviews = state.data?.reviews || [];
  if (state.reviewFilter === "priority") return reviews.filter((item) => item.score >= 90);
  if (state.reviewFilter === "cleanup") return reviews.filter((item) => item.score < 65 || item.recommendation === "清理");
  return reviews;
}

function renderReview() {
  const cards = filteredReviews();
  if (state.reviewIndex >= cards.length) state.reviewIndex = Math.max(0, cards.length - 1);
  $("#queue-title").textContent = state.reviewFilter === "priority" ? "高价值资料" : state.reviewFilter === "cleanup" ? "可快速清理" : "全部待判断";
  $("#queue-progress").textContent = cards.length ? `${state.reviewIndex + 1} / ${cards.length}` : "0 / 0";

  if (!cards.length) {
    $("#review-focus").innerHTML = `
      <div class="empty-decision">
        <span>✓</span>
        <h3>这一组已经清空</h3>
        <p>切换到其他分组继续，或者把注意力放回写作。</p>
      </div>`;
    $("#review-queue").innerHTML = "";
    return;
  }

  const item = cards[state.reviewIndex];
  const source = safeHttpUrl(item.source_url);
  $("#review-focus").innerHTML = `
    <div class="review-meta">
      <span class="score" aria-label="相关度 ${item.score} 分">${item.score}</span>
      <span class="tag">${escapeHtml(item.recommendation)}</span>
      ${item.kind ? `<span class="tag">${escapeHtml(item.kind)}</span>` : ""}
      ${item.confidence ? `<span class="tag">置信度 ${escapeHtml(item.confidence)}</span>` : ""}
    </div>
    <h3>${escapeHtml(item.title)}</h3>
    <p class="summary">${escapeHtml(item.summary || "这张卡还没有摘要，建议回到原笔记查看。")}</p>
    <div class="review-links">
      <a class="text-link" href="${escapeHtml(item.obsidian_uri)}">在 Obsidian 查看 ↗</a>
      ${source ? `<a class="text-link" href="${escapeHtml(source)}" target="_blank" rel="noopener noreferrer">查看来源 ↗</a>` : ""}
    </div>
    <div class="review-actions" aria-label="选择处理方式">
      <button class="decision-button primary" type="button" data-review-action="knowledge">沉淀为知识</button>
      <button class="decision-button writing" type="button" data-review-action="writing">进入写作</button>
      <button class="decision-button quiet" type="button" data-review-action="later">稍后再看</button>
      <button class="decision-button cleanup" type="button" data-review-action="cleanup">移入待清理</button>
    </div>
    <p class="key-hints">J / K 切换卡片 · 1–4 选择处理 · U 撤回 · O 打开原文</p>`;

  $$("[data-review-action]").forEach((button) => button.addEventListener("click", () => chooseAction(item, button.dataset.reviewAction)));

  $("#review-queue").innerHTML = cards.map((card, index) => `
    <button class="queue-item ${index === state.reviewIndex ? "active" : ""}" type="button" data-review-index="${index}">
      <span class="queue-score">${card.score}</span>
      <span><strong>${escapeHtml(card.title)}</strong><small>${escapeHtml(card.recommendation)} · ${escapeHtml(card.kind || "待分类")}</small></span>
    </button>
  `).join("");
  $$("[data-review-index]").forEach((button) => button.addEventListener("click", () => {
    state.reviewIndex = Number(button.dataset.reviewIndex);
    renderReview();
    if (window.innerWidth < 780) $("#review-focus").scrollIntoView({ behavior: "smooth", block: "start" });
  }));
}

async function chooseAction(item, action) {
  const buttons = $$("[data-review-action]");
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const result = await api("/api/reviews/action", {
      method: "POST",
      body: JSON.stringify({ path: item.path, action }),
    });
    state.undo = { path: item.path };
    state.data.reviews = state.data.reviews.filter((card) => card.path !== item.path);
    state.data.queued_reviews = [{ ...item, selected_action: action }, ...(state.data.queued_reviews || [])];
    state.data.counts.decision_pending -= 1;
    state.data.counts.queued += 1;
    if (item.score >= 90) state.data.counts.high_value = Math.max(0, state.data.counts.high_value - 1);
    if (item.score < 65 || item.recommendation === "清理") state.data.counts.low_value = Math.max(0, state.data.counts.low_value - 1);
    state.reviewIndex = Math.max(0, state.reviewIndex - 1);
    renderPipeline(state.data.counts);
    renderReview();
    renderQueued();
    renderBoard();
    showToast(`已选择「${result.label}」，等待自动处理`, true);
  } catch (error) {
    showToast(error.message);
    buttons.forEach((button) => { button.disabled = false; });
  }
}

async function undoAction() {
  if (!state.undo) return;
  try {
    await api("/api/reviews/action", {
      method: "POST",
      body: JSON.stringify({ path: state.undo.path, action: null }),
    });
    state.undo = null;
    await loadDashboard({ quiet: true });
    showToast("选择已撤销");
  } catch (error) {
    showToast(error.message);
  }
}

function renderQueued() {
  const cards = state.data.queued_reviews || [];
  const block = $("#queued-block");
  block.hidden = !cards.length;
  if (!cards.length) {
    $("#queued-list").innerHTML = "";
    return;
  }
  $("#queued-summary").textContent = `${cards.length} 条会在下一轮自动流程中执行，此前随时可撤回`;
  $("#queued-list").innerHTML = cards.map((item) => `
    <div class="queued-item">
      <span class="queued-tag ${escapeHtml(item.selected_action || "")}">${escapeHtml(ACTION_NAMES[item.selected_action] || "已勾选")}</span>
      <a href="${escapeHtml(item.obsidian_uri)}" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</a>
      <button class="queued-undo" type="button" data-queued-path="${escapeHtml(item.path)}">撤回</button>
    </div>
  `).join("");
  $$("[data-queued-path]").forEach((button) => button.addEventListener("click", () => withdrawQueued(button.dataset.queuedPath, button)));
}

async function withdrawQueued(path, button) {
  if (button) button.disabled = true;
  try {
    await api("/api/reviews/action", {
      method: "POST",
      body: JSON.stringify({ path, action: null }),
    });
    if (state.undo && state.undo.path === path) state.undo = null;
    await loadDashboard({ quiet: true });
    showToast("已撤回，卡片回到待判断队列");
  } catch (error) {
    showToast(error.message);
    if (button) button.disabled = false;
  }
}

function boardCard(item, column) {
  const props = [];
  if (column === "pending" || column === "queued") {
    props.push(`<span class="kprop score">${Number(item.score) || 0}</span>`);
    if (item.kind) props.push(`<span class="kprop">${escapeHtml(item.kind)}</span>`);
  }
  if (column === "queued") {
    props.push(`<span class="kprop action ${escapeHtml(item.selected_action || "")}">${escapeHtml(ACTION_NAMES[item.selected_action] || "已勾选")}</span>`);
  }
  if (column === "published" && item.platform) props.push(`<span class="kprop">${escapeHtml(item.platform)}</span>`);
  if (item.modified) props.push(`<span class="kprop time">${escapeHtml(formatRecent(item.modified))}</span>`);
  const draggable = column === "pending" || column === "queued";
  return `
    <div class="kcard${draggable ? " draggable" : ""}" ${draggable ? 'draggable="true"' : ""} data-card-path="${escapeHtml(item.path)}" data-card-column="${column}">
      <a class="kcard-title" href="${escapeHtml(item.obsidian_uri)}">${escapeHtml(item.title)}</a>
      ${props.length ? `<span class="kcard-props">${props.join("")}</span>` : ""}
      ${column === "queued" ? `<button class="kcard-undo" type="button" data-kanban-undo="${escapeHtml(item.path)}" aria-label="撤回这张卡">撤回</button>` : ""}
    </div>`;
}

function renderBoard() {
  const d = state.data;
  const columnData = {
    pending: d.reviews || [],
    queued: d.queued_reviews || [],
    writable: d.writable || [],
    drafts: d.drafts || [],
    published: d.published || [],
  };
  const LIMIT = 20;
  $("#board-columns").innerHTML = BOARD_COLUMNS.map((col) => {
    const cards = columnData[col.key];
    const shown = cards.slice(0, LIMIT);
    return `
      <div class="board-col" data-column="${col.key}">
        <div class="board-col-head">
          <span class="col-dot ${col.dot}"></span>
          <strong>${col.name}</strong>
          <span class="board-col-count">${cards.length}</span>
        </div>
        <div class="board-cards">
          ${shown.map((item) => boardCard(item, col.key)).join("") || `<p class="board-empty">暂无卡片</p>`}
          ${cards.length > LIMIT ? `<p class="board-more">还有 ${cards.length - LIMIT} 条</p>` : ""}
        </div>
      </div>`;
  }).join("");
  bindBoardEvents();
}

function canDropTo(key) {
  const from = state.dragging;
  if (!from) return false;
  return (key === "queued" && from.column === "pending") || (key === "pending" && from.column === "queued");
}

function bindBoardEvents() {
  $$("#board-columns .kcard[draggable='true']").forEach((card) => {
    card.addEventListener("dragstart", (event) => {
      state.dragging = { path: card.dataset.cardPath, column: card.dataset.cardColumn };
      card.classList.add("dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", card.dataset.cardPath);
    });
    card.addEventListener("dragend", () => {
      state.dragging = null;
      card.classList.remove("dragging");
      $$("#board-columns .board-col").forEach((col) => col.classList.remove("drop-target"));
    });
  });
  $$("#board-columns .board-col").forEach((col) => {
    const key = col.dataset.column;
    col.addEventListener("dragover", (event) => {
      if (!canDropTo(key)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      col.classList.add("drop-target");
    });
    col.addEventListener("dragleave", () => col.classList.remove("drop-target"));
    col.addEventListener("drop", (event) => {
      col.classList.remove("drop-target");
      const from = state.dragging;
      if (!from || !canDropTo(key)) return;
      event.preventDefault();
      if (key === "queued" && from.column === "pending") showActionMenu(event.clientX, event.clientY, from.path);
      else if (key === "pending" && from.column === "queued") withdrawQueued(from.path);
    });
  });
  $$("[data-kanban-undo]").forEach((button) => button.addEventListener("click", () => withdrawQueued(button.dataset.kanbanUndo, button)));
}

function showActionMenu(x, y, path) {
  const menu = $("#action-menu");
  menu.innerHTML = `
    <p>这张卡怎么处理？</p>
    ${Object.entries(ACTION_NAMES).map(([key, name]) => `<button type="button" data-menu-action="${key}">${name}</button>`).join("")}
    <button type="button" class="cancel" data-menu-cancel>取消</button>`;
  menu.hidden = false;
  const rect = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(12, Math.min(x, window.innerWidth - rect.width - 12))}px`;
  menu.style.top = `${Math.max(12, Math.min(y, window.innerHeight - rect.height - 12))}px`;
  menu.querySelectorAll("[data-menu-action]").forEach((button) => button.addEventListener("click", () => {
    hideActionMenu();
    const item = (state.data.reviews || []).find((card) => card.path === path);
    if (item) chooseAction(item, button.dataset.menuAction);
  }));
  menu.querySelector("[data-menu-cancel]").addEventListener("click", hideActionMenu);
}

function hideActionMenu() {
  const menu = $("#action-menu");
  menu.hidden = true;
  menu.innerHTML = "";
}

function renderTrend() {
  const points = state.data.trend || [];
  const current = points.at(-1)?.pending ?? state.data.counts.decision_pending;
  $("#trend-current").textContent = `${current} 条`;
  if (!points.length) {
    $("#trend-chart").innerHTML = `<p class="empty-row">健康日报生成后，这里会出现趋势。</p>`;
    return;
  }
  const width = 620;
  const height = 210;
  const pad = { x: 30, top: 24, bottom: 31 };
  const values = points.map((point) => Number(point.pending) || 0);
  const max = Math.max(...values, 10);
  const min = Math.min(...values, 0);
  const span = Math.max(1, max - min);
  const coords = values.map((value, index) => {
    const x = points.length === 1 ? width / 2 : pad.x + index * ((width - pad.x * 2) / (points.length - 1));
    const y = pad.top + (max - value) * ((height - pad.top - pad.bottom) / span);
    return { x, y, value, point: points[index] };
  });
  const line = coords.map((coord) => `${coord.x},${coord.y}`).join(" ");
  const area = `${coords[0].x},${height - pad.bottom} ${line} ${coords.at(-1).x},${height - pad.bottom}`;
  const grid = [0, .5, 1].map((ratio) => {
    const y = pad.top + ratio * (height - pad.top - pad.bottom);
    const label = Math.round(max - ratio * span);
    return `<line class="grid-line" x1="${pad.x}" y1="${y}" x2="${width - pad.x}" y2="${y}"/><text x="0" y="${y + 3}">${label}</text>`;
  }).join("");
  const dots = coords.map((coord, index) => `
    <circle cx="${coord.x}" cy="${coord.y}" r="5"><title>${escapeHtml(coord.point.date)}：${coord.value} 条</title></circle>
    <text x="${coord.x}" y="${height - 7}" text-anchor="middle">${escapeHtml(coord.point.date.slice(5))}</text>
  `).join("");
  $("#trend-chart").innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="审核积压从 ${values[0]} 条变化到 ${values.at(-1)} 条">
      ${grid}<polygon class="trend-area" points="${area}"/><polyline class="trend-line" points="${line}"/>${dots}
    </svg>`;
}

function renderKnowledgeMix() {
  const labelMap = {
    concept: "概念", entity: "人物组织", question: "问题", viewpoint: "观点",
    case: "案例", playbook: "方法", map: "地图", guide: "指南", "未标记": "未标记",
  };
  const entries = Object.entries(state.data.knowledge_kinds || {}).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((sum, [, value]) => sum + value, 0);
  const max = Math.max(...entries.map(([, value]) => value), 1);
  $("#knowledge-total").textContent = `${total} 篇`;
  $("#knowledge-mix").innerHTML = entries.map(([kind, value]) => `
    <div class="mix-row">
      <span>${escapeHtml(labelMap[kind] || kind)}</span>
      <div class="mix-track"><progress max="${max}" value="${value}" aria-label="${escapeHtml(labelMap[kind] || kind)} ${value} 篇"></progress></div>
      <strong>${value}</strong>
    </div>
  `).join("");
}

function renderRecent() {
  $("#recent-list").innerHTML = (state.data.recent || []).map((item) => `
    <a class="recent-item" href="${escapeHtml(item.obsidian_uri)}">
      <small>${escapeHtml(item.area)}${item.kind ? ` · ${escapeHtml(item.kind)}` : ""}</small>
      <strong>${escapeHtml(item.title)}</strong>
      <time datetime="${escapeHtml(item.modified)}">${escapeHtml(formatRecent(item.modified))}</time>
    </a>
  `).join("");
}

function connectEvents() {
  if (!("EventSource" in window)) return;
  const source = new EventSource("/api/events");
  source.addEventListener("change", () => {
    clearTimeout(state.eventTimer);
    state.eventTimer = setTimeout(refreshFromEvents, 400);
  });
}

async function refreshFromEvents() {
  const busy = $("#capture-drawer").classList.contains("open") || !$("#action-menu").hidden || state.dragging;
  if (busy) {
    clearTimeout(state.eventTimer);
    state.eventTimer = setTimeout(refreshFromEvents, 3000);
    return;
  }
  const previous = state.data;
  await loadDashboard({ quiet: true });
  maybeNotify(previous, state.data);
}

function maybeNotify(previous, next) {
  if (!previous || !next) return;
  const gained = next.counts.decision_pending - previous.counts.decision_pending;
  if (gained > 0) notifyUser(`${gained} 张新审核卡等你判断`);
  if (next.health === "red" && previous.health !== "red") {
    notifyUser(next.health_reasons[0] || "采集流程出现异常");
  }
}

function notificationsOn() {
  return "Notification" in window && localStorage.getItem("rb-notify") === "on";
}

function notifyUser(message) {
  if (notificationsOn() && Notification.permission === "granted" && document.visibilityState !== "visible") {
    const notice = new Notification("Ray's Brain", { body: message });
    notice.onclick = () => window.focus();
  } else {
    showToast(message);
  }
}

async function toggleNotifications() {
  if (!("Notification" in window)) {
    showToast("这个浏览器不支持系统通知");
    return;
  }
  if (notificationsOn()) {
    localStorage.setItem("rb-notify", "off");
    renderNotifyToggle();
    showToast("已关闭系统通知");
    return;
  }
  let permission = Notification.permission;
  if (permission === "default") permission = await Notification.requestPermission();
  if (permission !== "granted") {
    showToast("通知权限未授予，可在浏览器设置里开启");
    return;
  }
  localStorage.setItem("rb-notify", "on");
  renderNotifyToggle();
  showToast("有新审核卡或采集异常时会通知你");
}

function renderNotifyToggle() {
  $("#notify-toggle").classList.toggle("active", notificationsOn() && Notification.permission === "granted");
}

function showToast(message, withUndo = false) {
  clearTimeout(state.toastTimer);
  const toast = $("#toast");
  $("#toast-message").textContent = message;
  $("#toast-action").hidden = !withUndo;
  toast.hidden = false;
  state.toastTimer = setTimeout(() => {
    toast.hidden = true;
    if (withUndo) state.undo = null;
  }, withUndo ? 8000 : 3500);
}

function openCapture() {
  $("#capture-drawer").classList.add("open");
  $("#capture-drawer").setAttribute("aria-hidden", "false");
  $("#capture-drawer").removeAttribute("inert");
  $("#drawer-scrim").hidden = false;
  requestAnimationFrame(() => $("#drawer-scrim").classList.add("visible"));
  setTimeout(() => $("#capture-text").focus(), 180);
}

function closeCapture() {
  $("#capture-drawer").classList.remove("open");
  $("#capture-drawer").setAttribute("aria-hidden", "true");
  $("#capture-drawer").setAttribute("inert", "");
  $("#drawer-scrim").classList.remove("visible");
  setTimeout(() => { $("#drawer-scrim").hidden = true; }, 250);
  $("#capture-open").focus();
}

async function runSearch() {
  const input = $("#global-search");
  const query = input.value.trim();
  const tray = $("#search-tray");
  if (query.length < 2) {
    tray.hidden = true;
    $("#search-results").innerHTML = "";
    return;
  }
  tray.hidden = false;
  $("#search-summary").textContent = "正在搜索…";
  try {
    const payload = await api(`/api/search?q=${encodeURIComponent(query)}&scope=${encodeURIComponent($("#search-scope").value)}`);
    $("#search-summary").textContent = payload.results.length ? `找到 ${payload.results.length} 条结果` : "没有找到相关笔记";
    $("#search-results").innerHTML = payload.results.map((item) => `
      <a class="search-result" href="${escapeHtml(item.obsidian_uri)}">
        <strong>${escapeHtml(item.title)}</strong>
        <p>${escapeHtml(item.excerpt)}</p>
        <span>↗</span>
      </a>
    `).join("") || `<p class="empty-row">换一个更短的关键词试试。</p>`;
  } catch (error) {
    $("#search-summary").textContent = error.message;
  }
}

function bindEvents() {
  $("#refresh").addEventListener("click", () => loadDashboard());
  $("#notify-toggle").addEventListener("click", toggleNotifications);
  $("#capture-open").addEventListener("click", openCapture);
  $("#capture-close").addEventListener("click", closeCapture);
  $("#drawer-scrim").addEventListener("click", closeCapture);
  $("#toast-action").addEventListener("click", undoAction);
  $("#capture-text").addEventListener("input", (event) => {
    $("#capture-count").textContent = `${event.target.value.length} / 2000`;
  });
  $("#capture-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = $("#capture-text").value;
    const button = event.currentTarget.querySelector("button[type=submit]");
    button.disabled = true;
    button.textContent = "正在记入…";
    try {
      await api("/api/capture", { method: "POST", body: JSON.stringify({ text }) });
      $("#capture-text").value = "";
      $("#capture-count").textContent = "0 / 2000";
      closeCapture();
      showToast("灵感已记入收件箱");
      await loadDashboard({ quiet: true });
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
      button.textContent = "记入收件箱";
    }
  });

  $$(".filter-tab").forEach((button) => button.addEventListener("click", () => {
    state.reviewFilter = button.dataset.filter;
    state.reviewIndex = 0;
    $$(".filter-tab").forEach((item) => item.classList.toggle("active", item === button));
    renderReview();
  }));

  $("#global-search").addEventListener("input", () => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(runSearch, 180);
  });
  $("#search-scope").addEventListener("change", runSearch);
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      $("#global-search").focus();
      return;
    }
    if (event.key === "Escape") {
      if (!$("#action-menu").hidden) hideActionMenu();
      else if ($("#capture-drawer").classList.contains("open")) closeCapture();
      else {
        $("#global-search").value = "";
        $("#search-tray").hidden = true;
      }
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.target instanceof Element && event.target.closest("input, textarea, select")) return;
    if (!$("#action-menu").hidden) return;
    const key = event.key.toLowerCase();
    const cards = filteredReviews();
    const item = cards[state.reviewIndex];
    if (key === "j" || key === "k") {
      if (!cards.length) return;
      event.preventDefault();
      state.reviewIndex = Math.min(cards.length - 1, Math.max(0, state.reviewIndex + (key === "j" ? 1 : -1)));
      renderReview();
    } else if (KEY_ACTIONS[key] && item) {
      event.preventDefault();
      chooseAction(item, KEY_ACTIONS[key]);
    } else if (key === "u" && state.undo) {
      event.preventDefault();
      undoAction();
    } else if (key === "o" && item) {
      event.preventDefault();
      window.location.href = item.obsidian_uri;
    } else if (key === "/") {
      event.preventDefault();
      $("#global-search").focus();
    }
  });
  document.addEventListener("pointerdown", (event) => {
    const menu = $("#action-menu");
    if (!menu.hidden && !menu.contains(event.target)) hideActionMenu();
  });

  const observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    $$(".rail-link").forEach((link) => link.classList.toggle("active", link.getAttribute("href") === `#${visible.target.id}`));
  }, { rootMargin: "-20% 0px -60%", threshold: [0, .2, .6] });
  ["today", "review", "board", "knowledge"].forEach((id) => observer.observe(document.getElementById(id)));
}

bindEvents();
renderNotifyToggle();
connectEvents();
loadDashboard({ quiet: true });
