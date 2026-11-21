/* mathcoach Web 前端：Markdown + KaTeX 渲染，不做任何业务推理。 */
(() => {
  "use strict";

  const chat = document.getElementById("chat");
  const welcome = document.getElementById("welcome");
  const form = document.getElementById("form");
  const input = document.getElementById("question");
  const sendBtn = document.getElementById("send");
  const stopBtn = document.getElementById("stop");
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
  const stepPanel = document.getElementById("step-panel");
  const stepBackdrop = document.getElementById("step-backdrop");
  const stepClose = document.getElementById("step-close");
  const stepNewChat = document.getElementById("step-new-chat");
  const stepTitle = document.getElementById("step-panel-title");
  const stepQuote = document.getElementById("step-quote");
  const stepThread = document.getElementById("step-thread");
  const stepForm = document.getElementById("step-form");
  const stepInput = document.getElementById("step-question");
  const stepSend = document.getElementById("step-send");
  const stepStop = document.getElementById("step-stop");
  const ocrBtn = document.getElementById("ocr-btn");
  const ocrFile = document.getElementById("ocr-file");
  const ocrPreview = document.getElementById("ocr-preview");
  const ocrPreviewBody = document.getElementById("ocr-preview-body");
  const ocrPreviewClose = document.getElementById("ocr-preview-close");
  const ocrImage = document.getElementById("ocr-image");
  const ocrStatus = document.getElementById("ocr-status");
  const ocrStatusText = document.getElementById("ocr-status-text");
  const ocrSpinner = document.getElementById("ocr-spinner");
  const ocrStatusClose = document.getElementById("ocr-status-close");
  const ocrSourceToggle = document.getElementById("ocr-source-toggle");
  const ocrRetry = document.getElementById("ocr-retry");
  const ocrSourceEditor = document.getElementById("ocr-source-editor");
  const ocrSource = document.getElementById("ocr-source");
  const ocrSourceSave = document.getElementById("ocr-source-save");
  const ocrSourceCancel = document.getElementById("ocr-source-cancel");
  const ocrFormulaMap = document.getElementById("ocr-formula-map");
  const ocrCorrectForm = document.getElementById("ocr-correct-form");
  const ocrCorrectInput = document.getElementById("ocr-correct-input");
  const ocrCorrectSubmit = document.getElementById("ocr-correct-submit");
  const formulaBackdrop = document.getElementById("formula-backdrop");
  const formulaPanel = document.getElementById("formula-panel");
  const formulaPreview = document.getElementById("formula-preview");
  const formulaInput = document.getElementById("formula-input");
  const formulaSymbols = document.getElementById("formula-symbols");
  const formulaSave = document.getElementById("formula-save");
  const formulaCancel = document.getElementById("formula-cancel");
  const formulaClose = document.getElementById("formula-close");

  const DEFAULT_PLACEHOLDER = input.placeholder;
  const DRAFT_PLACEHOLDER = "可补充说明（可选），例如：只做第 (2) 问";
  const MAX_OCR_BYTES = 10 * 1024 * 1024;
  // 整页图面积大、竖长；小根号/上标在整页里最容易认错，提示用户裁单题。
  const OCR_PAGE_AREA = 1500000;
  let busy = false;
  let ocrBusy = false;
  let ocrFixBusy = false;
  let ocrFixRequestId = 0;
  let ocrFixAbort = null;
  let ocrProvider = "deepseek";
  let ocrAvailable = true;
  let ocrStatusTimer = null;
  let ocrImageUrl = null;
  let ocrAbort = null;
  let ocrRequestId = 0;
  // OCR 结果不再直接塞进输入框：它是一张可渲染、可逐条改公式的题目卡。
  let ocrDraft = "";
  let ocrSourceOpen = false;
  let formulaEditIndex = -1;
  let formulaLastFocus = null;
  const SESSION_STORAGE_KEY = "mathcoach.session_id";
  let sessionId = null;
  let solveAbort = null;
  let solveRequestId = "";

  let mode = null; // null = 模式选择页
  let papersState = "idle"; // idle | loading | ready | error
  let editions = [];
  let paperRequestId = 0;

  // 每个“步骤”有自己的迷你对话：
  // key = 主会话 + 轮次 + 步骤序号，value 里保存侧栏消息和独立 session id。
  const stepThreads = new Map();
  let activeStepKey = null;
  let lastStepButton = null;

  function newRequestId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID().replace(/-/g, "");
    }
    return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
  }

  // 页面刷新后从服务端恢复上一轮会话；隐私模式下 localStorage 可能不可用，
  // 失败时静默按“新对话”处理，不影响正常求解。
  function rememberSession(id) {
    if (!id) return;
    try {
      window.localStorage.setItem(SESSION_STORAGE_KEY, id);
    } catch (err) {
      // ignore
    }
  }

  function forgetSession() {
    try {
      window.localStorage.removeItem(SESSION_STORAGE_KEY);
    } catch (err) {
      // ignore
    }
  }

  function savedSessionId() {
    try {
      return window.localStorage.getItem(SESSION_STORAGE_KEY) || "";
    } catch (err) {
      return "";
    }
  }

  // 浏览器中断 fetch 只能让自己不再等待，服务端线程仍可能在跑。
  // 额外通知一次，让 Agent 在下一次模型/工具调用前停下。
  function requestCancel(requestId) {
    if (!requestId) return;
    fetch("/api/ask/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: requestId }),
      keepalive: true,
    }).catch(() => {
      // 取消失败不影响本地恢复输入；后台请求会按超时自行结束
    });
  }

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
        // 2d: 中文题干常见的 “... = $。”，公式末尾带空格。
        //     要求内容里至少有一个数学符号，避免把 “$5 元 ... $”
        //     这类普通文本误当成公式。
        + "|(?<!\\\\)\\$(?!\\s)(?=[^$\\n]*[\\\\^_{}=+*/<>-])"
        + "(?:\\\\.|[^$\\\\\\n])+?\\s\\$"
        // 2e: 常规 $...$，结束 $ 前不能是空白
        + "|(?<!\\\\)\\$(?!\\s)(?:\\\\.|[^$\\\\\\n])+?(?<!\\s)\\$)",
    ].join("|"),
    "g",
  );

  // 题库和模型偶尔会把 LaTeX 命令漏在 $...$ 外面，最常见的是填空题的
  // \underline{\qquad}、旧题的 \_\_\_，以及字面量换行 \n。它们不会命中
  // CODE_OR_MATH，用户就会直接看到源码。这里只修复公式和代码之外的文字。
  const BARE_LATEX = new RegExp(
    [
      "((?:\\\\_){2,})",
      "(\\\\(?:d?frac|t?frac)\\s*\\{[^{}]*\\}\\s*\\{[^{}]*\\})",
      "(\\\\(?:underline|overline|boxed|text|mathrm|mathbf|operatorname)\\s*\\{[^{}]*\\})",
      "(\\\\sqrt\\s*(?:\\[[^\\]]*\\])?\\s*\\{[^{}]*\\})",
    ].join("|"),
    "g",
  );

  function fixBareLatexSegment(segment) {
    // JSON / 模型输出的两个字面字符 “\n”，Markdown 不会把它当换行。
    const withNewlines = segment.replace(/\\n/g, "\n");
    return withNewlines.replace(BARE_LATEX, (match) =>
      match.startsWith("\\_")
        ? "$\\underline{\\qquad\\qquad}$"
        : `$${match}$`,
    );
  }

  function normalizeBareLatex(text) {
    const source = String(text || "");
    if (!source.includes("\\")) return source;
    const scanner = new RegExp(CODE_OR_MATH.source, "g");
    let result = "";
    let last = 0;
    let match;
    while ((match = scanner.exec(source))) {
      result += fixBareLatexSegment(source.slice(last, match.index));
      result += match[0];
      last = match.index + match[0].length;
    }
    result += fixBareLatexSegment(source.slice(last));
    return result;
  }

  function protectMath(text) {
    const store = [];
    const protectedText = normalizeBareLatex(text).replace(
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

  // 题目卡里的公式要能单独点开修改。索引顺序必须和 protectMath
  // 存入 store 的顺序一致，否则会改错公式。
  function collectDraftMath(text) {
    const source = String(text || "");
    const re = new RegExp(CODE_OR_MATH.source, "g");
    const formulas = [];
    let match;
    while ((match = re.exec(source))) {
      if (match[1] || !match[2]) continue; // 代码块里的 $ 不是公式
      const raw = match[2];
      let open;
      let close;
      let display;
      if (raw.startsWith("$$")) {
        open = "$$";
        close = "$$";
        display = true;
      } else if (raw.startsWith("\\[")) {
        open = "\\[";
        close = "\\]";
        display = true;
      } else if (raw.startsWith("\\(")) {
        open = "\\(";
        close = "\\)";
        display = false;
      } else {
        open = "$";
        close = "$";
        display = false;
      }
      formulas.push({
        start: match.index,
        end: match.index + raw.length,
        open,
        close,
        body: raw.slice(open.length, raw.length - close.length).trim(),
        display,
      });
    }
    return formulas;
  }

  // OCR 模型偶尔会输出 \ $...\ $、$ x^2 $，或在公式中间换行；
  // 这些写法会让 protectMath 认不出公式，用户就会看到源码。
  // 这里只做保守修复：先把转义的美元符号还原，再压平公式内部换行。
  function cleanOcrFormulaBody(body) {
    let clean = body.replace(/\s*\n\s*/g, " ").trim();
    let trailing = "";
    const punctuation = clean.match(/[。，、；：！？]+$/);
    if (punctuation) {
      trailing = punctuation[0];
      clean = clean.slice(0, -trailing.length).trim();
    }
    return { body: clean, trailing };
  }

  function normalizeOcrMath(text) {
    let result = String(text || "")
      .replace(/\\\$/g, "$")
      .replace(/＄/g, "$");

    result = result.replace(/\$\$([\s\S]*?)\$\$/g, (match, body) => {
      const clean = cleanOcrFormulaBody(body);
      return clean.body ? `$$${clean.body}$$${clean.trailing}` : match;
    });

    return result.replace(
      /(?<!\$)\$(?!\$)([^$]*?)\$(?!\$)/g,
      (match, body) => {
        if (!/[\\^_{}=+*/<>-]/.test(body)) return match;
        const clean = cleanOcrFormulaBody(body);
        return clean.body ? `$${clean.body}$${clean.trailing}` : match;
      },
    );
  }

  function renderDraftMarkdown(text) {
    if (!text) return "";
    if (!marked) return `<pre class="fallback">${escapeHtml(text)}</pre>`;
    try {
      const result = protectMath(text);
      let html = marked.parse(result.text, {
        gfm: true,
        breaks: true,
        async: false,
      });
      html = sanitize(html);
      // 独立公式独占一行时去掉 marked 包的外层 <p>，改由按钮自己占块。
      html = html.replace(
        /<p>\s*(MCKATEXTOKEN(\d+)END)\s*<\/p>/g,
        (match, token, index) => {
          const item = result.store[Number(index)];
          return item && item.display ? token : match;
        },
      );
      return html.replace(/MCKATEXTOKEN(\d+)END/g, (match, index) => {
        const item = result.store[Number(index)];
        if (!item) return match;
        const cls = item.display
          ? "ocr-draft-formula is-display"
          : "ocr-draft-formula";
        const title = `点击修改第 ${Number(index) + 1} 个公式`;
        return `<button type="button" class="${cls}" data-formula-index="${index}"
          title="${title}" aria-label="${title}">${renderMath(item.body, item.display)}</button>`;
      });
    } catch (err) {
      console.error("题目卡渲染失败", err);
      return renderMarkdown(text);
    }
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

  // 步骤标题是单行文本，不能套 renderMarkdown 的 <p>；用 parseInline
  // 渲染其中的 $...$ 行内公式，标题里的 A_n、\sqrt n 才不会露出源码。
  function renderInlineMarkdown(text) {
    if (!text) return "";
    if (!marked) return escapeHtml(text);
    try {
      const result = protectMath(text);
      const html = marked.parseInline(result.text, {
        gfm: true,
        breaks: false,
        async: false,
      });
      return restoreMath(sanitize(html), result.store);
    } catch (err) {
      console.error("行内 Markdown 渲染失败", err);
      return escapeHtml(text);
    }
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

  const FINAL_MARKER_RE =
    /(?:\*\*|__)?\s*(?:最终\s*答案|答案|final\s*answer|answer)\s*(?:\*\*|__)?\s*(?:[:：]|是|为)/i;
  const DISPLAY_MATH_RE =
    /\$\$[\s\S]*?\$\$|\$[^$\n]*\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)/g;

  function answerDisplayCandidate(raw) {
    const text = String(raw || "").trim().replace(/^[*_`]+|[*_`]+$/g, "").trim();
    const spans = text.match(DISPLAY_MATH_RE);
    if (spans && spans.length) {
      // 主答案通常比尾部条件更长：$f^{(n)}(1)=ne$（$n\ge 1$）应取前者。
      return spans.reduce((best, span) => (span.length > best.length ? span : best));
    }
    return text
      .replace(
        /^(?:最终结论(?:不变)?|最终答案|答案|所以|因此|故)\s*(?:[:：]|是|为)\s*/,
        "",
      )
      .replace(/[。．.；;，,]+$/, "")
      .trim();
  }

  // 旧版后端只返回判分器用的 final_answer（已经去掉 $ 并做了符号归一化），
  // 卡片直接展示会露出 lim_ntoinfty 这类源码。这里从原始回答的最后一行
  // 重新取回带 $...$ 的公式；新版后端会直接给 final_answer_display。
  function extractAnswerDisplay(answer) {
    const lines = String(answer || "").split(/\r?\n/);
    for (let i = lines.length - 1; i >= 0; i -= 1) {
      const match = lines[i].match(FINAL_MARKER_RE);
      if (!match) continue;
      let candidate = lines[i].slice(match.index + match[0].length);
      if (!candidate.trim()) {
        candidate = lines.slice(i + 1).find((line) => line.trim()) || "";
      }
      const display = answerDisplayCandidate(candidate);
      if (display) return display;
    }
    for (let i = lines.length - 1; i >= 0; i -= 1) {
      if (!lines[i].trim()) continue;
      const spans = lines[i].match(DISPLAY_MATH_RE);
      if (spans && spans.length) {
        return spans.reduce((best, span) => (span.length > best.length ? span : best));
      }
      break;
    }
    return "";
  }

  function formatFinalAnswer(value) {
    const text = String(value)
      .replace(/\(\(([^()]+)\)\/\(([^()]+)\)\)/g, "($1)/($2)")
      .replace(/\*\*/g, "^")
      .replace(/(?<!\\)\bpi\b/g, "π");

    // 服务端提取答案时会把 \frac{a}{b} 归一化成 ((a)/(b)) 交给判分器。
    // 卡片是展示给用户看的，这里把整个答案就是简单分数的情况还原成排版公式。
    const fraction = text.match(/^\(?([^()/]+)\)?\s*\/\s*\(?([^()/]+)\)?$/);
    if (fraction && !text.includes("$") && !text.includes("=")) {
      return `$\\frac{${latexifySimple(fraction[1])}}{${latexifySimple(fraction[2])}}$`;
    }

    // 没有 $...$ 的纯数学答案也直接排版，例如 x=1/2、x_n、-3。
    const plainMath =
      !/[一-鿿]/.test(text) &&
      /[=+\-*/^_\\<>]|π/.test(text);
    if (plainMath && !text.includes("$") && !text.includes("\\(")) {
      return `$${text}$`;
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
    const content = document.createElement("div");
    content.className = "content";
    // 用户发出去的内容同样走 Markdown + KaTeX，避免聊天记录里出现 $...$。
    content.innerHTML = renderMarkdown(text);
    bubble.appendChild(content);
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

  const AGENT_STAGES = [
    { key: "planner", label: "Planner", desc: "规划" },
    { key: "solver", label: "Solver", desc: "解题" },
    { key: "verifier", label: "Verifier", desc: "复核" },
    { key: "revision", label: "Revision", desc: "重解" },
  ];

  function createAgentProgress() {
    const article = document.createElement("article");
    article.className = "message assistant";
    article.innerHTML = `
      <div class="avatar">∑</div>
      <div class="bubble">
        <div class="agent-progress">
          <div class="agent-progress-head">
            <span class="spinner"></span>
            <span class="agent-current" data-label="正在启动多 Agent">正在启动多 Agent…</span>
          </div>
          <div class="agent-steps">
            ${AGENT_STAGES.map((stage) => `
              <div class="agent-step" data-stage="${stage.key}">
                <div class="agent-step-name">
                  <span class="agent-step-dot"></span>${stage.label}
                </div>
                <div class="agent-step-desc">${stage.desc}</div>
              </div>`).join("")}
          </div>
          <div class="agent-progress-detail"></div>
        </div>
      </div>`;

    const started = performance.now();
    article._started = started;
    article._timer = setInterval(() => {
      const current = article.querySelector(".agent-current");
      if (!current) return;
      const label = current.dataset.label || "多 Agent";
      current.textContent = `${label} · ${((performance.now() - started) / 1000).toFixed(1)}s`;
    }, 100);
    return article;
  }

  function updateAgentProgress(article, event) {
    if (!article || event.type !== "stage") return;
    const current = article.querySelector(".agent-current");
    const detail = article.querySelector(".agent-progress-detail");
    const stage = event.stage;
    if (stage === "done") {
      if (current) current.textContent = "多 Agent 完成";
      return;
    }

    const node = article.querySelector(`[data-stage="${stage}"]`);
    if (node) {
      node.classList.remove("running", "done", "error");
      if (event.status === "running") node.classList.add("running");
      else if (event.status === "error" || event.status === "refused") {
        node.classList.add("error");
      } else {
        node.classList.add("done");
      }
      const desc = node.querySelector(".agent-step-desc");
      if (desc && event.detail) desc.textContent = event.detail;
    }

    if (current) {
      const labels = {
        planner: "Planner",
        solver: "Solver",
        verifier: "Verifier",
        revision: "Revision",
      };
      const statusText =
        event.status === "running" ? "进行中"
          : event.status === "done" ? "完成"
            : event.status;
      current.dataset.label = `${labels[stage] || stage} · ${statusText}`;
    }
    if (detail && event.detail) detail.textContent = event.detail;
  }

  function isProofQuestion(text) {
    return /证明|求证|试证/.test(String(text || ""));
  }

  function addFastActions(message, question, data) {
    const bubble = message.querySelector(".bubble");
    if (!bubble || bubble.querySelector(".fast-actions")) return;

    const actions = document.createElement("div");
    actions.className = "fast-actions";

    const badge = document.createElement("span");
    const usedTools = Number(data.tool_calls || 0) > 0;
    badge.className = `verification-badge ${usedTools ? "ok" : "warn"}`;
    badge.textContent = usedTools
      ? "✓ 已工具验证 · 未独立复核"
      : "! 未工具验证 · 未独立复核";
    actions.appendChild(badge);

    const upgrade = document.createElement("button");
    upgrade.type = "button";
    upgrade.className = "upgrade-btn";
    upgrade.textContent = "查看详细过程";
    upgrade.addEventListener("click", async () => {
      upgrade.disabled = true;
      upgrade.textContent = "详细过程生成中…";
      await submitMultiQuestion(question, {
        appendUser: false,
        after: message,
      });
      upgrade.textContent = "已生成详细过程";
    });
    actions.appendChild(upgrade);

    bubble.appendChild(actions);
  }

  async function submitMultiQuestion(question, options = {}) {
    const requestId = newRequestId();
    const controller = new AbortController();
    solveRequestId = requestId;
    solveAbort = controller;

    const addUserMessage = options.appendUser !== false;
    const after = options.after || null;

    clearOcrStatus();
    clearOcrDraft();
    setBusy(true);
    hideWelcome();
    if (addUserMessage) {
      appendUser(question);
      input.value = "";
      resizeInput();
    }

    const progress = createAgentProgress();
    if (after) {
      after.insertAdjacentElement("afterend", progress);
    } else {
      chat.appendChild(progress);
    }
    scrollToBottom();

    let finished = false;
    try {
      const response = await fetch("/api/ask-multi", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          question,
          session_id: sessionId,
          request_id: requestId,
        }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(errorText(data.detail, response.status));
      }
      if (!response.body) throw new Error("浏览器不支持流式响应。");

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let split;
        while ((split = buffer.indexOf("\n\n")) >= 0) {
          const chunk = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          for (const line of chunk.split("\n")) {
            if (!line.startsWith("data:")) continue;
            const raw = line.slice(5).trim();
            if (!raw) continue;
            let event;
            try {
              event = JSON.parse(raw);
            } catch (_) {
              continue;
            }

            if (event.type === "final") {
              clearInterval(progress._timer);
              finished = true;
              if (event.error) {
                progress.replaceWith(createErrorMessage(event.error));
                scrollToBottom();
                continue;
              }
              const data = { ...event };
              if (data.session_id) {
                sessionId = data.session_id;
                rememberSession(sessionId);
              }
              input.placeholder = "继续追问这道题，例如：这一步为什么？";
              const message = createAssistantMessage(data);
              const meta = message.querySelector(".meta");
              if (meta) {
                meta.textContent =
                  `多 Agent · 耗时 ${Number(event.elapsed || 0).toFixed(1)}s`
                  + ` · 重解 ${event.revisions || 0} 次`
                  + ` · 复核 ${event.verdict || "uncertain"}`;
              }
              if (event.warning) {
                const note = document.createElement("div");
                note.className = "notice-box";
                note.textContent = `复核提示：${event.warning}`;
                message.querySelector(".bubble").appendChild(note);
              }
              progress.replaceWith(message);
              scrollToElement(message);
            } else if (event.type === "error") {
              clearInterval(progress._timer);
              finished = true;
              progress.replaceWith(
                createErrorMessage(event.message || "多 Agent 求解失败。"),
              );
            } else {
              updateAgentProgress(progress, event);
            }
          }
        }
      }

      if (!finished) {
        clearInterval(progress._timer);
        progress.replaceWith(
          createNoticeMessage("多 Agent 连接结束，但没有收到最终答案。"),
        );
      }
    } catch (err) {
      clearInterval(progress._timer);
      if (controller.signal.aborted) {
        progress.replaceWith(createNoticeMessage("已停止生成。"));
      } else {
        progress.replaceWith(createErrorMessage(err.message || "网络请求失败。"));
      }
    } finally {
      if (solveAbort === controller) solveAbort = null;
      if (solveRequestId === requestId) solveRequestId = "";
      setBusy(false);
      input.focus();
    }
  }

  async function submitSingleQuestion(question, mode = "standard") {
    const requestId = newRequestId();
    const controller = new AbortController();
    solveRequestId = requestId;
    solveAbort = controller;

    clearOcrStatus();
    clearOcrDraft();
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
        signal: controller.signal,
        body: JSON.stringify({
          question,
          session_id: sessionId,
          request_id: requestId,
          ...(mode === "fast" ? { mode: "fast" } : {}),
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(errorText(data.detail, response.status));
      }
      sessionId = data.session_id || sessionId;
      rememberSession(sessionId);
      input.placeholder = "继续追问这道题，例如：这一步为什么？";
      clearInterval(pending._timer);
      const message = createAssistantMessage(data);
      if (mode === "fast") addFastActions(message, question, data);
      pending.replaceWith(message);
      scrollToElement(userMessage);
    } catch (err) {
      clearInterval(pending._timer);
      if (controller.signal.aborted) {
        pending.replaceWith(createNoticeMessage("已停止生成。"));
      } else {
        pending.replaceWith(createErrorMessage(err.message || "网络请求失败。"));
      }
      scrollToElement(userMessage);
    } finally {
      if (solveAbort === controller) solveAbort = null;
      if (solveRequestId === requestId) solveRequestId = "";
      setBusy(false);
      input.focus();
    }
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

  function stepKeyFor(data, stepIndex) {
    const parent = data.session_id || "no-session";
    const turn = data.turn_id || data.turn || "1";
    return `${parent}:${turn}:${stepIndex}`;
  }

  function createSolution(data) {
    const solution = data.solution;
    const box = document.createElement("div");
    box.className = "solution";

    if (solution.intro) {
      const intro = document.createElement("div");
      intro.className = "solution-intro";
      intro.innerHTML = renderMarkdown(solution.intro);
      box.appendChild(intro);
    }

    const note = document.createElement("div");
    note.className = "solution-note";
    note.textContent = "点击任意步骤右侧的「问这一步」，会在侧栏单独追问，不打断主对话。";
    box.appendChild(note);

    const list = document.createElement("div");
    list.className = "solution-steps";

    solution.steps.forEach((step) => {
      const card = document.createElement("article");
      card.className = "solution-step";

      const head = document.createElement("div");
      head.className = "solution-step-head";

      const label = document.createElement("span");
      label.className = "solution-step-label";
      label.textContent = step.label || `第${step.index}步`;
      head.appendChild(label);

      if (step.title) {
        const title = document.createElement("div");
        title.className = "solution-step-title";
        title.innerHTML = renderInlineMarkdown(step.title);
        head.appendChild(title);
      }

      const ask = document.createElement("button");
      ask.type = "button";
      ask.className = "step-ask";
      ask.textContent = "问这一步";
      ask.setAttribute("aria-label", `针对${step.label || `第${step.index}步`}提问`);
      ask.addEventListener("click", () => {
        lastStepButton = ask;
        openStepPanel(data, step);
      });
      head.appendChild(ask);
      card.appendChild(head);

      const body = document.createElement("div");
      body.className = "solution-step-body";
      body.innerHTML = renderMarkdown(step.markdown || "");
      card.appendChild(body);

      list.appendChild(card);
    });

    box.appendChild(list);

    if (solution.outro) {
      const outro = document.createElement("div");
      outro.className = "solution-outro";
      outro.innerHTML = renderMarkdown(solution.outro);
      box.appendChild(outro);
    }

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
    if (
      data.solution
      && Array.isArray(data.solution.steps)
      && data.solution.steps.length
    ) {
      content.appendChild(createSolution(data));
    } else {
      content.innerHTML = renderMarkdown(
        data.display_answer || data.answer || "(空回答)",
      );
    }

    if (data.final_answer) {
      const card = article.querySelector(".answer-card");
      card.hidden = false;
      const finalValue =
        data.final_answer_display ||
        extractAnswerDisplay(data.answer) ||
        data.final_answer;
      card.querySelector(".value").innerHTML = renderInline(
        formatFinalAnswer(finalValue),
      );
    }

    const related = createRelatedCard(data.related_problems);
    if (related) article.querySelector(".related-slot").appendChild(related);

    const meta = article.querySelector(".meta");
    if (data.restored) {
      meta.textContent = "已从本地会话恢复，可继续追问";
    } else {
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
    }

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

  function createNoticeMessage(message) {
    const article = document.createElement("article");
    article.className = "message assistant";
    article.innerHTML = `
      <div class="avatar">∑</div>
      <div class="bubble"><div class="notice-box"></div></div>`;
    article.querySelector(".notice-box").textContent = message;
    return article;
  }

  // ---------- 步骤追问侧栏 ----------

  function stepStateFor(data, step) {
    const key = stepKeyFor(data, step.index);
    let state = stepThreads.get(key);
    if (!state) {
      state = {
        key,
        parentSessionId: data.session_id || "",
        parentTurnId: data.turn_id || null,
        parentTurn: data.turn || null,
        stepIndex: step.index,
        stepLabel: step.label || `第${step.index}步`,
        stepTitle: step.title || "",
        stepMarkdown: step.markdown || "",
        threadSessionId: null,
        messages: [],
        busy: false,
        abort: null,
        requestId: "",
      };
      stepThreads.set(key, state);
    }
    return state;
  }

  function renderStepQuote(state) {
    stepQuote.replaceChildren();
    if (!state.stepMarkdown) {
      stepQuote.classList.add("is-empty");
      return;
    }
    stepQuote.classList.remove("is-empty");

    const label = document.createElement("div");
    label.className = "step-quote-label";
    label.textContent = "引用步骤";
    stepQuote.appendChild(label);

    const title = document.createElement("div");
    title.className = "step-quote-title";
    title.innerHTML = state.stepTitle
      ? `${escapeHtml(state.stepLabel)} · ${renderInlineMarkdown(state.stepTitle)}`
      : escapeHtml(state.stepLabel);
    stepQuote.appendChild(title);

    const body = document.createElement("div");
    body.className = "step-quote-body";
    body.innerHTML = renderMarkdown(state.stepMarkdown);
    stepQuote.appendChild(body);
  }

  function renderStepMessages(state) {
    stepThread.replaceChildren();

    if (!state.messages.length && !state.busy) {
      const empty = document.createElement("div");
      empty.className = "step-empty";

      const mark = document.createElement("div");
      mark.className = "step-empty-mark";
      mark.textContent = "?";
      empty.appendChild(mark);

      const text = document.createElement("div");
      text.textContent = "这里只会讨论当前这一步，不会占用主对话。可以问“为什么想到这一步”“这个等号怎么来的”等。";
      empty.appendChild(text);
      stepThread.appendChild(empty);
      return;
    }

    state.messages.forEach((message) => {
      const item = document.createElement("div");
      if (message.role === "user") {
        item.className = "step-msg user";
        item.textContent = message.text;
      } else {
        item.className = "step-msg assistant";
        if (message.error) item.classList.add("error");
        if (message.error) {
          item.textContent = message.text;
        } else {
          const content = document.createElement("div");
          content.className = "content";
          content.innerHTML = renderMarkdown(message.text);
          item.appendChild(content);
        }
      }
      stepThread.appendChild(item);
    });

    if (state.busy) {
      const pending = document.createElement("div");
      pending.className = "step-pending";
      pending.innerHTML = '<span class="spinner"></span><span>正在解释这一步…</span>';
      stepThread.appendChild(pending);
    }

    requestAnimationFrame(() => {
      stepThread.scrollTop = stepThread.scrollHeight;
    });
  }

  function renderStepPanel(state) {
    stepTitle.innerHTML = state.stepTitle
      ? `${escapeHtml(state.stepLabel)} · ${renderInlineMarkdown(state.stepTitle)}`
      : escapeHtml(state.stepLabel);
    renderStepQuote(state);
    renderStepMessages(state);
    stepInput.disabled = state.busy;
    stepSend.disabled = state.busy;
    if (stepStop) stepStop.hidden = !state.busy;
    if (stepNewChat) stepNewChat.disabled = state.busy;
    stepInput.placeholder = state.busy
      ? "正在回答…"
      : `针对${state.stepLabel}提问，例如：这里为什么成立？`;
  }

  function openStepPanel(data, step) {
    const state = stepStateFor(data, step);
    activeStepKey = state.key;
    renderStepPanel(state);

    stepBackdrop.hidden = false;
    stepPanel.hidden = false;
    document.body.classList.add("step-panel-open");
    requestAnimationFrame(() => stepInput.focus());
  }

  function closeStepPanel(restoreFocus = true) {
    if (!stepPanel || stepPanel.hidden) return;

    stepPanel.hidden = true;
    stepBackdrop.hidden = true;
    document.body.classList.remove("step-panel-open");
    activeStepKey = null;

    if (restoreFocus && lastStepButton && lastStepButton.isConnected) {
      lastStepButton.focus();
    }
    lastStepButton = null;
  }

  function resizeStepInput() {
    stepInput.style.height = "auto";
    stepInput.style.height = `${Math.min(stepInput.scrollHeight, 120)}px`;
  }

  // 只重置当前这一步的追问会话，不关闭侧栏，也不影响主对话。
  function resetStepConversation() {
    const state = activeStepKey ? stepThreads.get(activeStepKey) : null;
    if (!state || state.busy) return;

    state.messages = [];
    state.threadSessionId = null;
    stepInput.value = "";
    resizeStepInput();
    renderStepPanel(state);
    stepInput.focus();
  }

  async function submitStepQuestion() {
    const state = activeStepKey ? stepThreads.get(activeStepKey) : null;
    const question = stepInput.value.trim();
    if (!state || !question || state.busy) return;

    const requestId = newRequestId();
    const controller = new AbortController();
    state.busy = true;
    state.abort = controller;
    state.requestId = requestId;
    state.messages.push({ role: "user", text: question });
    stepInput.value = "";
    resizeStepInput();
    renderStepPanel(state);

    try {
      const response = await fetch("/api/ask-step", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          question,
          parent_session_id: state.parentSessionId,
          parent_turn_id: state.parentTurnId,
          parent_turn: state.parentTurn,
          step_index: state.stepIndex,
          step_text: state.stepMarkdown,
          step_session_id: state.threadSessionId,
          request_id: requestId,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(errorText(data.detail, response.status));
      }

      state.threadSessionId = data.step_session_id || state.threadSessionId;
      state.messages.push({
        role: "assistant",
        text: data.display_answer || data.answer || "(空回答)",
        error: Boolean(data.error),
      });
    } catch (err) {
      if (controller.signal.aborted) {
        state.messages.push({
          role: "assistant",
          text: "已停止生成。",
          error: false,
        });
      } else {
        state.messages.push({
          role: "assistant",
          text: err.message || "网络请求失败。",
          error: true,
        });
      }
    } finally {
      if (state.abort === controller) state.abort = null;
      if (state.requestId === requestId) state.requestId = "";
      state.busy = false;
      if (activeStepKey === state.key) {
        renderStepPanel(state);
        if (!stepPanel.hidden) stepInput.focus();
      }
    }
  }

  function stopStepQuestion() {
    const state = activeStepKey ? stepThreads.get(activeStepKey) : null;
    if (!state || !state.busy) return;

    const requestId = state.requestId;
    if (state.abort) state.abort.abort();
    requestCancel(requestId);
  }

  // ---------- 真题浏览 ----------

  function setMode(next) {
    if (next !== "solve" && next !== "papers") return;
    if (next === mode) return;

    closeStepPanel(false);
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

    closeStepPanel(false);
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
    sub.textContent = `共 ${Number(data.count || 0)} 道题 · 题干来自题库，公式按本地缓存/整理稿排版`;
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

        // 官方答案默认收起：先让用户有机会自己思考，需要时再展开。
        // 没有收录答案的题也保留按钮，明确告诉用户“暂无”，而不是静默消失。
        const answerPanel = document.createElement("div");
        answerPanel.className = "paper-answer";
        answerPanel.hidden = true;

        const answer = String(problem.answer || "").trim();
        const solution = String(problem.solution || "").trim();
        if (answer || solution) {
          if (answer) {
            const label = document.createElement("p");
            label.className = "paper-answer-label";
            label.textContent = "官方答案";
            answerPanel.appendChild(label);

            const value = document.createElement("div");
            value.className = "paper-answer-value rendered";
            value.innerHTML = renderMarkdown(answer);
            answerPanel.appendChild(value);
          }
          if (solution) {
            const label = document.createElement("p");
            label.className = "paper-answer-label";
            label.textContent = "参考解答";
            answerPanel.appendChild(label);

            const body = document.createElement("div");
            body.className = "paper-answer-solution rendered";
            body.innerHTML = renderMarkdown(solution);
            answerPanel.appendChild(body);
          }
        } else {
          const empty = document.createElement("p");
          empty.className = "paper-answer-empty";
          empty.textContent =
            "暂无官方答案：当前题库还没有收录这道题的官方参考解答。";
          answerPanel.appendChild(empty);
        }

        const answerToggle = document.createElement("button");
        answerToggle.type = "button";
        answerToggle.className = "paper-answer-toggle";
        answerToggle.setAttribute("aria-expanded", "false");
        answerToggle.textContent = "显示答案";

        answerToggle.addEventListener("click", () => {
          const opening = answerPanel.hidden;
          answerPanel.hidden = !opening;
          answerToggle.textContent = opening ? "收起答案" : "显示答案";
          answerToggle.setAttribute("aria-expanded", String(opening));
        });

        item.appendChild(answerToggle);
        item.appendChild(answerPanel);

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
    // 图片识别的整页结果可能有好几道题，编辑区允许比普通追问更高
    input.style.height = `${Math.min(input.scrollHeight, 300)}px`;
  }

  function updateControls() {
    // 识别/二次校对期间不允许发送，避免旧题目晚到后插进已经发出的问题里
    sendBtn.disabled = busy || ocrBusy || ocrFixBusy;
    newChatBtn.disabled = busy;
    input.disabled = busy;
    if (ocrBtn) ocrBtn.disabled = !ocrAvailable || busy || ocrBusy || ocrFixBusy;
    if (homeBtn) homeBtn.disabled = busy;
    if (ocrSourceToggle) {
      ocrSourceToggle.disabled = busy || ocrBusy || ocrFixBusy || !ocrDraft;
    }
    if (ocrRetry) ocrRetry.disabled = busy || ocrBusy || ocrFixBusy || !ocrDraft;
    if (ocrCorrectInput) {
      ocrCorrectInput.disabled = busy || ocrBusy || ocrFixBusy;
    }
    if (ocrCorrectSubmit) {
      ocrCorrectSubmit.disabled = busy || ocrBusy || ocrFixBusy;
      ocrCorrectSubmit.textContent = ocrFixBusy ? "修正中…" : "AI 修正";
    }
    sendBtn.querySelector(".send-label").textContent = busy ? "求解中" : "发送";
    if (stopBtn) stopBtn.hidden = !busy;
  }

  function setBusy(next) {
    busy = next;
    updateControls();
  }

  function stopSolve() {
    if (!busy) return;
    const requestId = solveRequestId;
    if (solveAbort) solveAbort.abort();
    requestCancel(requestId);
  }

  function startNewChat() {
    if (busy) return;
    // 识别还没回来就开新对话时，旧结果不能插进新输入框
    if (ocrBusy) cancelOcr();

    // 新对话始终回到“问题目”，避免在真题页清空聊天后看不见结果
    setMode("solve");

    closeStepPanel(false);
    stepThreads.clear();
    lastStepButton = null;

    const oldSession = sessionId;
    sessionId = null;
    forgetSession();
    chat.querySelectorAll(".message").forEach((node) => node.remove());
    showWelcome();

    closeFormulaEditor(false);
    input.value = "";
    if (ocrFile) ocrFile.value = "";
    clearOcrStatus();
    clearOcrDraft();
    resetInputPlaceholder();
    resizeInput();
    chat.scrollTo({ top: 0, behavior: "smooth" });
    input.focus();

    if (oldSession) {
      fetch(`/api/session/${oldSession}`, { method: "DELETE" }).catch(() => {
        // 删除失败不影响本地开新对话，旧会话会按 TTL 自行过期
      });
    }
  }

  // 刷新页面后恢复上一轮会话。服务端只存最终问答，因此不显示轨迹，
  // 但最终答案卡、分步结构和“问这一步”仍然可用。
  async function restoreSession() {
    if (mode !== null || busy) return;
    const saved = savedSessionId();
    if (!saved) return;

    try {
      const response = await fetch(`/api/session/${encodeURIComponent(saved)}`);
      if (response.status === 404) {
        forgetSession();
        return;
      }
      if (!response.ok) return;

      const data = await response.json();
      // 等待请求期间用户可能已经点了模式选择，此时不要抢走界面。
      if (mode !== null || busy) return;
      const turns = Array.isArray(data.turns) ? data.turns : [];
      if (!turns.length) {
        forgetSession();
        return;
      }

      sessionId = data.session_id || saved;
      rememberSession(sessionId);
      setMode("solve");
      chat.querySelectorAll(".message").forEach((node) => node.remove());
      hideWelcome();

      turns.forEach((turn) => {
        appendUser(turn.question || "");
        chat.appendChild(createAssistantMessage({
          answer: turn.answer || "",
          display_answer: turn.display_answer || turn.answer || "",
          session_id: sessionId,
          turn_id: turn.id || "",
          turn: turn.turn || 1,
          follow_up: Boolean(turn.follow_up),
          final_answer: turn.final_answer || null,
          final_answer_display: turn.final_answer_display || null,
          solution: turn.solution || null,
          restored: true,
        }));
      });

      input.placeholder = "继续追问这道题，例如：这一步为什么？";
      resizeInput();
      chat.scrollTop = chat.scrollHeight;
    } catch (err) {
      // 服务未连接时保持欢迎页，下一次发送仍会走正常的错误提示。
    }
  }

  function errorText(detail, statusCode) {
    if (Array.isArray(detail)) {
      return detail.map((item) => item.msg || "参数不合法").join("；");
    }
    return detail || `请求失败（HTTP ${statusCode}）`;
  }

  // ---------- 图片识题 ----------

  function clearOcrStatus() {
    if (ocrStatusTimer) {
      clearTimeout(ocrStatusTimer);
      ocrStatusTimer = null;
    }
    if (!ocrStatus) return;
    ocrStatus.hidden = true;
    ocrStatus.className = "ocr-status";
    if (ocrSpinner) ocrSpinner.hidden = true;
    if (ocrStatusText) ocrStatusText.textContent = "";
    if (ocrStatusClose) ocrStatusClose.hidden = true;
  }

  function showOcrStatus(message, kind) {
    if (!ocrStatus) return;
    if (ocrStatusTimer) {
      clearTimeout(ocrStatusTimer);
      ocrStatusTimer = null;
    }
    ocrStatus.hidden = false;
    ocrStatus.className = `ocr-status ${kind}`;
    if (ocrStatusText) ocrStatusText.textContent = message;
    if (ocrSpinner) ocrSpinner.hidden = kind !== "loading";
    // 识别中按钮变成“取消”，成功提示过一会儿自动收起，错误保留给用户看
    if (ocrStatusClose) {
      const loadingLabel = ocrFixBusy ? "取消修正" : "取消识别";
      ocrStatusClose.hidden = false;
      ocrStatusClose.title = kind === "loading" ? loadingLabel : "关闭提示";
      ocrStatusClose.setAttribute(
        "aria-label",
        kind === "loading" ? loadingLabel : "关闭识别提示",
      );
    }
    if (kind === "ok") {
      ocrStatusTimer = setTimeout(clearOcrStatus, 12000);
    }
  }

  function clearOcrImage() {
    if (ocrImage) {
      ocrImage.hidden = true;
      ocrImage.removeAttribute("src");
    }
    if (ocrImageUrl) {
      URL.revokeObjectURL(ocrImageUrl);
      ocrImageUrl = null;
    }
    if (ocrPreview) ocrPreview.classList.add("no-image");
  }

  function resetInputPlaceholder() {
    const followUp = "继续追问这道题，例如：这一步为什么？";
    input.placeholder = ocrDraft
      ? DRAFT_PLACEHOLDER
      : (sessionId ? followUp : DEFAULT_PLACEHOLDER);
    input.setAttribute("aria-label", ocrDraft ? "补充说明" : "题目");
  }

  function hideOcrPreview() {
    if (!ocrPreview) return;
    ocrPreview.hidden = true;
    ocrSourceOpen = false;
    if (ocrPreviewBody) {
      ocrPreviewBody.hidden = false;
      ocrPreviewBody.innerHTML = "";
    }
    if (ocrSourceEditor) ocrSourceEditor.hidden = true;
    if (ocrCorrectForm) ocrCorrectForm.hidden = true;
    if (ocrSourceToggle) ocrSourceToggle.textContent = "编辑源码";
    if (ocrPreviewClose) {
      ocrPreviewClose.title = "清除识别结果";
      ocrPreviewClose.setAttribute("aria-label", "清除识别结果");
    }
    clearOcrImage();
  }

  function clearOcrDraft() {
    // 让在途的二次校对结果失效，避免清空后又被写回来
    ocrFixRequestId += 1;
    if (ocrFixAbort) {
      ocrFixAbort.abort();
      ocrFixAbort = null;
    }
    ocrFixBusy = false;
    if (ocrCorrectInput) ocrCorrectInput.value = "";
    ocrDraft = "";
    resetInputPlaceholder();
    updateControls();
    hideOcrPreview();
  }

  function cancelOcr() {
    // 让在途响应失效；服务端可能还在跑，但结果不会再写回题目卡
    ocrRequestId += 1;
    if (ocrAbort) {
      ocrAbort.abort();
      ocrAbort = null;
    }
    ocrBusy = false;
    updateControls();
    clearOcrStatus();
    // 重新识别失败/取消时，保留上一版题目卡；新图片已经清掉，避免图文对不上。
    clearOcrImage();
    if (ocrDraft) {
      showOcrPreview();
    } else {
      hideOcrPreview();
    }
  }

  function cancelOcrFix() {
    ocrFixRequestId += 1;
    if (ocrFixAbort) {
      ocrFixAbort.abort();
      ocrFixAbort = null;
    }
    ocrFixBusy = false;
    updateControls();
    clearOcrStatus();
    // 取消只丢掉这一轮校对，上一版题目卡继续保留。
    if (ocrDraft) showOcrPreview();
    else hideOcrPreview();
  }

  function renderOcrPreview() {
    if (!ocrPreview || !ocrPreviewBody || !ocrDraft || ocrSourceOpen) return;
    ocrPreviewBody.hidden = false;
    ocrPreviewBody.innerHTML = renderDraftMarkdown(ocrDraft);
    updateControls();
  }

  function showOcrPreview() {
    if (!ocrPreview) return;
    if (!ocrDraft) {
      hideOcrPreview();
      return;
    }
    ocrPreview.hidden = false;
    ocrSourceOpen = false;
    if (ocrSourceEditor) ocrSourceEditor.hidden = true;
    if (ocrCorrectForm) ocrCorrectForm.hidden = false;
    if (ocrSourceToggle) ocrSourceToggle.textContent = "编辑源码";
    if (ocrPreviewClose) {
      ocrPreviewClose.title = "清除识别结果";
      ocrPreviewClose.setAttribute("aria-label", "清除识别结果");
    }
    renderOcrPreview();
    updateControls();
  }

  function showOcrPreviewLoading() {
    if (!ocrPreview || !ocrPreviewBody) return;
    ocrPreview.hidden = false;
    ocrSourceOpen = false;
    if (ocrSourceEditor) ocrSourceEditor.hidden = true;
    if (ocrCorrectForm) ocrCorrectForm.hidden = true;
    if (ocrSourceToggle) ocrSourceToggle.textContent = "编辑源码";
    if (ocrPreviewClose) {
      ocrPreviewClose.title = "取消识别";
      ocrPreviewClose.setAttribute("aria-label", "取消识别");
    }
    ocrPreviewBody.hidden = false;
    ocrPreviewBody.innerHTML =
      '<p class="ocr-preview-wait">正在识别，完成后会生成可编辑的题目卡。</p>';
    updateControls();
  }

  async function imageLooksLikePage() {
    if (!ocrImage) return false;
    try {
      await ocrImage.decode();
    } catch {
      return false;
    }
    const width = ocrImage.naturalWidth;
    const height = ocrImage.naturalHeight;
    return (
      width > 0 &&
      height > 0 &&
      width * height > OCR_PAGE_AREA &&
      height > width * 1.05
    );
  }

  function setOcrDraft(text, options = {}) {
    const focusInput = options.focusInput !== false;
    const limit = Number(input.maxLength) > 0 ? Number(input.maxLength) : 5000;
    let next = normalizeBareLatex(
      normalizeOcrMath(String(text || "")),
    ).trim();
    let truncated = false;
    if (next.length > limit) {
      next = next.slice(0, limit);
      truncated = true;
    }

    ocrDraft = next;
    ocrSourceOpen = false;
    if (ocrSourceEditor) ocrSourceEditor.hidden = true;
    if (ocrPreviewBody) ocrPreviewBody.hidden = false;
    if (ocrSourceToggle) ocrSourceToggle.textContent = "编辑源码";
    resetInputPlaceholder();
    updateControls();

    if (!ocrDraft) {
      hideOcrPreview();
      return truncated;
    }

    showOcrPreview();
    if (focusInput) input.focus();
    return truncated;
  }

  function renderOcrFormulaMap() {
    if (!ocrFormulaMap) return;
    ocrFormulaMap.replaceChildren();
    const formulas = collectDraftMath(ocrDraft);
    if (!formulas.length) {
      const empty = document.createElement("p");
      empty.className = "ocr-formula-map-empty";
      empty.textContent = "没有检测到公式，可能整段都是普通文字。";
      ocrFormulaMap.appendChild(empty);
      return;
    }
    formulas.forEach((formula, index) => {
      const row = document.createElement("div");
      row.className = "ocr-map-row";

      const label = document.createElement("span");
      label.className = "ocr-map-label";
      label.textContent = `公式 ${index + 1}`;

      const preview = document.createElement("div");
      preview.className = "ocr-map-preview";
      preview.innerHTML = renderMath(formula.body, false);

      const code = document.createElement("code");
      code.className = "ocr-map-code";
      code.textContent = `${formula.open}${formula.body}${formula.close}`;

      row.append(label, preview, code);
      ocrFormulaMap.appendChild(row);
    });
  }

  function openOcrSource() {
    if (!ocrDraft || !ocrSource || !ocrSourceEditor) return;
    ocrSourceOpen = true;
    ocrSource.value = ocrDraft;
    renderOcrFormulaMap();
    ocrSourceEditor.hidden = false;
    if (ocrPreviewBody) ocrPreviewBody.hidden = true;
    if (ocrSourceToggle) ocrSourceToggle.textContent = "取消编辑";
    ocrSource.focus();
    ocrSource.setSelectionRange(ocrSource.value.length, ocrSource.value.length);
  }

  function closeOcrSource(restoreFocus = true) {
    if (!ocrSourceOpen) return;
    ocrSourceOpen = false;
    if (ocrSourceEditor) ocrSourceEditor.hidden = true;
    if (ocrPreviewBody) ocrPreviewBody.hidden = false;
    if (ocrSourceToggle) {
      ocrSourceToggle.textContent = "编辑源码";
      if (restoreFocus) ocrSourceToggle.focus();
    }
    renderOcrPreview();
  }

  function saveOcrSource() {
    if (!ocrSource) return;
    const next = ocrSource.value.trim();
    if (!next) {
      showOcrStatus("题目内容不能为空；要清除整张题目卡请点右上角 ×。", "error");
      return;
    }
    const limit = Number(input.maxLength) > 0 ? Number(input.maxLength) : 5000;
    if (next.length > limit) {
      showOcrStatus(`题目内容超过 ${limit} 字，请先删减后再保存。`, "error");
      return;
    }
    ocrSourceOpen = false;
    setOcrDraft(next);
    showOcrStatus("题目卡已更新，公式已重新排版。", "ok");
  }

  // 用户用大白话指出识别错误，再让模型带着原题校对一次。
  // 这一步只返回修正后的题干，不会开始解题。
  async function submitOcrCorrection() {
    if (!ocrCorrectInput) return;
    const instruction = ocrCorrectInput.value.trim();
    if (!instruction || !ocrDraft || busy || ocrBusy || ocrFixBusy) return;

    const draftAtStart = ocrDraft;
    const requestId = ++ocrFixRequestId;
    ocrFixBusy = true;
    ocrFixAbort = new AbortController();
    updateControls();
    showOcrStatus("正在根据你的说明修正题目…", "loading");

    try {
      const response = await fetch("/api/ocr/fix", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: draftAtStart, instruction }),
        signal: ocrFixAbort.signal,
      });
      const data = await response.json().catch(() => ({}));
      if (requestId !== ocrFixRequestId) return;
      if (!response.ok) {
        throw new Error(errorText(data.detail, response.status));
      }

      const text = String(data.text || "").trim();
      if (!text) {
        throw new Error("模型没有返回修正后的题目，请换个说法再试。");
      }

      const truncated = Boolean(data.truncated) || setOcrDraft(text);
      if (ocrCorrectInput) ocrCorrectInput.value = "";
      showOcrStatus(
        truncated
          ? "修正结果超过 5000 字，已截断；请用源码模式删减后再发送。"
          : "已按你的说明修正，公式已重新排版，请再核对一次。",
        truncated ? "error" : "ok",
      );
    } catch (err) {
      if (requestId !== ocrFixRequestId || err.name === "AbortError") return;
      showOcrStatus(err.message || "修正失败，请稍后重试。", "error");
    } finally {
      if (requestId === ocrFixRequestId) {
        ocrFixBusy = false;
        ocrFixAbort = null;
        updateControls();
      }
    }
  }

  function hydrateGuideMath() {
    document.querySelectorAll(".guide-math[data-tex]").forEach((node) => {
      node.innerHTML = renderMath(node.dataset.tex || "", false);
    });
  }

  // ---------- 公式级编辑 ----------

  const FORMULA_SYMBOLS = [
    { label: "分数", insert: "\\frac{}{}" },
    { label: "根号", insert: "\\sqrt{}" },
    { label: "n 次根", insert: "\\sqrt[n]{}" },
    { label: "上标", insert: "^{}" },
    { label: "下标", insert: "_{}" },
    { label: "积分", insert: "\\int_{}^{}" },
    { label: "求和", insert: "\\sum_{}^{}" },
    { label: "极限", insert: "\\lim_{x \\to }" },
    { label: "括号", insert: "\\left( {} \\right)" },
    { label: "≤", insert: "\\le " },
    { label: "≥", insert: "\\ge " },
    { label: "≠", insert: "\\ne " },
    { label: "π", insert: "\\pi " },
    { label: "∞", insert: "\\infty " },
    { label: "×", insert: "\\times " },
    { label: "·", insert: "\\cdot " },
  ];

  function buildFormulaSymbols() {
    if (!formulaSymbols) return;
    FORMULA_SYMBOLS.forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "formula-symbol-btn";
      button.textContent = item.label;
      button.title = `插入 ${item.insert}`;
      button.setAttribute("aria-label", `插入${item.label}`);
      button.addEventListener("click", () => insertFormulaSnippet(item.insert));
      formulaSymbols.appendChild(button);
    });
  }

  function insertFormulaSnippet(snippet) {
    if (!formulaInput) return;
    const value = formulaInput.value;
    const start = formulaInput.selectionStart ?? value.length;
    const end = formulaInput.selectionEnd ?? start;
    formulaInput.setRangeText(snippet, start, end, "end");
    // 把光标放进第一个空花括号，用户接着填内容即可。
    const brace = snippet.indexOf("{}");
    const cursor = start + (brace >= 0 ? brace + 1 : snippet.length);
    formulaInput.setSelectionRange(cursor, cursor);
    formulaInput.focus();
    updateFormulaPreview();
  }

  function updateFormulaPreview() {
    if (!formulaPreview || !formulaInput) return;
    const body = formulaInput.value.trim();
    if (!body) {
      formulaPreview.classList.add("is-empty");
      formulaPreview.textContent = "公式为空；保存后会从题目中删除这个公式。";
      return;
    }
    formulaPreview.classList.remove("is-empty");
    formulaPreview.innerHTML = renderMath(body, true);
  }

  function openFormulaEditor(index) {
    if (!formulaPanel || !formulaInput) return;
    const formula = collectDraftMath(ocrDraft)[index];
    if (!formula) return;

    formulaEditIndex = index;
    formulaLastFocus = document.activeElement;
    formulaInput.value = formula.body;
    updateFormulaPreview();
    if (formulaBackdrop) formulaBackdrop.hidden = false;
    formulaPanel.hidden = false;
    document.body.classList.add("formula-panel-open");
    requestAnimationFrame(() => {
      formulaInput.focus();
      formulaInput.setSelectionRange(formulaInput.value.length, formulaInput.value.length);
    });
  }

  function closeFormulaEditor(restoreFocus = true) {
    if (!formulaPanel || formulaPanel.hidden) return;
    formulaPanel.hidden = true;
    if (formulaBackdrop) formulaBackdrop.hidden = true;
    document.body.classList.remove("formula-panel-open");
    formulaEditIndex = -1;
    if (restoreFocus && formulaLastFocus && formulaLastFocus.isConnected) {
      formulaLastFocus.focus();
    }
    formulaLastFocus = null;
  }

  function saveFormula() {
    if (!formulaInput) return;
    const formula = collectDraftMath(ocrDraft)[formulaEditIndex];
    if (!formula) {
      closeFormulaEditor();
      return;
    }

    const body = formulaInput.value.trim();
    let next;
    if (!body) {
      // 保存空内容视为删除这个公式，而不是留下一个空公式。
      next = `${ocrDraft.slice(0, formula.start)}${ocrDraft.slice(formula.end)}`
        .replace(/\n{3,}/g, "\n\n")
        .trim();
    } else {
      next = ocrDraft.slice(0, formula.start)
        + formula.open
        + body
        + formula.close
        + ocrDraft.slice(formula.end);
    }

    if (!next) {
      showOcrStatus("题目内容不能为空；要清除整张题目卡请点右上角 ×。", "error");
      return;
    }
    const limit = Number(input.maxLength) > 0 ? Number(input.maxLength) : 5000;
    if (next.length > limit) {
      showOcrStatus(`题目内容超过 ${limit} 字，请先删减后再保存。`, "error");
      return;
    }

    const scrollTop = ocrPreviewBody ? ocrPreviewBody.scrollTop : 0;
    const editedIndex = formulaEditIndex;
    closeFormulaEditor(false);
    setOcrDraft(next, { focusInput: false });
    if (ocrPreviewBody) ocrPreviewBody.scrollTop = scrollTop;
    // 保存后把焦点放回同一个公式；公式被删掉时回到输入框。
    const buttons = ocrPreviewBody
      ? ocrPreviewBody.querySelectorAll(".ocr-draft-formula")
      : [];
    if (buttons[editedIndex]) {
      buttons[editedIndex].focus();
    } else {
      input.focus();
    }
  }

  async function recognizeImage(file) {
    if (!file || busy || ocrBusy || ocrFixBusy) return;

    if (file.type && !/^image\/(png|jpe?g|bmp)$/i.test(file.type)) {
      showOcrStatus("只支持 PNG / JPG / BMP 图片，请换一种格式。", "error");
      return;
    }
    if (ocrProvider !== "simpletex" && /^image\/bmp$/i.test(file.type || "")) {
      showOcrStatus("当前识题方式只支持 PNG / JPG 图片，请换一种格式。", "error");
      return;
    }
    if (file.size > MAX_OCR_BYTES) {
      showOcrStatus("图片超过 10 MB，请压缩或裁剪后再上传。", "error");
      return;
    }

    // 先显示原图，识别完成后和文本并排校对
    if (ocrCorrectInput) ocrCorrectInput.value = "";
    clearOcrImage();
    ocrImageUrl = URL.createObjectURL(file);
    if (ocrImage) {
      ocrImage.src = ocrImageUrl;
      ocrImage.hidden = false;
    }
    if (ocrPreview) ocrPreview.classList.remove("no-image");

    const requestId = ++ocrRequestId;
    ocrBusy = true;
    updateControls();
    showOcrPreviewLoading();
    showOcrStatus(
      ocrProvider === "ollama"
        ? "正在用本地模型识别（首次加载会慢一些）…"
        : "正在识别图片（整页识别可能需要十几秒）…",
      "loading",
    );

    const isPage = await imageLooksLikePage();
    if (requestId !== ocrRequestId) return;
    if (isPage) {
      showOcrStatus(
        "整页图片识别较慢，小根号/上标容易错；建议先裁到单题。",
        "loading",
      );
    }

    const body = new FormData();
    body.append("file", file, file.name || "problem.png");
    ocrAbort = new AbortController();

    try {
      const response = await fetch("/api/ocr", {
        method: "POST",
        body,
        signal: ocrAbort.signal,
      });
      const data = await response.json().catch(() => ({}));
      if (requestId !== ocrRequestId) return;
      if (!response.ok) {
        throw new Error(errorText(data.detail, response.status));
      }

      const text = String(data.text || "").trim();
      if (!text) {
        throw new Error("没有识别到文字，请换一张更清晰、方向端正的图片。");
      }

      const truncated = setOcrDraft(text);
      const confidence = Number(data.confidence);
      const confidenceText = Number.isFinite(confidence) && confidence > 0
        ? `（置信度 ${Math.round(confidence * 100)}%）`
        : "";
      const reviewHint = isPage
        ? "整页图请重点核对根号指数、上标和阶乘范围。"
        : "核对公式后直接发送。";
      showOcrStatus(
        truncated
          ? "识别结果超过 5000 字，已截断；请点“编辑源码”删掉不需要的题目后再发送。"
          : `已识别${confidenceText}并生成题目卡；${reviewHint}`,
        truncated ? "error" : "ok",
      );
    } catch (err) {
      if (requestId !== ocrRequestId || err.name === "AbortError") return;
      // 重新识别失败时保留上一版题目卡，并清掉新图片，避免图文对不上。
      clearOcrImage();
      if (ocrDraft) {
        showOcrPreview();
      } else {
        hideOcrPreview();
      }
      showOcrStatus(err.message || "图片识别失败，请稍后重试。", "error");
    } finally {
      if (requestId === ocrRequestId) {
        ocrBusy = false;
        ocrAbort = null;
        updateControls();
      }
    }
  }

  async function submitQuestion() {
    const supplement = input.value.trim();
    // OCR 结果和输入框里的补充说明一起发送；没有图片时行为与以前一致。
    const question = [ocrDraft.trim(), supplement]
      .filter(Boolean)
      .join("\n\n");
    if (!question || busy || ocrBusy || ocrFixBusy) return;

    // 默认先给快速答案；证明题自动转多 Agent，其他题可手动升级详细过程。
    if (isProofQuestion(question)) {
      await submitMultiQuestion(question);
    } else {
      await submitSingleQuestion(question, "fast");
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitQuestion();
  });

  if (stopBtn) {
    stopBtn.addEventListener("click", stopSolve);
  }

  if (stepStop) {
    stepStop.addEventListener("click", stopStepQuestion);
  }

  if (stepForm) {
    stepForm.addEventListener("submit", (event) => {
      event.preventDefault();
      submitStepQuestion();
    });
  }

  if (stepInput) {
    stepInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault();
        submitStepQuestion();
      }
    });
    stepInput.addEventListener("input", resizeStepInput);
  }

  if (stepClose) {
    stepClose.addEventListener("click", () => closeStepPanel());
  }

  if (stepNewChat) {
    stepNewChat.addEventListener("click", resetStepConversation);
  }

  if (stepBackdrop) {
    stepBackdrop.addEventListener("click", () => closeStepPanel());
  }

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (formulaPanel && !formulaPanel.hidden) {
      closeFormulaEditor();
      return;
    }
    if (stepPanel && !stepPanel.hidden) {
      closeStepPanel();
    }
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

  if (ocrBtn && ocrFile) {
    ocrBtn.addEventListener("click", () => {
      if (!busy && !ocrBusy && !ocrFixBusy) ocrFile.click();
    });
    ocrFile.addEventListener("change", () => {
      const file = ocrFile.files && ocrFile.files[0];
      // 清空 value，保证同一张图片再选一次也会触发 change
      ocrFile.value = "";
      recognizeImage(file);
    });
  }

  if (ocrStatusClose) {
    ocrStatusClose.addEventListener("click", () => {
      if (ocrBusy) {
        cancelOcr();
      } else if (ocrFixBusy) {
        cancelOcrFix();
      } else {
        clearOcrStatus();
      }
    });
  }

  if (ocrPreviewBody) {
    ocrPreviewBody.addEventListener("click", (event) => {
      const button = event.target.closest(".ocr-draft-formula");
      if (!button) return;
      openFormulaEditor(Number(button.dataset.formulaIndex));
    });
  }

  if (ocrSourceToggle) {
    ocrSourceToggle.addEventListener("click", () => {
      if (ocrSourceOpen) {
        closeOcrSource();
      } else {
        openOcrSource();
      }
    });
  }

  if (ocrSourceSave) {
    ocrSourceSave.addEventListener("click", saveOcrSource);
  }

  if (ocrSourceCancel) {
    ocrSourceCancel.addEventListener("click", () => closeOcrSource());
  }

  if (ocrSource) {
    ocrSource.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closeOcrSource();
        return;
      }
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        saveOcrSource();
      }
    });
  }

  if (ocrRetry) {
    ocrRetry.addEventListener("click", () => {
      if (!busy && !ocrBusy && !ocrFixBusy && ocrFile) ocrFile.click();
    });
  }

  if (ocrPreviewClose) {
    ocrPreviewClose.addEventListener("click", () => {
      if (ocrBusy) {
        cancelOcr();
      } else {
        clearOcrDraft();
      }
    });
  }

  if (ocrCorrectForm) {
    ocrCorrectForm.addEventListener("submit", (event) => {
      event.preventDefault();
      submitOcrCorrection();
    });
  }

  if (formulaInput) {
    formulaInput.addEventListener("input", updateFormulaPreview);
    formulaInput.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closeFormulaEditor();
        return;
      }
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        saveFormula();
      }
    });
  }

  if (formulaSave) formulaSave.addEventListener("click", saveFormula);
  if (formulaCancel) {
    formulaCancel.addEventListener("click", () => closeFormulaEditor());
  }
  if (formulaClose) {
    formulaClose.addEventListener("click", () => closeFormulaEditor());
  }
  if (formulaBackdrop) {
    formulaBackdrop.addEventListener("click", () => closeFormulaEditor());
  }
  buildFormulaSymbols();
  hydrateGuideMath();

  // 直接 Ctrl+V 粘贴截图，比先存文件再上传顺手
  document.addEventListener("paste", (event) => {
    if (busy || ocrBusy || ocrFixBusy || mode !== "solve") return;
    if (!composer || composer.hidden) return;
    // 步骤追问侧栏打开时，粘贴留给它自己处理
    if (stepPanel && !stepPanel.hidden) return;

    const items = event.clipboardData && event.clipboardData.items;
    if (!items) return;
    for (const item of items) {
      if (item.kind !== "file" || !item.type.startsWith("image/")) continue;
      const file = item.getAsFile();
      if (!file) continue;
      event.preventDefault();
      recognizeImage(file);
      return;
    }
  });

  const examples = document.getElementById("examples");
  if (examples) {
    examples.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-q]");
      if (!button || busy || ocrBusy || ocrFixBusy) return;
      // 示例题是一道全新的题，不能和已有 OCR 题目卡拼在一起。
      clearOcrDraft();
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
        ocrAvailable = false;
        if (ocrBtn) {
          ocrBtn.disabled = true;
          ocrBtn.title = "图片识题不可用：请先在 .env 中配置 OPENAI_API_KEY";
        }
        return;
      }
      status.className = "status ok";
      statusText.textContent = `模型 · ${data.model || "未命名"}`;
      ocrProvider = data.ocr_provider || "deepseek";
      ocrAvailable = data.ocr_available !== false;
      if (ocrBtn) {
        ocrBtn.disabled = !ocrAvailable;
        if (!ocrAvailable) {
          if (ocrProvider === "ollama") {
            ocrBtn.title = "图片识题不可用：请先启动本地 Ollama；文字提问仍可用";
          } else if (ocrProvider === "simpletex") {
            ocrBtn.title = "图片识题不可用：请检查 SIMPLETEX 配置；文字提问仍可用";
          } else {
            ocrBtn.title =
              "图片识题不可用：请检查 OPENAI_API_KEY / OPENAI_VISION_MODEL；文字提问仍可用";
          }
        } else {
          ocrBtn.title = "上传或粘贴题目图片";
        }
      }
      updateControls();
    } catch (err) {
      status.className = "status bad";
      statusText.textContent = "本地服务未连接";
    }
  }

  loadHealth();
  restoreSession();
})();
