"""题库索引：加载 JSONL、BM25 打分、按知识点做轻量加权。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from rag.bm25 import BM25
from rag.textnorm import detect_tags, tokenize


@dataclass
class SearchHit:
    problem: dict
    score: float
    bm25: float
    tag_score: float

    def to_dict(self) -> dict:
        return {
            **self.problem,
            "score": round(self.score, 4),
            "bm25": round(self.bm25, 4),
            "tag_score": round(self.tag_score, 3),
        }


class ProblemIndex:
    def __init__(self, problems: list[dict]) -> None:
        self.problems = problems
        self._doc_tokens = [
            tokenize(problem["question"], doc_side=True) for problem in problems
        ]
        self._bm25 = BM25(self._doc_tokens)
        self._doc_tags = [
            set(problem.get("knowledge_points") or []) for problem in problems
        ]

    @classmethod
    def load(cls, path: str | Path) -> "ProblemIndex":
        problems: list[dict] = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    problems.append(json.loads(line))
        return cls(problems)

    def __len__(self) -> int:
        return len(self.problems)

    def search(
        self,
        query: str,
        k: int = 5,
        *,
        stage: str | None = None,
        category: str | None = None,
        min_score: float = 0.0,
        tag_weight: float = 0.25,
    ) -> list[SearchHit]:
        """混合打分：BM25 + 知识点重合度。

        知识点权重用乘性系数，避免不同查询的 BM25 分数量纲不一致。
        tag_weight=0 时退化为纯 BM25，用于评测对比。
        """
        query_tokens = tokenize(query)
        query_tags = set(detect_tags(query))
        base_scores = self._bm25.scores(query_tokens)

        hits: list[SearchHit] = []
        for index, base in enumerate(base_scores):
            problem = self.problems[index]

            if stage and problem.get("stage") != stage:
                continue
            if category and not _match_category(problem, category):
                continue

            tag_score = 0.0
            if query_tags:
                tag_score = len(query_tags & self._doc_tags[index]) / len(query_tags)

            score = base * (1 + tag_weight * tag_score)
            if score <= 0 or score < min_score:
                continue

            hits.append(
                SearchHit(
                    problem=problem,
                    score=score,
                    bm25=base,
                    tag_score=tag_score,
                )
            )

        hits.sort(key=lambda hit: (-hit.score, hit.problem["id"]))
        return hits[:k]


def _match_category(problem: dict, category: str) -> bool:
    """按类别过滤。

    15/16 届才分 A/B；更早的试卷没有类别，归入“未分类”。
    用户说“找 A 类”时，未分类的历史题也应该算进来。
    """
    actual = problem.get("category") or "未分类"
    if category == "A":
        return actual in ("A", "未分类")
    return actual == category
