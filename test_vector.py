"""向量检索与 RRF 融合测试。

全部用假编码器，不下载 BGE 模型；模型相关的失败分支通过文件/环境变量模拟。
"""

import json

import numpy as np
import pytest

import tools
from rag.evaluate import evaluate
from rag.hybrid import hybrid_search, rrf_fuse, vector_search
from rag.index import ProblemIndex
from rag.vector import (
    VectorCacheError,
    VectorIndex,
    build_vector_index,
    embedding_text,
)


def _problem(pid, question, tags=None):
    return {
        "id": pid,
        "question": question,
        "source": f"出处 {pid}",
        "stage": "初赛",
        "category": "A",
        "number": 1,
        "knowledge_points": tags or [],
    }


def _sample_index():
    return ProblemIndex([
        _problem("p1", "计算二重积分，其中D由直线x+y=1与两坐标轴围成", ["二重积分"]),
        _problem("p2", "求幂级数的收敛域与和函数", ["级数"]),
        _problem("p3", "证明不等式：当x>0时 ln(1+x)<x", ["不等式"]),
    ])


def _doc_encoder(texts):
    vectors = []
    for text in texts:
        if "二重积分" in text:
            vectors.append([1.0, 0.0, 0.0])
        elif "幂级数" in text:
            vectors.append([0.0, 1.0, 0.0])
        else:
            vectors.append([0.0, 0.0, 1.0])
    return np.array(vectors, dtype=np.float32)


def _query_encoder(query):
    # 语义查询“曲面的通量”在词面上和任何题都不重合，只能靠向量召回
    if "曲面" in query:
        return np.array([1.0, 0.0, 0.0], dtype=np.float32)
    if "幂级数" in query:
        return np.array([0.0, 1.0, 0.0], dtype=np.float32)
    return np.array([0.0, 0.0, 1.0], dtype=np.float32)


@pytest.fixture(autouse=True)
def _clear_vector_cache():
    tools.clear_vector_cache()
    yield
    tools.clear_vector_cache()


# ---------- 向量索引 ----------

def test_embedding_text_adds_tags_but_not_source():
    """知识点是内容摘要，可以进编码文本；出处/届数不应抢语义权重。"""
    text = embedding_text({
        "question": "计算二重积分",
        "source": "第16届 非数学类 初赛",
        "knowledge_points": ["二重积分", "极坐标"],
    })
    assert "计算二重积分" in text
    assert "知识点：二重积分、极坐标" in text
    assert "第16届" not in text


def test_build_and_search_vector_index(tmp_path, monkeypatch):
    monkeypatch.setenv("CMC_EMBEDDING_MODEL", "fake-model")
    index = _sample_index()
    path = build_vector_index(
        index,
        tmp_path / "embeddings.npz",
        encoder=_doc_encoder,
        model_name="fake-model",
        show_progress=False,
    )

    vector_index = VectorIndex.load_for(
        index, path, query_encoder=_query_encoder
    )
    assert vector_index.dim == 3
    hits = vector_index.search("曲面上的通量怎么算", k=2)

    assert [hit.problem_id for hit in hits] == ["p1", "p2"]
    assert hits[0].score == pytest.approx(1.0)
    assert hits[0].rank == 1


def test_vector_search_respects_allowed_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("CMC_EMBEDDING_MODEL", "fake-model")
    index = _sample_index()
    path = build_vector_index(
        index, tmp_path / "e.npz", encoder=_doc_encoder, model_name="fake-model"
    )
    vector_index = VectorIndex.load_for(index, path, query_encoder=_query_encoder)

    hits = vector_index.search("曲面", k=3, allowed_ids={"p2", "p3"})
    assert {hit.problem_id for hit in hits} == {"p2", "p3"}
    assert "p1" not in [hit.problem_id for hit in hits]


def test_vector_cache_rejects_mismatched_index(tmp_path, monkeypatch):
    monkeypatch.setenv("CMC_EMBEDDING_MODEL", "fake-model")
    path = build_vector_index(
        _sample_index(),
        tmp_path / "e.npz",
        encoder=_doc_encoder,
        model_name="fake-model",
    )
    other = ProblemIndex([_problem("x1", "另一道题")])

    with pytest.raises(VectorCacheError, match="不一致"):
        VectorIndex.load_for(other, path)


def test_vector_cache_rejects_changed_model(tmp_path, monkeypatch):
    path = build_vector_index(
        _sample_index(),
        tmp_path / "e.npz",
        encoder=_doc_encoder,
        model_name="model-a",
    )
    monkeypatch.setenv("CMC_EMBEDDING_MODEL", "model-b")

    with pytest.raises(VectorCacheError, match="model-a"):
        VectorIndex.load_for(_sample_index(), path)


def test_missing_vector_cache_has_actionable_error(tmp_path):
    with pytest.raises(VectorCacheError, match="build_embeddings"):
        VectorIndex.load(tmp_path / "missing.npz")


# ---------- RRF ----------

def test_rrf_prefers_items_ranked_high_by_both_routes():
    fused = rrf_fuse([["b", "a"], ["a", "c"]], k=60)
    ids = [hit.problem_id for hit in fused]

    assert ids[0] == "a"
    assert fused[0].ranks == (2, 1)
    assert fused[0].score > fused[1].score


def test_rrf_counts_duplicate_ids_only_once():
    fused = rrf_fuse([["a", "a", "b"]], k=60)
    assert [hit.problem_id for hit in fused] == ["a", "b"]
    assert fused[0].ranks == (1,)


def test_rrf_supports_weights_and_top_k():
    fused = rrf_fuse(
        [["a"], ["b"]],
        weights=[2.0, 1.0],
        top_k=1,
    )
    assert [hit.problem_id for hit in fused] == ["a"]
    assert fused[0].score == pytest.approx(2.0 / 61)


def test_rrf_rejects_invalid_parameters():
    with pytest.raises(ValueError):
        rrf_fuse([["a"]], k=0)
    with pytest.raises(ValueError):
        rrf_fuse([["a"], ["b"]], weights=[1.0])


# ---------- 混合检索 ----------

@pytest.fixture
def fake_vector_index(tmp_path, monkeypatch):
    monkeypatch.setenv("CMC_EMBEDDING_MODEL", "fake-model")
    path = build_vector_index(
        _sample_index(),
        tmp_path / "e.npz",
        encoder=_doc_encoder,
        model_name="fake-model",
    )
    return VectorIndex.load_for(
        _sample_index(), path, query_encoder=_query_encoder
    )


def test_vector_search_returns_cosine_scores(fake_vector_index):
    hits = vector_search(_sample_index(), fake_vector_index, "曲面上的通量", k=1)
    assert hits[0].problem["id"] == "p1"
    assert hits[0].score == pytest.approx(1.0)
    assert hits[0].bm25_rank is None


def test_hybrid_search_rescues_semantic_query_without_lexical_overlap(fake_vector_index):
    index = _sample_index()
    # 这句话和题库没有共同词，BM25 返回空；向量把 p1 捞回来
    query = "曲面上的通量怎么算"
    assert index.search(query, k=3) == []
    hits = hybrid_search(index, fake_vector_index, query, k=3, tag_weight=0.0)

    assert hits[0].problem["id"] == "p1"
    assert hits[0].bm25_rank is None
    assert hits[0].vector_rank == 1


def test_hybrid_search_keeps_bm25_result_when_both_routes_agree(fake_vector_index):
    query = "求幂级数的收敛域"
    hits = hybrid_search(_sample_index(), fake_vector_index, query, k=2)

    assert hits[0].problem["id"] == "p2"
    assert hits[0].bm25_rank == 1
    assert hits[0].vector_rank == 1


def test_evaluate_accepts_custom_search_function():
    """评测器要能接入 BGE/RRF，而不只是 index.search。"""
    index = _sample_index()
    queries = [{
        "id": "q1",
        "query": "任意查询",
        "expected_ids": ["p1"],
        "tier": "easy",
    }]
    summary = evaluate(
        index,
        queries,
        k=3,
        search_fn=lambda query, limit: index.search("二重积分", k=limit),
    )
    assert summary["recall_at_1"] == 1.0


# ---------- 工具层回退 ----------

def _write_index(tmp_path):
    path = tmp_path / "problems.jsonl"
    rows = [
        _problem("p1", "计算二重积分，其中D由x+y=1围成"),
        _problem("p2", "求幂级数的收敛域"),
    ]
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def test_tool_falls_back_to_bm25_when_vector_cache_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CMC_INDEX_PATH", str(_write_index(tmp_path)))
    monkeypatch.setenv("CMC_EMBEDDINGS_PATH", str(tmp_path / "missing.npz"))
    monkeypatch.setenv("CMC_RETRIEVER", "hybrid")

    results = json.loads(tools.search_past_problems("计算二重积分", 2))
    assert results[0]["id"] == "p1"


def test_tool_falls_back_to_bm25_when_model_load_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("CMC_INDEX_PATH", str(_write_index(tmp_path)))
    monkeypatch.setenv("CMC_RETRIEVER", "hybrid")

    def broken():
        raise RuntimeError("模型加载失败")

    monkeypatch.setattr(tools, "load_vector_index", broken)
    results = json.loads(tools.search_past_problems("计算二重积分", 1))
    assert results[0]["id"] == "p1"


def test_tool_vector_mode_uses_vector_index(tmp_path, monkeypatch, fake_vector_index):
    monkeypatch.setenv("CMC_INDEX_PATH", str(_write_index(tmp_path)))
    monkeypatch.setenv("CMC_RETRIEVER", "vector")
    # 这里替换掉加载函数，避免工具层去加载真实 BGE 模型
    monkeypatch.setattr(tools, "load_vector_index", lambda: fake_vector_index)

    results = json.loads(tools.search_past_problems("曲面上的通量", 1))
    assert results[0]["id"] == "p1"


def test_retriever_mode_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("CMC_RETRIEVER", "magic")
    assert tools.retriever_mode() == "auto"
    status = tools.retriever_status()
    assert status["retriever"] == "auto"
    assert "embedding_model" in status
