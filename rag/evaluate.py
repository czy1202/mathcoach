"""检索评测：在固定查询集上计算 Recall@k / MRR@k。

用法：
    python -m rag.evaluate
    python -m rag.evaluate --k 10 --misses
    python -m rag.evaluate --queries rag/eval_queries.jsonl --json

查询集每行一条：

    {"id": "q01", "query": "...", "expected_ids": ["cmc-04-prelim-x-01"]}

expected_ids 可以写去重前被合并的题号（problems.jsonl 里的 aliases），
评测会自动映射到保留的那条。标准答案由人标注，不能按检索结果反推，
否则评测永远满分，失去意义。
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path
from typing import Callable

from rag.index import ProblemIndex


DEFAULT_QUERIES = Path("rag/eval_queries.jsonl")
DEFAULT_INDEX = Path("data/processed/problems.jsonl")

# (显示名, tag_weight)。第一行是不加权的 BM25 基线。
DEFAULT_SCHEMES = [
    ("BM25（不加权）", 0.0),
    ("BM25 + 标签加权", 0.25),
]


def load_queries(path: str | Path) -> list[dict]:
    """读取并校验查询集。"""
    queries: list[dict] = []
    with open(path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            query = str(item.get("query") or "").strip()
            expected = item.get("expected_ids")
            if not query or not isinstance(expected, list) or not expected:
                raise ValueError(f"{path}:{line_no} 缺少 query 或 expected_ids")
            queries.append({
                "id": str(item.get("id") or f"q{line_no:02d}"),
                "query": query,
                "expected_ids": [str(pid) for pid in expected],
                "tier": str(item.get("tier") or "未分类"),
            })
    if not queries:
        raise ValueError(f"{path} 里没有任何查询")
    return queries


def _alias_map(index: ProblemIndex) -> dict[str, str]:
    """把被去重合并的旧题号映射到保留下来的题号。"""
    mapping: dict[str, str] = {}
    for problem in index.problems:
        for alias in problem.get("aliases") or []:
            mapping[alias] = problem["id"]
    return mapping


def _metrics(results: list[dict]) -> dict:
    count = len(results)
    if not count:
        return {"n": 0, "recall_at_1": 0.0, "recall_at_k": 0.0, "mrr": 0.0}
    return {
        "n": count,
        "recall_at_1": sum(r["rank"] == 1 for r in results) / count,
        "recall_at_k": sum(r["hit"] for r in results) / count,
        "mrr": sum(1 / r["rank"] for r in results if r["rank"]) / count,
    }


def evaluate(
    index: ProblemIndex,
    queries: list[dict],
    *,
    k: int = 5,
    tag_weight: float = 0.25,
    search_fn: Callable[[str, int], list] | None = None,
) -> dict:
    """跑一遍查询集，返回逐条结果和汇总指标。

    ``search_fn`` 让评测可以接入任意检索器（BGE、RRF 等），
    只要它返回带 ``.problem`` 的 hit 列表。
    """
    aliases = _alias_map(index)
    known = {problem["id"] for problem in index.problems} | set(aliases)

    results: list[dict] = []
    unknown: set[str] = set()
    for item in queries:
        expected = {aliases.get(pid, pid) for pid in item["expected_ids"]}
        unknown.update(pid for pid in expected if pid not in known)

        if search_fn is None:
            hits = index.search(item["query"], k=k, tag_weight=tag_weight)
        else:
            hits = search_fn(item["query"], k)
        ids = [hit.problem["id"] for hit in hits]
        rank = next(
            (position for position, pid in enumerate(ids, 1) if pid in expected),
            None,
        )
        results.append({
            "id": item["id"],
            "query": item["query"],
            "tier": item.get("tier", "未分类"),
            "expected_ids": item["expected_ids"],
            "rank": rank,
            "top_ids": ids,
            "top_source": hits[0].problem["source"] if hits else "",
            "hit": rank is not None,
        })

    tiers: dict[str, list[dict]] = {}
    for result in results:
        tiers.setdefault(result["tier"], []).append(result)

    return {
        **_metrics(results),
        "k": k,
        "tag_weight": tag_weight,
        "unknown_ids": sorted(unknown),
        "by_tier": {name: _metrics(items) for name, items in tiers.items()},
        "results": results,
    }


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _scheme_name(label: str, weight: float) -> str:
    if weight == 0:
        return label
    return f"{label}（{weight:g}）"


def _print_table(summaries: list[tuple[str, dict]], k: int) -> None:
    name_width = max(_display_width(name) for name, _ in summaries) + 2
    header = (
        _pad("方案", name_width)
        + _pad("Recall@1", 12)
        + _pad(f"Recall@{k}", 12)
        + "MRR"
    )
    print(header)
    print("-" * _display_width(header))

    baseline = summaries[0][1]
    for name, summary in summaries:
        row = (
            _pad(name, name_width)
            + _pad(f"{summary['recall_at_1']:.1%}", 12)
            + _pad(f"{summary['recall_at_k']:.1%}", 12)
            + f"{summary['mrr']:.3f}"
        )
        if summary is not baseline:
            delta_recall = summary["recall_at_k"] - baseline["recall_at_k"]
            delta_mrr = summary["mrr"] - baseline["mrr"]
            row += f"   ({delta_recall:+.1%} / {delta_mrr:+.3f})"
        print(row)


def _print_tiers(label: str, summary: dict) -> None:
    tiers = summary.get("by_tier") or {}
    if not tiers:
        return

    print(f"分难度（{label}）：")
    name_width = max(_display_width(name) for name in tiers) + 2
    header = (
        _pad("难度", name_width)
        + _pad("条数", 8)
        + _pad("Recall@1", 12)
        + _pad(f"Recall@{summary['k']}", 12)
        + "MRR"
    )
    print(header)
    print("-" * _display_width(header))
    for name, metrics in tiers.items():
        print(
            _pad(name, name_width)
            + _pad(str(metrics["n"]), 8)
            + _pad(f"{metrics['recall_at_1']:.1%}", 12)
            + _pad(f"{metrics['recall_at_k']:.1%}", 12)
            + f"{metrics['mrr']:.3f}"
        )


def _print_misses(label: str, summary: dict, k: int) -> None:
    misses = [r for r in summary["results"] if not r["hit"]]
    if not misses:
        print(f"{label}：全部命中。")
        return

    print(f"{label}：{len(misses)} 条未命中")
    for item in misses:
        expected = "、".join(item["expected_ids"])
        top = item["top_ids"][0] if item["top_ids"] else "无结果"
        print(f"  {item['id']}  {item['query']}")
        print(f"      期望：{expected}")
        print(f"      实际前 1：{top}")


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="CMC 真题检索评测")
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--index", default=str(DEFAULT_INDEX))
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument(
        "--tag-weight",
        type=float,
        default=0.25,
        help="标签加权系数，用于第二组方案（0 表示与基线相同）",
    )
    parser.add_argument("--misses", action="store_true", help="列出未命中的查询")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于留档")
    parser.add_argument(
        "--embeddings",
        default=None,
        help="向量缓存路径，默认读 CMC_EMBEDDINGS_PATH",
    )
    parser.add_argument(
        "--no-vector",
        action="store_true",
        help="只跑 BM25 两组方案，不加载 BGE",
    )
    parser.add_argument("--rrf-k", type=int, default=60, help="RRF 平滑常数")
    parser.add_argument("--bm25-weight", type=float, default=1.5)
    parser.add_argument("--vector-weight", type=float, default=1.0)
    args = parser.parse_args()

    index = ProblemIndex.load(args.index)
    queries = load_queries(args.queries)

    schemes: list[tuple[str, Callable[[str, int], list]]] = [
        (
            _scheme_name(DEFAULT_SCHEMES[0][0], DEFAULT_SCHEMES[0][1]),
            lambda query, limit: index.search(
                query, k=limit, tag_weight=DEFAULT_SCHEMES[0][1]
            ),
        ),
        (
            _scheme_name(DEFAULT_SCHEMES[1][0], args.tag_weight),
            lambda query, limit: index.search(
                query, k=limit, tag_weight=args.tag_weight
            ),
        ),
    ]

    vector_index = None
    if not args.no_vector:
        try:
            from rag.vector import VectorIndex

            vector_index = VectorIndex.load_for(
                index,
                args.embeddings,
            )
        except Exception as exc:
            print(f"跳过向量方案：{exc}", file=sys.stderr)

    if vector_index is not None:
        from rag.hybrid import hybrid_search, vector_search

        schemes.append((
            "BGE 向量",
            lambda query, limit: vector_search(
                index, vector_index, query, k=limit
            ),
        ))
        schemes.append((
            f"BM25×{args.bm25_weight:g} + BGE×{args.vector_weight:g}"
            f" + RRF（k={args.rrf_k}）",
            lambda query, limit: hybrid_search(
                index,
                vector_index,
                query,
                k=limit,
                rrf_k=args.rrf_k,
                bm25_weight=args.bm25_weight,
                vector_weight=args.vector_weight,
            ),
        ))

    summaries: list[tuple[str, dict]] = []
    for name, search_fn in schemes:
        summaries.append((
            name,
            evaluate(
                index,
                queries,
                k=args.k,
                tag_weight=0.0,
                search_fn=search_fn,
            ),
        ))

    if args.json:
        print(json.dumps(
            [{"scheme": name, **summary} for name, summary in summaries],
            ensure_ascii=False,
            indent=2,
        ))
        return

    print(f"题库：{len(index)} 道 | 查询：{len(queries)} 条 | k = {args.k}")
    print()
    _print_table(summaries, args.k)

    if len(summaries) > 1:
        print()
        _print_tiers(summaries[-1][0], summaries[-1][1])

    unknown = sorted({pid for _, summary in summaries for pid in summary["unknown_ids"]})
    if unknown:
        print()
        print(f"警告：这些题号在题库里不存在：{'、'.join(unknown)}")

    if args.misses:
        print()
        for name, summary in summaries:
            _print_misses(name, summary, args.k)
            print()


if __name__ == "__main__":
    main()
