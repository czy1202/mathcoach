"""BM25 + BGE 向量召回的 RRF 融合。

为什么要 RRF（Reciprocal Rank Fusion）：

- BM25 分数和余弦相似度量纲不同，直接相加会被某一方的数值范围带偏；
- RRF 只看名次：``score = Σ weight / (k + rank)``，两路都排得靠前的题
  自然得到更高分，不需要调分数归一化；
- ``k``（默认 60）越大，名次差异越平滑，常见取值 60。

典型覆盖场景：

- 查询里有原题词：BM25 排第一，向量也靠前，融合后仍然第一；
- 查询只给语义（“圆环区域上带指数衰减因子的二重积分”）：BM25 可能完全
  没有词面重合，向量召回把它捞回来，融合后进入 Top-K。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from rag.index import ProblemIndex, SearchHit
from rag.vector import VectorIndex, VectorHit


DEFAULT_RRF_K = 60


@dataclass
class RRFHit:
    problem_id: str
    score: float
    ranks: tuple[int | None, ...]
    best_rank: int


@dataclass
class HybridHit:
    problem: dict
    score: float
    bm25: float = 0.0
    vector: float = 0.0
    bm25_rank: int | None = None
    vector_rank: int | None = None

    @property
    def problem_id(self) -> str:
        return self.problem["id"]

    def to_dict(self) -> dict:
        return {
            **self.problem,
            "score": round(self.score, 4),
            "bm25": round(self.bm25, 4),
            "vector": round(self.vector, 4),
            "bm25_rank": self.bm25_rank,
            "vector_rank": self.vector_rank,
        }


def rrf_fuse(
    rankings: Sequence[Sequence[str]],
    *,
    k: int = DEFAULT_RRF_K,
    weights: Sequence[float] | None = None,
    top_k: int | None = None,
) -> list[RRFHit]:
    """把多路有序题号列表按 RRF 合并。

    每一路内部重复的题号只算第一次出现的位置，避免同一路重复贡献分数。
    """
    if k <= 0:
        raise ValueError("RRF 的 k 必须大于 0")
    if weights is None:
        weights = [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("weights 的数量必须与 rankings 一致")

    scores: dict[str, float] = {}
    ranks: dict[str, list[int | None]] = {}
    for route, ranking in enumerate(rankings):
        seen: set[str] = set()
        rank = 0
        for raw_id in ranking:
            problem_id = str(raw_id)
            if not problem_id or problem_id in seen:
                continue
            seen.add(problem_id)
            rank += 1
            scores[problem_id] = scores.get(problem_id, 0.0) + (
                float(weights[route]) / (k + rank)
            )
            ranks.setdefault(problem_id, [None] * len(rankings))[route] = rank

    fused = [
        RRFHit(
            problem_id=problem_id,
            score=score,
            ranks=tuple(ranks[problem_id]),
            best_rank=min(
                rank for rank in ranks[problem_id] if rank is not None
            ),
        )
        for problem_id, score in scores.items()
    ]
    fused.sort(key=lambda hit: (-hit.score, hit.best_rank, hit.problem_id))
    if top_k is not None:
        fused = fused[: max(0, int(top_k))]
    return fused


def vector_search(
    index: ProblemIndex,
    vector_index: VectorIndex,
    query: str,
    k: int = 5,
    *,
    allowed_ids: Iterable[str] | None = None,
) -> list[HybridHit]:
    """只用 BGE 向量召回，保留余弦分数，便于评测和调试。"""
    by_id = {problem["id"]: problem for problem in index.problems}
    hits: list[HybridHit] = []
    for hit in vector_index.search(query, k=k, allowed_ids=allowed_ids):
        problem = by_id.get(hit.problem_id)
        if problem is None:
            continue
        hits.append(
            HybridHit(
                problem=problem,
                score=hit.score,
                vector=hit.score,
                vector_rank=hit.rank,
            )
        )
    return hits


def hybrid_search(
    index: ProblemIndex,
    vector_index: VectorIndex,
    query: str,
    k: int = 5,
    *,
    candidates: int | None = None,
    rrf_k: int = DEFAULT_RRF_K,
    bm25_weight: float = 1.5,
    vector_weight: float = 1.0,
    tag_weight: float = 0.0,
    allowed_ids: Iterable[str] | None = None,
) -> list[HybridHit]:
    """BM25 与 BGE 各取一批候选，再用 RRF 融合。

    候选数默认是 ``max(k * 4, 20)``：只取 k 条会让 RRF 没有“翻盘”空间，
    候选太多则会把低相关题带进融合结果。417 道题的量级下 20 条足够。

    BM25 权重略高于向量（1.5 : 1）：数学题里的公式和术语是强信号，
    词面精确命中的精度高；向量负责补同义表述和语义查询。这个比例在
    47 条评测查询上同时保住了 Recall@5，并把 Recall@1 从 74.5% 提到 83.0%。
    """
    if k <= 0:
        return []
    if candidates is None:
        candidates = max(int(k) * 4, 20)
    candidates = max(int(k), int(candidates))

    allowed = None if allowed_ids is None else {str(pid) for pid in allowed_ids}

    bm25_hits: list[SearchHit] = index.search(
        query, k=candidates, tag_weight=tag_weight
    )
    if allowed is not None:
        bm25_hits = [
            hit for hit in bm25_hits if hit.problem["id"] in allowed
        ]

    vector_hits: list[VectorHit] = vector_index.search(
        query, k=candidates, allowed_ids=allowed
    )

    bm25_rank = {
        hit.problem["id"]: rank for rank, hit in enumerate(bm25_hits, start=1)
    }
    vector_rank = {hit.problem_id: hit.rank for hit in vector_hits}
    bm25_score = {hit.problem["id"]: hit.score for hit in bm25_hits}
    vector_score = {hit.problem_id: hit.score for hit in vector_hits}

    fused = rrf_fuse(
        [
            [hit.problem["id"] for hit in bm25_hits],
            [hit.problem_id for hit in vector_hits],
        ],
        k=rrf_k,
        weights=[bm25_weight, vector_weight],
        top_k=k,
    )

    by_id = {problem["id"]: problem for problem in index.problems}
    results: list[HybridHit] = []
    for hit in fused:
        problem = by_id.get(hit.problem_id)
        if problem is None:
            continue
        results.append(
            HybridHit(
                problem=problem,
                score=hit.score,
                bm25=bm25_score.get(hit.problem_id, 0.0),
                vector=vector_score.get(hit.problem_id, 0.0),
                bm25_rank=bm25_rank.get(hit.problem_id),
                vector_rank=vector_rank.get(hit.problem_id),
            )
        )
    return results
