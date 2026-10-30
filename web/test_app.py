"""Web MVP 的自动化测试。

运行方式（在 mathcoach 目录下执行）：
    python -m pytest web/test_app.py -v
"""

import json

import pytest
import config
from fastapi.testclient import TestClient

import tools
import web.app as web_app
from llm import LLMResponse, StubClient, ToolCall


client = TestClient(web_app.app)


@pytest.fixture(autouse=True)
def _clear_sessions():
    """每个测试从空会话开始，避免全局存储互相污染。"""
    web_app.SESSIONS.clear()
    yield
    web_app.SESSIONS.clear()


@pytest.fixture(autouse=True)
def _clear_index_cache():
    """题库缓存按路径缓存；换临时题库时必须清掉。"""
    tools._INDEX_CACHE = None
    tools._INDEX_CACHE_PATH = None
    yield
    tools._INDEX_CACHE = None
    tools._INDEX_CACHE_PATH = None


@pytest.fixture
def paper_env(tmp_path, monkeypatch):
    """写一份最小题库，覆盖正卷、补赛和 2024 分题型。"""
    path = tmp_path / "papers.jsonl"
    rows = [
        {"id": "cmc-01-prelim-x-01", "question": "第一届初赛第一题", "source": "",
         "stage": "初赛", "category": "未分类", "number": 1, "knowledge_points": ["极限"]},
        {"id": "cmc-01-prelim-x-02", "question": "第一届初赛第二题", "source": "",
         "stage": "初赛", "category": "未分类", "number": 2, "knowledge_points": []},
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
    return path


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


# ---------- 求解接口 ----------

def test_ask_with_stub_client(monkeypatch, tmp_path):
    """用 StubClient 跑通一次问答，并返回轨迹摘要。"""
    monkeypatch.setattr(web_app, "_client", StubClient())
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    response = client.post("/api/ask", json={"question": "验证恒等式"})

    assert response.status_code == 200
    data = response.json()
    assert data["final_answer"] == "1"
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
