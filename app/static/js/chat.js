(() => {
  "use strict";

  // ---- Elements ----
  const $ = (id) => document.getElementById(id);
  const shell = $("shell");
  const sidebar = $("sidebar");
  const backdrop = $("backdrop");
  const main = $("main");
  const messages = $("messages");
  const thread = $("thread");
  const form = $("composer");
  const input = $("message");
  const sendButton = $("send-button");
  const charCount = $("char-count");
  const scrollDown = $("scroll-down");
  const historyList = $("history-list");
  const historyRefresh = $("history-refresh");
  const chatTitle = $("chat-title");
  const chatSub = $("chat-sub");
  const menuButton = $("menu-button");
  const toasts = $("toasts");
  const emptyTemplate = $("empty-template");
  const MAX_CHARS = Number(input.dataset.max) || 1000;
  const REQUEST_TIMEOUT_MS = 120000;
  const STATUS_POLL_MS = 20000;
  const STORAGE_KEY = "gc_active_chat";
  const themeToggle = $("theme-toggle");
  const isTouch = window.matchMedia("(hover: none)").matches;

  // ---- Theme (Light / Dark) ----
  const THEME_KEY = "gc_theme";
  const initTheme = () => {
    const saved = localStorage.getItem(THEME_KEY);
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const theme = saved === "dark" || saved === "light" ? saved : (prefersDark ? "dark" : "light");
    document.documentElement.setAttribute("data-theme", theme);
    if (themeToggle) {
      themeToggle.setAttribute("title", `Switch to ${theme === "dark" ? "light" : "dark"} mode`);
      themeToggle.setAttribute("aria-label", `Switch to ${theme === "dark" ? "light" : "dark"} mode`);
    }
  };
  initTheme();
  if (themeToggle) {
    themeToggle.addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
      const next = current === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem(THEME_KEY, next);
      themeToggle.setAttribute("title", `Switch to ${next === "dark" ? "light" : "dark"} mode`);
      themeToggle.setAttribute("aria-label", `Switch to ${next === "dark" ? "light" : "dark"} mode`);
    });
  }
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
    if (!localStorage.getItem(THEME_KEY)) {
      const theme = e.matches ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", theme);
    }
  });

  const TOOL_LABELS = {
    recommend_cards: "Personalised ranking",
    list_cards: "Card catalogue",
    get_card_details: "Card details",
    compare_cards: "Card comparison",
    whatsapp_compare_cards: "Card comparison",
    check_eligibility: "Eligibility check",
    generate_content_brief: "Category research",
    whatsapp_resolve_card_or_category: "Card lookup",
  };
  const KIND_LABELS = { clarification: "Needs a detail", blocked: "Safety filter" };

  const ICONS = {
    copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    retry: '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/>',
    shield: '<path d="M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6z"/><path d="M9 12l2 2 4-4"/>',
  };
  const svg = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;

  // ---- State ----
  // Every chat keeps its own messages; replies are always stored on the chat that sent the request.
  const chats = new Map();   // id -> {id, title, messages, loaded, pending, unread, updated, turns}
  let activeId = null;
  let historyLoaded = false;
  let loadToken = 0;

  const newId = () => {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  };
  const activeChat = () => chats.get(activeId);
  const storage = {
    get() { try { return sessionStorage.getItem(STORAGE_KEY); } catch { return null; } },
    set(value) { try { sessionStorage.setItem(STORAGE_KEY, value); } catch { /* optional */ } },
  };

  // ---- Safe, minimal markdown rendering (everything is escaped first) ----
  const escapeHtml = (text) =>
    text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  function inline(text) {
    let html = escapeHtml(text);
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    html = html.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
      '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
    html = html.replace(/(^|\s)_([^_\n]+)_(?=\s|$|[.,!?])/g, '$1<span class="note">$2</span>');
    return html;
  }

  function renderTable(rows) {
    const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
    const body = rows.filter((row) => !/^\s*\|?[\s:|-]+\|?\s*$/.test(row));
    if (!body.length) return "";
    const [head, ...rest] = body;
    const th = cells(head).map((c) => `<th>${inline(c)}</th>`).join("");
    const trs = rest.map((r) => `<tr>${cells(r).map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("");
    return `<div class="table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table></div>`;
  }

  function renderMarkdown(source) {
    const lines = source.replace(/\r/g, "").split("\n");
    let html = "";
    let table = [];
    const lists = [];   // open lists: {type, indent}; each keeps its last <li> open for nesting

    const closeLists = (indent = -1) => {
      while (lists.length && lists[lists.length - 1].indent > indent) {
        html += `</li></${lists.pop().type}>`;
      }
    };
    const closeTable = () => { if (table.length) { html += renderTable(table); table = []; } };

    for (const line of lines) {
      if (/^\s*\|.*\|\s*$/.test(line)) { closeLists(); table.push(line); continue; }
      closeTable();
      if (line.trim() === "") continue;   // blank lines don't end a list, so numbering stays continuous

      const item = line.match(/^(\s*)([-*•]|(\d+)[.)])\s+(.*)$/);
      if (item) {
        const indent = item[1].length;
        const type = item[3] ? "ol" : "ul";
        closeLists(indent);
        const top = lists[lists.length - 1];
        if (top && top.indent === indent && top.type !== type) {
          html += `</li></${lists.pop().type}>`;
        }
        const current = lists[lists.length - 1];
        if (current && current.indent === indent) {
          html += "</li>";
        } else {
          const start = type === "ol" && item[3] !== "1" ? ` start="${Number(item[3])}"` : "";
          html += `<${type}${start}>`;
          lists.push({ type, indent });
        }
        html += `<li>${inline(item[4])}`;
        continue;
      }

      if (lists.length && /^\s{2,}\S/.test(line)) {   // indented continuation of a list item
        html += `<div class="li-cont">${inline(line.trim())}</div>`;
        continue;
      }
      closeLists();
      const heading = line.match(/^\s*#{1,4}\s+(.*)$/);
      html += heading ? `<h4>${inline(heading[1])}</h4>` : `<p>${inline(line)}</p>`;
    }
    closeLists();
    closeTable();
    return html;
  }

  // ---- Formatting helpers ----
  const timeFormat = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
  const dateFormat = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });
  const parseDate = (value) => { const d = value ? new Date(value) : null; return d && !isNaN(d) ? d : null; };
  const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();

  function dayBucket(date) {
    if (!date) return "Earlier";
    const days = Math.round((startOfDay(new Date()) - startOfDay(date)) / 86400000);
    if (days <= 0) return "Today";
    if (days === 1) return "Yesterday";
    if (days < 7) return "Previous 7 days";
    return "Earlier";
  }

  function shortWhen(date) {
    if (!date) return "";
    return dayBucket(date) === "Today" ? timeFormat.format(date) : dateFormat.format(date);
  }

  const titleFrom = (text) => {
    const clean = text.replace(/\s+/g, " ").trim();
    return clean.length > 70 ? `${clean.slice(0, 67).trimEnd()}...` : clean || "New chat";
  };

  // ---- Toasts ----
  function toast(message, { type = "success", action = null, duration = 3200 } = {}) {
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.innerHTML = '<span class="toast-dot" aria-hidden="true"></span>';
    const text = document.createElement("span");
    text.textContent = message;
    el.appendChild(text);
    if (action) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = action.label;
      button.addEventListener("click", () => { action.run(); dismiss(); });
      el.appendChild(button);
    }
    const dismiss = () => {
      if (el.classList.contains("leaving")) return;
      el.classList.add("leaving");
      el.addEventListener("animationend", () => el.remove(), { once: true });
    };
    toasts.appendChild(el);
    while (toasts.children.length > 3) toasts.firstElementChild.remove();
    setTimeout(dismiss, duration);
  }

  // ---- Ripple feedback on every button ----
  function ripple(target, x, y) {
    const rect = target.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height) * 2.2;
    const ink = document.createElement("span");
    ink.className = "ripple-ink";
    ink.style.width = ink.style.height = `${size}px`;
    ink.style.left = `${(x ?? rect.left + rect.width / 2) - rect.left - size / 2}px`;
    ink.style.top = `${(y ?? rect.top + rect.height / 2) - rect.top - size / 2}px`;
    target.appendChild(ink);
    ink.addEventListener("animationend", () => ink.remove(), { once: true });
  }
  document.addEventListener("pointerdown", (event) => {
    const target = event.target.closest("button");
    if (target && !target.disabled && event.button === 0) ripple(target, event.clientX, event.clientY);
  });
  document.addEventListener("click", (event) => {   // keyboard activation (Enter/Space) ripples from the centre
    const target = event.target.closest("button");
    if (target && !target.disabled && event.detail === 0) ripple(target);
  });

  // ---- Scrolling ----
  const nearBottom = () => messages.scrollHeight - messages.scrollTop - messages.clientHeight < 120;

  function scrollToBottom(smooth = true) {
    messages.scrollTo({ top: messages.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }

  function updateScrollButton() {
    const hide = nearBottom();
    scrollDown.hidden = hide;
    if (hide) scrollDown.classList.remove("has-new");
  }

  // ---- Message rendering ----
  function messageTime(message) {
    const date = parseDate(message.time);
    return date ? shortWhen(date) : "";
  }

  function buildUserMessage(message) {
    const wrapper = document.createElement("div");
    wrapper.className = "message user";
    const col = document.createElement("div");
    col.className = "msg-col";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = message.text;
    col.appendChild(bubble);
    const when = messageTime(message);
    if (when) {
      const footer = document.createElement("div");
      footer.className = "msg-footer";
      footer.innerHTML = `<span class="msg-time">${escapeHtml(when)}</span>`;
      col.appendChild(footer);
    }
    wrapper.appendChild(col);
    return wrapper;
  }

  function buildBotMessage(message, chatId) {
    const kind = message.kind || "answer";
    const wrapper = document.createElement("div");
    wrapper.className = `message bot${kind === "error" ? " error" : ""}${kind === "blocked" ? " blocked" : ""}`;
    wrapper.innerHTML = '<span class="avatar" aria-hidden="true">G</span>';

    const col = document.createElement("div");
    col.className = "msg-col";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = renderMarkdown(message.text || "");
    col.appendChild(bubble);

    for (const notice of message.notices || []) {
      const note = document.createElement("div");
      note.className = "notice";
      note.innerHTML = svg("shield");
      const text = document.createElement("span");
      text.textContent = notice;
      note.appendChild(text);
      col.appendChild(note);
    }

    if (message.retry) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "retry-button";
      retry.innerHTML = `${svg("retry")}<span>Try again</span>`;
      retry.addEventListener("click", () => retryMessage(chatId, message));
      col.appendChild(retry);
    }

    const footer = document.createElement("div");
    footer.className = "msg-footer";
    const when = messageTime(message);
    if (when) footer.insertAdjacentHTML("beforeend", `<span class="msg-time">${escapeHtml(when)}</span>`);
    if (KIND_LABELS[kind]) footer.insertAdjacentHTML("beforeend", `<span class="tag kind">${KIND_LABELS[kind]}</span>`);
    const labels = [...new Set((message.tools_used || []).map((t) => TOOL_LABELS[t] || "Great.cards data"))];
    for (const label of labels) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = `Source: ${label}`;
      footer.appendChild(tag);
    }
    if (kind !== "error" && message.text) {
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "msg-action";
      copy.setAttribute("aria-label", "Copy answer");
      copy.innerHTML = `${svg("copy")}<span>Copy</span>`;
      copy.addEventListener("click", () => copyText(message.text, copy));
      footer.appendChild(copy);
    }
    col.appendChild(footer);
    wrapper.appendChild(col);
    return wrapper;
  }

  function buildMessage(message, chatId) {
    return message.role === "user" ? buildUserMessage(message) : buildBotMessage(message, chatId);
  }

  async function copyText(text, button) {
    try {
      await navigator.clipboard.writeText(text);
      button.classList.add("done");
      button.innerHTML = `${svg("check")}<span>Copied</span>`;
      setTimeout(() => {
        button.classList.remove("done");
        button.innerHTML = `${svg("copy")}<span>Copy</span>`;
      }, 1800);
    } catch {
      toast("Couldn't copy to the clipboard", { type: "error" });
    }
  }

  function appendMessage(message, chatId) {
    const stick = nearBottom();
    document.getElementById("empty")?.remove();
    const el = buildMessage(message, chatId);
    el.classList.add("enter");
    thread.insertBefore(el, document.getElementById("typing"));
    if (stick || message.role === "user") {
      scrollToBottom();
    } else {
      scrollDown.hidden = false;
      scrollDown.classList.add("has-new");
    }
  }

  // ---- Typing indicator ----
  let typingTimers = [];

  function showTyping() {
    hideTyping();
    const wrapper = document.createElement("div");
    wrapper.className = "message bot enter";
    wrapper.id = "typing";
    wrapper.innerHTML =
      '<span class="avatar" aria-hidden="true">G</span><div class="msg-col"><div class="bubble typing-bubble">' +
      '<span class="typing" aria-hidden="true"><span></span><span></span><span></span></span>' +
      '<span class="typing-label">Thinking…</span></div></div>';
    thread.appendChild(wrapper);
    const label = wrapper.querySelector(".typing-label");
    const steps = [[2500, "Checking Great.cards data…"], [9000, "Still working on it…"], [25000, "Almost there, thanks for waiting…"]];
    typingTimers = steps.map(([delay, text]) => setTimeout(() => { label.textContent = text; }, delay));
    scrollToBottom();
  }

  function hideTyping() {
    typingTimers.forEach(clearTimeout);
    typingTimers = [];
    document.getElementById("typing")?.remove();
  }

  // ---- Thread (main panel) ----
  function renderEmpty() {
    const node = emptyTemplate.content.firstElementChild.cloneNode(true);
    node.id = "empty";
    thread.appendChild(node);
  }

  function renderThread() {
    const chat = activeChat();
    hideTyping();
    thread.replaceChildren();
    thread.classList.remove("switching");
    void thread.offsetWidth;   // restart the fade animation
    thread.classList.add("switching");
    if (!chat.messages.length) {
      renderEmpty();
    } else {
      const fragment = document.createDocumentFragment();
      for (const message of chat.messages) fragment.appendChild(buildMessage(message, chat.id));
      thread.appendChild(fragment);
    }
    if (chat.pending) showTyping();
    scrollToBottom(false);
    updateHeader();
    updateComposer();
    updateScrollButton();
  }

  function renderThreadLoading() {
    hideTyping();
    thread.replaceChildren();
    thread.insertAdjacentHTML("beforeend",
      '<div class="thread-skeleton" aria-label="Loading conversation">' +
      '<div class="skeleton s-user"></div><div class="skeleton s-bot"></div>' +
      '<div class="skeleton s-user"></div><div class="skeleton s-bot"></div></div>');
  }

  function renderThreadError(id) {
    thread.replaceChildren();
    const box = document.createElement("div");
    box.className = "thread-error";
    box.innerHTML = "<h2>Couldn't load this chat</h2><p>Check that the server is running, then try again.</p>";
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "btn";
    retry.innerHTML = `${svg("retry")}<span>Retry</span>`;
    retry.addEventListener("click", () => openChat(id, { force: true }));
    box.appendChild(retry);
    thread.appendChild(box);
  }

  function updateHeader() {
    const chat = activeChat();
    chatTitle.textContent = chat?.title || "New chat";
    const count = chat ? chat.messages.filter((m) => m.role === "user").length : 0;
    chatSub.textContent = chat?.pending ? "Advisor is replying…"
      : count ? `${count} question${count === 1 ? "" : "s"}` : "Credit card advisor";
    document.title = chat && chat.messages.length ? `${chat.title} · Card Advisor` : "Great.cards Credit Card Advisor";
  }

  // ---- Composer ----
  function updateComposer() {
    const chat = activeChat();
    const pending = Boolean(chat?.pending);
    const length = input.value.length;
    sendButton.disabled = pending || !input.value.trim();
    sendButton.classList.toggle("busy", pending);
    sendButton.setAttribute("aria-label", pending ? "Waiting for the reply" : "Send message");
    if (length >= MAX_CHARS * 0.8) {
      charCount.textContent = `${length}/${MAX_CHARS}`;
      charCount.classList.toggle("limit", length >= MAX_CHARS);
    } else {
      charCount.textContent = "";
    }
  }

  function autoResize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
  }

  function nudgeComposer() {
    form.classList.remove("shake");
    void form.offsetWidth;
    form.classList.add("shake");
  }

  // ---- Sidebar: chat history ----
  function renderHistory() {
    const items = [...chats.values()]
      .filter((chat) => chat.messages.length || chat.turns || chat.id === activeId)
      .sort((a, b) => (b.updated?.getTime() || Infinity) - (a.updated?.getTime() || Infinity));
    historyList.setAttribute("aria-busy", "false");

    if (!items.length || (items.length === 1 && !items[0].messages.length && !items[0].turns)) {
      historyList.innerHTML = '<div class="history-empty">No previous chats yet.<br>Your conversations will appear here.</div>';
      return;
    }

    const known = new Set([...historyList.querySelectorAll(".history-item")].map((el) => el.dataset.id));
    const fragment = document.createDocumentFragment();
    let group = "";
    for (const chat of items) {
      const bucket = chat.updated ? dayBucket(chat.updated) : "Today";
      if (bucket !== group) {
        group = bucket;
        const heading = document.createElement("div");
        heading.className = "history-group";
        heading.textContent = bucket;
        fragment.appendChild(heading);
      }
      const button = document.createElement("button");
      button.type = "button";
      button.className = "history-item";
      if (historyLoaded && known.size && !known.has(chat.id)) button.classList.add("enter");
      button.dataset.id = chat.id;
      if (chat.id === activeId) button.setAttribute("aria-current", "true");
      const turns = chat.messages.length ? chat.messages.filter((m) => m.role === "user").length : chat.turns;
      const meta = chat.pending ? "Replying…"
        : [shortWhen(chat.updated), turns ? `${turns} message${turns === 1 ? "" : "s"}` : "Empty"].filter(Boolean).join(" · ");
      button.innerHTML =
        `<span class="history-body"><span class="history-title"></span><span class="history-meta"></span></span>` +
        (chat.pending ? '<span class="spinner" aria-hidden="true"></span>'
          : chat.unread ? '<span class="history-badge" aria-label="New reply"></span>' : "");
      button.querySelector(".history-title").textContent = chat.title;
      button.querySelector(".history-meta").textContent = meta;
      button.title = chat.title;
      fragment.appendChild(button);
    }
    historyList.replaceChildren(fragment);
  }

  function renderHistorySkeleton() {
    historyList.setAttribute("aria-busy", "true");
    historyList.innerHTML = '<div class="skeleton-row"></div>'.repeat(4);
  }

  function renderHistoryError() {
    historyList.setAttribute("aria-busy", "false");
    historyList.innerHTML = '<div class="history-error">Couldn\'t load your chats.<br><button type="button">Try again</button></div>';
    historyList.querySelector("button").addEventListener("click", () => loadHistory({ showSkeleton: true }));
  }

  historyList.addEventListener("click", (event) => {
    const item = event.target.closest(".history-item");
    if (item) openChat(item.dataset.id);
  });

  async function loadHistory({ showSkeleton = false, announce = false } = {}) {
    if (showSkeleton) renderHistorySkeleton();
    historyRefresh.classList.add("spinning");
    try {
      const response = await fetch("/api/conversations", { credentials: "same-origin" });
      if (!response.ok) throw new Error(String(response.status));
      const list = await response.json();
      for (const summary of list) {
        const existing = chats.get(summary.id);
        const updated = parseDate(summary.updated);
        if (existing) {
          existing.turns = summary.turns;
          if (!existing.pending) existing.updated = updated || existing.updated;
          if (!existing.messages.length) existing.title = summary.title;
        } else {
          chats.set(summary.id, {
            id: summary.id, title: summary.title, messages: [], loaded: false,
            pending: false, unread: false, updated, turns: summary.turns,
          });
        }
      }
      renderHistory();
      historyLoaded = true;
      if (announce) toast("Chat list updated");
      return true;
    } catch {
      if (!historyLoaded) renderHistoryError();
      else if (announce) toast("Couldn't refresh your chats", { type: "error" });
      return false;
    } finally {
      historyRefresh.classList.remove("spinning");
    }
  }

  // ---- Chats ----
  function startNewChat({ silent = false } = {}) {
    const current = activeChat();
    closeDrawer();
    if (current && !current.messages.length && !current.pending) {
      input.focus();
      if (!silent) toast("You're already in a new chat", { type: "info" });
      return;
    }
    const chat = { id: newId(), title: "New chat", messages: [], loaded: true, pending: false, unread: false, updated: null, turns: 0 };
    chats.set(chat.id, chat);
    activeId = chat.id;
    storage.set(chat.id);
    renderThread();
    renderHistory();
    if (!silent || !isTouch) input.focus();   // don't pop the phone keyboard on page load
    if (!silent) toast("New chat started");
  }

  async function openChat(id, { force = false } = {}) {
    const chat = chats.get(id);
    if (!chat) return;
    closeDrawer();
    if (id === activeId && !force) return;
    activeId = id;
    storage.set(id);
    chat.unread = false;
    renderHistory();
    if (chat.loaded) { renderThread(); return; }

    const token = ++loadToken;
    chatTitle.textContent = chat.title;
    chatSub.textContent = "Loading conversation…";
    renderThreadLoading();
    updateComposer();
    try {
      const response = await fetch(`/api/conversations/${encodeURIComponent(id)}`, { credentials: "same-origin" });
      if (!response.ok) throw new Error(String(response.status));
      const data = await response.json();
      if (!chat.loaded) {   // a reply may have arrived meanwhile; never overwrite local messages
        chat.messages = data.messages;
        chat.title = data.title || chat.title;
        chat.loaded = true;
      }
      if (token === loadToken && activeId === id) renderThread();
    } catch {
      if (token === loadToken && activeId === id) {
        renderThreadError(id);
        chatSub.textContent = "Couldn't load";
        toast("Couldn't load that conversation", { type: "error" });
      }
    }
  }

  // ---- Networking ----
  async function sendMessage(text) {
    const message = text.trim();
    const chat = activeChat();
    if (!message || !chat) return;
    if (chat.pending) {
      nudgeComposer();
      toast("Please wait for the current reply", { type: "info" });
      return;
    }
    if (!chat.loaded) {
      toast("This chat is still loading", { type: "info" });
      return;
    }

    const now = new Date();
    chat.messages.push({ role: "user", text: message, time: now.toISOString() });
    if (chat.title === "New chat") chat.title = titleFrom(message);
    chat.pending = true;
    chat.updated = now;
    appendMessage(chat.messages[chat.messages.length - 1], chat.id);
    input.value = "";
    autoResize();
    showTyping();
    updateHeader();
    updateComposer();
    renderHistory();
    closeDrawer();

    const reply = await requestReply(chat.id, message);
    reply.time = new Date().toISOString();
    chat.messages.push(reply);
    chat.pending = false;
    chat.updated = new Date();
    if (reply.kind !== "error" || !reply.retry) chat.turns = chat.messages.filter((m) => m.role === "user").length;

    if (chat.id === activeId) {
      hideTyping();
      appendMessage(reply, chat.id);
      updateHeader();
      updateComposer();
      if (reply.retry) toast(reply.text, { type: "error" });
    } else {
      chat.unread = true;
      toast(`New reply in “${chat.title}”`, { type: "info", action: { label: "View", run: () => openChat(chat.id) } });
    }
    renderHistory();
    refreshStatus();
  }

  async function requestReply(chatId, message) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ message, chat_id: chatId }),
        signal: controller.signal,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        return { role: "assistant", kind: "error", text: data.detail || "Something went wrong. Please try again.", retry: true, request: message };
      }
      return { role: "assistant", text: data.reply, kind: data.kind, tools_used: data.tools_used || [], notices: data.notices || [] };
    } catch (error) {
      const text = error.name === "AbortError"
        ? "The reply took too long. Please try again."
        : "I couldn't reach the server. Check your connection and try again.";
      return { role: "assistant", kind: "error", text, retry: true, request: message };
    } finally {
      clearTimeout(timer);
    }
  }

  function retryMessage(chatId, failed) {
    const chat = chats.get(chatId);
    if (!chat || chat.pending) return;
    const index = chat.messages.indexOf(failed);
    if (index < 1) return;
    chat.messages.splice(index - 1, 2);   // drop the failed exchange; it is re-sent below
    if (chat.id === activeId) renderThread();
    sendMessage(failed.request);
  }

  // ---- Service status ----
  const statusTargets = {
    llm: { card: $("status-llm"), mini: $("mini-llm"), name: "Gemini LLM" },
    mcp: { card: $("status-mcp"), mini: $("mini-mcp"), name: "MCP server" },
  };
  const lastStates = {};

  function applyStatus(key, status) {
    const target = statusTargets[key];
    target.card.dataset.state = status.state;
    target.mini.dataset.state = status.state;
    target.card.querySelector(".status-label").textContent = status.label;
    const tip = `${target.name}: ${status.label}${status.detail ? ` (${status.detail})` : ""}`;
    target.card.title = tip;
    target.mini.title = tip;
    target.card.setAttribute("aria-label", tip);
    const previous = lastStates[key];
    lastStates[key] = status.state;
    if (previous && previous !== status.state) {
      if (status.state === "ready" && (previous === "offline" || previous === "degraded")) toast(`${target.name} is back online`);
      else if (status.state === "offline" || status.state === "degraded") toast(`${target.name}: ${status.label}`, { type: "error" });
    }
  }

  let statusBusy = false;
  async function refreshStatus() {
    if (statusBusy) return;
    statusBusy = true;
    try {
      const response = await fetch("/api/status", { credentials: "same-origin" });
      if (!response.ok) throw new Error(String(response.status));
      const data = await response.json();
      applyStatus("llm", data.llm);
      applyStatus("mcp", data.mcp);
    } catch {
      const down = { state: "offline", label: "Server unreachable", detail: "The app server didn't respond" };
      applyStatus("llm", down);
      applyStatus("mcp", down);
    } finally {
      statusBusy = false;
    }
  }

  // ---- Drawer (tablet & mobile) ----
  const drawerQuery = window.matchMedia("(max-width: 900px)");
  let lastFocus = null;

  function openDrawer() {
    lastFocus = document.activeElement;
    shell.classList.add("drawer-open");
    backdrop.hidden = false;
    menuButton.setAttribute("aria-expanded", "true");
    main.inert = true;
    setTimeout(() => $("new-chat").focus(), 50);
  }

  function closeDrawer() {
    if (!shell.classList.contains("drawer-open")) return;
    shell.classList.remove("drawer-open");
    menuButton.setAttribute("aria-expanded", "false");
    main.inert = false;
    setTimeout(() => { if (!shell.classList.contains("drawer-open")) backdrop.hidden = true; }, 300);
    if (lastFocus && document.body.contains(lastFocus)) lastFocus.focus();
  }

  drawerQuery.addEventListener("change", () => { if (!drawerQuery.matches) closeDrawer(); });

  // ---- Wiring ----
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    sendMessage(input.value);
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (input.value.trim()) sendMessage(input.value);
    }
  });
  input.addEventListener("input", () => { autoResize(); updateComposer(); });

  document.addEventListener("click", (event) => {
    const example = event.target.closest("[data-prompt]");
    if (!example) return;
    const chat = activeChat();
    if (chat?.pending) { nudgeComposer(); toast("Please wait for the current reply", { type: "info" }); return; }
    sendMessage(example.dataset.prompt);
  });

  $("new-chat").addEventListener("click", () => startNewChat());
  $("head-new").addEventListener("click", () => startNewChat());
  $("sidebar-close").addEventListener("click", closeDrawer);
  menuButton.addEventListener("click", openDrawer);
  backdrop.addEventListener("click", closeDrawer);
  historyRefresh.addEventListener("click", () => loadHistory({ announce: true }));
  scrollDown.addEventListener("click", () => scrollToBottom());
  messages.addEventListener("scroll", updateScrollButton, { passive: true });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDrawer();
    if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === "o") {
      event.preventDefault();
      startNewChat();
    }
  });

  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });
  setInterval(() => { if (!document.hidden) refreshStatus(); }, STATUS_POLL_MS);

  // ---- Start ----
  if (window.matchMedia("(max-width: 600px)").matches) input.placeholder = "Ask about credit cards…";
  const remembered = storage.get();   // the chat this tab had open before a reload
  startNewChat({ silent: true });
  const blankId = activeId;
  renderHistorySkeleton();
  refreshStatus();
  loadHistory().then((ok) => {
    const blank = chats.get(blankId);
    if (ok && remembered && chats.has(remembered) && activeId === blankId && !blank.messages.length) {
      chats.delete(blankId);
      openChat(remembered);
    }
  });
})();
