"""mathcoach Web MVP。

用法（在项目根目录执行）：
    python -m web.app
    然后打开 http://127.0.0.1:8000

设计边界：
1. 只监听 127.0.0.1，默认不对外网开放；
2. API Key 只留在服务端 .env，浏览器永远拿不到；
3. 解题逻辑全部复用 agent.Agent，这里只做 HTTP 包装；
4. 每道题写一条 trace，返回耗时、token 和工具调用摘要。
"""

import asyncio
import json
import os
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import ocr
from agent import CANCELLED_MESSAGE, FAST_SYSTEM_PROMPT, Agent
from contracts import Budget
from eval.extract import extract_final_answer, extract_final_answer_display
from llm import OpenAIClient
from multi_agent import Orchestrator, Planner, Solver, Verifier
from rag.latex import LatexStore
from solution import extract_solution_steps
from tools import (
    index_available,
    load_problem_index,
    parse_edition,
    retriever_status,
    search_past_problems,
)
from tracing import TraceRecorder, new_trace_file
from web.sessions import Session, SessionStore


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TRACE_DIR = BASE_DIR.parent / "traces"

MAX_QUESTION_CHARS = 5000
MAX_CONCURRENT = 2          # 本地演示，同时最多跑两道题，避免并发烧额度

# OCR 二次校对：只修题干，不解题。输出长度仍以题目上限封顶。
_OCR_FIX_SYSTEM_PROMPT = """\
你是数学题目 OCR 校对助手。用户会给你一段从图片识别出的题目 Markdown，\
以及一句大白话纠错说明。

只按纠错说明修正题干中的文字和 LaTeX，保持题意、题号、顺序和原有内容不变。
必须遵守：
1. 只输出修正后的完整题干，不要解释、不要前言后语、不要 Markdown 代码围栏。
2. 行内公式写成 $...$，独立公式写成 $$...$$；不要在美元符号前加反斜杠。
3. 公式内部不要换行，不要保留 $ 后和 $ 前的多余空格。
4. 不要添加原识别结果中没有的条件、数字或结论；不确定时保持原样。
5. 用户指出一处错误时只改那一处，其余内容原样输出；绝对不要开始解题。
"""

_FIX_PREFIX_RE = re.compile(
    r"^\s*(?:修正后(?:的)?(?:题目|题干|结果)|修正结果|题目|题干|结果)\s*[:：]\s*"
)


def clean_ocr_fix(text: str) -> str:
    """去掉模型偶尔包上的代码围栏或“修正后的题目：”前缀。"""
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            cleaned = "\n".join(lines[1:-1]).strip()
    return _FIX_PREFIX_RE.sub("", cleaned).strip()


# 匹配单独一行的答案标记，用于从展示文本里去掉它
_ANSWER_LINE_RE = re.compile(
    r"(?m)^\s*(?:\*\*|__)?\s*(?:最终\s*答案|答案|final\s*answer|answer)"
    r"\s*(?:\*\*|__)?\s*(?:[:：]|是|为).*$",
    re.IGNORECASE,
)


# ---------- 应用与静态资源 ----------

app = FastAPI(
    title="mathcoach",
    description="CMC / 考研数学解题 Agent 的本地 Web 演示",
    version="0.1.0",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_client = None
_client_lock = threading.Lock()
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
# 图片识别另算：它不占模型并发额度，但连续点击会重复消耗 OCR 额度。
_ocr_lock = threading.Lock()


def session_db_path() -> Path | None:
    """返回会话数据库路径；设置 CMC_SESSION_DB=off 可退回纯内存模式。"""
    raw = os.environ.get("CMC_SESSION_DB", "data/sessions.db").strip()
    if raw.lower() in {"", "off", "none", "memory"}:
        return None
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR.parent / path


SESSION_DB_PATH = session_db_path()

SESSIONS = SessionStore(
    persist_path=SESSION_DB_PATH,
    namespace="main",
)

# 网页端“停止生成”：浏览器先中断自己的等待，再按 request_id 置位这里的事件。
# 已经发出的单次模型调用无法从另一个线程强杀，但 Agent 会在下一次调用前
# 看到信号并停止，不会继续执行工具和后续步骤。
_CANCEL_LOCK = threading.Lock()
_CANCEL_EVENTS: dict[str, tuple[float, threading.Event, Session | None]] = {}
_CANCEL_TTL = 300.0


def register_cancel(
    request_id: str | None,
    owner: Session | None = None,
) -> threading.Event | None:
    """为一次请求登记取消信号；request_id 为空时返回 None。"""
    if not request_id:
        return None
    now = time.monotonic()
    with _CANCEL_LOCK:
        expired = [
            key for key, (created, _, _) in _CANCEL_EVENTS.items()
            if now - created > _CANCEL_TTL
        ]
        for key in expired:
            _CANCEL_EVENTS.pop(key, None)
        event = threading.Event()
        _CANCEL_EVENTS[request_id] = (now, event, owner)
        return event


def drop_cancel(request_id: str | None, event: threading.Event | None) -> None:
    """请求结束后清掉登记，避免取消表随会话无限增长。"""
    if not request_id or event is None:
        return
    with _CANCEL_LOCK:
        current = _CANCEL_EVENTS.get(request_id)
        if current is not None and current[1] is event:
            _CANCEL_EVENTS.pop(request_id, None)


def request_cancel(request_id: str) -> bool:
    """置位取消信号，并立刻释放会话，让用户可以马上重新提问。

    旧请求仍在后台跑，但它结束时会发现 active_request_id 已经被取消清掉，
    因此不会把半截回答写回会话。已经发出的单次模型调用仍无法强杀。
    """
    with _CANCEL_LOCK:
        current = _CANCEL_EVENTS.get(request_id)
        if current is None:
            return False
        event = current[1]
        owner = current[2]
        event.set()

    if owner is not None:
        with owner.lock:
            if owner.active_request_id == request_id:
                owner.active_request_id = None
    return True

# 步骤追问有自己独立的短对话，绝不能写进主会话，否则主对话会出现
# “用户问：这一步为什么”这种没有上下文的孤立消息。
STEP_SESSIONS = SessionStore(
    max_sessions=200,
    max_history_turns=4,
    max_history_chars=24000,
    max_assistant_chars=4000,
    persist_path=SESSION_DB_PATH,
    namespace="step",
)


def get_client():
    """懒加载模型客户端，避免模块导入时就要求配置好密钥。"""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = OpenAIClient()
    return _client


_fast_client = None
_fast_client_lock = threading.Lock()


def get_fast_client():
    """快速模式客户端：关闭 reasoning，优先低延迟。"""
    global _fast_client
    if _fast_client is None:
        with _fast_client_lock:
            if _fast_client is None:
                _fast_client = OpenAIClient(reasoning_effort="none")
    return _fast_client


_latex_store = None
_latex_store_path: Path | None = None
_latex_lock = threading.Lock()


def latex_cache_path() -> Path:
    """展示用 LaTeX 缓存的位置，可用 CMC_LATEX_PATH 覆盖（测试用）。"""
    return Path(
        os.environ.get("CMC_LATEX_PATH", "data/processed/question_latex.json")
    )


def load_latex_store() -> LatexStore:
    """懒加载题干 LaTeX 缓存；文件缺失时返回空缓存，不影响接口。"""
    global _latex_store, _latex_store_path
    path = latex_cache_path()
    with _latex_lock:
        if _latex_store is None or _latex_store_path != path:
            _latex_store = LatexStore.load(path)
            _latex_store_path = path
        return _latex_store


def display_question_for(problem: dict | None) -> str:
    """取模型重建后的展示题干；没有缓存或内容已过期时返回空串。"""
    if not problem:
        return ""
    try:
        return load_latex_store().get(problem) or ""
    except Exception:
        return ""


def display_text_for(problem: dict | None) -> str:
    """试卷/卡片实际展示的题干。

    优先用 LaTeX 缓存；第 17 届的题干本身就按 Markdown + LaTeX 整理，
    带 ``display_ready`` 标记，直接返回原文，不再走模型重建。
    """
    display = display_question_for(problem)
    if display:
        return display
    if problem and problem.get("display_ready"):
        return str(problem.get("question") or "")
    return ""


# ---------- 请求 / 响应结构 ----------

class AskRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=1,
        max_length=MAX_QUESTION_CHARS,
        description="用户输入的题目文本",
    )
    session_id: str | None = Field(
        default=None,
        max_length=64,
        description="服务端返回的会话 id；首次提问不传",
    )
    request_id: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="客户端生成的一次性请求 id，用于“停止生成”",
    )
    mode: Literal["standard", "fast"] = Field(
        default="standard",
        description="fast：直接看答案；standard：完整讲解",
    )


class CancelRequest(BaseModel):
    """取消一次正在进行的求解请求。"""

    request_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class MultiAskRequest(BaseModel):
    """多 Agent 深度检查请求。"""

    question: str = Field(
        ...,
        min_length=1,
        max_length=MAX_QUESTION_CHARS,
        description="用户输入的题目文本",
    )
    session_id: str | None = Field(
        default=None,
        max_length=64,
        description="已有会话 id；首次提问不传",
    )
    request_id: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="客户端生成的一次性请求 id",
    )


class OcrResponse(BaseModel):
    """一次整页图片识别的结果；只返回文本，不自动提交解题。"""

    text: str
    mode: str = ""
    confidence: float | None = None


class OcrFixRequest(BaseModel):
    """用户用大白话指出识别错误后的二次校对请求。"""

    text: str = Field(..., min_length=1, max_length=MAX_QUESTION_CHARS)
    instruction: str = Field(..., min_length=1, max_length=500)


class OcrFixResponse(BaseModel):
    text: str
    elapsed: float = 0.0
    truncated: bool = False


class StepAskRequest(BaseModel):
    """针对主对话中某一个解答步骤的提问。"""

    question: str = Field(..., min_length=1, max_length=MAX_QUESTION_CHARS)
    parent_session_id: str = Field(..., max_length=64)
    parent_turn_id: str | None = Field(default=None, max_length=64)
    parent_turn: int | None = Field(default=None, ge=1)
    step_index: int = Field(..., ge=1)
    step_text: str = Field(default="", max_length=20000)
    step_session_id: str | None = Field(default=None, max_length=64)
    request_id: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class StepInfo(BaseModel):
    step: int | None = None
    elapsed: float = 0.0
    tool_count: int = 0


class ToolInfo(BaseModel):
    step: int | None = None
    name: str = ""
    args: dict = Field(default_factory=dict)
    ok: bool = True
    elapsed: float = 0.0
    result: str = ""


class RelatedProblem(BaseModel):
    """相关真题卡片。只含题干和出处，解答由模型另行组织。"""

    id: str = ""
    source: str = ""
    question: str = ""
    display_question: str = ""
    knowledge_points: list[str] = Field(default_factory=list)
    score: float = 0.0


class PaperProblem(BaseModel):
    """一份试卷中的一道题；题干来自题库，不由模型复述。"""

    id: str = ""
    paper_id: str = ""
    edition: int | None = None
    year: int | None = None
    stage: str = ""
    category: str = ""
    supplement: str = ""
    section: str = ""
    number: int = 0
    source: str = ""
    question: str = ""
    display_question: str = ""
    answer: str = ""
    solution: str = ""
    knowledge_points: list[str] = Field(default_factory=list)


class PaperGroup(BaseModel):
    stage: str = ""
    category: str = ""
    supplement: str = ""
    section: str = ""
    label: str = ""
    count: int = 0
    numbers: list[int] = Field(default_factory=list)
    missing: list[int] = Field(default_factory=list)
    complete: bool = True
    problems: list[PaperProblem] = Field(default_factory=list)


class PaperSummary(BaseModel):
    stage: str = ""
    category: str = ""
    supplement: str = ""
    section: str = ""
    label: str = ""
    count: int = 0
    numbers: list[int] = Field(default_factory=list)
    missing: list[int] = Field(default_factory=list)
    complete: bool = True


class PaperEdition(BaseModel):
    edition: int
    label: str = ""
    total: int = 0
    papers: list[PaperSummary] = Field(default_factory=list)


class PaperIndexResponse(BaseModel):
    editions: list[PaperEdition] = Field(default_factory=list)


class PaperResponse(BaseModel):
    edition: int | None = None
    year: int | None = None
    count: int = 0
    groups: list[PaperGroup] = Field(default_factory=list)


class SolutionStep(BaseModel):
    index: int
    label: str = ""
    title: str = ""
    markdown: str = ""


class SolutionBreakdown(BaseModel):
    intro: str = ""
    steps: list[SolutionStep] = Field(default_factory=list)
    outro: str = ""


class SessionTurn(BaseModel):
    """刷新页面后恢复一条历史回答所需的最小字段。"""

    id: str = ""
    question: str = ""
    answer: str = ""
    display_answer: str = ""
    final_answer: str | None = None
    final_answer_display: str | None = None
    solution: SolutionBreakdown | None = None
    turn: int = 1
    follow_up: bool = False


class SessionResponse(BaseModel):
    session_id: str
    original_question: str = ""
    turns: list[SessionTurn] = Field(default_factory=list)


class AskResponse(BaseModel):
    answer: str
    display_answer: str
    session_id: str
    turn_id: str | None = None
    turn: int = 1
    follow_up: bool = False
    final_answer: str | None = None
    final_answer_display: str | None = None
    error: str | None = None
    elapsed: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    tokens: int = 0
    trace_file: str | None = None
    steps: list[StepInfo] = Field(default_factory=list)
    tools: list[ToolInfo] = Field(default_factory=list)
    related_problems: list[RelatedProblem] = Field(default_factory=list)
    paper: PaperResponse | None = None
    solution: SolutionBreakdown | None = None


class StepAskResponse(BaseModel):
    """步骤侧栏的一次回答；刻意不返回 final_answer / related / paper。"""

    answer: str
    display_answer: str
    step_session_id: str
    step_index: int = 1
    step_label: str = ""
    turn: int = 1
    error: str | None = None
    elapsed: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    tokens: int = 0
    trace_file: str | None = None
    steps: list[StepInfo] = Field(default_factory=list)
    tools: list[ToolInfo] = Field(default_factory=list)


# ---------- 纯函数 ----------

def normalize_math_delimiters(text: str) -> str:
    """把 LaTeX 的 \\\\(...\\\\) / \\\\[...\\\\] 统一成 $...$ / $$...$$。

    前端统一用美元符号分隔符。负向后行断言避免把 LaTeX 换行
    ``\\\\[2pt]`` 里的 ``\\\\[`` 误当成公式开头。
    """
    text = re.sub(r"(?<!\\)\\\[", "$$", text)
    text = re.sub(r"(?<!\\)\\\]", "$$", text)
    text = re.sub(r"(?<!\\)\\\(", "$", text)
    text = re.sub(r"(?<!\\)\\\)", "$", text)
    return text


_MATH_CHARS_RE = re.compile(r"\\[A-Za-z]+|[=^_{}]|[A-Za-z]'|[A-Za-z]\s*\(")
_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]+")
_CJK_SPLIT_RE = re.compile(r"[\u4e00-\u9fff，。；：、！？（）【】“”‘’]+")
_CHINESE_PUNCT = "，。；：、！？（）【】“”‘’"


def _wrap_bare_math(segment: str) -> str:
    """给一段不含现有定界符的文本补 $...$。

    只包裹“像数学”的片段，中文和普通说明保持原样；中文标点留在
    公式外面，避免 KaTeX 把句读排进数学模式。
    """
    if not segment.strip() or "$" in segment:
        return segment
    if not _MATH_CHARS_RE.search(segment):
        return segment

    core = segment
    prefix = ""
    suffix = ""
    while core and core[0] in _CHINESE_PUNCT:
        prefix += core[0]
        core = core[1:]
    while core and core[-1] in _CHINESE_PUNCT:
        suffix = core[-1] + suffix
        core = core[:-1]
    if not core.strip():
        return segment

    leading = core[:len(core) - len(core.lstrip())]
    trailing = core[len(core.rstrip()):]
    body = core.strip()
    return f"{prefix}{leading}${body}${trailing}{suffix}"


def ensure_display_math(text: str | None) -> str:
    """把答案/解答里的裸 LaTeX 补上 $...$，供前端 KaTeX 渲染。

    已有 $...$ / $$...$$ / \\\\(...\\\\) / \\\\[...\\\\] 的内容原样保留；
    只处理定界符之外的片段。OCR/模型摘录经常产生 ``\\frac{1}{2}``
    这种没有美元符号的答案，前端会把它当普通文本显示。
    """
    if not text:
        return ""
    text = normalize_math_delimiters(str(text))
    # 先按现有公式定界符切开，只处理公式外面的文本
    parts = re.split(r"(\$\$.*?\$\$|\$.*?\$)", text, flags=re.S)
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        if part.startswith("$"):
            out.append(part)
            continue
        # 中文连续段不包裹；其余片段按“是否像数学”决定
        pos = 0
        for match in _CJK_SPLIT_RE.finditer(part):
            out.append(_wrap_bare_math(part[pos:match.start()]))
            out.append(match.group(0))
            pos = match.end()
        out.append(_wrap_bare_math(part[pos:]))
    return "".join(out)


def strip_answer_marker(text: str) -> str:
    """去掉正文里的最后一行“最终答案：...”，避免和答案卡片重复。"""
    matches = list(_ANSWER_LINE_RE.finditer(text))
    if matches:
        last = matches[-1]
        text = text[:last.start()] + text[last.end():]

    # 去掉因此变空的结尾标题，例如 "## 最终答案" 下面已经没有内容
    text = re.sub(
        r"(?:\n\s*)+#{1,6}\s*(?:最终\s*答案|答案)\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()


def collect_steps(events: list[dict]) -> list[dict]:
    """从 trace 事件里提取模型调用步骤。"""
    steps = []
    for event in events:
        if event.get("type") != "llm":
            continue
        steps.append({
            "step": event.get("step"),
            "elapsed": float(event.get("elapsed") or 0),
            "tool_count": len(event.get("tool_calls") or []),
        })
    return steps


_CLARIFY_RE = re.compile(
    r"请(?:确认|补充|回复|告诉|说明|明确)|哪一(?:场|届|年|份)|想要哪"
)


def asks_for_more_info(text: str | None) -> bool:
    """回答是不是在向用户要信息，而不是给出完整解答。

    这类回答没有“最终答案”行，不能走“最后一行像答案”的兜底，
    否则“请确认初赛还是决赛第 10 题”会被误抽成最终答案。
    """
    return bool(_CLARIFY_RE.search(text or ""))


def parse_solution(answer: str) -> SolutionBreakdown | None:
    """把展示文本解析成前端可点击的步骤；识别不到时返回 None。"""
    parsed = extract_solution_steps(answer)
    if not parsed or not parsed.get("steps"):
        return None
    return SolutionBreakdown(**parsed)


def find_solution_step(
    answer: str,
    index: int,
    fallback_text: str = "",
) -> tuple[dict, SolutionBreakdown | None] | None:
    """按顺序取第 index 步；解析失败时退回前端传来的步骤原文。

    优先用服务端解析结果，避免浏览器里的文本被截断或改写后，模型看到
    的“引用步骤”和实际回答不一致。
    """
    solution = parse_solution(answer)
    if solution and 1 <= index <= len(solution.steps):
        step = solution.steps[index - 1]
        return step.model_dump(), solution

    fallback = (fallback_text or "").strip()
    if fallback:
        return {
            "index": index,
            "label": f"第{index}步",
            "title": "",
            "markdown": fallback[:12000],
        }, solution
    return None


def build_step_seed(
    original_question: str,
    turn_question: str,
    answer: str,
) -> str:
    """给步骤追问会话的第一次“阅读材料”，只存在侧栏会话里。"""
    parts = ["这是主对话里的一道题和完整解答，请把它作为后续追问的背景。"]
    if original_question:
        parts.append(f"【原题】\n{original_question}")
    if turn_question and turn_question != original_question:
        parts.append(f"【本轮问题】\n{turn_question}")

    body = answer
    if len(body) > 8000:
        body = body[:8000] + "\n…（后文省略）…"
    parts.append(f"【完整解答】\n{body}")
    return "\n\n".join(parts)


def build_step_question(
    question: str,
    parent_turn: int,
    step: dict,
) -> str:
    """构造只针对某一步的提问，并把步骤原文作为引用带上。"""
    label = str(step.get("label") or f"第{step.get('index', 1)}步")
    title = str(step.get("title") or "").strip()
    heading = f"{label}：{title}" if title else label
    step_text = str(step.get("markdown") or "")[:12000]

    return (
        "请只针对我下面引用的步骤作答，不要重新解整题，"
        "也不要输出“最终答案：”行。可以解释这一步的动机、依据、"
        "计算细节或常见误区。\n\n"
        f"主对话第 {parent_turn} 轮解答中的{heading}：\n"
        f"【引用步骤】\n{step_text}\n【引用结束】\n\n"
        f"我的问题：{question}"
    )


def _parse_hits(raw: object) -> list[dict]:
    """把 search_past_problems 返回的 JSON 字符串解析成卡片字段。

    工具结果会经过 trace 序列化，所以这里按字符串处理；解析失败
    一律当成没有检索结果，不影响主答案展示。
    """
    if not isinstance(raw, str):
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    hits: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        tags = item.get("knowledge_points") or []
        hits.append({
            "id": str(item.get("id") or ""),
            "source": str(item.get("source") or ""),
            "question": question,
            "knowledge_points": [str(tag) for tag in tags][:6],
            "score": float(item.get("score") or 0),
        })
    return hits


def collect_related(
    events: list[dict],
    question: str,
    *,
    enabled: bool,
    follow_up: bool = False,
    limit: int = 3,
) -> list[dict]:
    """从轨迹里取检索结果；模型没调工具时补一次直接检索。

    模型偶尔会跳过检索直接作答，这时仍应展示相关真题，所以做一次
    fallback。追问不补检索：用户问的是“这一步为什么”，拿它去搜题库
    只会得到无关结果。检索失败（题库缺失等）按无结果处理。
    """
    if not enabled:
        return []

    hits: list[dict] = []
    seen: set[str] = set()
    for event in events:
        if event.get("type") != "tool" or event.get("name") != "search_past_problems":
            continue
        for hit in _parse_hits(event.get("result")):
            key = hit["id"] or hit["question"]
            if key in seen:
                continue
            seen.add(key)
            hits.append(hit)

    # 按届定位题目的请求本身不是题干，拿它做相似度检索只会得到无关卡片。
    used_locator = any(
        event.get("type") == "tool" and event.get("name") == "get_past_problem"
        for event in events
    )
    if not hits and not follow_up and not used_locator:
        try:
            hits = _parse_hits(search_past_problems(question, limit))
        except Exception:
            hits = []

    hits = hits[:limit]
    _attach_related_display(hits)
    return hits


def _attach_related_display(hits: list[dict]) -> None:
    """补全题干和展示用 LaTeX。

    工具结果为了控制上下文只带 300 字题干；卡片展示时回到题库取全文，
    再附上构建阶段缓存的 ``display_question``。题库或缓存缺失时保持
    工具结果原样，页面仍有可读文本。
    """
    if not hits:
        return
    try:
        index = load_problem_index() if index_available() else None
    except Exception:
        index = None
    if index is None:
        return

    for hit in hits:
        problem = index.get(hit.get("id", ""))
        if not problem:
            continue
        full_question = str(problem.get("question") or "").strip()
        if full_question:
            hit["question"] = full_question
        display = display_text_for(problem)
        if display:
            hit["display_question"] = display


def _attach_paper_display(index, groups: list[dict]) -> None:
    """给按届浏览的每道题附上展示用 LaTeX。"""
    for group in groups:
        for entry in group.get("problems") or []:
            problem = index.get(str(entry.get("id") or ""))
            display = display_text_for(problem)
            if display:
                entry["display_question"] = display
            if entry.get("answer"):
                entry["answer"] = ensure_display_math(entry["answer"])
            if entry.get("solution"):
                entry["solution"] = ensure_display_math(entry["solution"])


def build_paper_payload(
    *,
    edition: int | None = None,
    year: int | None = None,
    stage: str | None = None,
    category: str | None = None,
    supplement: str | None = None,
    section: str | None = None,
) -> dict | None:
    """按条件读取完整试卷；没有匹配题目时返回 None。"""
    if not index_available():
        return None
    index = load_problem_index()
    entries = index.list_problems(
        edition=edition,
        year=year,
        stage=stage or None,
        category=category or None,
        supplement=supplement,
    )
    if section:
        # 2024/2025 的卷子按选择题/填空题/解答题分开浏览
        entries = [entry for entry in entries if entry.get("section") == section]
    if not entries:
        return None
    groups = index.group_problems(entries)
    _attach_paper_display(index, groups)
    return {
        "edition": edition,
        "year": year,
        "count": len(entries),
        "groups": groups,
    }


def collect_paper(events: list[dict]) -> dict | None:
    """从轨迹里的 list_past_problems 调用还原完整试卷。

    工具结果只带题号和出处；这里按同一组参数重新读题库，返回完整题干。
    这样模型既不需要把整卷抄进上下文，Web 端也不会展示被截断的文本。
    """
    for event in reversed(events):
        if event.get("type") != "tool" or event.get("name") != "list_past_problems":
            continue

        args = event.get("args") or {}
        try:
            edition = parse_edition(args.get("edition"))
            raw_year = args.get("year")
            year = None
            if raw_year not in (None, ""):
                match = re.search(r"\d{4}", str(raw_year))
                year = int(match.group(0)) if match else None
            if edition is None and year is None:
                continue

            payload = build_paper_payload(
                edition=edition,
                year=year,
                stage=args.get("stage"),
                category=args.get("category"),
                supplement=args.get("supplement"),
            )
        except Exception:
            payload = None

        if payload:
            return payload
    return None


def collect_tools(events: list[dict], max_result: int = 500) -> list[dict]:
    """从 trace 事件里提取工具调用摘要。"""
    tools = []
    for event in events:
        if event.get("type") != "tool":
            continue
        tools.append({
            "step": event.get("step"),
            "name": event.get("name", ""),
            "args": event.get("args") or {},
            "ok": bool(event.get("ok", True)),
            "elapsed": float(event.get("elapsed") or 0),
            "result": str(event.get("result", ""))[:max_result],
        })
    return tools


# ---------- 路由 ----------

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """返回单页应用。"""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    """给前端显示当前模型状态；不返回密钥本身。"""
    return {
        "ok": True,
        "model": config.OPENAI_MODEL,
        "key_set": bool(config.OPENAI_API_KEY.strip()),
        "ocr_configured": ocr.is_configured(),
        "ocr_available": ocr.is_available(),
        "ocr_provider": ocr.get_provider(),
        "papers_available": index_available(),
        "sessions": SESSIONS.size(),
        "step_sessions": STEP_SESSIONS.size(),
        "session_persistent": SESSIONS.persistent,
        **retriever_status(),
    }


@app.get("/api/session/{session_id}", response_model=SessionResponse)
def get_session(session_id: str) -> SessionResponse:
    """恢复一个仍然存在的会话，供刷新页面后重建聊天记录。

    只返回持久化下来的最终问答；轨迹和工具事件没有存库，因此恢复后
    不显示“查看解题轨迹”，但步骤追问和继续追问仍然可用。
    """
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail="会话不存在或已过期，请开始新对话。",
        )

    with session.lock:
        original_question = session.original_question
        turns = [dict(turn) for turn in session.turns]

    restored: list[SessionTurn] = []
    for index, turn in enumerate(turns, 1):
        answer = str(turn.get("answer") or "")
        normalized = normalize_math_delimiters(answer)
        # 第一轮才允许“最后一行像答案”的兜底；追问和列真题不猜答案。
        allow_fallback = index == 1 and not asks_for_more_info(answer)
        display_answer = strip_answer_marker(normalized)
        restored.append(SessionTurn(
            id=str(turn.get("id") or ""),
            question=str(turn.get("question") or ""),
            answer=answer,
            display_answer=display_answer,
            final_answer=extract_final_answer(
                answer,
                allow_fallback=allow_fallback,
            ),
            final_answer_display=extract_final_answer_display(
                normalized,
                allow_fallback=allow_fallback,
            ),
            solution=parse_solution(display_answer),
            turn=index,
            follow_up=index > 1,
        ))

    return SessionResponse(
        session_id=session.id,
        original_question=original_question,
        turns=restored,
    )


@app.post("/api/ocr", response_model=OcrResponse)
def recognize_image(file: UploadFile = File(...)) -> OcrResponse:
    """把题目截图识别成可编辑文本。

    只做识别，不调用解题模型，也不自动提交。识别结果由前端生成题目卡，
    用户核对公式和题号后再自己点发送。
    """
    if not ocr.is_configured():
        provider = ocr.get_provider()
        if provider == "deepseek":
            detail = (
                "服务端未配置图片识别：请在 .env 中填写 OPENAI_API_KEY，"
                "并确认 OPENAI_VISION_MODEL=deepseek-flash，然后重启服务。"
            )
        elif provider == "simpletex":
            detail = (
                "服务端未配置图片识别：请在 .env 中填写 SIMPLETEX_TOKEN，"
                "或同时填写 SIMPLETEX_APP_ID 与 SIMPLETEX_APP_SECRET，"
                "然后重启服务。"
            )
        else:
            detail = (
                f"服务端图片识别配置无效（OCR_PROVIDER={provider!r}），"
                "可选 deepseek / ollama / simpletex。"
            )
        raise HTTPException(status_code=503, detail=detail)

    # 多读 1 字节即可判断是否超限，避免把超大文件整个读进内存
    data = file.file.read(ocr.MAX_IMAGE_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="图片内容为空，请重新选择。")
    if len(data) > ocr.MAX_IMAGE_BYTES:
        limit_mb = ocr.MAX_IMAGE_BYTES // (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"图片超过 {limit_mb} MB，请压缩或裁剪后再上传。",
        )
    detected = ocr.detect_image_type(data)
    if detected is None:
        raise HTTPException(
            status_code=415,
            detail="只支持 PNG / JPG / BMP 图片，请换一种格式。",
        )
    # 本地 llama.cpp 路线只解码 PNG / JPG，BMP 在进模型前就拒绝，
    # 否则会变成一条含糊的 502。
    if ocr.get_provider() == "ollama" and detected[0] not in {"png", "jpg"}:
        raise HTTPException(
            status_code=415,
            detail="本地识别只支持 PNG / JPG 图片，请换一种格式。",
        )

    if not _ocr_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="已有一张图片正在识别，请稍等几秒再试。",
        )
    try:
        result = ocr.recognize(data)
    except ocr.OcrError as exc:
        # 上游失败统一按 502 返回；具体原因可以直接展示给用户
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        _ocr_lock.release()

    return OcrResponse(
        text=result.text,
        mode=result.mode,
        confidence=result.confidence,
    )


@app.post("/api/ocr/fix", response_model=OcrFixResponse)
def fix_ocr_text(payload: OcrFixRequest) -> OcrFixResponse:
    """带着原识别结果和用户的纠错说明，让模型做一轮题干校对。

    只返回修正后的题干；不写入会话、不触发解题。
    """
    if not config.OPENAI_API_KEY.strip():
        raise HTTPException(
            status_code=503,
            detail="服务端未配置解题模型 API Key，无法进行二次校对。",
        )

    text = payload.text.strip()
    instruction = payload.instruction.strip()
    if not text:
        raise HTTPException(status_code=400, detail="识别结果为空，无法校对。")
    if not instruction:
        raise HTTPException(status_code=400, detail="请先用一句话说明哪里识别错了。")

    # 和解题共用并发额度，避免用户连续点“AI 修正”打满上游。
    if not _slots.acquire(timeout=0.5):
        raise HTTPException(
            status_code=429,
            detail="模型正忙，请等当前请求结束后再试。",
        )

    started = time.perf_counter()
    try:
        response = get_client().chat([
            {"role": "system", "content": _OCR_FIX_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "原识别结果：\n"
                    f"{text}\n\n"
                    f"用户纠错说明：{instruction}"
                ),
            },
        ])
        corrected = clean_ocr_fix(response.content)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"题目修正失败：{exc}",
        ) from exc
    finally:
        _slots.release()

    if not corrected:
        raise HTTPException(
            status_code=502,
            detail="模型没有返回修正后的题目，请换个说法再试。",
        )

    truncated = len(corrected) > MAX_QUESTION_CHARS
    return OcrFixResponse(
        text=corrected[:MAX_QUESTION_CHARS],
        elapsed=round(time.perf_counter() - started, 3),
        truncated=truncated,
    )


@app.get("/api/papers", response_model=PaperIndexResponse)
def list_papers() -> dict:
    """返回第 1 届到题库最高届的试卷目录，供“查看真题”页渲染选项条。"""
    if not index_available():
        raise HTTPException(
            status_code=503,
            detail="题库未构建，请先在项目根目录运行 python -m rag.build。",
        )
    return {"editions": load_problem_index().paper_index()}


@app.get("/api/paper", response_model=PaperResponse)
def get_paper(
    edition: int | None = None,
    year: int | None = None,
    stage: str | None = None,
    category: str | None = None,
    supplement: str | None = None,
    section: str | None = None,
) -> dict:
    """返回一份试卷的全部题目。

    supplement 省略表示不限补赛；传空字符串表示只要正卷。
    这个接口不调用模型，题干直接来自题库，因此不会漏题或改写。
    """
    if edition is None and year is None:
        raise HTTPException(status_code=400, detail="请指定届数或年份。")
    if not index_available():
        raise HTTPException(
            status_code=503,
            detail="题库未构建，请先在项目根目录运行 python -m rag.build。",
        )

    payload = build_paper_payload(
        edition=edition,
        year=year,
        stage=stage,
        category=category,
        supplement=supplement,
        section=section,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="没有找到符合条件的真题。")
    return payload


@app.post("/api/ask/cancel")
def cancel_ask(req: CancelRequest) -> dict:
    """停止一次仍在排队的求解；已经结束时返回 cancelled=False。"""
    return {"cancelled": request_cancel(req.request_id)}


@app.post("/api/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    """求解一道题，或在已有会话里追问。"""
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="题目不能为空。")
    if not config.OPENAI_API_KEY.strip():
        raise HTTPException(
            status_code=503,
            detail="服务端未配置 OPENAI_API_KEY，请在项目根目录 .env 中填写后重启。",
        )

    # 本地演示不做排队系统，直接拒绝过载请求，避免悄悄消耗额度
    if not _slots.acquire(timeout=0.5):
        raise HTTPException(status_code=429, detail="当前有题目正在求解，请稍后再试。")

    session: Session | None = None
    cancel_event: threading.Event | None = None
    request_key = req.request_id or uuid.uuid4().hex
    try:
        session = SESSIONS.get_or_create(req.session_id)

        # 只在校验和取上下文时短暂持锁，模型调用期间不占用会话锁。
        # 这样用户点“停止”后可以立刻追问，旧请求也不会再把结果写回来。
        if not session.lock.acquire(timeout=0.5):
            raise HTTPException(
                status_code=409,
                detail="该会话正在处理上一条消息，请稍后再试。",
            )
        try:
            if not SESSIONS.begin_request(session, request_key):
                raise HTTPException(
                    status_code=409,
                    detail="该会话正在处理上一条消息，请稍后再试。",
                )
            history = SESSIONS.history_for(session)
            follow_up = bool(history)
            turn = len(session.turns) + 1
        finally:
            session.lock.release()

        cancel_event = register_cancel(req.request_id, owner=session)

        trace_path = new_trace_file(TRACE_DIR, prefix="web")
        tracer = TraceRecorder(
            case_id=f"web-{session.id[:8]}-t{turn}",
            question=question,
            path=trace_path,
        )

        # 快速模式不检索；题库没构建时也不开检索工具，避免无意义的工具错误
        fast = req.mode == "fast"
        retrieval = index_available() and not fast
        agent_client = get_fast_client() if fast else get_client()
        agent = Agent(
            client=agent_client,
            retrieval=retrieval,
            max_steps=8 if fast else 15,
            step_timeout=45.0 if fast else 30.0,
            system_prompt=FAST_SYSTEM_PROMPT if fast else None,
        )

        start = time.perf_counter()
        try:
            answer = agent.run(
                question,
                trace=tracer,
                history=history,
                cancel_event=cancel_event,
            )
        except Exception as e:  # 单题失败不拖垮服务
            answer = f"错误：求解失败（{type(e).__name__}: {e}）"
            tracer.add("error", kind="agent_exception", message=answer)
        elapsed = time.perf_counter() - start
        cancelled = cancel_event is not None and cancel_event.is_set()
        if cancelled:
            answer = CANCELLED_MESSAGE

        tracer.finish(
            request_elapsed=round(elapsed, 4),
            session_id=session.id,
            turn=turn,
            follow_up=follow_up,
            history_messages=len(history),
            history_chars=sum(len(m["content"]) for m in history),
        )

        # 列真题不是解题，不要把正文里的数字误当成最终答案
        paper = None if cancelled else (
            collect_paper(tracer.events) if retrieval else None
        )
        allow_answer_fallback = (
            not follow_up
            and paper is None
            and not asks_for_more_info(answer)
        )
        final_answer = extract_final_answer(
            answer,
            allow_fallback=allow_answer_fallback,
        )
        final_answer_display = extract_final_answer_display(
            normalize_math_delimiters(answer),
            allow_fallback=allow_answer_fallback,
        )
        error = tracer.error
        if cancelled:
            error = CANCELLED_MESSAGE
        elif error is None and answer.startswith("错误："):
            error = answer

        display_answer = strip_answer_marker(
            normalize_math_delimiters(answer)
        )
        solution = parse_solution(display_answer)

        turn_id = None
        with session.lock:
            # 取消或“新对话”可能已经把这次请求踢掉；只有仍持有会话的
            # 请求才能追加回答，避免停止后旧结果污染下一轮上下文。
            still_owner = SESSIONS.end_request(session, request_key)
            if still_owner and error is None:
                if cancel_event is None or not cancel_event.is_set():
                    turn_id = SESSIONS.append_turn(session, question, answer)

        return AskResponse(
            answer=answer,
            display_answer=display_answer,
            session_id=session.id,
            turn_id=turn_id,
            turn=turn,
            follow_up=follow_up,
            final_answer=final_answer,
            final_answer_display=final_answer_display,
            error=error,
            elapsed=round(elapsed, 2),
            llm_calls=tracer.llm_calls,
            tool_calls=tracer.tool_calls,
            tool_errors=tracer.tool_errors,
            tokens=tracer.usage["total_tokens"],
            trace_file=trace_path.name,
            steps=collect_steps(tracer.events),
            tools=collect_tools(tracer.events),
            related_problems=[] if paper else collect_related(
                tracer.events,
                question,
                enabled=retrieval,
                follow_up=follow_up,
            ),
            paper=paper,
            solution=solution,
        )
    finally:
        if session is not None:
            with session.lock:
                SESSIONS.end_request(session, request_key)
        drop_cancel(req.request_id, cancel_event)
        _slots.release()


@app.post("/api/ask-multi")
def ask_multi(req: MultiAskRequest):
    """Planner → Solver → Verifier 深度检查，SSE 推送阶段进度。

    首次提问会创建会话，最终回答写入会话，这样回答下方的“问这一步”
    仍能复用原有的步骤追问接口。
    """
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="题目不能为空。")
    if not config.OPENAI_API_KEY.strip():
        raise HTTPException(
            status_code=503,
            detail="服务端未配置 OPENAI_API_KEY，请在项目根目录 .env 中填写后重启。",
        )
    if not _slots.acquire(timeout=0.5):
        raise HTTPException(status_code=429, detail="当前有题目正在求解，请稍后再试。")

    request_key = req.request_id or uuid.uuid4().hex
    session: Session | None = None
    try:
        session = SESSIONS.get_or_create(req.session_id)
        if not session.lock.acquire(timeout=0.5):
            raise HTTPException(
                status_code=409,
                detail="该会话正在处理上一条消息，请稍后再试。",
            )
        try:
            if not SESSIONS.begin_request(session, request_key):
                raise HTTPException(
                    status_code=409,
                    detail="该会话正在处理上一条消息，请稍后再试。",
                )
            history = SESSIONS.history_for(session)
            follow_up = bool(history)
            turn = len(session.turns) + 1
        finally:
            session.lock.release()
    except Exception:
        if session is not None:
            with session.lock:
                SESSIONS.end_request(session, request_key)
        _slots.release()
        raise

    client = get_client()
    retrieval = index_available()
    trace_path = new_trace_file(TRACE_DIR, prefix="multi")
    tracer = TraceRecorder(case_id="multi", question=question, path=trace_path)
    events: queue.Queue = queue.Queue()
    final_event: dict = {}

    def emit(event: dict) -> None:
        if event.get("type") == "final":
            answer = str(event.get("answer") or "")
            normalized = normalize_math_delimiters(answer)
            display = strip_answer_marker(normalized)
            event["display_answer"] = display
            event["final_answer_display"] = extract_final_answer_display(
                normalized, allow_fallback=True
            )
            solution = parse_solution(display)
            event["solution"] = solution.model_dump() if solution else None
            event["trace_file"] = trace_path.name
            final_event.clear()
            final_event.update(event)
        else:
            events.put(event)

    # 规划和复核不需要长推理，关掉 reasoning 明显更快；Solver 仍保留默认推理。
    fast_client = OpenAIClient(reasoning_effort="none")
    orchestrator = Orchestrator(
        Planner(fast_client),
        Solver(client, retrieval=retrieval),
        Verifier(fast_client),
        budget=Budget(),
        emit=emit,
        trace=tracer,
    )

    def run() -> None:
        result = None
        try:
            result = orchestrator.run(question)
            turn_id = None
            with session.lock:
                still_owner = SESSIONS.end_request(session, request_key)
                if still_owner and result.answer and not result.answer.startswith("错误："):
                    turn_id = SESSIONS.append_turn(session, question, result.answer)
            final_event.update({
                "session_id": session.id,
                "turn_id": turn_id,
                "turn": turn,
                "follow_up": follow_up,
            })
            events.put(final_event)
            tracer.finish(
                request_elapsed=round(result.elapsed, 4),
                mode="multi",
                session_id=session.id,
                turn=turn,
                verdict=result.review.verdict.value if result.review else "",
            )
        except Exception as exc:
            with session.lock:
                SESSIONS.end_request(session, request_key)
            events.put({
                "type": "error",
                "message": f"多 Agent 求解失败（{type(exc).__name__}: {exc}）",
            })
        finally:
            events.put(None)
            _slots.release()

    threading.Thread(target=run, daemon=True).start()

    async def stream():
        while True:
            event = await asyncio.to_thread(events.get)
            if event is None:
                break
            yield "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/ask-step", response_model=StepAskResponse)
def ask_step(req: StepAskRequest) -> StepAskResponse:
    """只针对主对话里的某一步作答。

    侧栏使用独立的 STEP_SESSIONS：这里产生的问答不会追加到主会话，
    因此主对话的轮次、历史上下文和页面上的消息列表都保持不变。
    """
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空。")
    if not config.OPENAI_API_KEY.strip():
        raise HTTPException(
            status_code=503,
            detail="服务端未配置 OPENAI_API_KEY，请在项目根目录 .env 中填写后重启。",
        )

    if not _slots.acquire(timeout=0.5):
        raise HTTPException(status_code=429, detail="当前有题目正在求解，请稍后再试。")

    thread: Session | None = None
    cancel_event: threading.Event | None = None
    request_key = req.request_id or uuid.uuid4().hex
    try:
        parent = SESSIONS.get(req.parent_session_id)
        if parent is None:
            raise HTTPException(
                status_code=404,
                detail="原对话已过期，请回到主对话重新提问。",
            )

        if not parent.lock.acquire(timeout=0.5):
            raise HTTPException(
                status_code=409,
                detail="该会话正在处理上一条消息，请稍后再试。",
            )

        try:
            turn_data = SESSIONS.get_turn_by_id(parent, req.parent_turn_id)
            if turn_data is None:
                turn_data = SESSIONS.get_turn(parent, req.parent_turn)
            if turn_data is None:
                raise HTTPException(
                    status_code=404,
                    detail="找不到这条回答，请回到主对话重新点击步骤。",
                )

            original_question = parent.original_question or str(
                turn_data.get("question") or ""
            )
            turn_question = str(turn_data.get("question") or "")
            parent_answer = str(turn_data.get("answer") or "")
            try:
                parent_turn_number = parent.turns.index(turn_data) + 1
            except ValueError:
                parent_turn_number = req.parent_turn or 1
        finally:
            parent.lock.release()

        parent_display = strip_answer_marker(
            normalize_math_delimiters(parent_answer)
        )
        found = find_solution_step(
            parent_display,
            req.step_index,
            req.step_text,
        )
        if found is None:
            raise HTTPException(
                status_code=404,
                detail="没有找到这一步，请回到主对话刷新后重试。",
            )
        step, _solution = found
        step_label = str(step.get("label") or f"第{req.step_index}步")
        step_question = build_step_question(question, parent_turn_number, step)

        thread = STEP_SESSIONS.get_or_create(req.step_session_id)
        if not thread.lock.acquire(timeout=0.5):
            raise HTTPException(
                status_code=409,
                detail="这个步骤追问正在处理上一条消息，请稍后再试。",
            )
        try:
            if not STEP_SESSIONS.begin_request(thread, request_key):
                raise HTTPException(
                    status_code=409,
                    detail="这个步骤追问正在处理上一条消息，请稍后再试。",
                )
            if not thread.turns:
                STEP_SESSIONS.append_turn(
                    thread,
                    build_step_seed(
                        original_question,
                        turn_question,
                        parent_answer,
                    ),
                    "好的，我已阅读原题和完整解答，会只回答被追问的部分。",
                )

            history = STEP_SESSIONS.history_for(thread)
            turn = max(1, len(thread.turns))
        finally:
            thread.lock.release()

        cancel_event = register_cancel(req.request_id, owner=thread)

        trace_path = new_trace_file(TRACE_DIR, prefix="step")
        tracer = TraceRecorder(
            case_id=f"step-{thread.id[:8]}-t{turn}",
            question=step_question,
            path=trace_path,
        )

        start = time.perf_counter()
        try:
            # 侧栏只解释步骤，不需要再检索题库，避免无关结果干扰。
            answer = Agent(client=get_client(), retrieval=False).run(
                step_question,
                trace=tracer,
                history=history,
                cancel_event=cancel_event,
            )
        except Exception as e:
            answer = f"错误：求解失败（{type(e).__name__}: {e}）"
            tracer.add("error", kind="agent_exception", message=answer)
        elapsed = time.perf_counter() - start
        cancelled = cancel_event is not None and cancel_event.is_set()
        if cancelled:
            answer = CANCELLED_MESSAGE

        tracer.finish(
            request_elapsed=round(elapsed, 4),
            session_id=thread.id,
            turn=turn,
            scope="step",
            history_messages=len(history),
            history_chars=sum(len(m["content"]) for m in history),
        )

        error = tracer.error
        if cancelled:
            error = CANCELLED_MESSAGE
        elif error is None and answer.startswith("错误："):
            error = answer

        with thread.lock:
            # 只写侧栏会话；主 SESSIONS 完全不动。取消后不再追加。
            still_owner = STEP_SESSIONS.end_request(thread, request_key)
            if still_owner and error is None:
                if cancel_event is None or not cancel_event.is_set():
                    STEP_SESSIONS.append_turn(thread, step_question, answer)

        return StepAskResponse(
            answer=answer,
            display_answer=strip_answer_marker(
                normalize_math_delimiters(answer)
            ),
            step_session_id=thread.id,
            step_index=req.step_index,
            step_label=step_label,
            turn=turn,
            error=error,
            elapsed=round(elapsed, 2),
            llm_calls=tracer.llm_calls,
            tool_calls=tracer.tool_calls,
            tool_errors=tracer.tool_errors,
            tokens=tracer.usage["total_tokens"],
            trace_file=trace_path.name,
            steps=collect_steps(tracer.events),
            tools=collect_tools(tracer.events),
        )
    finally:
        if thread is not None:
            with thread.lock:
                STEP_SESSIONS.end_request(thread, request_key)
        drop_cancel(req.request_id, cancel_event)
        _slots.release()


@app.delete("/api/session/{session_id}", include_in_schema=False)
def delete_session(session_id: str) -> dict:
    """结束一个会话；页面上的“新对话”会调用这里。"""
    return {"ok": SESSIONS.reset(session_id)}


def _warm_retrieval() -> None:
    """启动时后台加载题库/BGE，避免第一道题承担模型加载的 8～10 秒。"""
    try:
        if not index_available():
            return
        load_problem_index()
        from tools import load_vector_index, vector_available

        if vector_available():
            load_vector_index()
    except Exception:
        # 预热失败不影响正常请求，后续检索会自动回退 BM25
        pass


if __name__ == "__main__":
    import uvicorn

    threading.Thread(target=_warm_retrieval, daemon=True).start()
    # 显式绑定本机地址：没有登录和限流之前，不要暴露到公网
    uvicorn.run(app, host="127.0.0.1", port=8000)
