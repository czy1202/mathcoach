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

import json
import re
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from agent import Agent
from eval.extract import extract_final_answer
from llm import OpenAIClient
from tools import (
    index_available,
    load_problem_index,
    parse_edition,
    search_past_problems,
)
from tracing import TraceRecorder, new_trace_file
from web.sessions import SessionStore


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TRACE_DIR = BASE_DIR.parent / "traces"

MAX_QUESTION_CHARS = 5000
MAX_CONCURRENT = 2          # 本地演示，同时最多跑两道题，避免并发烧额度

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
SESSIONS = SessionStore()


def get_client():
    """懒加载模型客户端，避免模块导入时就要求配置好密钥。"""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = OpenAIClient()
    return _client


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


class AskResponse(BaseModel):
    answer: str
    display_answer: str
    session_id: str
    turn: int = 1
    follow_up: bool = False
    final_answer: str | None = None
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


# ---------- 纯函数 ----------

def normalize_math_delimiters(text: str) -> str:
    """把 LaTeX 的 \\\\(...\\\\) / \\\\[...\\\\] 统一成 $...$ / $$...$$。

    texmath 只认美元符号分隔符。负向后行断言避免把 LaTeX 换行
    ``\\\\[2pt]`` 里的 ``\\\\[`` 误当成公式开头。
    """
    text = re.sub(r"(?<!\\)\\\[", "$$", text)
    text = re.sub(r"(?<!\\)\\\]", "$$", text)
    text = re.sub(r"(?<!\\)\\\(", "$", text)
    text = re.sub(r"(?<!\\)\\\)", "$", text)
    return text


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

    if not hits and not follow_up:
        try:
            hits = _parse_hits(search_past_problems(question, limit))
        except Exception:
            hits = []

    return hits[:limit]


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
    return {
        "edition": edition,
        "year": year,
        "count": len(entries),
        "groups": index.group_problems(entries),
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
        "papers_available": index_available(),
    }


@app.get("/api/papers", response_model=PaperIndexResponse)
def list_papers() -> dict:
    """返回第 1-16 届的试卷目录，供“查看真题”页渲染选项条。"""
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

    index = load_problem_index()
    entries = index.list_problems(
        edition=edition,
        year=year,
        stage=stage or None,
        category=category or None,
        supplement=supplement,
    )
    if section:
        entries = [entry for entry in entries if entry.get("section") == section]
    if not entries:
        raise HTTPException(status_code=404, detail="没有找到符合条件的真题。")

    return {
        "edition": edition,
        "year": year,
        "count": len(entries),
        "groups": index.group_problems(entries),
    }


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

    try:
        session = SESSIONS.get_or_create(req.session_id)

        # 同一个会话串行处理，避免两条追问同时读到同一份旧上下文
        if not session.lock.acquire(timeout=0.5):
            raise HTTPException(
                status_code=409,
                detail="该会话正在处理上一条消息，请稍后再试。",
            )

        try:
            history = SESSIONS.history_for(session)
            follow_up = bool(history)
            turn = len(session.turns) + 1

            trace_path = new_trace_file(TRACE_DIR, prefix="web")
            tracer = TraceRecorder(
                case_id=f"web-{session.id[:8]}-t{turn}",
                question=question,
                path=trace_path,
            )

            # 题库没构建时不开检索工具，避免每轮都拿到“题库不存在”的工具错误
            retrieval = index_available()

            start = time.perf_counter()
            try:
                answer = Agent(client=get_client(), retrieval=retrieval).run(
                    question,
                    trace=tracer,
                    history=history,
                )
            except Exception as e:  # 单题失败不拖垮服务
                answer = f"错误：求解失败（{type(e).__name__}: {e}）"
                tracer.add("error", kind="agent_exception", message=answer)
            elapsed = time.perf_counter() - start

            tracer.finish(
                request_elapsed=round(elapsed, 4),
                session_id=session.id,
                turn=turn,
                follow_up=follow_up,
                history_messages=len(history),
                history_chars=sum(len(m["content"]) for m in history),
            )

            # 列真题不是解题，不要把正文里的数字误当成最终答案
            paper = collect_paper(tracer.events) if retrieval else None
            final_answer = extract_final_answer(
                answer,
                allow_fallback=not follow_up and paper is None,
            )
            error = tracer.error
            if error is None and answer.startswith("错误："):
                error = answer

            if error is None:
                SESSIONS.append_turn(session, question, answer)

            return AskResponse(
                answer=answer,
                display_answer=strip_answer_marker(
                    normalize_math_delimiters(answer)
                ),
                session_id=session.id,
                turn=turn,
                follow_up=follow_up,
                final_answer=final_answer,
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
            )
        finally:
            session.lock.release()
    finally:
        _slots.release()


@app.delete("/api/session/{session_id}", include_in_schema=False)
def delete_session(session_id: str) -> dict:
    """结束一个会话；页面上的“新对话”会调用这里。"""
    return {"ok": SESSIONS.reset(session_id)}


if __name__ == "__main__":
    import uvicorn

    # 显式绑定本机地址：没有登录和限流之前，不要暴露到公网
    uvicorn.run(app, host="127.0.0.1", port=8000)
