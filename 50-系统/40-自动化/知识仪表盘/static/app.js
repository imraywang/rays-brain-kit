const state = {
  data: null,
  reviewFilter: "priority",
  reviewIndex: 0,
  searchTimer: null,
  toastTimer: null,
  eventTimer: null,
  undo: null,
  dragging: null,
  note: { open: false, follow: false, stack: [], current: null },
  angleTarget: null,
};

const EXPECTED_SCHEMA_VERSION = 3;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function reviewActions() {
  return state.data?.review_actions || [];
}

function actionName(key) {
  const canonical = key === "writing" ? "topic" : key;
  return reviewActions().find((item) => item.key === canonical)?.ui_label || "已勾选";
}

function actionClass(key) {
  return {
    knowledge: "primary",
    topic: "topic",
    both: "both",
    paused: "quiet",
    cleanup: "cleanup",
  }[key] || "";
}

const BOARD_COLUMNS = [
  { key: "pending", name: "待判断", dot: "red" },
  { key: "queued", name: "等待处理", dot: "yellow" },
  { key: "topics", name: "候选选题", dot: "blue" },
  { key: "continuations", name: "可续写", dot: "blue" },
  { key: "tasks", name: "写作任务", dot: "purple" },
  { key: "drafts", name: "草稿", dot: "purple" },
  { key: "published", name: "已发布", dot: "green" },
  { key: "feedback", name: "待复盘", dot: "yellow" },
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

function valueProps(item) {
  const props = [];
  if (String(item.knowledge_value ?? "").trim()) {
    props.push(`<span class="kprop value knowledge-value">知识 ${escapeHtml(item.knowledge_value)}</span>`);
  }
  if (String(item.writing_value ?? "").trim()) {
    props.push(`<span class="kprop value writing-value">写作 ${escapeHtml(item.writing_value)}</span>`);
  }
  if (String(item.timeliness ?? "").trim()) {
    props.push(`<span class="kprop value timeliness">时效 ${escapeHtml(item.timeliness)}</span>`);
  }
  return props;
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
    const payload = await api("/api/dashboard");
    if (payload.schema_version !== EXPECTED_SCHEMA_VERSION) {
      throw new Error("后台还是旧版本，请重新启动知识仪表盘");
    }
    state.data = payload;
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
    ["待判断", counts.decision_pending, "今天的阻力"],
    ["候选选题", counts.topic_candidates, "通过审核"],
    ["可续写", counts.topic_continuations, "已有剩余角度"],
    ["写作任务", counts.writing_tasks, "已经立项"],
    ["草稿", counts.drafts, "正在写"],
    ["已发布", counts.published, "内容成品"],
  ];
  $("#pipeline").innerHTML = steps.map(([label, value, note], index) => `
    <div class="pipeline-step ${index === 1 && value > 0 ? "attention" : ""}">
      <span>${escapeHtml(label)}</span>
      <strong>${value}</strong>
      <small>${escapeHtml(note)}</small>
    </div>
  `).join("");

  const ledger = [
    ["高价值待判断", counts.high_value],
    ["低成本可清理", counts.low_value],
    ["长期知识", counts.knowledge],
    ["待反馈", counts.feedback_pending],
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
      ${valueProps(item).join("")}
    </div>
    <h3><a href="#" data-note-path="${escapeHtml(item.path)}" data-note-follow="1">${escapeHtml(item.title)}</a></h3>
    <p class="summary">${escapeHtml(item.summary || "这张卡还没有摘要，点标题阅读全文再判断。")}</p>
    <div class="review-links">
      <a class="text-link" href="#" data-note-path="${escapeHtml(item.path)}" data-note-follow="1">阅读全文 →</a>
      <a class="text-link" href="${escapeHtml(item.obsidian_uri)}">在 Obsidian 打开 ↗</a>
      ${source ? `<a class="text-link" href="${escapeHtml(source)}" target="_blank" rel="noopener noreferrer">查看来源 ↗</a>` : ""}
    </div>
    <div class="review-actions" aria-label="选择处理方式">
      ${reviewActions().map((action) => `
        <button class="decision-button ${actionClass(action.key)}" type="button"
          data-review-action="${escapeHtml(action.key)}">${escapeHtml(action.ui_label)}</button>
      `).join("")}
    </div>
    <p class="key-hints">J / K 切换卡片 · 1–5 选择处理 · U 撤回 · O 阅读全文 · ⇧O 打开 Obsidian</p>`;

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
    syncNoteDrawerWithReview();
    showToast(`已选择「${result.label}」，等待自动处理`, true);
  } catch (error) {
    showToast(error.message);
    buttons.forEach((button) => { button.disabled = false; });
  }
}

async function undoAction() {
  if (!state.undo) return;
  const undo = state.undo;
  try {
    if (undo.type === "transition") {
      await transitionNote(undo.path, undo.to);
      state.undo = null;
      await loadDashboard({ quiet: true });
      if (state.note.open && state.note.current?.path === undo.path) {
        await openNote({ path: undo.path }, { push: false });
      }
      showToast("状态已撤回");
      return;
    }
    await api("/api/reviews/action", {
      method: "POST",
      body: JSON.stringify({ path: undo.path, action: null }),
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
      <span class="queued-tag ${escapeHtml(item.selected_action || "")}">${escapeHtml(actionName(item.selected_action))}</span>
      <a href="#" data-note-path="${escapeHtml(item.path)}" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</a>
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
  if ((column === "topics" || column === "continuations" || column === "tasks") && Number(item.priority_score)) {
    props.push(`<span class="kprop score">优先 ${Number(item.priority_score)}</span>`);
  }
  props.push(...valueProps(item));
  if (column === "queued") {
    props.push(`<span class="kprop action ${escapeHtml(item.selected_action || "")}">${escapeHtml(actionName(item.selected_action))}</span>`);
  }
  if (column === "published" && item.platform) props.push(`<span class="kprop">${escapeHtml(item.platform)}</span>`);
  if (column === "feedback" && item.due_at) props.push(`<span class="kprop timeliness">截止 ${escapeHtml(String(item.due_at))}</span>`);
  if (item.modified) props.push(`<span class="kprop time">${escapeHtml(formatRecent(item.modified))}</span>`);
  const statusLabel = statusLabelOf(item.kind, item.status);
  if (statusLabel && ["topics", "continuations", "tasks"].includes(column)) {
    props.unshift(`<span class="kprop status">${escapeHtml(statusLabel)}</span>`);
  }
  const draggable = ["pending", "queued", "topics", "continuations"].includes(column);
  return `
    <div class="kcard${draggable ? " draggable" : ""}" ${draggable ? 'draggable="true"' : ""} data-card-path="${escapeHtml(item.path)}" data-card-column="${column}">
      <a class="kcard-title" href="#" data-note-path="${escapeHtml(item.path)}">${escapeHtml(item.title)}</a>
      ${props.length ? `<span class="kcard-props">${props.join("")}</span>` : ""}
      ${column === "queued" ? `<button class="kcard-undo" type="button" data-kanban-undo="${escapeHtml(item.path)}" aria-label="撤回这张卡">撤回</button>` : ""}
      ${column === "feedback" ? `<button class="kcard-quick" type="button" data-feedback-done="${escapeHtml(item.path)}">✓ 已复盘</button>` : ""}
    </div>`;
}

function statusLabelOf(kind, status) {
  const proto = state.data?.board_protocol?.[kind];
  return proto?.status_labels?.[status] || "";
}

function renderBoard() {
  const d = state.data;
  const columnData = {
    pending: d.reviews || [],
    queued: d.queued_reviews || [],
    topics: d.topic_candidates || [],
    continuations: d.topic_continuations || [],
    tasks: d.writing_tasks || [],
    drafts: d.drafts || [],
    published: d.published || [],
    feedback: (d.feedback || []).filter((item) => item.pending),
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
  if (key === "queued" && from.column === "pending") return true;
  if (key === "pending" && from.column === "queued") return true;
  if (key === "tasks" && ["topics", "continuations"].includes(from.column)) return true;
  return false;
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
      else if (key === "tasks") openAngleDialog(from.path);
    });
  });
  $$("[data-kanban-undo]").forEach((button) => button.addEventListener("click", () => withdrawQueued(button.dataset.kanbanUndo, button)));
  $$("[data-feedback-done]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await markFeedbackReviewed(button.dataset.feedbackDone);
    } catch (error) {
      showToast(error.message);
      button.disabled = false;
    }
  }));
}

function showActionMenu(x, y, path) {
  const menu = $("#action-menu");
  menu.innerHTML = `
    <p>这张卡怎么处理？</p>
    ${reviewActions().map((action) => `<button type="button" data-menu-action="${escapeHtml(action.key)}">${escapeHtml(action.ui_label)}</button>`).join("")}
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
    <a class="recent-item" href="#" data-note-path="${escapeHtml(item.path)}">
      <small>${escapeHtml(item.area)}${item.kind ? ` · ${escapeHtml(item.kind)}` : ""}</small>
      <strong>${escapeHtml(item.title)}</strong>
      <time datetime="${escapeHtml(item.modified)}">${escapeHtml(formatRecent(item.modified))}</time>
    </a>
  `).join("");
}

/* ---- Markdown 渲染：零依赖，覆盖 vault 常用子集；不透传原始 HTML ---- */

function splitOnce(value, separator) {
  const index = value.indexOf(separator);
  return index < 0 ? [value, ""] : [value.slice(0, index), value.slice(index + 1)];
}

const IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|avif|bmp)$/i;

function mdInline(text) {
  let out = escapeHtml(text);
  const codes = [];
  out = out.replace(/`([^`]+)`/g, (m, code) => {
    codes.push(code);
    return `${codes.length - 1}`;
  });
  out = out.replace(/!\[\[([^\]]+)\]\]/g, (m, inner) => {
    const [targetRaw, alias] = splitOnce(inner, "|");
    const target = targetRaw.trim();
    if (IMAGE_EXT.test(target)) {
      return `<img class="md-img" loading="lazy" src="/api/asset?link=${encodeURIComponent(target)}" alt="${alias.trim() || target}">`;
    }
    return `<a href="#" class="wikilink embed" data-wikilink="${target}">📄 ${alias.trim() || target}</a>`;
  });
  out = out.replace(/\[\[([^\]]+)\]\]/g, (m, inner) => {
    const [targetRaw, alias] = splitOnce(inner, "|");
    const target = targetRaw.trim();
    return `<a href="#" class="wikilink" data-wikilink="${target}">${alias.trim() || target}</a>`;
  });
  out = out.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+[^)]*)?\)/g, (m, alt, url) => {
    if (/^https?:\/\//i.test(url)) {
      return `<a href="${url}" target="_blank" rel="noopener noreferrer">🖼 ${alt || "外部图片"}</a>`;
    }
    const name = url.split("/").pop();
    return `<img class="md-img" loading="lazy" src="/api/asset?link=${encodeURIComponent(decodeURIComponent(name))}" alt="${alt}">`;
  });
  out = out.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+[^)]*)?\)/g, (m, label, url) =>
    /^(https?:|obsidian:)/i.test(url)
      ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`
      : label);
  out = out.replace(/(^|[^"'=\]>])(https?:\/\/[^\s<>&]+[^\s<>&.,;:!?)])/g,
    '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  out = out.replace(/==([^=]+)==/g, "<mark>$1</mark>");
  out = out.replace(/(\d+)/g, (m, index) => `<code>${codes[Number(index)]}</code>`);
  return out;
}

function renderMarkdown(source, { skipTitle = "" } = {}) {
  const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
  const html = [];
  let sawContent = false;
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      const buffer = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) {
        buffer.push(lines[i]);
        i += 1;
      }
      i += 1;
      html.push(`<pre><code>${escapeHtml(buffer.join("\n"))}</code></pre>`);
      sawContent = true;
      continue;
    }
    if (!line.trim()) { i += 1; continue; }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      i += 1;
      if (!sawContent && heading[1].length === 1 && skipTitle && heading[2].trim() === skipTitle) {
        sawContent = true;
        continue;
      }
      sawContent = true;
      const level = Math.min(heading[1].length + 1, 6);
      html.push(`<h${level}>${mdInline(heading[2])}</h${level}>`);
      continue;
    }
    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { html.push("<hr>"); i += 1; sawContent = true; continue; }
    if (/^>/.test(line)) {
      const buffer = [];
      while (i < lines.length && /^>/.test(lines[i])) {
        buffer.push(lines[i].replace(/^>\s?/, ""));
        i += 1;
      }
      html.push(`<blockquote>${buffer.map(mdInline).join("<br>")}</blockquote>`);
      sawContent = true;
      continue;
    }
    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const items = [];
      while (i < lines.length) {
        const item = lines[i].match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/);
        if (!item) break;
        const depth = Math.min(3, Math.floor(item[1].replace(/\t/g, "  ").length / 2));
        const task = item[3].match(/^\[([ xX])\]\s*(.*)$/);
        const body = task
          ? `<input type="checkbox" disabled${task[1] === " " ? "" : " checked"}> ${mdInline(task[2])}`
          : mdInline(item[3]);
        items.push(`<li class="md-indent-${depth}${task ? " md-task" : ""}">${body}</li>`);
        i += 1;
      }
      html.push(`<ul class="md-list">${items.join("")}</ul>`);
      sawContent = true;
      continue;
    }
    if (/^\|/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\|/.test(lines[i])) {
        rows.push(lines[i]);
        i += 1;
      }
      const cellsOf = (row) => row.replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
      const bodyRows = rows.filter((row) => !/^[\s:|-]+$/.test(row));
      const table = bodyRows.map((row, index) => {
        const tag = index === 0 ? "th" : "td";
        return `<tr>${cellsOf(row).map((cell) => `<${tag}>${mdInline(cell)}</${tag}>`).join("")}</tr>`;
      }).join("");
      html.push(`<div class="md-table"><table>${table}</table></div>`);
      sawContent = true;
      continue;
    }
    const buffer = [];
    while (
      i < lines.length && lines[i].trim()
      && !/^\s*```|^(#{1,6})\s|^>|^\s*([-*+]|\d+\.)\s+|^\||^(-{3,}|\*{3,}|_{3,})\s*$/.test(lines[i])
    ) {
      buffer.push(lines[i]);
      i += 1;
    }
    html.push(`<p>${buffer.map(mdInline).join("<br>")}</p>`);
    sawContent = true;
  }
  return html.join("");
}

/* ---- 阅读抽屉 ---- */

function noteDrawerOpenState() {
  return $("#note-drawer").classList.contains("open");
}

async function transitionNote(path, to, expectedMtime = null) {
  if (expectedMtime === null) {
    const fresh = await api(`/api/note?path=${encodeURIComponent(path)}`);
    expectedMtime = fresh.mtime_ns;
  }
  return api("/api/note/transition", {
    method: "POST",
    body: JSON.stringify({ path, to, expected_mtime_ns: expectedMtime }),
  });
}

function reverseTransitionOf(kind, from, to) {
  const proto = state.data?.board_protocol?.[kind];
  return proto?.transitions?.find((t) => t.from === to && t.to === from) || null;
}

async function runTransition(note, transition) {
  if (transition.confirm && !window.confirm(transition.confirm)) return;
  try {
    const result = await transitionNote(note.path, transition.to, note.mtime_ns);
    if (reverseTransitionOf(note.kind, transition.from, transition.to)) {
      state.undo = { type: "transition", path: note.path, to: transition.from };
      showToast(`已${result.label}（现在是「${result.to_label}」）`, true);
    } else {
      showToast(`已${result.label}`);
    }
    await loadDashboard({ quiet: true });
    if (noteDrawerOpenState() && state.note.current?.path === note.path) {
      await openNote({ path: note.path }, { push: false });
    }
  } catch (error) {
    showToast(error.message);
  }
}

async function markFeedbackReviewed(path) {
  const note = await api(`/api/note?path=${encodeURIComponent(path)}`);
  const proto = state.data?.board_protocol?.[note.kind];
  const transition = proto?.transitions?.find((t) => t.from === note.status && t.to === "reviewed");
  if (!transition) throw new Error("这张卡当前不支持标记复盘");
  await runTransition(note, transition);
}

function noteContextActions(note) {
  const buttons = [];
  const status = (note.frontmatter?.status || "").trim();
  const pendingCard = (state.data?.reviews || []).find((card) => card.path === note.path);
  const queuedCard = (state.data?.queued_reviews || []).find((card) => card.path === note.path);
  if (pendingCard) {
    buttons.push(...reviewActions().map((action) => `
      <button class="decision-button ${actionClass(action.key)}" type="button"
        data-drawer-review="${escapeHtml(action.key)}">${escapeHtml(action.ui_label)}</button>`));
  } else if (queuedCard) {
    buttons.push(`<button class="decision-button" type="button" data-drawer-withdraw="1">撤回「${escapeHtml(actionName(queuedCard.selected_action))}」</button>`);
  }
  if (note.kind === "topic-candidate" && ["candidate", "parked"].includes(status)) {
    buttons.push(`<button class="decision-button primary" type="button" data-drawer-promote="1">立项为写作任务…</button>`);
  }
  const proto = state.data?.board_protocol?.[note.kind];
  if (proto) {
    proto.transitions.filter((t) => t.from === status).forEach((t, index) => {
      buttons.push(`<button class="decision-button${t.to === "cancelled" || t.to === "closed" ? " cleanup" : ""}" type="button" data-drawer-transition="${index}">${escapeHtml(t.label)}</button>`);
    });
  }
  return buttons.join("");
}

function bindNoteActions(note) {
  const status = (note.frontmatter?.status || "").trim();
  const proto = state.data?.board_protocol?.[note.kind];
  const available = proto ? proto.transitions.filter((t) => t.from === status) : [];
  $$("#note-actions [data-drawer-transition]").forEach((button) => {
    button.addEventListener("click", () => {
      const transition = available[Number(button.dataset.drawerTransition)];
      if (transition) runTransition(note, transition);
    });
  });
  $$("#note-actions [data-drawer-review]").forEach((button) => {
    button.addEventListener("click", () => {
      const card = (state.data?.reviews || []).find((item) => item.path === note.path);
      if (!card) return;
      const cards = filteredReviews();
      const index = cards.findIndex((item) => item.path === note.path);
      if (index >= 0) state.reviewIndex = index;
      chooseAction(card, button.dataset.drawerReview);
    });
  });
  const withdraw = $("#note-actions [data-drawer-withdraw]");
  if (withdraw) {
    withdraw.addEventListener("click", async () => {
      await withdrawQueued(note.path);
      if (noteDrawerOpenState()) await openNote({ path: note.path }, { push: false });
    });
  }
  const promote = $("#note-actions [data-drawer-promote]");
  if (promote) promote.addEventListener("click", () => openAngleDialog(note.path, note.title));
}

function renderNoteDrawer(note) {
  state.note.current = note;
  const meta = note.frontmatter || {};
  $("#note-path").textContent = note.path.length > 58 ? `…${note.path.slice(-58)}` : note.path;
  $("#note-obsidian").href = note.obsidian_uri;
  $("#note-kicker").textContent = [note.kind, statusLabelOf(note.kind, note.status) || note.status]
    .filter(Boolean).join(" · ") || "笔记";
  $("#note-title").textContent = note.title;
  const chips = [];
  const score = Number(meta.priority_score || meta.relevance_score || 0);
  if (score) chips.push(`<span class="kprop score">${score}</span>`);
  chips.push(...valueProps({
    knowledge_value: meta.knowledge_value_score || meta.knowledge_value || "",
    writing_value: meta.writing_value_score || meta.writing_value || "",
    timeliness: meta.timeliness || meta.freshness_status || "",
  }));
  if (meta.selected_angle) chips.push(`<span class="kprop">角度：${escapeHtml(meta.selected_angle)}</span>`);
  if (meta.fresh_until) chips.push(`<span class="kprop timeliness">新鲜至 ${escapeHtml(meta.fresh_until)}</span>`);
  const source = safeHttpUrl(meta.source_url || "");
  if (source) chips.push(`<a class="kprop link" href="${escapeHtml(source)}" target="_blank" rel="noopener noreferrer">来源 ↗</a>`);
  $("#note-chips").innerHTML = chips.join("");
  $("#note-actions").innerHTML = noteContextActions(note);
  $("#note-body").innerHTML = renderMarkdown(note.body, { skipTitle: note.title });
  bindNoteActions(note);
  $("#note-back").hidden = state.note.stack.length < 2;
  $("#note-scroll").scrollTop = 0;
}

async function openNote(ref, { push = true, follow = false } = {}) {
  const query = ref.path
    ? `path=${encodeURIComponent(ref.path)}`
    : `link=${encodeURIComponent(ref.link || "")}`;
  try {
    const note = await api(`/api/note?${query}`);
    if (push && state.note.current?.path !== note.path) state.note.stack.push(note.path);
    if (!push && state.note.stack.length) state.note.stack[state.note.stack.length - 1] = note.path;
    if (!state.note.stack.length) state.note.stack.push(note.path);
    state.note.follow = follow;
    renderNoteDrawer(note);
    if (!noteDrawerOpenState()) {
      const drawer = $("#note-drawer");
      drawer.classList.add("open");
      drawer.setAttribute("aria-hidden", "false");
      drawer.removeAttribute("inert");
      $("#note-scrim").hidden = false;
      requestAnimationFrame(() => $("#note-scrim").classList.add("visible"));
      state.note.open = true;
    }
  } catch (error) {
    showToast(error.message);
  }
}

function closeNoteDrawer() {
  const drawer = $("#note-drawer");
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  drawer.setAttribute("inert", "");
  $("#note-scrim").classList.remove("visible");
  setTimeout(() => { $("#note-scrim").hidden = true; }, 250);
  state.note = { open: false, follow: false, stack: [], current: null };
}

async function noteDrawerBack() {
  if (state.note.stack.length < 2) return;
  state.note.stack.pop();
  const previous = state.note.stack[state.note.stack.length - 1];
  await openNote({ path: previous }, { push: false });
}

function syncNoteDrawerWithReview() {
  if (!noteDrawerOpenState() || !state.note.follow) return;
  const cards = filteredReviews();
  const item = cards[state.reviewIndex];
  if (item) {
    state.note.stack = [item.path];
    openNote({ path: item.path }, { push: false, follow: true });
  } else {
    closeNoteDrawer();
  }
}

/* ---- 立项角度弹窗 ---- */

function openAngleDialog(path, title = "") {
  state.angleTarget = path;
  const source = title
    || (state.data?.topic_candidates || []).concat(state.data?.topic_continuations || [])
      .find((item) => item.path === path)?.title
    || path.split("/").pop().replace(/\.md$/, "");
  $("#angle-topic-title").textContent = source;
  $("#angle-scrim").hidden = false;
  $("#angle-dialog").hidden = false;
  setTimeout(() => $("#angle-input").focus(), 60);
}

function closeAngleDialog() {
  state.angleTarget = null;
  $("#angle-dialog").hidden = true;
  $("#angle-scrim").hidden = true;
  $("#angle-input").value = "";
}

async function submitAngleDialog() {
  const path = state.angleTarget;
  const angle = $("#angle-input").value.trim();
  if (!path || !angle) return;
  const button = $("#angle-dialog button[type=submit]");
  button.disabled = true;
  button.textContent = "正在立项…";
  try {
    const result = await api("/api/promote", {
      method: "POST",
      body: JSON.stringify({ path, angle }),
    });
    closeAngleDialog();
    await loadDashboard({ quiet: true });
    showToast("已立项，写作任务已建好");
    if (result.writing_task) await openNote({ path: result.writing_task });
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "建立写作任务";
  }
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
  const busy = $("#capture-drawer").classList.contains("open") || !$("#action-menu").hidden || !$("#angle-dialog").hidden || state.dragging;
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
      <a class="search-result" href="#" data-note-path="${escapeHtml(item.path)}">
        <strong>${escapeHtml(item.title)}</strong>
        <p>${escapeHtml(item.excerpt)}</p>
        <span>→</span>
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
      if (!$("#angle-dialog").hidden) closeAngleDialog();
      else if (!$("#action-menu").hidden) hideActionMenu();
      else if (noteDrawerOpenState()) closeNoteDrawer();
      else if ($("#capture-drawer").classList.contains("open")) closeCapture();
      else {
        $("#global-search").value = "";
        $("#search-tray").hidden = true;
      }
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.target instanceof Element && event.target.closest("input, textarea, select")) return;
    if (!$("#action-menu").hidden || !$("#angle-dialog").hidden) return;
    const key = event.key.toLowerCase();
    if (key === "u" && state.undo) {
      event.preventDefault();
      undoAction();
      return;
    }
    // 抽屉在静态阅读（非审核跟随）时，屏蔽审核快捷键，避免对着看不见的卡片操作。
    if (noteDrawerOpenState() && !state.note.follow) return;
    const cards = filteredReviews();
    const item = cards[state.reviewIndex];
    if (key === "j" || key === "k") {
      if (!cards.length) return;
      event.preventDefault();
      state.reviewIndex = Math.min(cards.length - 1, Math.max(0, state.reviewIndex + (key === "j" ? 1 : -1)));
      renderReview();
      syncNoteDrawerWithReview();
    } else if (reviewActions().some((action) => String(action.shortcut) === key) && item) {
      event.preventDefault();
      const action = reviewActions().find((candidate) => String(candidate.shortcut) === key);
      chooseAction(item, action.key);
    } else if (key === "o" && item) {
      event.preventDefault();
      if (event.shiftKey) {
        window.location.href = item.obsidian_uri;
      } else if (noteDrawerOpenState() && state.note.follow) {
        closeNoteDrawer();
      } else {
        openNote({ path: item.path }, { follow: true });
      }
    } else if (key === "/") {
      event.preventDefault();
      $("#global-search").focus();
    }
  });

  document.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    const noteLink = event.target.closest("[data-note-path]");
    if (noteLink) {
      event.preventDefault();
      openNote({ path: noteLink.dataset.notePath }, { follow: noteLink.dataset.noteFollow === "1" });
      return;
    }
    const wiki = event.target.closest("[data-wikilink]");
    if (wiki) {
      event.preventDefault();
      openNote({ link: wiki.dataset.wikilink });
    }
  });

  $("#note-close").addEventListener("click", closeNoteDrawer);
  $("#note-scrim").addEventListener("click", closeNoteDrawer);
  $("#note-back").addEventListener("click", noteDrawerBack);
  $("#angle-cancel").addEventListener("click", closeAngleDialog);
  $("#angle-scrim").addEventListener("click", closeAngleDialog);
  $("#angle-dialog").addEventListener("submit", (event) => {
    event.preventDefault();
    submitAngleDialog();
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
