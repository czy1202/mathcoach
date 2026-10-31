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
  const modeGate = document.getElementById("mode-gate");
  const modeCards = Array.from(document.querySelectorAll(".mode-card"));
  const homeBtn = document.getElementById("home-btn");
  const papersView = document.getElementById("papers-view");
  const editionList = document.getElementById("edition-list");
  const paperDetail = document.getElementById("paper-detail");
  const paperPlaceholder = document.getElementById("paper-placeholder");
  const paperContent = document.getElementById("paper-content");
  const composer = document.querySelector(".composer");

  const DEFAULT_PLACEHOLDER = input.placeholder;
  let busy = false;
  let sessionId = null;

  let mode = null; // null = 模式选择页
  let papersState = "idle"; // idle | loading | ready | error
  let editions = [];
  let paperRequestId = 0;

  // ---------- Markdown / 公式渲染 ----------
  //
  // 渲染顺序很关键：markdown 会把 `_` 当成斜体、把 `*` 当成加粗，
  // 直接喂给 marked 会破坏 LaTeX（例如 y_1、x^*）。所以先把数学块
  // 抽出来换成纯字母数字占位符，渲染完 markdown 后再用 KaTeX 还原。
  // 代码块和行内代码里的 $ 不算公式，避免把 shell 变量之类误渲染。

  const marked = window.marked || null;

  const CODE_OR_MATH = new RegExp(
    [
      "(```[\\s\\S]*?```|~~~[\\s\\S]*?~~~|`[^`\\n]*`)", // 1: 代码，原样保留
      "(\\$\\$[\\s\\S]+?\\$\\$"                          // 2a: $$...$$ 独立公式
        + "|\\\\\\[[\\s\\S]+?\\\\\\]"                    // 2b: \[...\] 独立公式
        + "|\\\\\\([\\s\\S]+?\\\\\\)"                    // 2c: \(...\) 行内公式
        + "|(?<!\\\\)\\$(?!\\s)(?:\\\\.|[^$\\\\\\n])+?(?<!\\s)\\$)", // 2d: $...$
    ].join("|"),
    "g",
  );

  function protectMath(text) {
    const store = [];
    const protectedText = String(text).replace(
      CODE_OR_MATH,
      (match, code, math) => {
        if (code || !math) return match;
        let body;
        let display;
        if (math.startsWith("$$")) {
          body = math.slice(2, -2);
          display = true;
        } else if (math.startsWith("\\[")) {
          body = math.slice(2, -2);
          display = true;
        } else if (math.startsWith("\\(")) {
          body = math.slice(2, -2);
          display = false;
        } else {
          body = math.slice(1, -1);
          display = false;
        }
        const token = `MCKATEXTOKEN${store.length}END`;
        store.push({ token, body: body.trim(), display });
        return token;
      },
    );
    return { text: protectedText, store };
  }

  function renderMath(body, displayMode) {
    if (!window.katex) {
      const wrapped = displayMode ? `$$${body}$$` : `$${body}$`;
      return `<span class="math-fallback">${escapeHtml(wrapped)}</span>`;
    }
    try {
      // 行内 $\lim_{x \to 0}$ 在 TeX 里默认把下标放在右侧；本项目的
      // 排版要求是下标落在 lim 正下方，所以统一补一个 \limits。
      const tex = body.replace(/\\lim(?![a-zA-Z])(?!\\limits)/g, "\\lim\\limits");
      return window.katex.renderToString(tex, {
        displayMode,
        throwOnError: false,
        strict: "ignore",
        trust: false,
        output: "htmlAndMathml",
      });
    } catch (err) {
      console.error("公式渲染失败", err);
      return `<span class="math-fallback">${escapeHtml(
        displayMode ? `$$${body}$$` : `$${body}$`,
      )}</span>`;
    }
  }

  function restoreMath(html, store) {
    if (!store.length) return html;
    // 独立公式独占一行时，marked 会包一层 <p>；去掉它，避免
    // .katex-display 的块级排版和段落边距叠在一起。
    const unwrapped = html.replace(
      /<p>\s*(MCKATEXTOKEN(\d+)END)\s*<\/p>/g,
      (match, token, index) => {
        const item = store[Number(index)];
        return item && item.display ? token : match;
      },
    );
    return unwrapped.replace(/MCKATEXTOKEN(\d+)END/g, (match, index) => {
      const item = store[Number(index)];
      if (!item) return match;
      return renderMath(item.body, item.display);
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
    if (!marked) return `<pre class="fallback">${escapeHtml(text)}</pre>`;
    let protectedText = String(text);
    let store = [];
    try {
      const result = protectMath(text);
      protectedText = result.text;
      store = result.store;
      // 先渲染 markdown 并把占位符原样留在 HTML 里，再消毒，
      // 最后才插入 KaTeX 生成的排版（KaTeX 会对 TeX 源做转义）。
      const html = marked.parse(protectedText, {
        gfm: true,
        breaks: true,
        async: false,
      });
      return restoreMath(sanitize(html), store);
    } catch (err) {
      console.error("Markdown 渲染失败", err);
      try {
        return restoreMath(sanitize(escapeHtml(protectedText)), store);
      } catch (_) {
        return `<pre class="fallback">${escapeHtml(text)}</pre>`;
      }
    }
  }

  // 最终答案卡片只占一行，用 parseInline 避免多出 <p> 影响卡片布局。
  function renderInline(text) {
    if (!text) return "";
    if (!marked || typeof marked.parseInline !== "function") {
      return escapeHtml(text);
    }
    try {
      const result = protectMath(text);
      const html = marked.parseInline(result.text, { gfm: true, breaks: false });
      return restoreMath(sanitize(html), result.store);
    } catch (err) {
      console.error("行内内容渲染失败", err);
      return escapeHtml(text);
    }
  }

  function latexifySimple(expr) {
    return String(expr)
      .trim()
      .replace(/\bpi\b/g, "\\pi ")
      .replace(/π/g, "\\pi ")
      .replace(/\bsqrt\(([^()]*)\)/g, "\\sqrt{$1}")
      .replace(/\*/g, "\\cdot ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function formatFinalAnswer(value) {
    const text = String(value)
      .replace(/\(\(([^()]+)\)\/\(([^()]+)\)\)/g, "($1)/($2)")
      .replace(/\*\*/g, "^")
      .replace(/(?<!\\)\bpi\b/g, "π");

    // 服务端提取答案时会把 \frac{a}{b} 归一化成 ((a)/(b)) 交给判分器。
    // 卡片是展示给用户看的，这里把整个答案就是简单分数的情况还原成排版公式。
    const fraction = text.match(/^\(?([^()/]+)\)?\s*\/\s*\(?([^()/]+)\)?$/);
    if (fraction && !text.includes("$")) {
      return `$\\frac{${latexifySimple(fraction[1])}}{${latexifySimple(fraction[2])}}$`;
    }
    return text;
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

      const question = document.createElement("div");
      question.className = "related-question";
      if (item.display_question) {
        // 构建阶段已把 PDF 原文重建为 Markdown + LaTeX，走和助手回答
        // 相同的 KaTeX 管线；没有缓存时退回纯文本，至少不会显示乱码源码。
        question.classList.add("rendered");
        question.innerHTML = renderMarkdown(item.display_question);
      } else {
        question.textContent = item.question || "";
      }
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
      card.querySelector(".value").innerHTML = renderInline(
        formatFinalAnswer(data.final_answer),
      );
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

  // ---------- 真题浏览 ----------

  function setMode(next) {
    if (next !== "solve" && next !== "papers") return;
    if (next === mode) return;

    mode = next;
    if (modeGate) modeGate.hidden = true;
    modeCards.forEach((card) => {
      card.setAttribute("aria-pressed", String(card.dataset.mode === next));
    });

    const solving = next === "solve";
    chat.hidden = !solving;
    if (composer) composer.hidden = !solving;
    papersView.hidden = solving;

    if (solving) {
      input.focus();
    } else {
      loadPapers();
    }
  }

  function showGate() {
    if (busy || mode === null) return;

    mode = null;
    chat.hidden = true;
    if (composer) composer.hidden = true;
    papersView.hidden = true;
    if (modeGate) modeGate.hidden = false;
    modeCards.forEach((card) => card.removeAttribute("aria-pressed"));

    if (modeCards[0]) modeCards[0].focus();
  }

  function renderPapersError(message) {
    editionList.replaceChildren();

    const box = document.createElement("div");
    box.className = "papers-error";
    box.textContent = message;

    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "ghost-btn";
    retry.textContent = "重新加载";
    retry.addEventListener("click", () => {
      papersState = "idle";
      loadPapers();
    });
    box.appendChild(retry);

    editionList.appendChild(box);
  }

  async function loadPapers() {
    if (papersState === "loading" || papersState === "ready") return;
    papersState = "loading";

    editionList.replaceChildren();
    const loading = document.createElement("p");
    loading.className = "papers-loading";
    loading.textContent = "正在加载题库…";
    editionList.appendChild(loading);

    try {
      const response = await fetch("/api/papers");
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = errorText(data.detail, response.status);
        if (response.status === 404 && detail === "Not Found") {
          throw new Error("后端还没加载真题接口，请重启 python -m web.app 后刷新页面。");
        }
        throw new Error(detail);
      }
      const list = Array.isArray(data.editions) ? data.editions : [];
      if (!list.length) {
        throw new Error("题库里还没有可浏览的试卷。");
      }
      editions = list;
      papersState = "ready";
      renderEditions();
    } catch (err) {
      papersState = "error";
      renderPapersError(err.message || "题库加载失败。");
    }
  }

  function renderEditions() {
    editionList.replaceChildren();

    editions.forEach((edition) => {
      const row = document.createElement("div");
      row.className = "edition-row";

      const label = document.createElement("div");
      label.className = "edition-label";
      label.textContent = `${edition.label || `第${edition.edition}届`} · 共 ${
        Number(edition.total || 0)
      } 题`;
      row.appendChild(label);

      const buttons = document.createElement("div");
      buttons.className = "paper-buttons";

      const papers = Array.isArray(edition.papers) ? edition.papers : [];
      papers.forEach((paper) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "paper-btn";

        const name = document.createElement("span");
        name.textContent = paper.label || "试卷";
        button.appendChild(name);

        const count = document.createElement("span");
        count.className = "count";
        count.textContent = `${Number(paper.count || 0)} 题`;
        button.appendChild(count);

        if (paper.complete === false) {
          count.classList.add("warn");
          button.title = `缺题号：${(paper.missing || []).join("、")}`;
        }

        button.addEventListener("click", () => {
          openPaper(button, edition, paper);
        });
        buttons.appendChild(button);
      });

      row.appendChild(buttons);
      editionList.appendChild(row);
    });
  }

  function revealPaperOnNarrowScreen() {
    if (window.matchMedia("(max-width: 900px)").matches) {
      // 等题目渲染完再滚，否则详情还只有占位高度，会停在卷子中间
      paperDetail.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  async function openPaper(button, edition, paper) {
    const requestId = ++paperRequestId;

    editionList.querySelectorAll(".paper-btn.is-active").forEach((node) => {
      node.classList.remove("is-active");
    });
    button.classList.add("is-active");

    paperPlaceholder.hidden = true;
    paperContent.hidden = false;
    paperContent.replaceChildren();

    const loading = document.createElement("p");
    loading.className = "papers-loading";
    loading.textContent = "正在读取完整题目…";
    paperContent.appendChild(loading);

    const params = new URLSearchParams();
    params.set("edition", String(edition.edition));
    if (paper.stage) params.set("stage", paper.stage);
    if (paper.category) params.set("category", paper.category);
    if (paper.supplement !== undefined && paper.supplement !== null) {
      params.set("supplement", paper.supplement);
    }
    if (paper.section) params.set("section", paper.section);

    try {
      const response = await fetch(`/api/paper?${params.toString()}`);
      const data = await response.json().catch(() => ({}));
      if (requestId !== paperRequestId) return;
      if (!response.ok) {
        const detail = errorText(data.detail, response.status);
        if (response.status === 404 && detail === "Not Found") {
          throw new Error("后端还没加载真题接口，请重启 python -m web.app 后刷新页面。");
        }
        throw new Error(detail);
      }
      renderPaper(edition, paper, data);
    } catch (err) {
      if (requestId !== paperRequestId) return;
      paperContent.replaceChildren();

      const box = document.createElement("div");
      box.className = "paper-placeholder";

      const mark = document.createElement("div");
      mark.className = "paper-placeholder-mark";
      mark.textContent = "!";
      box.appendChild(mark);

      const title = document.createElement("h3");
      title.textContent = "这份试卷暂时打不开";
      box.appendChild(title);

      const note = document.createElement("p");
      note.textContent = err.message || "读取失败，请稍后再试。";
      box.appendChild(note);

      paperContent.appendChild(box);
      revealPaperOnNarrowScreen();
    }
  }

  function renderPaper(edition, paper, data) {
    paperContent.replaceChildren();

    const groups = Array.isArray(data.groups) ? data.groups : [];
    const allComplete = groups.every((group) => group.complete !== false);

    const titleRow = document.createElement("div");
    titleRow.className = "paper-title-row";

    const heading = document.createElement("div");
    const title = document.createElement("h2");
    title.className = "paper-title";
    title.textContent = `${edition.label || `第${edition.edition}届`} · ${
      paper.label || "试卷"
    }`;
    heading.appendChild(title);

    const sub = document.createElement("p");
    sub.className = "paper-title-sub";
    sub.textContent = `共 ${Number(data.count || 0)} 道题 · 题干来自题库，公式按本地重建缓存排版`;
    heading.appendChild(sub);
    titleRow.appendChild(heading);

    const badge = document.createElement("span");
    badge.className = `paper-badge ${allComplete ? "ok" : "warn"}`;
    badge.textContent = allComplete ? "题号完整" : "存在缺号";
    titleRow.appendChild(badge);

    paperContent.appendChild(titleRow);

    groups.forEach((group) => {
      const section = document.createElement("section");
      section.className = "paper-group";

      const head = document.createElement("div");
      head.className = "paper-group-head";

      const name = document.createElement("h3");
      name.textContent = group.label || "试卷";
      head.appendChild(name);

      const count = document.createElement("span");
      count.textContent = `${Number(group.count || 0)} 题`;
      head.appendChild(count);
      section.appendChild(head);

      if (Array.isArray(group.missing) && group.missing.length) {
        const note = document.createElement("p");
        note.className = "paper-group-note warn";
        note.textContent = `题库缺题号：${group.missing.join("、")}`;
        section.appendChild(note);
      }

      const problems = Array.isArray(group.problems) ? group.problems : [];
      problems.forEach((problem) => {
        const item = document.createElement("article");
        item.className = "paper-question";

        const qHead = document.createElement("div");
        qHead.className = "paper-q-head";

        const number = document.createElement("span");
        number.className = "paper-q-num";
        number.textContent = `第 ${problem.number ?? "?"} 题`;
        qHead.appendChild(number);

        const source = document.createElement("span");
        source.className = "paper-q-source";
        source.textContent = problem.source || "";
        qHead.appendChild(source);
        item.appendChild(qHead);

        const text = document.createElement("div");
        text.className = "paper-q-text";
        if (problem.display_question) {
          text.classList.add("rendered");
          text.innerHTML = renderMarkdown(problem.display_question);
        } else {
          text.textContent = problem.question || "";
        }
        item.appendChild(text);

        const tags = Array.isArray(problem.knowledge_points)
          ? problem.knowledge_points.slice(0, 4)
          : [];
        if (tags.length) {
          const tagRow = document.createElement("div");
          tagRow.className = "paper-q-tags";
          tags.forEach((tag) => {
            const chip = document.createElement("span");
            chip.className = "paper-q-tag";
            chip.textContent = tag;
            tagRow.appendChild(chip);
          });
          item.appendChild(tagRow);
        }

        section.appendChild(item);
      });

      paperContent.appendChild(section);
    });

    revealPaperOnNarrowScreen();
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
    if (homeBtn) homeBtn.disabled = next;
    sendBtn.querySelector(".send-label").textContent = next ? "求解中" : "发送";
  }

  function startNewChat() {
    if (busy) return;

    // 新对话始终回到“问题目”，避免在真题页清空聊天后看不见结果
    setMode("solve");

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

  modeCards.forEach((card) => {
    card.addEventListener("click", () => {
      setMode(card.dataset.mode === "papers" ? "papers" : "solve");
    });
  });

  if (homeBtn) homeBtn.addEventListener("click", showGate);

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
})();
