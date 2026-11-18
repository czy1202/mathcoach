"""Web MVP 的自动化测试。

运行方式（在 mathcoach 目录下执行）：
    python -m pytest web/test_app.py -v
"""

import json
import threading

import pytest
import config
from fastapi.testclient import TestClient

import tools
import web.app as web_app
from llm import LLMResponse, StubClient, ToolCall
from web.sessions import SessionStore


client = TestClient(web_app.app)


@pytest.fixture(autouse=True)
def _clear_sessions(monkeypatch):
    """每个测试从空的内存会话开始，不触碰本地持久化数据库。"""
    monkeypatch.setattr(web_app, "SESSIONS", SessionStore())
    monkeypatch.setattr(
        web_app,
        "STEP_SESSIONS",
        SessionStore(
            max_sessions=200,
            max_history_turns=4,
            max_history_chars=24000,
            max_assistant_chars=4000,
        ),
    )
    with web_app._CANCEL_LOCK:
        web_app._CANCEL_EVENTS.clear()
    yield
    with web_app._CANCEL_LOCK:
        web_app._CANCEL_EVENTS.clear()


@pytest.fixture(autouse=True)
def _clear_index_cache():
    """题库缓存按路径缓存；换临时题库时必须清掉。"""
    tools._INDEX_CACHE = None
    tools._INDEX_CACHE_PATH = None
    tools._INDEX_CACHE_ANSWERS_MTIME = None
    yield
    tools._INDEX_CACHE = None
    tools._INDEX_CACHE_PATH = None
    tools._INDEX_CACHE_ANSWERS_MTIME = None


@pytest.fixture
def paper_env(tmp_path, monkeypatch):
    """写一份最小题库，覆盖正卷、补赛和 2024 分题型。"""
    path = tmp_path / "papers.jsonl"
    rows = [
        {"id": "cmc-01-prelim-x-01", "question": "第一届初赛第一题", "source": "",
         "stage": "初赛", "category": "未分类", "number": 1, "knowledge_points": ["极限"],
         "answer": "$1$", "solution": "由题意得 $1$。"},
        {"id": "cmc-01-prelim-x-02", "question": "第一届初赛第二题", "source": "",
         "stage": "初赛", "category": "未分类", "number": 2, "knowledge_points": [],
         "answer": r"\frac{1}{2}", "solution": r"由定义得 \frac{1}{2}。"},
        {"id": "cmc-01-final-x-01", "question": "第一届决赛第一题", "source": "",
         "stage": "决赛", "category": "未分类", "number": 1, "knowledge_points": []},
        {"id": "cmc-13-prelim-x-01", "question": "第十三届初赛第一题", "source": "",
         "stage": "初赛", "category": "未分类", "number": 1, "knowledge_points": []},
        {"id": "cmc-13-prelim-s-x-02", "question": "第十三届初赛补赛第二题", "source": "",
         "stage": "初赛", "category": "未分类", "number": 2, "knowledge_points": []},
        {"id": "cmc-2024-fill-01", "question": "2024 年填空题", "source": "2024年真题",
         "stage": "初赛", "category": "未分类", "number": 1, "year": 2024,
         "section": "填空题", "knowledge_points": []},
    ]
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    monkeypatch.setenv("CMC_INDEX_PATH", str(path))
    # 测试题库自带答案；不要让本地真实的 answers.jsonl 覆盖它
    monkeypatch.setenv("CMC_ANSWERS_PATH", str(tmp_path / "answers.jsonl"))
    return path


def test_ensure_display_math_wraps_bare_latex():
    """OCR/模型摘录的裸 LaTeX 要补上美元符号，前端才会走 KaTeX。"""
    assert web_app.ensure_display_math(r"\frac{1}{2}") == r"$\frac{1}{2}$"
    assert web_app.ensure_display_math(r"2\pi") == r"$2\pi$"


def test_ensure_display_math_keeps_existing_delimiters():
    """已有 $...$ / $$...$$ 的内容不能重复包裹。"""
    assert web_app.ensure_display_math(r"$x=1$") == r"$x=1$"
    assert web_app.ensure_display_math(r"答案：$$\frac{1}{2}$$") == r"答案：$$\frac{1}{2}$$"


def test_ensure_display_math_splits_chinese_punctuation():
    """中文和中文标点留在公式外，避免 KaTeX 排进数学模式。"""
    result = web_app.ensure_display_math(
        r"g'(x)=\frac{1}{x}，g'(0)=\frac A2；在x=0处连续"
    )
    assert r"$g'(x)=\frac{1}{x}$" in result
    assert r"$g'(0)=\frac A2$" in result
    assert "，$g" in result
    assert "；在" in result


def test_ensure_display_math_leaves_plain_text():
    """纯中文/纯文字答案保持原样。"""
    assert web_app.ensure_display_math("得证") == "得证"
    assert web_app.ensure_display_math("") == ""


def test_get_paper_normalizes_bare_latex(paper_env):
    """试卷接口返回的官方答案也要经过同一套公式定界符清洗。"""
    response = client.get("/api/paper", params={
        "edition": 1,
        "stage": "初赛",
        "category": "未分类",
        "supplement": "",
    })
    assert response.status_code == 200
    problem = response.json()["groups"][0]["problems"][1]
    assert problem["answer"] == r"$\frac{1}{2}$"
    assert r"$\frac{1}{2}$" in problem["solution"]


class CountingClient:
    """返回“本轮看到了几个用户消息”，用来验证历史是否真的传给了模型。"""

    def __init__(self) -> None:
        self.user_counts: list[int] = []

    def chat(self, messages, tools=None):
        count = sum(1 for m in messages if m["role"] == "user")
        self.user_counts.append(count)
        return LLMResponse(content=f"本轮看到 {count} 个用户消息\n最终答案：{count}")


class PlainFollowupClient:
    """第一轮给完整答案，第二轮像真实追问一样不带最终答案行。"""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content="完整解答\n最终答案：1")
        return LLMResponse(content="这一步用了分部积分，因为被积函数是多项式乘指数函数。")


# ---------- 页面与健康检查 ----------

def test_index_returns_page():
    """根路径应返回单页应用。"""
    response = client.get("/")

    assert response.status_code == 200
    assert "mathcoach" in response.text
    assert "app.js" in response.text


def test_health_does_not_expose_key():
    """健康检查只报告是否配置了密钥，不返回密钥本身。"""
    response = client.get("/api/health")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert "key_set" in data
    assert "OPENAI_API_KEY" not in data


# ---------- 真题浏览 ----------

def test_papers_index_lists_sixteen_editions(paper_env):
    """“查看真题”页需要第 1-16 届的完整目录。"""
    response = client.get("/api/papers")

    assert response.status_code == 200
    editions = response.json()["editions"]
    assert [item["edition"] for item in editions] == list(range(1, 17))
    assert editions[0]["total"] == 3
    assert [paper["label"] for paper in editions[0]["papers"]] == ["初赛", "决赛"]
    assert editions[1]["total"] == 0


def test_get_paper_returns_full_questions(paper_env):
    """试卷接口直接返回题库原文，不经过模型改写。"""
    response = client.get("/api/paper", params={
        "edition": 1,
        "stage": "初赛",
        "category": "未分类",
        "supplement": "",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    assert len(data["groups"]) == 1
    group = data["groups"][0]
    assert group["label"] == "初赛"
    assert group["numbers"] == [1, 2]
    assert group["complete"] is True
    assert group["problems"][0]["question"] == "第一届初赛第一题"
    assert group["problems"][0]["knowledge_points"] == ["极限"]
    assert group["problems"][0]["answer"] == "$1$"
    assert group["problems"][0]["solution"] == "由题意得 $1$。"


def test_paper_index_includes_seventeenth_edition(tmp_path, monkeypatch):
    """题库里出现第 17 届时，目录要自动延长到第 17 届。"""
    path = tmp_path / "papers17.jsonl"
    row = {
        "id": "cmc-17-prelim-a-01",
        "question": "第十七届初赛 A 类第一题",
        "display_ready": True,
        "answer": "$a=-\\frac13$。",
        "solution": "由 Taylor 展开可得。",
        "stage": "初赛",
        "edition": 17,
        "year": 2025,
        "category": "A",
        "supplement": "",
        "number": 1,
        "knowledge_points": ["极限"],
    }
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setenv("CMC_INDEX_PATH", str(path))
    monkeypatch.setenv("CMC_LATEX_PATH", str(tmp_path / "missing-latex.json"))

    editions = client.get("/api/papers").json()["editions"]
    assert [item["edition"] for item in editions] == list(range(1, 18))
    assert editions[-1]["total"] == 1
    assert editions[-1]["papers"][0]["label"] == "初赛 A类"

    data = client.get("/api/paper", params={"edition": 17}).json()
    problem = data["groups"][0]["problems"][0]
    assert problem["answer"] == "$a=-\\frac13$。"
    assert problem["solution"] == "由 Taylor 展开可得。"
    # 没有 LaTeX 缓存时，带 display_ready 的整理题干直接用于展示。
    assert problem["display_question"] == "第十七届初赛 A 类第一题"


def test_get_paper_filters_supplement(paper_env):
    regular = client.get("/api/paper", params={
        "edition": 13,
        "stage": "初赛",
        "supplement": "",
    }).json()
    supplement = client.get("/api/paper", params={
        "edition": 13,
        "stage": "初赛",
        "supplement": "补赛",
    }).json()

    assert regular["count"] == 1
    assert regular["groups"][0]["problems"][0]["supplement"] == ""
    assert supplement["count"] == 1
    assert supplement["groups"][0]["problems"][0]["supplement"] == "补赛"


def test_get_paper_filters_section_by_year(paper_env):
    response = client.get("/api/paper", params={"year": 2024, "section": "填空题"})

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["groups"][0]["section"] == "填空题"


def test_get_paper_requires_edition_or_year():
    response = client.get("/api/paper")

    assert response.status_code == 400
    assert "届数或年份" in response.json()["detail"]


def test_get_paper_returns_404_for_unknown_edition(paper_env):
    response = client.get("/api/paper", params={"edition": 99})

    assert response.status_code == 404


def test_paper_endpoints_return_503_without_index(tmp_path, monkeypatch):
    monkeypatch.setenv("CMC_INDEX_PATH", str(tmp_path / "missing.jsonl"))

    assert client.get("/api/papers").status_code == 503
    assert client.get("/api/paper", params={"edition": 1}).status_code == 503


def test_collect_paper_rebuilds_from_tool_args(paper_env):
    """Agent 只拿到题号时，Web 层按同一组参数重新读题库补全题干。"""
    events = [{
        "type": "tool",
        "name": "list_past_problems",
        "args": {"edition": "第一届"},
    }]
    payload = web_app.collect_paper(events)

    assert payload["edition"] == 1
    assert payload["count"] == 3
    assert payload["groups"][0]["problems"][0]["question"] == "第一届初赛第一题"


def test_collect_paper_ignores_unusable_events(paper_env):
    assert web_app.collect_paper([]) is None
    assert web_app.collect_paper([{
        "type": "tool",
        "name": "list_past_problems",
        "args": {},
    }]) is None
    assert web_app.collect_paper([{
        "type": "tool",
        "name": "search_past_problems",
        "args": {"query": "极限"},
    }]) is None


class PaperClient:
    """第一轮调用列卷工具，第二轮给出简短说明。"""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        name="list_past_problems",
                        args={"edition": "第一届"},
                        id="call_1",
                    )
                ],
            )
        return LLMResponse(content="找到 3 道题，按初赛和决赛分组。")


def test_ask_returns_paper_instead_of_answer_card(paper_env, monkeypatch, tmp_path):
    """“我要第一届真题”应返回完整试卷，而不是把正文数字当成最终答案。"""
    monkeypatch.setattr(web_app, "_client", PaperClient())
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    response = client.post("/api/ask", json={"question": "我要第一届真题"})

    assert response.status_code == 200
    data = response.json()
    assert data["paper"]["edition"] == 1
    assert data["paper"]["count"] == 3
    assert data["paper"]["groups"][0]["problems"][0]["question"] == "第一届初赛第一题"
    assert data["final_answer"] is None
    assert data["related_problems"] == []
    assert [tool["name"] for tool in data["tools"]] == ["list_past_problems"]


class ClarifyClient:
    """定位到多份试卷后，模型反问用户要初赛还是决赛。"""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        name="get_past_problem",
                        args={"edition": "第一届", "position": "last"},
                        id="call_1",
                    )
                ],
            )
        return LLMResponse(
            content="初赛和决赛各有一道第 2 题。请确认你要哪一场？"
        )


def test_ask_clarification_has_no_answer_card(paper_env, monkeypatch, tmp_path):
    """反问用户场次时，正文里的题号不能被抽成“最终答案”。"""
    monkeypatch.setattr(web_app, "_client", ClarifyClient())
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    response = client.post("/api/ask", json={"question": "第一届压轴题"})

    assert response.status_code == 200
    data = response.json()
    assert [tool["name"] for tool in data["tools"]] == ["get_past_problem"]
    assert data["final_answer"] is None
    assert data["related_problems"] == []


# ---------- 求解接口 ----------

def test_ask_with_stub_client(monkeypatch, tmp_path):
    """用 StubClient 跑通一次问答，并返回轨迹摘要。"""
    monkeypatch.setattr(web_app, "_client", StubClient())
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    response = client.post("/api/ask", json={"question": "验证恒等式"})

    assert response.status_code == 200
    data = response.json()
    assert data["final_answer"] == "1"
    assert data["final_answer_display"] == "1"
    assert "最终答案：1" in data["answer"]
    assert "最终答案：" not in data["display_answer"]
    assert data["llm_calls"] == 2
    assert data["tool_calls"] == 1
    assert data["tools"][0]["name"] == "verify_with_sympy"
    assert data["steps"]
    assert data["session_id"]
    assert data["turn"] == 1
    assert data["follow_up"] is False
    assert (tmp_path / data["trace_file"]).exists()


def test_ask_returns_renderable_final_answer(monkeypatch, tmp_path):
    """最终答案卡片要拿到保留 $...$ 的展示版，而不是判分器格式。"""

    class FormulaClient:
        def chat(self, messages, tools=None):
            return LLMResponse(
                content="推导略。\n\n最终答案：$\\lim_{n\\to\\infty}x_n=b$"
            )

    monkeypatch.setattr(web_app, "_client", FormulaClient())
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    response = client.post("/api/ask", json={"question": "求极限"})

    assert response.status_code == 200
    data = response.json()
    assert data["final_answer"] == "lim_{ntoinfty}x_n=b"
    assert data["final_answer_display"] == "$\\lim_{n\\to\\infty}x_n=b$"


def test_ask_rejects_blank_question():
    """只有空白的题目应被拒绝。"""
    response = client.post("/api/ask", json={"question": "   "})

    assert response.status_code == 400
    assert "不能为空" in response.json()["detail"]


def test_ask_rejects_too_long_question():
    """超长题目由请求模型直接拒绝。"""
    response = client.post("/api/ask", json={"question": "x" * 5001})

    assert response.status_code == 422


def test_ask_without_api_key(monkeypatch):
    """未配置密钥时应返回明确的 503，而不是 500。"""
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(web_app, "_client", None)

    response = client.post("/api/ask", json={"question": "1+1"})

    assert response.status_code == 503
    assert "OPENAI_API_KEY" in response.json()["detail"]


# ---------- 多轮会话 ----------

def test_same_session_keeps_context(monkeypatch, tmp_path):
    """带同一个 session_id 的第二问，应该看到上一轮问答。"""
    model = CountingClient()
    monkeypatch.setattr(web_app, "_client", model)
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    first = client.post("/api/ask", json={"question": "原题"}).json()
    second = client.post("/api/ask", json={
        "question": "追问",
        "session_id": first["session_id"],
    }).json()

    assert first["turn"] == 1
    assert first["follow_up"] is False
    assert second["session_id"] == first["session_id"]
    assert second["turn"] == 2
    assert second["follow_up"] is True
    assert model.user_counts == [1, 2]
    assert second["final_answer"] == "2"


def test_sessions_are_isolated(monkeypatch, tmp_path):
    """两个独立会话不应互相看到对方的上下文。"""
    model = CountingClient()
    monkeypatch.setattr(web_app, "_client", model)
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    first = client.post("/api/ask", json={"question": "题目 A"}).json()
    second = client.post("/api/ask", json={"question": "题目 B"}).json()

    assert first["session_id"] != second["session_id"]
    assert first["turn"] == second["turn"] == 1
    assert model.user_counts == [1, 1]


def test_unknown_session_id_starts_fresh(monkeypatch, tmp_path):
    """服务重启后旧 id 失效，应自动开始新会话而不是报错。"""
    model = CountingClient()
    monkeypatch.setattr(web_app, "_client", model)
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    response = client.post("/api/ask", json={
        "question": "旧会话里的新问题",
        "session_id": "0" * 32,
    })

    assert response.status_code == 200
    data = response.json()
    assert data["session_id"] != "0" * 32
    assert data["turn"] == 1
    assert data["follow_up"] is False


def test_get_session_restores_turns_and_steps(monkeypatch, tmp_path):
    """刷新页面后应能取回问答、最终答案和可点击步骤。"""
    monkeypatch.setattr(web_app, "_client", SolutionClient())
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    created = client.post("/api/ask", json={"question": "求积分"}).json()
    response = client.get(f"/api/session/{created['session_id']}")

    assert response.status_code == 200
    data = response.json()
    assert data["session_id"] == created["session_id"]
    assert data["original_question"] == "求积分"
    assert len(data["turns"]) == 1
    turn = data["turns"][0]
    assert turn["id"] == created["turn_id"]
    assert turn["question"] == "求积分"
    assert turn["follow_up"] is False
    assert turn["final_answer"] == "1"
    assert [step["label"] for step in turn["solution"]["steps"]] == [
        "第一步", "第二步",
    ]


def test_get_session_returns_404_for_unknown_id():
    response = client.get(f"/api/session/{'0' * 32}")

    assert response.status_code == 404
    assert "已过期" in response.json()["detail"]


def test_delete_session_resets_context(monkeypatch, tmp_path):
    """删除会话后再用旧 id 提问，应该从第一轮重新开始。"""
    model = CountingClient()
    monkeypatch.setattr(web_app, "_client", model)
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    first = client.post("/api/ask", json={"question": "原题"}).json()
    deleted = client.delete(f"/api/session/{first['session_id']}")

    second = client.post("/api/ask", json={
        "question": "重新开始",
        "session_id": first["session_id"],
    }).json()

    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True
    assert second["session_id"] != first["session_id"]
    assert second["turn"] == 1
    assert model.user_counts == [1, 1]


def test_followup_without_marker_has_no_answer_card(monkeypatch, tmp_path):
    """追问回答没有“最终答案：”行时，不应把正文误当成答案。"""
    model = PlainFollowupClient()
    monkeypatch.setattr(web_app, "_client", model)
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    first = client.post("/api/ask", json={"question": "原题"}).json()
    second = client.post("/api/ask", json={
        "question": "这一步为什么？",
        "session_id": first["session_id"],
    }).json()

    assert first["final_answer"] == "1"
    assert second["follow_up"] is True
    assert second["final_answer"] is None
    assert "分部积分" in second["display_answer"]


# ---------- 分步解答与步骤追问 ----------

FULL_SOLUTION = """## 涉及的知识点

- 分部积分

## 解题思路

先求原函数，再代入上下限。

## 分步解题过程

### 第一步：求原函数

由分部积分得 $\\int x e^x\\,dx = (x-1)e^x + C$。

### 第二步：代入上下限

所以 $\\int_0^1 x e^x\\,dx = 1$。

## 最终答案

最终答案：$1$
"""


class SolutionClient:
    """主对话返回带步骤的完整解答，侧栏返回只解释某一步的短回答。"""

    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[list[dict]] = []

    def chat(self, messages, tools=None):
        self.calls += 1
        self.requests.append([dict(message) for message in messages])
        if self.calls == 1:
            return LLMResponse(content=FULL_SOLUTION)
        return LLMResponse(
            content="因为 $x e^x$ 是多项式乘指数函数，所以用分部积分。"
        )


def _step_payload(parent: dict, **overrides) -> dict:
    payload = {
        "question": "这一步为什么想到分部积分？",
        "parent_session_id": parent["session_id"],
        "parent_turn_id": parent["turn_id"],
        "parent_turn": parent["turn"],
        "step_index": 1,
    }
    payload.update(overrides)
    return payload


def test_ask_returns_parsed_solution_steps(monkeypatch, tmp_path):
    """完整解答应带上前端可点击的步骤结构。"""
    model = SolutionClient()
    monkeypatch.setattr(web_app, "_client", model)
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    response = client.post("/api/ask", json={"question": "求积分"})

    assert response.status_code == 200
    data = response.json()
    assert data["turn_id"]
    assert data["solution"]["intro"].startswith("## 涉及的知识点")
    assert [step["label"] for step in data["solution"]["steps"]] == [
        "第一步", "第二步",
    ]
    assert [step["title"] for step in data["solution"]["steps"]] == [
        "求原函数", "代入上下限",
    ]
    assert "## 最终答案" not in data["solution"]["steps"][-1]["markdown"]
    assert data["solution"]["outro"] == ""


def test_ask_step_does_not_touch_main_session(monkeypatch, tmp_path):
    """侧栏追问只能写 STEP_SESSIONS，不能改变主对话的消息和轮次。"""
    model = SolutionClient()
    monkeypatch.setattr(web_app, "_client", model)
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    parent = client.post("/api/ask", json={"question": "求积分"}).json()
    session = web_app.SESSIONS.get(parent["session_id"])
    before = list(session.turns)

    response = client.post("/api/ask-step", json=_step_payload(parent))

    assert response.status_code == 200
    data = response.json()
    assert "分部积分" in data["display_answer"]
    assert data["step_label"] == "第一步"
    assert data["step_session_id"]
    assert list(session.turns) == before

    thread = web_app.STEP_SESSIONS.get(data["step_session_id"])
    assert thread is not None
    assert len(thread.turns) == 2  # 一条阅读材料 + 一条真实追问
    assert thread.turns[1]["question"].startswith("请只针对我下面引用的步骤作答")

    # 送给模型的最后一条消息要带引用步骤和用户问题
    step_request = model.requests[-1]
    assert "【引用步骤】" in step_request[-1]["content"]
    assert "求原函数" in step_request[-1]["content"]
    assert "这一步为什么想到分部积分" in step_request[-1]["content"]


def test_ask_step_thread_can_continue(monkeypatch, tmp_path):
    """同一个 step_session_id 可以继续追问，仍然不进入主会话。"""
    model = SolutionClient()
    monkeypatch.setattr(web_app, "_client", model)
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    parent = client.post("/api/ask", json={"question": "求积分"}).json()
    first = client.post("/api/ask-step", json=_step_payload(parent)).json()
    main_turns = list(web_app.SESSIONS.get(parent["session_id"]).turns)

    second = client.post("/api/ask-step", json=_step_payload(
        parent,
        question="再解释得细一点",
        step_session_id=first["step_session_id"],
    )).json()

    assert second["step_session_id"] == first["step_session_id"]
    assert second["turn"] == 2
    assert len(web_app.STEP_SESSIONS.get(second["step_session_id"]).turns) == 3
    assert list(web_app.SESSIONS.get(parent["session_id"]).turns) == main_turns


def test_ask_step_falls_back_to_parent_turn(monkeypatch, tmp_path):
    """旧前端没有 turn_id 时，仍可用主对话轮次定位回答。"""
    model = SolutionClient()
    monkeypatch.setattr(web_app, "_client", model)
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    parent = client.post("/api/ask", json={"question": "求积分"}).json()
    payload = _step_payload(parent, parent_turn_id=None, parent_turn=1)

    response = client.post("/api/ask-step", json=payload)

    assert response.status_code == 200


def test_ask_step_rejects_unknown_parent(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app, "_client", SolutionClient())
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    response = client.post("/api/ask-step", json={
        "question": "这一步为什么？",
        "parent_session_id": "0" * 32,
        "parent_turn": 1,
        "step_index": 1,
    })

    assert response.status_code == 404
    assert "原对话已过期" in response.json()["detail"]


def test_ask_step_uses_fallback_text_when_no_headings(monkeypatch, tmp_path):
    """回答没有步骤标题时，用前端传来的步骤原文仍然可以追问。"""
    monkeypatch.setattr(web_app, "_client", PlainFollowupClient())
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    parent = client.post("/api/ask", json={"question": "原题"}).json()
    assert parent["solution"] is None

    response = client.post("/api/ask-step", json={
        "question": "这里为什么？",
        "parent_session_id": parent["session_id"],
        "parent_turn_id": parent["turn_id"],
        "step_index": 1,
        "step_text": "完整解答中的第一行推导",
    })

    assert response.status_code == 200


# ---------- 展示辅助函数 ----------

def test_normalize_math_delimiters():
    """LaTeX 分隔符应统一成美元符号，同时保护换行写法。"""
    assert web_app.normalize_math_delimiters(r"\(x^2\)") == "$x^2$"
    assert web_app.normalize_math_delimiters(r"\[x^2\]") == "$$x^2$$"
    assert web_app.normalize_math_delimiters(r"\\[2pt]") == r"\\[2pt]"


def test_strip_answer_marker():
    """展示文本应去掉最后一行答案标记。"""
    text = "解题过程\n\n最终答案：1/2"
    assert web_app.strip_answer_marker(text) == "解题过程"


def test_strip_answer_marker_keeps_body_when_missing():
    """没有答案标记时不应改动正文。"""
    assert web_app.strip_answer_marker("计算结果是 1/2。") == "计算结果是 1/2。"


def test_strip_answer_marker_removes_empty_trailing_heading():
    """去掉答案标记后，结尾空标题不应留下。"""
    text = "解题过程\n\n## 最终答案\n\n最终答案：1/2"
    assert web_app.strip_answer_marker(text) == "解题过程"


# ---------- 停止生成 ----------

def test_cancel_endpoint_sets_registered_event():
    """浏览器点停止后，服务端对应请求的取消事件应被置位。"""
    event = web_app.register_cancel("req-test-cancel")
    assert event is not None
    assert not event.is_set()

    response = client.post(
        "/api/ask/cancel",
        json={"request_id": "req-test-cancel"},
    )

    assert response.status_code == 200
    assert response.json() == {"cancelled": True}
    assert event.is_set()
    web_app.drop_cancel("req-test-cancel", event)


def test_cancel_unknown_request_is_noop():
    """请求已经结束时再次取消不应报错。"""
    response = client.post(
        "/api/ask/cancel",
        json={"request_id": "req-does-not-exist"},
    )

    assert response.status_code == 200
    assert response.json() == {"cancelled": False}


def test_cancel_rejects_invalid_request_id():
    response = client.post(
        "/api/ask/cancel",
        json={"request_id": "bad id!"},
    )

    assert response.status_code == 422


class SelfCancellingClient:
    """模型返回时，用户已经点了停止。"""

    def chat(self, messages, tools=None):
        web_app.request_cancel("req-e2e-cancel")
        return LLMResponse(content="最终答案：1")


def test_ask_cancelled_does_not_append_turn(monkeypatch, tmp_path):
    """取消后不能把半截回答写进会话，下一轮也不应带这段历史。"""
    monkeypatch.setattr(web_app, "_client", SelfCancellingClient())
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    response = client.post(
        "/api/ask",
        json={"question": "测试取消", "request_id": "req-e2e-cancel"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["error"] == "已停止生成。"
    assert data["answer"] == "已停止生成。"
    assert data["turn_id"] is None


class BlockingClient:
    """模型调用卡住，用来模拟用户点停止时服务端线程还没返回。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def chat(self, messages, tools=None):
        self.started.set()
        self.release.wait(3)
        return LLMResponse(content="最终答案：旧回答")


def test_cancel_releases_session_for_immediate_followup(monkeypatch, tmp_path):
    """停止后旧请求仍在跑，用户也应能立刻追问，且旧结果不能写回。"""
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)
    monkeypatch.setattr(web_app, "_client", StubClient())

    first = client.post("/api/ask", json={"question": "原题"}).json()
    session_id = first["session_id"]

    blocking = BlockingClient()
    monkeypatch.setattr(web_app, "_client", blocking)
    result: dict = {}

    def run_old_request() -> None:
        try:
            result["data"] = web_app.ask(web_app.AskRequest(
                question="旧问题",
                session_id=session_id,
                request_id="req-blocking",
            ))
        except Exception as exc:  # pragma: no cover - 失败时便于定位
            result["error"] = exc

    worker = threading.Thread(target=run_old_request)
    worker.start()
    assert blocking.started.wait(3)

    cancelled = client.post(
        "/api/ask/cancel",
        json={"request_id": "req-blocking"},
    )
    assert cancelled.json() == {"cancelled": True}

    # 旧模型调用还没返回，但会话已释放，新追问应直接成功。
    monkeypatch.setattr(web_app, "_client", StubClient())
    second = web_app.ask(web_app.AskRequest(
        question="新问题",
        session_id=session_id,
    ))
    assert second.error is None
    assert second.turn == 2

    blocking.release.set()
    worker.join(5)
    assert not worker.is_alive()

    session = web_app.SESSIONS.get(session_id)
    assert [turn["question"] for turn in session.turns] == ["原题", "新问题"]
    assert result["data"].error == "已停止生成。"
    assert result["data"].turn_id is None
