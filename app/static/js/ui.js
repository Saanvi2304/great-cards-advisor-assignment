/* Shared UI helpers for the chat (/) and admin (/admin) pages: theme, safe markdown, date formatting,
   toasts and the ripple effect on buttons. Exposed as window.GCUI; no dependencies. */
(() => {
  "use strict";

  // ---- Theme (Light / Dark) ----
  const THEME_KEY = "gc_theme";
  const themeToggle = document.getElementById("theme-toggle");
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
  const toasts = () => document.getElementById("toasts");

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
    toasts().appendChild(el);
    while (toasts().children.length > 3) toasts().firstElementChild.remove();
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
    const target = event.target.closest("button, a.btn");
    if (target && !target.disabled && event.button === 0) ripple(target, event.clientX, event.clientY);
  });
  document.addEventListener("click", (event) => {   // keyboard activation (Enter/Space) ripples from the centre
    const target = event.target.closest("button, a.btn");
    if (target && !target.disabled && event.detail === 0) ripple(target);
  });

  window.GCUI = {
    escapeHtml, inline, renderMarkdown, timeFormat, dateFormat, parseDate, dayBucket, shortWhen, toast, ripple,
  };
})();
