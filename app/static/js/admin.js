(() => {
  "use strict";

  const { escapeHtml, renderMarkdown, parseDate, shortWhen, timeFormat, toast } = window.GCUI;
  const $ = (id) => document.getElementById(id);
  const HEALTH_RETRY_MS = [15000, 30000, 60000, 120000, 300000];   // backoff while a check is failing
  const LOG_POLL_MS = 5000;

  const SECTIONS = {
    overview: { title: "Overview", sub: "Health, configuration and usage" },
    history: { title: "Chat history & logs", sub: "Every saved transcript and the application log" },
    settings: { title: "Model & API settings", sub: "Change the model, API key and MCP server at runtime" },
    guardrails: { title: "Guardrails", sub: "Input and output safety checks" },
    endpoints: { title: "All endpoints", sub: "Pages and API routes discovered from the running app" },
  };
  const KIND_LABELS = { answer: "Answer", local: "Local reply", clarification: "Clarification", blocked: "Blocked", error: "Error" };
  const BLOCK_LABELS = { prompt_injection: "Prompt injection", fraud: "Fraud", tax_advice: "Tax-only", off_topic: "Off-topic" };

  const shell = $("shell");
  const main = $("main");
  const backdrop = $("backdrop");
  const menuButton = $("menu-button");
  let current = null;
  const loaded = {};   // section -> true once fetched

  // ---------- Helpers ----------
  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }

  async function api(path, { method = "GET", body } = {}) {
    const options = { method, credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch {
      throw new ApiError("Couldn't reach the server.", 0);
    }
    const data = await response.json().catch(() => ({}));
    if (response.status === 401 || response.status === 403) {
      if (path.startsWith("/admin/api") && !path.endsWith("/login")) showLock(data.detail, response.status === 401);
    }
    if (!response.ok) throw new ApiError(data.detail || `Request failed (${response.status})`, response.status);
    return data;
  }

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const fmtNumber = (n) => new Intl.NumberFormat().format(n || 0);
  const fmtMs = (ms) => (ms == null ? "–" : ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`);
  const fmtWhen = (iso) => {
    const date = parseDate(iso);
    return date ? `${shortWhen(date)}${shortWhen(date).includes(":") ? "" : `, ${timeFormat.format(date)}`}` : "–";
  };
  const fmtUptime = (seconds) => {
    const d = Math.floor(seconds / 86400), h = Math.floor((seconds % 86400) / 3600), m = Math.floor((seconds % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  };
  const fmtBytes = (b) => (b > 1048576 ? `${(b / 1048576).toFixed(1)} MB` : b > 1024 ? `${Math.round(b / 1024)} KB` : `${b} B`);

  async function withLoading(button, task) {
    button.classList.add("loading");
    button.disabled = true;
    try { return await task(); } finally { button.classList.remove("loading"); button.disabled = false; }
  }

  function renderBars(container, entries, { labels = {}, kindClass = false } = {}) {
    container.replaceChildren();
    const rows = Object.entries(entries || {});
    if (!rows.length) { container.appendChild(el("p", "empty-note", "Nothing recorded yet.")); return; }
    const max = Math.max(...rows.map(([, v]) => v));
    for (const [name, value] of rows) {
      const row = el("div", "bar-row");
      row.appendChild(el("span", "bar-name", labels[name] || name));
      const track = el("span", "bar-track");
      const fill = el("span", `bar-fill${kindClass ? ` k-${name}` : ""}`);
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el("span", "bar-count", fmtNumber(value)));
      container.appendChild(row);
      requestAnimationFrame(() => { fill.style.width = `${Math.max(4, (value / max) * 100)}%`; });
    }
  }

  // ---------- Access ----------
  function showLock(detail, canLogin) {
    $("lock").hidden = false;
    $("lock-text").textContent = detail || "The admin console is only available on the machine running the server.";
    $("login-form").hidden = !canLogin;
    if (canLogin) $("login-token").focus();
  }

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter || $("login-form").querySelector("button");
    try {
      await withLoading(button, () => api("/admin/api/login", { method: "POST", body: { token: $("login-token").value } }));
      $("login-token").value = "";
      $("lock").hidden = true;
      toast("Signed in");
      Object.keys(loaded).forEach((k) => delete loaded[k]);
      boot(false);
    } catch (error) {
      toast(error.message, { type: "error" });
      $("login-token").select();
    }
  });

  $("logout").addEventListener("click", async () => {
    try { await api("/admin/api/logout", { method: "POST", body: {} }); } catch { /* the lock screen explains */ }
    location.reload();
  });

  // ---------- Navigation ----------
  function show(section, { force = false } = {}) {
    if (!SECTIONS[section]) section = "overview";
    current = section;
    for (const name of Object.keys(SECTIONS)) $(`section-${name}`).hidden = name !== section;
    document.querySelectorAll(".nav-item").forEach((item) => {
      if (item.dataset.section === section) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    $("section-title").textContent = SECTIONS[section].title;
    $("section-sub").textContent = SECTIONS[section].sub;
    document.title = `${SECTIONS[section].title} · Admin`;
    if (location.hash !== `#${section}`) history.replaceState(null, "", `#${section}`);
    $("admin-scroll").scrollTop = 0;
    closeDrawer();
    if (force || !loaded[section]) load(section);
  }

  function load(section) {
    loaded[section] = true;
    const loaders = { overview: loadOverview, history: loadHistory, settings: loadSettings, guardrails: loadGuardrails, endpoints: loadEndpoints };
    return loaders[section]().catch((error) => {
      loaded[section] = false;
      if (error.status !== 401 && error.status !== 403) toast(error.message, { type: "error" });
    });
  }

  document.querySelectorAll(".nav-item").forEach((item) => item.addEventListener("click", () => show(item.dataset.section)));
  window.addEventListener("hashchange", () => show(location.hash.slice(1)));
  $("refresh-section").addEventListener("click", async () => {
    const button = $("refresh-section");
    button.classList.add("spinning");
    await load(current);
    if (current === "overview") await refreshHealth(true);
    button.classList.remove("spinning");
    toast("Refreshed");
  });

  // Drawer (tablet & mobile), same behaviour as the chat page
  let lastFocus = null;
  function openDrawer() {
    lastFocus = document.activeElement;
    shell.classList.add("drawer-open");
    backdrop.hidden = false;
    menuButton.setAttribute("aria-expanded", "true");
    main.inert = true;
    setTimeout(() => document.querySelector(".nav-item[aria-current]")?.focus(), 50);
  }
  function closeDrawer() {
    if (!shell.classList.contains("drawer-open")) return;
    shell.classList.remove("drawer-open");
    menuButton.setAttribute("aria-expanded", "false");
    main.inert = false;
    setTimeout(() => { if (!shell.classList.contains("drawer-open")) backdrop.hidden = true; }, 300);
    if (lastFocus && document.body.contains(lastFocus)) lastFocus.focus();
  }
  menuButton.addEventListener("click", openDrawer);
  $("sidebar-close").addEventListener("click", closeDrawer);
  backdrop.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });
  window.matchMedia("(max-width: 900px)").addEventListener("change", (e) => { if (!e.matches) closeDrawer(); });

  // ---------- Health ----------
  const lastStates = {};
  function applyHealth(health) {
    for (const key of ["llm", "mcp"]) {
      const status = health[key];
      const name = key === "llm" ? "Gemini LLM" : "MCP server";
      const side = $(`status-${key}`);
      side.dataset.state = status.state;
      side.querySelector(".status-label").textContent = status.label;
      side.title = `${name}: ${status.label}${status.detail ? ` (${status.detail})` : ""}`;
      const card = $(`hc-${key}`);
      card.dataset.state = status.state;
      card.classList.remove("checking");
      card.querySelector(".hc-label").textContent = status.label;
      card.querySelector(".hc-detail").textContent = status.detail || "";
      card.querySelector(".hc-latency").textContent = fmtMs(status.latency_ms);
      card.querySelector(".hc-checked").textContent = status.checked_at ? fmtWhen(status.checked_at) : "–";
      const previous = lastStates[key];
      lastStates[key] = status.state;
      if (previous && previous !== status.state) {
        if (status.state === "ready") toast(`${name} is connected`);
        else if (status.state === "offline" || status.state === "degraded") toast(`${name}: ${status.label}`, { type: "error" });
      }
    }
    const summary = { ok: "All systems connected", degraded: "Partly available", down: "Not connected" }[health.status] || "";
    $("health-summary").textContent = summary ? `${summary} · checked ${fmtWhen(health.checked_at)}` : "";
    setGuardBadge(health.guardrails_enabled);
  }

  // /health is checked once on load and when asked; it's only re-checked (with backoff) while something is failing.
  let healthRetry = null;
  let healthAttempt = 0;
  async function refreshHealth(force = false) {
    clearTimeout(healthRetry);
    if (force) ["llm", "mcp"].forEach((k) => $(`hc-${k}`).classList.add("checking"));
    let failed = true;
    try {
      const response = await fetch(`/health${force ? "?refresh=true" : ""}`, { credentials: "same-origin" });
      if (!response.ok) throw new Error();
      const health = await response.json();
      applyHealth(health);
      failed = [health.llm, health.mcp].some((s) => s.state !== "ready" && s.state !== "off");
    } catch {
      const down = { state: "offline", label: "Server unreachable", detail: "The app server didn't respond", latency_ms: null, checked_at: "" };
      applyHealth({ status: "down", llm: down, mcp: down, checked_at: new Date().toISOString() });
    }
    if (!failed) { healthAttempt = 0; return; }
    const delay = HEALTH_RETRY_MS[Math.min(healthAttempt++, HEALTH_RETRY_MS.length - 1)];
    healthRetry = setTimeout(retryHealth, delay);
  }
  function retryHealth() {
    if (!document.hidden) refreshHealth();
    else document.addEventListener("visibilitychange", retryHealth, { once: true });
  }

  $("run-health").addEventListener("click", (event) =>
    withLoading(event.currentTarget, async () => { await refreshHealth(true); toast("Health check complete"); }));

  function setGuardBadge(enabled) {
    $("nav-guard-badge").hidden = enabled !== false;
  }

  // ---------- Overview ----------
  async function loadOverview() {
    const data = await api("/admin/api/overview");
    applyHealth(data.health);
    const s = data.stats;
    const grid = $("stat-grid");
    grid.replaceChildren();
    const tiles = [
      ["Chats saved", fmtNumber(s.chats), `${fmtNumber(s.browsers)} browser${s.browsers === 1 ? "" : "s"}`],
      ["Turns", fmtNumber(s.turns), `${fmtNumber(s.turns_today)} today`],
      ["Avg response", fmtMs(s.avg_response_ms), `${fmtNumber(data.active_conversations)} chats in memory`],
      ["Blocked / rejected", fmtNumber(Object.values(s.blocked).reduce((a, b) => a + b, 0)),
        `${fmtNumber(s.mcp_rejected)} tool calls rejected`],
    ];
    for (const [label, value, sub] of tiles) {
      const tile = el("div", "stat");
      tile.append(el("div", "stat-label", label), el("div", "stat-value", value), el("div", "stat-sub", sub));
      grid.appendChild(tile);
    }

    const cfg = data.settings;
    const kv = $("config-summary");
    kv.replaceChildren();
    const rows = [
      ["Model", cfg.llm_model.value + (cfg.llm_model.overridden ? " (runtime)" : "")],
      ["API key", cfg.gemini_api_key.set ? cfg.gemini_api_key.masked + (cfg.gemini_api_key.overridden ? " (runtime)" : "") : "Not set"],
      ["MCP server", cfg.mcp_url.set ? cfg.mcp_url.masked + (cfg.mcp_url.overridden ? " (runtime)" : "") : "Not set"],
      ["Guardrails", cfg.guardrails.enabled ? "On" : "Off"],
      ["Uptime", `${fmtUptime(data.uptime_seconds)} (since ${fmtWhen(data.started_at)})`],
    ];
    for (const [k, v] of rows) kv.append(el("dt", "", k), el("dd", k === "API key" || k === "MCP server" ? "mono" : "", v));

    renderBars($("kind-bars"), s.kinds, { labels: KIND_LABELS, kindClass: true });
    renderBars($("tool-bars"), s.tools);
    renderBars($("blocked-bars"), s.blocked, { labels: BLOCK_LABELS });

    const recent = $("recent-chats");
    recent.replaceChildren();
    if (!data.recent.length) recent.appendChild(el("p", "empty-note", "No chats saved yet."));
    for (const chat of data.recent) {
      const item = el("button", "mini-item");
      item.type = "button";
      const body = el("span", "mini-body");
      body.append(el("span", "mini-title", chat.title),
        el("span", "mini-meta", `${fmtWhen(chat.updated)} · ${chat.turns} turn${chat.turns === 1 ? "" : "s"} · session ${chat.tag || "?"}`));
      item.appendChild(body);
      item.addEventListener("click", () => { show("history"); openTranscript(chat.name); });
      recent.appendChild(item);
    }
  }

  // ---------- Chat history ----------
  let transcripts = [];
  let filter = "all";
  let openName = null;

  async function loadHistory() {
    const list = $("admin-chat-list");
    if (!transcripts.length) list.innerHTML = '<div class="skeleton-row"></div>'.repeat(6);
    transcripts = await api("/admin/api/chats?limit=500");
    renderTranscriptList();
    if (!$("tab-applog").hidden) loadAppLog();
  }

  function renderTranscriptList() {
    const query = $("chat-search").value.trim().toLowerCase();
    const matches = transcripts.filter((t) => {
      if (filter === "blocked" && !t.blocked) return false;
      if (filter === "errors" && !t.errors) return false;
      if (filter === "guardoff" && !t.guardrails_off) return false;
      return !query || [t.title, t.id, t.tag, t.name, t.model].some((v) => (v || "").toLowerCase().includes(query));
    });
    $("chat-count").textContent = `${matches.length} of ${transcripts.length} transcript${transcripts.length === 1 ? "" : "s"}`;
    const list = $("admin-chat-list");
    list.replaceChildren();
    if (!matches.length) {
      list.appendChild(el("p", "empty-note", transcripts.length ? "No transcripts match." : "No chats have been saved yet."));
      return;
    }
    for (const t of matches) {
      const item = el("button", "t-item");
      item.type = "button";
      item.dataset.name = t.name;
      if (t.name === openName) item.setAttribute("aria-current", "true");
      item.appendChild(el("span", "t-title", t.title));
      const meta = el("span", "t-meta");
      meta.append(el("span", "", fmtWhen(t.updated)), el("span", "", `${t.turns} turn${t.turns === 1 ? "" : "s"}`),
        el("span", "mono", t.tag || "?"));
      if (t.blocked) meta.appendChild(el("span", "flag red", `${t.blocked} blocked`));
      if (t.errors) meta.appendChild(el("span", "flag amber", `${t.errors} error${t.errors === 1 ? "" : "s"}`));
      if (t.guardrails_off) meta.appendChild(el("span", "flag red", "guardrails off"));
      item.appendChild(meta);
      list.appendChild(item);
    }
  }

  $("admin-chat-list").addEventListener("click", (event) => {
    const item = event.target.closest(".t-item");
    if (item) openTranscript(item.dataset.name);
  });
  $("chat-search").addEventListener("input", renderTranscriptList);
  $("chat-filters").addEventListener("click", (event) => {
    const chip = event.target.closest(".chip-filter");
    if (!chip) return;
    filter = chip.dataset.filter;
    $("chat-filters").querySelectorAll(".chip-filter").forEach((c) => c.setAttribute("aria-pressed", String(c === chip)));
    renderTranscriptList();
  });

  const ICON_BACK = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg>';
  const ICON_DOWNLOAD = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>';

  async function openTranscript(name) {
    openName = name;
    renderTranscriptList();
    const viewer = $("viewer");
    $("split").classList.add("viewing");
    viewer.innerHTML = '<div class="viewer-body"><div class="thread-skeleton"><div class="skeleton s-user"></div><div class="skeleton s-bot"></div><div class="skeleton s-user"></div></div></div>';
    let data;
    try {
      data = await api(`/admin/api/chats/${encodeURIComponent(name)}`);
    } catch (error) {
      if (openName === name) {
        viewer.innerHTML = '<div class="viewer-empty"><h2>Couldn\'t load this transcript</h2></div>';
        viewer.querySelector("h2").insertAdjacentElement("afterend", el("p", "muted", error.message));
      }
      return;
    }
    if (openName !== name) return;

    const head = el("div", "viewer-head");
    const back = el("button", "icon-button viewer-back");
    back.type = "button";
    back.setAttribute("aria-label", "Back to the list");
    back.innerHTML = ICON_BACK;
    back.addEventListener("click", () => { $("split").classList.remove("viewing"); openName = null; renderTranscriptList(); });
    const title = el("div", "v-title");
    title.append(el("h2", "", data.title));
    const meta = el("div", "t-meta");
    meta.append(el("span", "", `Started ${fmtWhen(data.started)}`), el("span", "", `${data.turns} turns`),
      el("span", "", data.model || "model ?"), el("span", "mono", `session ${data.tag || "?"}`), el("span", "", fmtBytes(data.size_bytes)));
    title.appendChild(meta);
    const actions = el("div", "viewer-actions");
    const tabs = el("div", "tabs");
    const convTab = el("button", "tab", "Conversation");
    const rawTab = el("button", "tab", "Raw log");
    [convTab, rawTab].forEach((t) => { t.type = "button"; });
    tabs.append(convTab, rawTab);
    const download = el("button", "btn small");
    download.type = "button";
    download.innerHTML = `${ICON_DOWNLOAD}<span>Download</span>`;
    download.addEventListener("click", () => {
      const url = URL.createObjectURL(new Blob([data.raw], { type: "text/plain" }));
      const link = Object.assign(document.createElement("a"), { href: url, download: `${data.name}.log` });
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("Download started");
    });
    actions.append(tabs, download);
    head.append(back, title, actions);

    const body = el("div", "viewer-body");
    const renderView = (mode) => {
      convTab.setAttribute("aria-selected", String(mode === "conv"));
      rawTab.setAttribute("aria-selected", String(mode === "raw"));
      body.replaceChildren(mode === "conv" ? buildConversation(data.turns_detail) : buildRaw(data.raw));
      body.scrollTop = 0;
    };
    convTab.addEventListener("click", () => renderView("conv"));
    rawTab.addEventListener("click", () => renderView("raw"));
    viewer.replaceChildren(head, body);
    renderView("conv");
  }

  function buildConversation(turns) {
    const thread = el("div", "thread");
    if (!turns.length) thread.appendChild(el("p", "empty-note", "This transcript has no turns."));
    for (const turn of turns) {
      const user = el("div", "message user");
      const ucol = el("div", "msg-col");
      ucol.appendChild(el("div", "bubble", turn.user || "(empty)"));
      user.appendChild(ucol);

      const bot = el("div", `message bot${turn.kind === "error" ? " error" : ""}${turn.kind === "blocked" ? " blocked" : ""}`);
      bot.innerHTML = '<span class="avatar" aria-hidden="true">G</span>';
      const col = el("div", "msg-col");
      const bubble = el("div", "bubble");
      bubble.innerHTML = renderMarkdown(turn.reply || "");
      col.appendChild(bubble);
      for (const notice of turn.notices) col.appendChild(el("div", "notice", notice));
      const meta = el("div", "turn-meta");
      meta.append(el("span", "", fmtWhen(turn.time)), el("span", "flag blue", KIND_LABELS[turn.kind] || turn.kind || "?"));
      if (turn.duration_ms) meta.appendChild(el("span", "", fmtMs(turn.duration_ms)));
      if (turn.blocked_reason) meta.appendChild(el("span", "flag red", BLOCK_LABELS[turn.blocked_reason] || turn.blocked_reason));
      if (turn.guardrails_off) meta.appendChild(el("span", "flag red", "guardrails off"));
      for (const call of turn.mcp_calls) {
        meta.appendChild(el("span", `mcp-call${call.status === "ok" ? "" : " bad"}`, `${call.tool} → ${call.status}`));
      }
      col.appendChild(meta);
      bot.appendChild(col);
      thread.append(user, bot);
    }
    return thread;
  }

  function buildRaw(raw) {
    const pre = el("pre", "log-view");
    pre.innerHTML = raw.split("\n").map((line) => {
      const safe = escapeHtml(line);
      return /^---- /.test(line) ? `<span class="lv-turn">${safe}</span>` : safe;
    }).join("\n");
    return pre;
  }

  // Tabs: transcripts / application log
  document.querySelectorAll('#section-history [role="tab"]').forEach((tab) => {
    tab.addEventListener("click", () => {
      const which = tab.dataset.tab;
      document.querySelectorAll('#section-history [role="tab"]').forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
      $("tab-transcripts").hidden = which !== "transcripts";
      $("tab-applog").hidden = which !== "applog";
      if (which === "applog") loadAppLog();
      else setLogPolling(false);
      setLogPolling(which === "applog" && $("log-auto").checked);
    });
  });

  async function loadAppLog({ quiet = false } = {}) {
    const view = $("app-log");
    if (!quiet) view.classList.add("skeleton");
    try {
      const data = await api(`/admin/api/logs/app?lines=${$("log-lines").value}&level=${$("log-level").value}`);
      const stick = view.scrollHeight - view.scrollTop - view.clientHeight < 40;
      view.innerHTML = data.lines.length ? data.lines.map((line) => {
        const m = line.match(/^(\S+ \S+) (DEBUG|INFO|WARNING|ERROR|CRITICAL) (.*)$/);
        if (!m) return escapeHtml(line);
        return `<span class="lv-time">${escapeHtml(m[1])}</span> <span class="lv-${m[2]}">${m[2]}</span> ${escapeHtml(m[3])}`;
      }).join("\n") : "The log is empty.";
      if (stick || !quiet) view.scrollTop = view.scrollHeight;
    } catch (error) {
      if (!quiet) toast(error.message, { type: "error" });
    } finally {
      view.classList.remove("skeleton");
    }
  }

  let logTimer = null;
  function setLogPolling(on) {
    clearInterval(logTimer);
    logTimer = on ? setInterval(() => { if (!document.hidden && current === "history") loadAppLog({ quiet: true }); }, LOG_POLL_MS) : null;
  }
  $("log-refresh").addEventListener("click", (e) => withLoading(e.currentTarget, () => loadAppLog()));
  $("log-level").addEventListener("change", () => loadAppLog());
  $("log-lines").addEventListener("change", () => loadAppLog());
  $("log-auto").addEventListener("change", (e) => { setLogPolling(e.target.checked); toast(e.target.checked ? "Auto-refresh on" : "Auto-refresh off", { type: "info" }); });

  // ---------- Settings ----------
  const form = $("settings-form");
  const fModel = $("f-model");
  const fKey = $("f-key");
  const fMcp = $("f-mcp");

  function setSource(id, overridden) {
    const badge = $(id);
    badge.textContent = overridden ? "Runtime override" : "From .env";
    badge.classList.toggle("override", overridden);
  }

  function renderSettings(cfg) {
    fModel.value = cfg.llm_model.value;
    $("model-list").replaceChildren(...cfg.model_suggestions.map((m) => Object.assign(document.createElement("option"), { value: m })));
    $("hint-model").textContent = `Default from .env: ${cfg.llm_model.default || "not set"}`;
    fKey.placeholder = cfg.gemini_api_key.set ? `Current: ${cfg.gemini_api_key.masked}` : "Not set: paste a Gemini API key";
    fMcp.placeholder = cfg.mcp_url.set ? `Current: ${cfg.mcp_url.masked}` : "Not set: https://…";
    setSource("src-model", cfg.llm_model.overridden);
    setSource("src-key", cfg.gemini_api_key.overridden);
    setSource("src-mcp", cfg.mcp_url.overridden);
    const kv = $("settings-current");
    kv.replaceChildren();
    const rows = [
      ["Provider", cfg.provider],
      ["Model", cfg.llm_model.value],
      ["API key", cfg.gemini_api_key.set ? cfg.gemini_api_key.masked : "Not set"],
      ["MCP server", cfg.mcp_url.set ? cfg.mcp_url.masked : "Not set"],
      ["LLM timeout", `${cfg.llm_timeout_seconds} s`],
      ["Status", cfg.configured ? "Configured" : `Missing: ${cfg.problems.join(", ")}`],
    ];
    for (const [k, v] of rows) kv.append(el("dt", "", k), el("dd", k === "API key" || k === "MCP server" ? "mono" : "", v));
    $("reset-settings").disabled = !(cfg.llm_model.overridden || cfg.gemini_api_key.overridden || cfg.mcp_url.overridden);
  }

  async function loadSettings() {
    renderSettings(await api("/admin/api/settings"));
  }

  function formValues() {
    $("settings-error").hidden = true;
    [fModel, fKey, fMcp].forEach((f) => f.classList.remove("invalid"));
    const values = { llm_model: fModel.value.trim(), gemini_api_key: fKey.value.trim(), mcp_url: fMcp.value.trim() };
    let problem = "";
    if (!/^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$/.test(values.llm_model)) { problem = "Enter a model name such as gemini-2.5-flash."; fModel.classList.add("invalid"); }
    else if (values.gemini_api_key && (values.gemini_api_key.length < 10 || /\s/.test(values.gemini_api_key))) { problem = "That API key doesn't look right (no spaces, at least 10 characters)."; fKey.classList.add("invalid"); }
    else if (values.mcp_url && !/^https?:\/\/[^\s/]+/.test(values.mcp_url)) { problem = "The MCP URL must start with http:// or https://."; fMcp.classList.add("invalid"); }
    if (problem) {
      $("settings-error").textContent = problem;
      $("settings-error").hidden = false;
      return null;
    }
    return values;
  }

  function showResult(title, health, extra = "") {
    const box = $("settings-result");
    box.hidden = false;
    box.replaceChildren(el("h3", "", title));
    for (const key of ["llm", "mcp"]) {
      const s = health[key];
      const row = el("div", "result-row");
      row.dataset.state = s.state;
      row.append(el("span", "status-dot"), el("span", "r-name", key === "llm" ? "Gemini LLM" : "MCP server"),
        el("span", "", s.label), el("span", "r-detail", [s.detail, s.latency_ms != null ? fmtMs(s.latency_ms) : ""].filter(Boolean).join(" · ")));
      box.appendChild(row);
    }
    if (extra) box.appendChild(el("p", "muted small", extra));
  }

  const describeChanges = (changed) => changed.length
    ? `Changed: ${changed.map((c) => ({ llm_model: "model", gemini_api_key: "API key", mcp_url: "MCP URL" })[c]).join(", ")}.`
    : "Nothing changed.";

  $("test-settings").addEventListener("click", (event) => {
    const values = formValues();
    if (!values) return;
    withLoading(event.currentTarget, async () => {
      try {
        const health = await api("/admin/api/settings/test", { method: "POST", body: values });
        showResult(health.status === "ok" ? "Test passed. Nothing has been applied yet." : "Test finished with problems. Nothing has been applied.", health);
        toast(health.status === "ok" ? "Connection test passed" : "Connection test found problems", { type: health.status === "ok" ? "success" : "error" });
      } catch (error) { toast(error.message, { type: "error" }); }
    });
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const values = formValues();
    if (!values) return;
    withLoading($("save-settings"), async () => {
      try {
        const data = await api("/admin/api/settings", { method: "PUT", body: values });
        fKey.value = "";
        fMcp.value = "";
        renderSettings(data.settings);
        applyHealth(data.health);
        showResult("Applied", data.health, describeChanges(data.changed));
        toast(data.changed.length ? "Settings applied" : "No changes to apply", { type: data.changed.length ? "success" : "info" });
        loaded.overview = false;
      } catch (error) {
        $("settings-error").textContent = error.message;
        $("settings-error").hidden = false;
        toast("Settings weren't applied", { type: "error" });
      }
    });
  });

  $("reset-settings").addEventListener("click", (event) => {
    withLoading(event.currentTarget, async () => {
      try {
        const data = await api("/admin/api/settings/reset", { method: "POST", body: {} });
        fKey.value = "";
        fMcp.value = "";
        renderSettings(data.settings);
        applyHealth(data.health);
        showResult("Reset to .env defaults", data.health, describeChanges(data.changed));
        toast("Back to .env defaults");
        loaded.overview = false;
      } catch (error) { toast(error.message, { type: "error" }); }
    });
  });

  $("toggle-key").addEventListener("click", (event) => {
    const button = event.currentTarget;
    const visible = fKey.type === "password";
    fKey.type = visible ? "text" : "password";
    button.setAttribute("aria-pressed", String(visible));
    button.setAttribute("aria-label", visible ? "Hide the key you typed" : "Show the key you typed");
  });

  // ---------- Guardrails ----------
  const guardSwitch = $("guard-switch");

  function renderGuardrails(g) {
    guardSwitch.disabled = false;
    guardSwitch.setAttribute("aria-checked", String(g.enabled));
    guardSwitch.querySelector(".switch-label").textContent = g.enabled ? "On" : "Off";
    $("guard-card").classList.toggle("off", !g.enabled);
    $("section-guardrails").classList.toggle("guard-off", !g.enabled);
    $("guard-sub").textContent = `${g.enabled ? "On" : "Off"} for new chat turns · .env default: ${g.default ? "on" : "off"}`
      + (g.overridden ? " · runtime override (resets on restart)" : "");
    setGuardBadge(g.enabled);
  }

  async function loadGuardrails() {
    const [g, overview] = await Promise.all([api("/admin/api/guardrails"), api("/admin/api/overview")]);
    renderGuardrails(g);
    renderBars($("blocked-bars"), overview.stats.blocked, { labels: BLOCK_LABELS });
  }

  async function setGuardrails(enabled) {
    guardSwitch.disabled = true;
    try {
      renderGuardrails(await api("/admin/api/guardrails", { method: "PUT", body: { enabled } }));
      toast(enabled ? "Guardrails are on" : "Guardrails are off", { type: enabled ? "success" : "error" });
      loaded.overview = false;
    } catch (error) {
      guardSwitch.disabled = false;
      toast(error.message, { type: "error" });
    }
  }

  guardSwitch.addEventListener("click", () => {
    const on = guardSwitch.getAttribute("aria-checked") === "true";
    if (on) {
      $("guard-confirm").hidden = false;
      $("guard-confirm-cancel").focus();
    } else {
      setGuardrails(true);
    }
  });
  $("guard-confirm-off").addEventListener("click", () => { $("guard-confirm").hidden = true; setGuardrails(false); });
  $("guard-confirm-cancel").addEventListener("click", () => { $("guard-confirm").hidden = true; guardSwitch.focus(); });

  // ---------- Endpoints ----------
  let routes = [];
  let kindFilter = "All";

  async function loadEndpoints() {
    routes = (await api("/admin/api/endpoints")).routes;
    const kinds = ["All", ...new Set(routes.map((r) => r.kind))];
    const chips = $("endpoint-filters");
    chips.replaceChildren(...kinds.map((kind) => {
      const chip = el("button", "chip-filter", kind === "All" ? `All (${routes.length})` : kind);
      chip.type = "button";
      chip.dataset.kind = kind;
      chip.setAttribute("aria-pressed", String(kind === kindFilter));
      return chip;
    }));
    renderEndpoints();
  }

  function renderEndpoints() {
    const query = $("endpoint-search").value.trim().toLowerCase();
    const rows = routes.filter((r) => (kindFilter === "All" || r.kind === kindFilter)
      && (!query || `${r.path} ${r.purpose} ${r.methods.join(" ")}`.toLowerCase().includes(query)));
    $("endpoint-count").textContent = `${rows.length} of ${routes.length} routes, read from the running app.`;
    const body = $("endpoint-rows");
    body.replaceChildren();
    for (const r of rows) {
      const tr = el("tr");
      const methods = el("td");
      r.methods.forEach((m) => methods.appendChild(el("span", `method m-${m}`, m)));
      const path = el("td");
      path.appendChild(el("span", "ep-path", r.path));
      const query = r.params.filter((p) => p.startsWith("?"));
      if (query.length) path.appendChild(el("span", "ep-params", query.join(" ")));
      const access = el("td");
      access.appendChild(el("span", r.access === "Admin" ? "access-admin" : "access-public", r.access));
      const actions = el("td", "ep-actions");
      const simpleGet = r.methods.includes("GET") && !r.path.includes("{");
      if (simpleGet && (r.kind === "Page")) {
        const link = el("a", "btn small", "Open");
        link.href = r.path;
        actions.appendChild(link);
      } else if (simpleGet) {
        const tryButton = el("button", "btn small", "Try");
        tryButton.type = "button";
        tryButton.addEventListener("click", () => tryEndpoint(r, tr, tryButton));
        actions.appendChild(tryButton);
      }
      tr.append(methods, path, el("td", "", r.purpose), el("td", "kind-badge", r.kind), access, actions);
      body.appendChild(tr);
    }
    if (!rows.length) {
      const tr = el("tr");
      const td = el("td", "empty-note", "No routes match.");
      td.colSpan = 6;
      tr.appendChild(td);
      body.appendChild(tr);
    }
  }

  async function tryEndpoint(route, row, button) {
    const existing = row.nextElementSibling;
    if (existing?.classList.contains("preview-row")) { existing.remove(); button.textContent = "Try"; return; }
    await withLoading(button, async () => {
      let text;
      try {
        const response = await fetch(route.path, { credentials: "same-origin" });
        const raw = await response.text();
        try { text = `${response.status} ${response.statusText}\n${JSON.stringify(JSON.parse(raw), null, 2)}`; }
        catch { text = `${response.status} ${response.statusText}\n${raw.slice(0, 4000)}`; }
      } catch {
        text = "Request failed: the server didn't respond.";
      }
      const preview = el("tr", "preview-row");
      const td = el("td");
      td.colSpan = 6;
      td.appendChild(el("pre", "log-view", text));
      preview.appendChild(td);
      row.after(preview);
    });
    button.textContent = "Hide";
  }

  $("endpoint-search").addEventListener("input", renderEndpoints);
  $("endpoint-filters").addEventListener("click", (event) => {
    const chip = event.target.closest(".chip-filter");
    if (!chip) return;
    kindFilter = chip.dataset.kind;
    $("endpoint-filters").querySelectorAll(".chip-filter").forEach((c) => c.setAttribute("aria-pressed", String(c === chip)));
    renderEndpoints();
  });

  // ---------- Start ----------
  async function start() {
    let session;
    try {
      session = await api("/admin/api/session");
    } catch {
      showLock("Couldn't reach the server.", false);
      return;
    }
    if (!session.authorized) {
      showLock(session.token_login ? "Sign in with the ADMIN_TOKEN from the server's .env to continue." : undefined, session.token_login);
      return;
    }
    boot(session.local);
  }

  function boot(local) {
    $("logout").hidden = local;
    show(current || location.hash.slice(1) || "overview", { force: true });
    refreshHealth();
  }
  start();
})();
