"""题干 LaTeX 重建与展示链路测试。

运行方式（在 mathcoach 目录下执行）：
    python -m pytest test_latex.py -v
"""

import json

import pytest
from fastapi.testclient import TestClient

import tools
import web.app as web_app
from llm import LLMResponse
from rag.latex import (
    LatexStore,
    build_latex,
    clean_latex,
    parse_response,
    validate_latex,
)


# ---------- 解析与校验 ----------

def test_parse_response_accepts_array_and_fences():
    rows = [{"id": "p1", "latex": "$x$"}]
    assert parse_response(json.dumps(rows)) == rows
    assert parse_response("```json\n" + json.dumps(rows) + "\n```") == rows
    assert parse_response("结果如下：\n" + json.dumps(rows) + "\n以上。") == rows


def test_parse_response_accepts_wrapped_dict():
    rows = [{"id": "p1", "latex": "$x$"}]
    assert parse_response(json.dumps({"results": rows})) == rows


def test_parse_response_ignores_garbage():
    assert parse_response("") == []
    assert parse_response("not json") == []
    assert parse_response('{"id": "p1"}') == []


def test_validate_latex_rejects_empty_and_fences():
    assert validate_latex("x", "")[0] is False
    assert validate_latex("x", "```latex\n$x$\n```")[0] is False
    assert validate_latex("x", "$x$")[0] is True


def test_validate_latex_preserves_numbers():
    """模型漏掉题干里的数字时宁可回退原文，也不展示错题。"""
    raw = "设 x>0，求 f(2) 的值。"
    assert validate_latex(raw, "设 $x>0$，求 $f(2)$ 的值。")[0] is True
    assert validate_latex(raw, "设 $x>0$，求 $f(3)$ 的值。")[0] is False


def test_validate_latex_allows_limit_reordering():
    """PDF 先抽上限后抽下限，LaTeX 是下限在前，数字多重集相同就应通过。"""
    raw = "计算 ∫ 2 0 f(x) dx"
    latex = "计算 $\\int_0^2 f(x)\\,dx$"
    assert validate_latex(raw, latex)[0] is True


def test_validate_latex_tolerates_page_number_noise():
    """页码混进题干时最多允许少 2 个数字；再少就可能是漏条件。"""
    raw = "求 f(2) 的值。 12"      # 末尾的 12 是页码
    assert validate_latex(raw, "求 $f(2)$ 的值。")[0] is True

    raw = "设 a=1, b=2, c=3, d=4。"
    assert validate_latex(raw, "设 $a=1$。")[0] is False


def test_validate_latex_allows_inferred_zero():
    """题干末尾被 PDF 截断时，模型补上 c>0 不应被误杀。"""
    raw = "设 a>0, b>0, c>"
    assert validate_latex(raw, "设 $a>0,\\ b>0,\\ c>0$")[0] is True


# ---------- 缓存 ----------

def _problem(problem_id="p1", question="求 1+2", layout="求 1+2"):
    return {"id": problem_id, "question": question, "question_layout": layout}


def test_store_roundtrip_and_hash_invalidation(tmp_path):
    path = tmp_path / "latex.json"
    store = LatexStore.load(path)
    problem = _problem()
    store.set(problem, "求 $1+2$。", model="test")
    store.save()

    loaded = LatexStore.load(path)
    assert loaded.get(problem) == "求 $1+2$。"

    changed = _problem(question="求 2+3")
    assert loaded.get(changed) is None
    assert loaded.get(_problem(problem_id="missing")) is None


def test_store_bad_json_is_empty(tmp_path):
    path = tmp_path / "latex.json"
    path.write_text("{not json", encoding="utf-8")
    assert LatexStore.load(path).entries == {}


def test_clean_latex_moves_period_out_of_math():
    assert clean_latex(r"$\lim x = \text{。}$") == "$\\lim x =$。"
    assert clean_latex(r"$$x = 1 \text{。}$$") == "$$x = 1$$。"
    assert clean_latex(r"正文 $x$，结束。") == "正文 $x$，结束。"


# ---------- 构建 ----------

class FakeLatexClient:
    """按输入 id 返回预设 LaTeX，记录调用次数便于验证缓存跳过。"""

    def __init__(self, table):
        self.table = table
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        rows = [
            {"id": item["id"], "latex": self.table[item["id"]]}
            for item in payload
            if item["id"] in self.table
        ]
        return LLMResponse(content=json.dumps(rows, ensure_ascii=False))


def test_build_latex_writes_cache_and_skips_on_rerun(tmp_path):
    path = tmp_path / "latex.json"
    problems = [_problem("p1"), _problem("p2", "求 3+4")]
    client = FakeLatexClient({
        "p1": "求 $1+2$。",
        "p2": "求 $3+4$。",
    })

    first = build_latex(
        problems, client=client, cache_path=path, show_progress=False
    )
    assert first["ok"] == 2
    assert first["failed"] == 0
    assert client.calls == 1

    second = build_latex(
        problems, client=client, cache_path=path, show_progress=False
    )
    assert second["pending"] == 0
    assert second["skipped"] == 2
    assert client.calls == 1
    assert LatexStore.load(path).get(problems[1]) == "求 $3+4$。"


def test_build_latex_counts_invalid_outputs(tmp_path):
    path = tmp_path / "latex.json"
    problems = [_problem()]
    client = FakeLatexClient({"p1": "求 $1+3$。"})  # 数字与题干不一致

    stats = build_latex(
        problems, client=client, cache_path=path, show_progress=False
    )

    assert stats["ok"] == 0
    assert stats["failed"] == 1
    assert LatexStore.load(path).get(problems[0]) is None


class BrokenBatchClient(FakeLatexClient):
    """整批请求失败，逐题请求才成功，验证降级路径。"""

    def chat(self, messages, tools=None):
        payload = json.loads(messages[-1]["content"])
        if len(payload) > 1:
            self.calls += 1
            return LLMResponse(content="not json")
        return super().chat(messages, tools)


def test_build_latex_falls_back_to_single_item_requests(tmp_path):
    path = tmp_path / "latex.json"
    problems = [_problem("p1"), _problem("p2", "求 3+4")]
    client = BrokenBatchClient({
        "p1": "求 $1+2$。",
        "p2": "求 $3+4$。",
    })

    stats = build_latex(
        problems,
        client=client,
        cache_path=path,
        batch_size=2,
        show_progress=False,
    )

    assert stats["ok"] == 2
    assert stats["failed"] == 0


# ---------- Web 接口 ----------

@pytest.fixture(autouse=True)
def _reset_caches():
    tools._INDEX_CACHE = None
    tools._INDEX_CACHE_PATH = None
    web_app._latex_store = None
    web_app._latex_store_path = None
    yield
    tools._INDEX_CACHE = None
    tools._INDEX_CACHE_PATH = None
    web_app._latex_store = None
    web_app._latex_store_path = None


@pytest.fixture
def latex_env(tmp_path, monkeypatch):
    index_path = tmp_path / "problems.jsonl"
    problem = _problem("cmc-01-prelim-x-01", "求 1+2", "求\n1+2")
    with open(index_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            **problem,
            "source": "第1届初赛第1题",
            "stage": "初赛",
            "category": "未分类",
            "number": 1,
            "knowledge_points": ["极限"],
        }, ensure_ascii=False) + "\n")

    cache_path = tmp_path / "latex.json"
    store = LatexStore.load(cache_path)
    store.set(problem, "求 $1+2$。", model="test")
    store.save()

    monkeypatch.setenv("CMC_INDEX_PATH", str(index_path))
    monkeypatch.setenv("CMC_LATEX_PATH", str(cache_path))
    return problem


def test_paper_api_includes_display_question(latex_env):
    client = TestClient(web_app.app)
    response = client.get("/api/paper", params={
        "edition": 1,
        "stage": "初赛",
        "supplement": "",
    })

    assert response.status_code == 200
    problem = response.json()["groups"][0]["problems"][0]
    assert problem["question"] == "求 1+2"
    assert problem["display_question"] == "求 $1+2$。"


def test_get_past_problem_returns_display_latex(latex_env):
    """定位到的题目也要用构建期 LaTeX，模型拿到的不是 PDF 乱码。"""
    data = json.loads(tools.get_past_problem(
        edition=1, stage="初赛", number=1
    ))

    assert data["id"] == "cmc-01-prelim-x-01"
    assert data["question"] == "求 $1+2$。"


def test_related_problems_include_full_question_and_latex(latex_env):
    payload = json.dumps([{
        "id": "cmc-01-prelim-x-01",
        "source": "第1届初赛第1题",
        "question": "求 1+",  # 工具结果被截断时用题库全文补全
        "knowledge_points": ["极限"],
        "score": 9,
    }], ensure_ascii=False)

    hits = web_app.collect_related(
        [{"type": "tool", "name": "search_past_problems", "result": payload}],
        "题目",
        enabled=True,
    )

    assert hits[0]["question"] == "求 1+2"
    assert hits[0]["display_question"] == "求 $1+2$。"


def test_related_problems_fall_back_without_cache(latex_env, monkeypatch, tmp_path):
    monkeypatch.setenv("CMC_LATEX_PATH", str(tmp_path / "missing.json"))
    payload = json.dumps([{
        "id": "cmc-01-prelim-x-01",
        "source": "第1届初赛第1题",
        "question": "截断",
        "score": 9,
    }], ensure_ascii=False)

    hits = web_app.collect_related(
        [{"type": "tool", "name": "search_past_problems", "result": payload}],
        "题目",
        enabled=True,
    )

    assert hits[0]["question"] == "求 1+2"
    assert "display_question" not in hits[0]
