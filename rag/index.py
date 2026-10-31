"""题库索引：加载 JSONL、BM25 打分、按知识点做轻量加权。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from rag.bm25 import BM25
from rag.papers import make_source, parse_problem_id
from rag.textnorm import detect_tags, tokenize


_STAGE_ORDER = {"初赛": 0, "决赛": 1}
_CATEGORY_ORDER = {"A": 0, "B": 1, "未分类": 2}
_SUPPLEMENT_ORDER = {"": 0, "补赛": 1, "补赛1": 2, "补赛2": 3, "补赛3": 4}
_SECTION_ORDER = {"选择题": 0, "填空题": 1, "解答题": 2}


def problem_year(problem: dict) -> int | None:
    """取题目所属年份。

    第 1-16 届按届数组织，没有年份；2024/2025 两份卷子的
    ``edition`` 为空，只能从出处里解析。
    """
    raw = problem.get("year")
    if raw not in (None, ""):
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass

    match = re.match(r"\s*(\d{4})\s*年", str(problem.get("source") or ""))
    return int(match.group(1)) if match else None


def _paper_memberships(problem: dict) -> list[dict]:
    """把一道题还原成它出现过的所有试卷位置。

    去重会把跨届重复的题合并成一条，并在 ``aliases`` 里保留旧 id。
    例如第 13 届决赛第 9 题与第 12 届重复，主记录属于第 12 届，
    但按第 13 届浏览时仍应把这道题算进去。
    """
    memberships: list[dict] = []
    for paper_id in [problem.get("id"), *(problem.get("aliases") or [])]:
        parsed = parse_problem_id(paper_id)
        if not parsed:
            continue
        parsed["paper_id"] = paper_id
        parsed["source"] = make_source(
            parsed["edition"],
            parsed["stage"],
            parsed["category"],
            parsed["supplement"],
            parsed["number"],
        )
        memberships.append(parsed)
    return memberships


def _entry(problem: dict, membership: dict) -> dict:
    """统一的试卷条目：既保留主记录，也保留这道题在本卷中的位置。"""
    return {
        "id": problem["id"],
        "paper_id": membership["paper_id"],
        "edition": membership.get("edition"),
        "year": problem_year(problem),
        "stage": membership.get("stage", ""),
        "category": membership.get("category") or "未分类",
        "supplement": membership.get("supplement") or "",
        "section": problem.get("section") or "",
        "number": int(membership.get("number") or 0),
        "source": membership.get("source") or problem.get("source", ""),
        "question": problem.get("question", ""),
        "knowledge_points": list(problem.get("knowledge_points") or []),
    }


def _entry_sort_key(entry: dict) -> tuple:
    return (
        _STAGE_ORDER.get(entry["stage"], 9),
        _CATEGORY_ORDER.get(entry["category"], 9),
        _SUPPLEMENT_ORDER.get(entry["supplement"], 9),
        _SECTION_ORDER.get(entry["section"], 9),
        entry["number"],
        entry["paper_id"],
    )


def _paper_label(stage: str, category: str, supplement: str, section: str) -> str:
    parts = [stage] if stage else []
    if supplement:
        parts.append(supplement)
    if category in ("A", "B"):
        parts.append(f"{category}类")
    if section:
        parts.append(section)
    return " ".join(parts) or "试卷"


def _group_sort_key(group: dict) -> tuple:
    return _entry_sort_key({
        "stage": group["stage"],
        "category": group["category"],
        "supplement": group["supplement"],
        "section": group["section"],
        "number": 0,
        "paper_id": "",
    })


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
        # 相关真题和试卷浏览拿到的可能只是 id（或去重后保留的旧 id），
        # 这里建一张反查表，便于补全题干和展示用 LaTeX。
        self._by_id: dict[str, dict] = {}
        for problem in problems:
            for problem_id in [problem.get("id"), *(problem.get("aliases") or [])]:
                if problem_id:
                    self._by_id.setdefault(str(problem_id), problem)

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

    def get(self, problem_id: str) -> dict | None:
        """按 id 取主记录；去重时合并的旧 id 也能命中。"""
        return self._by_id.get(str(problem_id or ""))

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

    def list_problems(
        self,
        *,
        edition: int | None = None,
        year: int | None = None,
        stage: str | None = None,
        category: str | None = None,
        supplement: str | None = None,
    ) -> list[dict]:
        """按试卷条件列出题目，不参与 BM25 打分。

        与 ``search`` 的区别：
        - ``search`` 返回 Top-K 相似题；
        - ``list_problems`` 返回符合条件的**全部**题目，用于按届数浏览。

        ``supplement=None`` 表示不限制补赛；``supplement=""`` 只取正卷。
        去重合并过的题会通过 aliases 还原到它出现过的每一份试卷。
        """
        if edition is not None:
            edition = int(edition)
        if year is not None:
            year = int(year)
        stage = stage or None
        category = category or None

        entries: list[dict] = []
        seen: set[str] = set()

        for problem in self.problems:
            if year is not None and problem_year(problem) != year:
                continue

            if edition is not None:
                memberships = [
                    item for item in _paper_memberships(problem)
                    if item["edition"] == edition
                ]
            else:
                # 没有届数时使用主记录；2024/2025 就属于这种模式。
                memberships = [{
                    "paper_id": problem.get("id", ""),
                    "edition": problem.get("edition"),
                    "stage": problem.get("stage", ""),
                    "category": problem.get("category") or "未分类",
                    "supplement": problem.get("supplement") or "",
                    "number": int(problem.get("number") or 0),
                    "source": problem.get("source", ""),
                }]

            for membership in memberships:
                if stage and membership.get("stage") != stage:
                    continue
                if category and not _match_category(membership, category):
                    continue
                if supplement is not None and (
                    membership.get("supplement") or ""
                ) != supplement:
                    continue

                paper_id = membership["paper_id"]
                if paper_id in seen:
                    continue
                seen.add(paper_id)
                entries.append(_entry(problem, membership))

        entries.sort(key=_entry_sort_key)
        return entries

    @staticmethod
    def group_problems(entries: list[dict]) -> list[dict]:
        """把题目按 初赛/决赛、类别、补赛、题型 分组，并计算缺号。"""
        buckets: dict[tuple, list[dict]] = {}
        for entry in entries:
            key = (
                entry.get("stage", ""),
                entry.get("category") or "未分类",
                entry.get("supplement") or "",
                entry.get("section") or "",
            )
            buckets.setdefault(key, []).append(entry)

        groups: list[dict] = []
        for (stage, category, supplement, section), items in buckets.items():
            items.sort(key=lambda item: (item["number"], item["paper_id"]))
            numbers = sorted({item["number"] for item in items if item["number"] > 0})
            max_number = numbers[-1] if numbers else 0
            missing = [n for n in range(1, max_number + 1) if n not in numbers]
            groups.append({
                "stage": stage,
                "category": category,
                "supplement": supplement,
                "section": section,
                "label": _paper_label(stage, category, supplement, section),
                "count": len(items),
                "numbers": numbers,
                "missing": missing,
                "complete": bool(numbers) and not missing,
                "problems": items,
            })

        groups.sort(key=_group_sort_key)
        return groups

    def paper_index(self) -> list[dict]:
        """给前端用的试卷目录：第 1-16 届，每届列出各份试卷的题数。"""
        editions: list[dict] = []
        for edition in range(1, 17):
            entries = self.list_problems(edition=edition)
            groups = self.group_problems(entries)
            papers = [
                {key: value for key, value in group.items() if key != "problems"}
                for group in groups
            ]
            editions.append({
                "edition": edition,
                "label": f"第{edition}届",
                "papers": papers,
                "total": sum(paper["count"] for paper in papers),
            })
        return editions


def _match_category(problem: dict, category: str) -> bool:
    """按类别过滤。

    15/16 届才分 A/B；更早的试卷没有类别，归入“未分类”。
    用户说“找 A 类”时，未分类的历史题也应该算进来。
    """
    actual = problem.get("category") or "未分类"
    if category == "A":
        return actual in ("A", "未分类")
    return actual == category
