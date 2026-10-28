/* mathcoach Web 前端：Markdown + KaTeX 渲染，不做任何业务推理。 */
(() => {
  "use strict";

  const chat = document.getElementById("chat");
  const welcome = document.getElementById("welcome");
  const form = document.getElementById("form");
  const input = document.getElementById("question");
  const sendBtn = document.getElementById("send");
  const newChatBtn = document.getElementById("new-chat");
  const status = document.getElementById("status");
  const statusText = document.getElementById("status-text");

  const DEFAULT_PLACEHOLDER = input.placeholder;
  let busy = false;
  let sessionId = null;

  // ---------- Markdown / 公式渲染 ----------

  const md = typeof window.markdownit === "function"
    ? window.markdownit({ html: false, linkify: true, breaks: true })
    : null;

  if (md && window.texmath && window.katex) {
    md.use(window.texmath, {
      engine: window.katex,
      delimiters: "dollars",
      katexOptions: {
        throwOnError: false,
        strict: "ignore",
        trust: false,
      },
    });
  }

  function escapeHtml(text) {
    return String(text)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function sanitize(html) {
    if (!window.DOMPurify) return html;
    return window.DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true, mathMl: true },
    });
  }

  function renderMarkdown(text) {
    if (!text) return "";
    if (!md) return `<pre class="fallback">${escapeHtml(text)}</pre>`;
    try {
      return sanitize(md.render(text));
    } catch (err) {
      console.error("Markdown 渲染失败", err);
      return `<pre class="fallback">${escapeHtml(text)}</pre>`;
    }
  }

  function formatFinalAnswer(value) {
    return String(value)
      .replace(/\(\(([^()]+)\)\/\(([^()]+)\)\)/g, "($1)/($2)")
      .replace(/\*\*/g, "^")
      .replace(/\bpi\b/g, "π");
  }

  // ---------- 消息 DOM ----------

  function hideWelcome() {
    if (welcome) welcome.hidden = true;
  }

  function showWelcome() {
    if (welcome) welcome.hidden = false;
  }

  function appendUser(text) {
    const article = document.createElement("article");
    article.className = "message user";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    article.appendChild(bubble);
    chat.appendChild(article);
    return article;
  }

  function scrollToElement(element) {
    if (!element) return;
    const top = element.getBoundingClientRect().top
      - chat.getBoundingClientRect().top
      + chat.scrollTop
      - 18;
    chat.scrollTo({ top, behavior: "smooth" });
  }

  function createPending() {
    const article = document.createElement("article");
    article.className = "message assistant";
    article.innerHTML = `
      <div class="avatar">∑</div>
      <div class="bubble">
        <div class="thinking">
          <span class="spinner"></span>
          <span class="thinking-text">正在分析题目…</span>
        </div>
      </div>`;
    const label = article.querySelector(".thinking-text");
    const started = performance.now();
    article._timer = setInterval(() => {
      const seconds = ((performance.now() - started) / 1000).toFixed(1);
      label.textContent = `正在分析题目并调用工具验证… ${seconds}s`;
    }, 100);
    return article;
  }

  function buildTrace(data) {
    const body = document.createElement("div");
    body.className = "trace-body";

    const steps = Array.isArray(data.steps) ? data.steps : [];
    const tools = Array.isArray(data.tools) ? data.tools : [];

    if (!steps.length && !tools.length) {
      body.textContent = "本次没有记录到轨迹。";
      return body;
    }

    steps.forEach((step) => {
      const row = document.createElement("div");
      row.className = "trace-step";
      const seconds = Number(step.elapsed || 0).toFixed(2);
      const suffix = step.tool_count
        ? `请求 ${step.tool_count} 个工具`
        : "生成回答";
      row.textContent = `第 ${step.step ?? "?"} 步 · 模型调用 ${seconds}s · ${suffix}`;
      body.appendChild(row);
    });

    tools.forEach((tool) => {
      const item = document.createElement("div");
      item.className = "trace-tool" + (tool.ok ? "" : " bad");

      const title = document.createElement("div");
      title.className = "trace-tool-title";
      const ms = (Number(tool.elapsed || 0) * 1000).toFixed(0);
      title.textContent = `${tool.ok ? "✓" : "✕"} ${tool.name || "工具"} · ${ms}ms`;
      item.appendChild(title);

      const args = document.createElement("pre");
      args.textContent = JSON.stringify(tool.args || {}, null, 2);
      item.appendChild(args);

      const result = document.createElement("pre");
      result.textContent = String(tool.result || "");
      item.appendChild(result);

      body.appendChild(item);
    });

    return body;
  }

  function createRelatedCard(list) {
    if (!Array.isArray(list) || !list.length) return null;

    const box = document.createElement("section");
    box.className = "related";

    const head = document.createElement("div");
    head.className = "related-head";
    head.textContent = "相关真题";
    box.appendChild(head);

    const listEl = document.createElement("div");
    listEl.className = "related-list";

    list.forEach((item) => {
      const card = document.createElement("div");
      card.className = "related-item";

      const source = document.createElement("div");
      source.className = "related-source";
      source.textContent = item.source || "CMC 历年真题";
      card.appendChild(source);

      const question = document.createElement("p");
      question.className = "related-question";
      question.textContent = item.question || "";
      card.appendChild(question);

      const tags = Array.isArray(item.knowledge_points)
        ? item.knowledge_points.slice(0, 4)
        : [];
      if (tags.length) {
        const tagRow = document.createElement("div");
        tagRow.className = "related-tags";
        tags.forEach((tag) => {
          const chip = document.createElement("span");
          chip.className = "related-tag";
          chip.textContent = tag;
          tagRow.appendChild(chip);
        });
        card.appendChild(tagRow);
      }

      listEl.appendChild(card);
    });

    box.appendChild(listEl);
    return box;
  }

  function createAssistantMessage(data) {
    const article = document.createElement("article");
    article.className = "message assistant";
    article.innerHTML = `
      <div class="avatar">∑</div>
      <div class="bubble">
        <div class="content"></div>
        <div class="answer-card" hidden>
          <div class="label">最终答案</div>
          <div class="value"></div>
        </div>
        <div class="related-slot"></div>
        <div class="meta"></div>
        <details class="trace" hidden>
          <summary></summary>
          <div class="trace-slot"></div>
        </details>
        <div class="error-slot"></div>
      </div>`;

    const content = article.querySelector(".content");
    content.innerHTML = renderMarkdown(data.display_answer || data.answer || "(空回答)");

    if (data.final_answer) {
      const card = article.querySelector(".answer-card");
      card.hidden = false;
      card.querySelector(".value").textContent = formatFinalAnswer(data.final_answer);
    }

    const related = createRelatedCard(data.related_problems);
    if (related) article.querySelector(".related-slot").appendChild(related);

    const meta = article.querySelector(".meta");
    const parts = [
      `耗时 ${Number(data.elapsed || 0).toFixed(2)}s`,
      `模型 ${data.llm_calls || 0} 次`,
      `工具 ${data.tool_calls || 0} 次`,
      `${Number(data.tokens || 0).toLocaleString()} tokens`,
    ];
    if (Number(data.turn || 1) > 1) {
      parts.unshift(`第 ${data.turn} 轮`);
    }
    if (data.tool_errors) parts.push(`工具错误 ${data.tool_errors} 次`);
    meta.textContent = parts.join(" · ");

    const details = article.querySelector(".trace");
    if ((data.steps && data.steps.length) || (data.tools && data.tools.length)) {
      details.hidden = false;
      details.querySelector("summary").textContent =
        `查看解题轨迹（${data.llm_calls || 0} 次模型调用 · ${data.tool_calls || 0} 次工具调用）`;
      details.querySelector(".trace-slot").appendChild(buildTrace(data));
    }

    if (data.error) {
      const box = document.createElement("div");
      box.className = "error-box";
      box.textContent = data.error;
      article.querySelector(".error-slot").appendChild(box);
    }

    if (data.trace_file) {
      const traceNote = document.createElement("div");
      traceNote.className = "meta";
      traceNote.textContent = `轨迹文件：${data.trace_file}`;
      article.querySelector(".bubble").appendChild(traceNote);
    }

    return article;
  }

  function createErrorMessage(message) {
    const article = document.createElement("article");
    article.className = "message assistant";
    article.innerHTML = `
      <div class="avatar">∑</div>
      <div class="bubble"><div class="error-box"></div></div>`;
    article.querySelector(".error-box").textContent = message;
    return article;
  }

  // ---------- 交互 ----------

  function scrollToBottom() {
    chat.scrollTop = chat.scrollHeight;
  }

  function resizeInput() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
  }

  function setBusy(next) {
    busy = next;
    sendBtn.disabled = next;
    newChatBtn.disabled = next;
    input.disabled = next;
    sendBtn.querySelector(".send-label").textContent = next ? "求解中" : "发送";
  }

  function startNewChat() {
    if (busy) return;

    const oldSession = sessionId;
    sessionId = null;
    chat.querySelectorAll(".message").forEach((node) => node.remove());
    showWelcome();

    input.value = "";
    input.placeholder = DEFAULT_PLACEHOLDER;
    resizeInput();
    chat.scrollTo({ top: 0, behavior: "smooth" });
    input.focus();

    if (oldSession) {
      fetch(`/api/session/${oldSession}`, { method: "DELETE" }).catch(() => {
        // 删除失败不影响本地开新对话，旧会话会按 TTL 自行过期
      });
    }
  }

  function errorText(detail, statusCode) {
    if (Array.isArray(detail)) {
      return detail.map((item) => item.msg || "参数不合法").join("；");
    }
    return detail || `请求失败（HTTP ${statusCode}）`;
  }

  async function submitQuestion() {
    const question = input.value.trim();
    if (!question || busy) return;

    setBusy(true);
    hideWelcome();
    const userMessage = appendUser(question);
    input.value = "";
    resizeInput();

    const pending = createPending();
    chat.appendChild(pending);
    scrollToBottom();

    try {
      const response = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, session_id: sessionId }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(errorText(data.detail, response.status));
      }
      sessionId = data.session_id || sessionId;
      input.placeholder = "继续追问这道题，例如：这一步为什么？";
      clearInterval(pending._timer);
      pending.replaceWith(createAssistantMessage(data));
      // 回滚到用户刚问的那一题，让读者从题目和回答开头看起；
      // 最终答案卡片仍然在消息末尾，往下滚即可看到。
      scrollToElement(userMessage);
    } catch (err) {
      clearInterval(pending._timer);
      pending.replaceWith(createErrorMessage(err.message || "网络请求失败。"));
      scrollToElement(userMessage);
    } finally {
      setBusy(false);
      input.focus();
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitQuestion();
  });

  newChatBtn.addEventListener("click", startNewChat);

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      submitQuestion();
    }
  });

  input.addEventListener("input", resizeInput);

  const examples = document.getElementById("examples");
  if (examples) {
    examples.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-q]");
      if (!button || busy) return;
      input.value = button.dataset.q || "";
      resizeInput();
      submitQuestion();
    });
  }

  // ---------- 启动 ----------

  async function loadHealth() {
    try {
      const response = await fetch("/api/health");
      if (!response.ok) throw new Error("health check failed");
      const data = await response.json();
      if (!data.key_set) {
        status.className = "status bad";
        statusText.textContent = "未配置 API Key";
        return;
      }
      status.className = "status ok";
      statusText.textContent = `模型 · ${data.model || "未命名"}`;
    } catch (err) {
      status.className = "status bad";
      statusText.textContent = "本地服务未连接";
    }
  }

  loadHealth();
  input.focus();
})();
