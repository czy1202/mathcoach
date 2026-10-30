"""RAG 层测试：文本归一化、BM25、试卷解析、索引检索与 Agent/Web 接线。

运行方式（在 mathcoach 目录下执行）：
    pytest -v
"""

import json

import pytest

import tools
from agent import Agent
from llm import LLMResponse
from rag.bm25 import BM25
from rag.evaluate import evaluate, load_queries
from rag.index import ProblemIndex
from rag.papers import (
    dedupe,
    parse_problem_id,
    parse_recent_text,
    parse_retypeset_text,
)
from rag.textnorm import detect_tags, normalize_for_search, tokenize
from web.app import _parse_hits, collect_related


# ---------- 归一化与分词 ----------

def test_symbol_glyphs_are_mapped_to_ascii_tokens():
    """2024/2025 PDF 的 Symbol 私用区码位要还原成可检索的记号。"""
    assert "int" in normalize_for_search(" f(x) dx")
    assert "->" in normalize_for_search("x 0")
    assert "inf" in normalize_for_search("")


def test_unicode_math_symbols_are_mapped():
    """人类写法（∫ ∑ ∞）和 LaTeX 写法走同一套规则。"""
    assert "int" in normalize_for_search("∫ x dx")
    assert "iint" in normalize_for_search("∬ f(x, y) dxdy")
    assert "sum" in normalize_for_search("∑ 1/n^2")


def test_latex_integral_with_subscript_is_not_dropped():
    r"""\int_0^1 里 int 后面是下划线，不能被当成未知命令删掉。"""
    normalized = normalize_for_search(r"\int_0^1 x^2 dx")
    assert normalized.startswith("int")
    assert "int" in tokenize(r"\int_0^1 x^2 dx")


def test_latex_aliases_unify_notation():
    r"""\infty 与 ∞、\leq 与 ≤ 要归一到同一个记号。"""
    assert "inf" in normalize_for_search(r"\sum_{n=1}^{\infty} a_n")
    assert "<=" in normalize_for_search(r"a \leq b")
    assert "->" in normalize_for_search(r"x \to 0")


def test_doc_side_artifact_restore_only_applies_to_documents():
    """PDF 里的 Z 表示积分号；查询侧不能把变量 z 也当成积分号。"""
    assert "int" in normalize_for_search("Z f(x) dx", doc_side=True)
    assert "int" not in normalize_for_search("z = x + y", doc_side=True)
    assert "int" not in normalize_for_search("Z f(x) dx")


def test_tokenize_uses_chinese_bigrams():
    """中文按二元组切分，数学术语无需词典即可匹配。"""
    tokens = tokenize("二重积分")
    assert {"二重", "重积", "积分"} <= set(tokens)


def test_superscript_digits_normalize_to_plain_digits():
    """用户打 x²，文档里是 x2，两者要能匹配。"""
    assert "x2" in normalize_for_search("x²")
    assert tokenize("x²") == tokenize("x2")


# ---------- 知识点识别 ----------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("计算二重积分，其中D是由直线x+y=1与两坐标轴围成的三角形区域", "二重积分"),
        ("求极限 lim(x→0) (tan x - sin x)/x^3", "极限"),
        ("证明不等式：当 x>0 时，ln(1+x) < x", "不等式"),
        ("求幂级数的收敛域与和函数", "级数"),
        ("求微分方程 y' + y = x 的通解", "微分方程"),
        (r"\iiint_\Omega z\,dxdydz", "三重积分"),
        (r"\oint_L P\,dx + Q\,dy", "曲线积分"),
    ],
)
def test_detect_tags_hits_expected_knowledge_point(text, expected):
    assert expected in detect_tags(text)


def test_detect_tags_ignores_generic_wording():
    """“连续函数”只是题设条件，不应打成“连续”标签。"""
    assert "连续" not in detect_tags("设f(x)是连续函数，求f'(x)")


def test_detect_tags_respects_ascii_word_boundaries():
    """point 里的 oint、slim 里的 lim 不是数学记号，不能误命中。"""
    assert "曲线积分" not in detect_tags("设 P 是曲线上的 point")
    assert "极限" not in detect_tags("a slim chance")


def test_detect_tags_returns_stable_unique_list():
    tags = detect_tags("求二重积分，并用极坐标计算该二重积分")
    assert tags.count("二重积分") == 1


# ---------- BM25 ----------

def test_bm25_ranks_matching_document_first():
    index = BM25([
        ["极限", "lim", "sin"],
        ["积分", "dx"],
        ["级数", "收敛"],
    ])
    scores = index.scores(["积分", "dx"])
    assert scores[1] > scores[0]
    assert scores[1] > scores[2]


def test_bm25_empty_query_and_empty_corpus_return_zero():
    assert BM25([["a"], ["b"]]).scores([]) == [0.0, 0.0]
    assert BM25([]).scores(["a"]) == []


# ---------- 试卷解析 ----------

_RETYPESET = """
第十六届非数学类A 类
题目1
计算二重积分，其中D是由直线x+y=1与两坐标轴围成的三角形区域。
题目2
求极限 lim(x→0) (tan x - sin x)/x^3。
"""


def test_parse_retypeset_text_builds_ids_and_sources():
    problems = parse_retypeset_text(_RETYPESET, stage_hint="初赛", source_file="x.pdf")
    assert [p["id"] for p in problems] == ["cmc-16-prelim-a-01", "cmc-16-prelim-a-02"]
    assert problems[0]["stage"] == "初赛"          # 标题没写“初赛”，用目录兜底
    assert problems[0]["category"] == "A"
    assert "第16届" in problems[0]["source"]


_RECENT = """
一、填空题（本题共6小题）
1、求极限 lim x→0 sin x / x。
2、设 z = x^2 + y^2，求 dz。
二、解答题（本题共5小题）
1、计算二重积分 ∫∫ x dxdy，其中 D 为单位圆。
"""


def test_parse_recent_text_splits_sections_and_numbers():
    problems = parse_recent_text(_RECENT, year=2024)
    assert [p["id"] for p in problems] == [
        "cmc-2024-fill-01",
        "cmc-2024-fill-02",
        "cmc-2024-solve-01",
    ]
    assert problems[0]["section"] == "填空题"
    assert problems[2]["section"] == "解答题"
    assert all(p["year"] == 2024 for p in problems)


def test_dedupe_merges_same_question_and_keeps_aliases():
    raw = [
        {"id": "a", "question": "求极限 lim x→0 sin x/x", "source": "A"},
        {"id": "b", "question": "  求极限 lim x→0 sin x/x  ", "source": "B"},
    ]
    unique = dedupe(raw)
    assert len(unique) == 1
    assert unique[0]["id"] == "a"
    assert "B" in unique[0]["source"]
    assert unique[0]["aliases"] == ["b"]


# ---------- 索引检索 ----------

def _problem(pid, question, *, stage="初赛", category="A", tags=None):
    return {
        "id": pid,
        "question": question,
        "source": f"出处 {pid}",
        "stage": stage,
        "category": category,
        "number": 1,
        "knowledge_points": tags or detect_tags(question),
    }


def _sample_index() -> ProblemIndex:
    return ProblemIndex([
        _problem("p1", "计算二重积分，其中D由直线x+y=1与两坐标轴围成", tags=["二重积分"]),
        _problem("p2", "求幂级数的收敛域与和函数", stage="决赛", tags=["级数"]),
        _problem("p3", "证明不等式：当x>0时 ln(1+x)<x", category="未分类", tags=["不等式"]),
    ])


def test_index_search_returns_most_relevant_first():
    hits = _sample_index().search("计算二重积分，其中D是三角形区域", k=3)
    assert hits[0].problem["id"] == "p1"
    assert hits[0].tag_score > 0
    if len(hits) > 1:
        assert hits[0].score > hits[1].score


def test_index_search_filters_by_stage_and_category():
    index = _sample_index()
    finals = index.search("求幂级数的收敛域", k=3, stage="决赛")
    assert [hit.problem["id"] for hit in finals] == ["p2"]

    a_like = index.search("证明不等式", k=5, category="A")
    assert "p3" in [hit.problem["id"] for hit in a_like]   # 未分类算进 A


def test_index_search_excludes_zero_scores():
    hits = _sample_index().search("完全无关的查询词")
    assert hits == []


def test_tag_weight_zero_falls_back_to_bm25():
    """评测基线要能关掉标签加权，此时总分应等于 BM25。"""
    hits = _sample_index().search("计算二重积分", k=3, tag_weight=0.0)
    assert hits[0].score == hits[0].bm25


# ---------- 试卷浏览 ----------

def test_parse_problem_id_restores_paper_position():
    """题目 id 能还原届数、阶段、类别、补赛和题号。"""
    assert parse_problem_id("cmc-01-prelim-x-01") == {
        "edition": 1,
        "stage": "初赛",
        "category": "未分类",
        "supplement": "",
        "number": 1,
    }
    assert parse_problem_id("cmc-13-prelim-s-x-09")["supplement"] == "补赛"
    assert parse_problem_id("cmc-13-prelim-s2-x-09")["supplement"] == "补赛2"
    assert parse_problem_id("cmc-16-final-b-11")["category"] == "B"


def test_parse_problem_id_rejects_unknown_formats():
    assert parse_problem_id("p1") is None
    assert parse_problem_id("cmc-2024-fill-01") is None
    assert parse_problem_id("") is None
    assert parse_problem_id(None) is None


def _paper_problem(pid, question, *, tags=None, aliases=None):
    return {
        "id": pid,
        "question": question,
        "source": "",
        "stage": "初赛",
        "category": "A",
        "number": 1,
        "knowledge_points": tags or [],
        "aliases": list(aliases or []),
    }


def _paper_index() -> ProblemIndex:
    return ProblemIndex([
        _paper_problem("cmc-13-final-x-01", "第一题"),
        _paper_problem(
            "cmc-12-final-x-02",
            "第二题（与第 13 届重复）",
            tags=["极限"],
            aliases=["cmc-13-final-x-02"],
        ),
        _paper_problem("cmc-13-final-x-03", "第三题"),
        _paper_problem("cmc-13-prelim-x-01", "初赛正卷第一题"),
        _paper_problem("cmc-13-prelim-s-x-02", "初赛补赛第二题"),
    ])


def test_list_problems_expands_deduped_aliases():
    """去重时合并到别届的题，仍应出现在它原本所属的试卷里。"""
    entries = _paper_index().list_problems(edition=13, stage="决赛")
    assert [entry["number"] for entry in entries] == [1, 2, 3]
    assert [entry["paper_id"] for entry in entries] == [
        "cmc-13-final-x-01",
        "cmc-13-final-x-02",
        "cmc-13-final-x-03",
    ]
    # 别名展开后题号连续，不应把被合并的第 2 题误报为缺失
    group = _paper_index().group_problems(entries)[0]
    assert group["missing"] == []
    assert group["complete"] is True


def test_list_problems_filters_regular_and_supplement():
    index = _paper_index()
    regular = index.list_problems(edition=13, stage="初赛", supplement="")
    supplement = index.list_problems(edition=13, stage="初赛", supplement="补赛")
    both = index.list_problems(edition=13, stage="初赛")

    assert [entry["number"] for entry in regular] == [1]
    assert [entry["number"] for entry in supplement] == [2]
    assert len(both) == 2


def test_group_problems_reports_missing_numbers():
    entries = [
        {"stage": "初赛", "category": "未分类", "supplement": "",
         "section": "", "number": 1, "paper_id": "a"},
        {"stage": "初赛", "category": "未分类", "supplement": "",
         "section": "", "number": 3, "paper_id": "b"},
    ]
    group = ProblemIndex.group_problems(entries)[0]

    assert group["numbers"] == [1, 3]
    assert group["missing"] == [2]
    assert group["complete"] is False


def test_paper_index_always_covers_editions_1_to_16():
    editions = _paper_index().paper_index()
    assert [item["edition"] for item in editions] == list(range(1, 17))
    # 样例题库只有第 13 届，其余届仍应返回空目录而不是报错
    assert editions[12]["total"] == 5
    assert editions[0]["total"] == 0
    assert editions[0]["papers"] == []


# ---------- 检索评测 ----------

def test_evaluate_maps_aliases_and_groups_tiers():
    index = ProblemIndex([
        {**_problem("p1", "计算二重积分，其中D由x+y=1围成"), "aliases": ["old-p1"]},
        _problem("p2", "求幂级数的收敛域"),
    ])
    queries = [
        {"id": "q1", "query": "计算二重积分", "expected_ids": ["old-p1"], "tier": "easy"},
        {"id": "h1", "query": "幂级数收敛域", "expected_ids": ["p2"], "tier": "hard"},
    ]
    summary = evaluate(index, queries, k=5)

    assert summary["n"] == 2
    assert summary["recall_at_1"] == 1.0
    assert summary["by_tier"]["easy"]["n"] == 1
    assert summary["by_tier"]["hard"]["recall_at_k"] == 1.0


def test_evaluate_reports_unknown_expected_ids():
    summary = evaluate(
        _sample_index(),
        [{"id": "x1", "query": "计算二重积分", "expected_ids": ["nope"], "tier": "hard"}],
        k=5,
    )
    assert summary["unknown_ids"] == ["nope"]
    assert summary["recall_at_k"] == 0.0


def test_load_queries_rejects_missing_expected(tmp_path):
    path = tmp_path / "queries.jsonl"
    path.write_text('{"query": "只有查询没有答案"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_queries(path)


# ---------- 工具层 ----------

@pytest.fixture(autouse=True)
def _clear_tool_index_cache():
    """每个用例前后清掉工具层的索引缓存，避免读到上一个临时题库。"""
    tools._INDEX_CACHE = None
    tools._INDEX_CACHE_PATH = None
    yield
    tools._INDEX_CACHE = None
    tools._INDEX_CACHE_PATH = None


def _write_index(tmp_path):
    path = tmp_path / "problems.jsonl"
    problems = [
        _problem("p1", "计算二重积分，其中D由直线x+y=1与两坐标轴围成"),
        _problem("p2", "求幂级数的收敛域与和函数"),
    ]
    with open(path, "w", encoding="utf-8") as handle:
        for problem in problems:
            handle.write(json.dumps(problem, ensure_ascii=False) + "\n")
    return path


def test_search_past_problems_returns_compact_json(tmp_path, monkeypatch):
    monkeypatch.setenv("CMC_INDEX_PATH", str(_write_index(tmp_path)))
    results = json.loads(tools.search_past_problems("计算二重积分", "1"))
    assert len(results) == 1
    assert results[0]["id"] == "p1"
    assert set(results[0]) == {"id", "source", "question", "knowledge_points", "score"}
    assert len(results[0]["question"]) <= 300


def test_search_past_problems_clamps_k_and_handles_blank(tmp_path, monkeypatch):
    monkeypatch.setenv("CMC_INDEX_PATH", str(_write_index(tmp_path)))
    # 非数字 k 回落到 3，超出范围钳到 5，都不报错
    assert json.loads(tools.search_past_problems("积分", "abc")) == json.loads(
        tools.search_past_problems("积分", "3")
    )
    assert len(json.loads(tools.search_past_problems("积分", "99"))) <= 5
    assert tools.search_past_problems("   ") == "[]"


def test_search_past_problems_without_index_returns_error_text(tmp_path, monkeypatch):
    monkeypatch.setenv("CMC_INDEX_PATH", str(tmp_path / "missing.jsonl"))
    result = tools.run_tool("search_past_problems", {"query": "极限"})
    assert isinstance(result, str)
    assert result.startswith("错误：")
    assert "题库不存在" in result
    assert "rag.build" in result


def test_index_available_reflects_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CMC_INDEX_PATH", str(tmp_path / "missing.jsonl"))
    assert tools.index_available() is False
    monkeypatch.setenv("CMC_INDEX_PATH", str(_write_index(tmp_path)))
    assert tools.index_available() is True


# ---------- 试卷浏览工具 ----------

def _write_paper_index(tmp_path):
    path = tmp_path / "papers.jsonl"
    problems = [
        {"id": "cmc-01-prelim-x-01", "question": "第一届初赛第一题", "source": "",
         "stage": "初赛", "category": "未分类", "number": 1, "knowledge_points": []},
        {"id": "cmc-01-prelim-x-02", "question": "第一届初赛第二题", "source": "",
         "stage": "初赛", "category": "未分类", "number": 2, "knowledge_points": []},
        {"id": "cmc-01-final-x-01", "question": "第一届决赛第一题", "source": "",
         "stage": "决赛", "category": "未分类", "number": 1, "knowledge_points": []},
        {"id": "cmc-2024-fill-01", "question": "2024 年填空题", "source": "2024年真题",
         "stage": "初赛", "category": "未分类", "number": 1, "year": 2024,
         "section": "填空题", "knowledge_points": []},
    ]
    with open(path, "w", encoding="utf-8") as handle:
        for problem in problems:
            handle.write(json.dumps(problem, ensure_ascii=False) + "\n")
    return path


@pytest.mark.parametrize(
    "value, expected",
    [
        (1, 1),
        ("16", 16),
        ("第一届", 1),
        ("第十六届", 16),
        ("第3届初赛", 3),
        ("十", 10),
        ("十二", 12),
        (None, None),
        ("", None),
    ],
)
def test_parse_edition_accepts_common_forms(value, expected):
    assert tools.parse_edition(value) == expected


def test_parse_edition_rejects_bool():
    """True 会被当成 1，届数工具里应明确拒绝。"""
    with pytest.raises(ValueError):
        tools.parse_edition(True)


def test_list_past_problems_returns_counts_without_question_text(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CMC_INDEX_PATH", str(_write_paper_index(tmp_path)))
    data = json.loads(tools.list_past_problems(edition="第一届"))

    assert data["edition"] == 1
    assert data["count"] == 3
    assert [group["label"] for group in data["groups"]] == ["初赛", "决赛"]
    assert data["groups"][0]["numbers"] == [1, 2]
    assert data["groups"][0]["complete"] is True
    # 工具结果只带题号和出处，完整题干由 Web 端另行读取
    assert "question" not in data["groups"][0]


def test_list_past_problems_filters_by_year_and_section(tmp_path, monkeypatch):
    monkeypatch.setenv("CMC_INDEX_PATH", str(_write_paper_index(tmp_path)))
    data = json.loads(tools.list_past_problems(year="2024 年"))

    assert data["year"] == 2024
    assert data["count"] == 1
    assert data["groups"][0]["section"] == "填空题"


def test_list_past_problems_requires_edition_or_year(tmp_path, monkeypatch):
    monkeypatch.setenv("CMC_INDEX_PATH", str(_write_paper_index(tmp_path)))
    data = json.loads(tools.list_past_problems())

    assert "error" in data
    assert data["available_editions"] == list(range(1, 17))


def test_list_past_problems_reports_empty_result(tmp_path, monkeypatch):
    monkeypatch.setenv("CMC_INDEX_PATH", str(_write_paper_index(tmp_path)))
    data = json.loads(tools.list_past_problems(edition=9))

    assert "没有找到" in data["error"]


# ---------- Agent 接线 ----------

class _RecordingClient:
    def __init__(self):
        self.tools_seen = []
        self.messages_seen = []

    def chat(self, messages, tools=None):
        self.tools_seen.append(tools)
        self.messages_seen.append(messages)
        return LLMResponse(content="最终答案：1")


def _tool_names(schema):
    return [item["function"]["name"] for item in schema or []]


def test_agent_hides_retrieval_tool_by_default():
    client = _RecordingClient()
    Agent(client=client).run("测试题")
    assert "search_past_problems" not in _tool_names(client.tools_seen[0])
    assert "search_past_problems" not in client.messages_seen[0][0]["content"]


def test_agent_includes_retrieval_tool_when_enabled():
    client = _RecordingClient()
    Agent(client=client, retrieval=True).run("测试题")
    assert "search_past_problems" in _tool_names(client.tools_seen[0])
    assert "search_past_problems" in client.messages_seen[0][0]["content"]


def test_retrieval_tool_only_requires_query():
    client = _RecordingClient()
    Agent(client=client, retrieval=True).run("测试题")
    schema = next(
        item for item in client.tools_seen[0]
        if item["function"]["name"] == "search_past_problems"
    )
    assert schema["function"]["parameters"]["required"] == ["query"]


def test_agent_exposes_paper_listing_tool_when_enabled():
    """查看整卷走独立工具，且所有参数都可选（届数/年份二选一）。"""
    client = _RecordingClient()
    Agent(client=client, retrieval=True).run("我要第一届真题")

    schema = next(
        item for item in client.tools_seen[0]
        if item["function"]["name"] == "list_past_problems"
    )
    assert schema["function"]["parameters"]["required"] == []
    system_prompt = client.messages_seen[0][0]["content"]
    assert "list_past_problems" in system_prompt
    assert "不要逐题解答" in system_prompt


# ---------- Web 层：相关真题 ----------

def _tool_event(result):
    return {"type": "tool", "name": "search_past_problems", "result": result}


def test_parse_hits_ignores_bad_payloads():
    assert _parse_hits("not json") == []
    assert _parse_hits("{}") == []
    assert _parse_hits("[]") == []
    assert _parse_hits('[{"id": "p1"}]') == []       # 没有题干，不展示


def test_collect_related_extracts_and_dedupes_tool_events():
    payload = json.dumps(
        [
            {"id": "p1", "source": "第1届", "question": "题一", "knowledge_points": ["极限"], "score": 9},
            {"id": "p1", "source": "第1届", "question": "题一", "knowledge_points": ["极限"], "score": 9},
            {"id": "p2", "source": "第2届", "question": "题二", "knowledge_points": [], "score": 5},
        ],
        ensure_ascii=False,
    )
    hits = collect_related([_tool_event(payload)], "题目", enabled=True)
    assert [hit["id"] for hit in hits] == ["p1", "p2"]
    assert hits[0]["knowledge_points"] == ["极限"]


def test_collect_related_disabled_returns_empty():
    payload = json.dumps([{"id": "p1", "question": "题一"}])
    assert collect_related([_tool_event(payload)], "题目", enabled=False) == []


def test_collect_related_falls_back_for_new_question(monkeypatch):
    import web.app as web_app

    monkeypatch.setattr(
        web_app,
        "search_past_problems",
        lambda query, k=3: json.dumps(
            [{"id": "p9", "source": "第9届", "question": "题九", "score": 1}],
            ensure_ascii=False,
        ),
    )
    hits = collect_related([], "新题目", enabled=True, follow_up=False)
    assert [hit["id"] for hit in hits] == ["p9"]


def test_collect_related_does_not_fall_back_for_follow_up(monkeypatch):
    import web.app as web_app

    called = []
    monkeypatch.setattr(
        web_app,
        "search_past_problems",
        lambda query, k=3: called.append(query) or "[]",
    )
    assert collect_related([], "这一步为什么", enabled=True, follow_up=True) == []
    assert called == []
