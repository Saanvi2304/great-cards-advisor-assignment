(() => {
  "use strict";

  const chat = document.getElementById("chat");
  const form = document.getElementById("composer");
  const input = document.getElementById("message");
  const sendButton = document.getElementById("send-button");
  const resetButton = document.getElementById("reset-button");
  const welcomeHtml = chat.innerHTML;

  const TOOL_LABELS = {
    recommend_cards: "Personalised ranking",
    list_cards: "Card catalogue",
    get_card_details: "Card details",
    compare_cards: "Card comparison",
    check_eligibility: "Eligibility check",
    generate_content_brief: "Category research",
    whatsapp_resolve_card_or_category: "Card lookup",
  };

  let busy = false;

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
    let list = null;
    let table = [];

    const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
    const closeTable = () => { if (table.length) { html += renderTable(table); table = []; } };

    for (const line of lines) {
      if (/^\s*\|.*\|\s*$/.test(line)) { closeList(); table.push(line); continue; }
      closeTable();

      const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
      const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
      const heading = line.match(/^\s*#{1,4}\s+(.*)$/);

      if (bullet || numbered) {
        const type = bullet ? "ul" : "ol";
        if (list !== type) { closeList(); html += `<${type}>`; list = type; }
        html += `<li>${inline((bullet || numbered)[1])}</li>`;
      } else if (heading) {
        closeList();
        html += `<h4>${inline(heading[1])}</h4>`;
      } else if (line.trim() === "") {
        closeList();
      } else {
        closeList();
        html += `<p>${inline(line)}</p>`;
      }
    }
    closeList();
    closeTable();
    return html;
  }

  // ---- Message rendering ----
  function scrollToBottom() {
    chat.scrollTop = chat.scrollHeight;
  }

  function addUserMessage(text) {
    const wrapper = document.createElement("div");
    wrapper.className = "message user";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    wrapper.appendChild(bubble);
    chat.appendChild(wrapper);
    scrollToBottom();
  }

  function addBotMessage(text, { kind = "answer", tools = [], notices = [] } = {}) {
    const wrapper = document.createElement("div");
    wrapper.className = `message bot${kind === "error" ? " error" : ""}`;

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = renderMarkdown(text);
    wrapper.appendChild(bubble);

    for (const notice of notices) {
      const note = document.createElement("div");
      note.className = "notice";
      note.textContent = notice;
      wrapper.appendChild(note);
    }

    const labels = [...new Set(tools.map((t) => TOOL_LABELS[t] || "Great.cards data"))];
    if (labels.length) {
      const meta = document.createElement("div");
      meta.className = "meta";
      for (const label of labels) {
        const tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = `Source: ${label}`;
        meta.appendChild(tag);
      }
      wrapper.appendChild(meta);
    }

    chat.appendChild(wrapper);
    scrollToBottom();
  }

  function showTyping() {
    const wrapper = document.createElement("div");
    wrapper.className = "message bot";
    wrapper.id = "typing";
    wrapper.innerHTML = '<div class="bubble"><span class="typing"><span></span><span></span><span></span></span></div>';
    chat.appendChild(wrapper);
    scrollToBottom();
  }

  function hideTyping() {
    document.getElementById("typing")?.remove();
  }

  function setBusy(state) {
    busy = state;
    sendButton.disabled = state;
    input.disabled = state;
    if (!state) input.focus();
  }

  // ---- Networking ----
  async function sendMessage(text) {
    const message = text.trim();
    if (!message || busy) return;

    document.getElementById("suggestions")?.remove();
    addUserMessage(message);
    input.value = "";
    autoResize();
    setBusy(true);
    showTyping();

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ message }),
      });
      const data = await response.json().catch(() => ({}));
      hideTyping();
      if (!response.ok) {
        addBotMessage(data.detail || "Something went wrong. Please try again.", { kind: "error" });
        return;
      }
      addBotMessage(data.reply, { kind: data.kind, tools: data.tools_used || [], notices: data.notices || [] });
    } catch (error) {
      hideTyping();
      addBotMessage("I couldn't reach the server. Check your connection and try again.", { kind: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function resetChat() {
    if (busy) return;
    try {
      await fetch("/api/reset", { method: "POST", credentials: "same-origin" });
    } catch (error) {
      /* the local view is reset regardless */
    }
    chat.innerHTML = welcomeHtml;
    bindSuggestions();
    input.focus();
  }

  // ---- UI wiring ----
  function autoResize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  }

  function bindSuggestions() {
    chat.querySelectorAll(".chip").forEach((chip) => {
      chip.addEventListener("click", () => sendMessage(chip.textContent));
    });
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    sendMessage(input.value);
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendMessage(input.value);
    }
  });

  input.addEventListener("input", autoResize);
  resetButton.addEventListener("click", resetChat);
  bindSuggestions();
  input.focus();
})();
