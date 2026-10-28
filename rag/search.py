"""题库检索：命令行 demo 与 Agent 可调用的函数。

用法：
    python -m rag.search "求 lim(x→0) (tan x - sin x)/x^3" --top 5
    python -m rag.search "求极限" --stage 初赛 --json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rag.index import ProblemIndex, SearchHit


DEFAULT_INDEX = Path("data/processed/problems.jsonl")


def search_problems(
    query: str,
    *,
    k: int = 5,
    index_path: str | Path = DEFAULT_INDEX,
    stage: str | None = None,
    category: str | None = None,
) -> list[dict]:
    """供工具层调用的简洁接口，返回可直接 JSON 序列化的结果。"""
    index = ProblemIndex.load(index_path)
    return [hit.to_dict() for hit in index.search(query, k=k, stage=stage, category=category)]


def _print_hits(hits: list[SearchHit]) -> None:
    if not hits:
        print("没有找到相关题目。（题库为空，或查询词与题库没有重合）")
        return

    for rank, hit in enumerate(hits, start=1):
        problem = hit.problem
        tags = "、".join(problem.get("knowledge_points") or []) or "无"
        print(
            f"{rank}. {hit.score:6.3f}  [{problem['source']}]  "
            f"BM25={hit.bm25:.2f} 标签={hit.tag_score:.2f}"
        )
        print(f"   知识点：{tags}")
        print(f"   {problem['question'][:220]}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="在 CMC 真题库里检索相似题")
    parser.add_argument("query")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--index", default=str(DEFAULT_INDEX))
    parser.add_argument("--stage", choices=["初赛", "决赛"])
    parser.add_argument("--category", choices=["A", "B", "未分类"])
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于脚本处理")
    args = parser.parse_args()

    index = ProblemIndex.load(args.index)
    hits = index.search(
        args.query,
        k=args.top,
        stage=args.stage,
        category=args.category,
    )

    if args.json:
        print(json.dumps([hit.to_dict() for hit in hits], ensure_ascii=False, indent=2))
    else:
        print(f"题库：{len(index)} 道 | 查询：{args.query}\n")
        _print_hits(hits)


if __name__ == "__main__":
    main()
