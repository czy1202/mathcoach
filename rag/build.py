"""构建题库：PDF -> JSONL。

用法：
    python -m rag.build
    python -m rag.build --papers-dir "C:/path/to/数学竞赛分享" --out data/processed/problems.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

from rag.papers import dedupe, extract_papers
from rag.textnorm import detect_tags


DEFAULT_PAPERS_DIR = Path(
    os.environ.get("CMC_PAPERS_DIR", Path.home() / "Desktop" / "数学竞赛分享")
)
DEFAULT_OUT = Path("data/processed/problems.jsonl")


def _sort_key(problem: dict) -> tuple:
    return (
        problem.get("edition") or 99,
        problem.get("year") or 0,
        problem.get("stage", ""),
        problem.get("number", 0),
        problem["id"],
    )


def build(papers_dir: str | Path, out_path: str | Path) -> tuple[list[dict], int]:
    raw = extract_papers(papers_dir)
    problems = dedupe(raw)

    for problem in problems:
        problem["knowledge_points"] = detect_tags(problem["question"])

    problems.sort(key=_sort_key)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        for problem in problems:
            handle.write(json.dumps(problem, ensure_ascii=False) + "\n")

    return problems, len(raw)


def _report(raw_count: int, problems: list[dict], out_path: str | Path) -> None:
    stages = Counter(problem.get("stage") for problem in problems)
    categories = Counter(problem.get("category") or "未分类" for problem in problems)
    tagged = sum(1 for problem in problems if problem.get("knowledge_points"))
    tag_counter = Counter(
        tag for problem in problems for tag in problem.get("knowledge_points", [])
    )

    print(f"抽取题目：{raw_count} 道")
    print(f"去重后：  {len(problems)} 道 -> {out_path}")
    print(f"阶段：    " + " / ".join(f"{k} {v}" for k, v in stages.items()))
    print(f"类别：    " + " / ".join(f"{k} {v}" for k, v in categories.items()))
    print(f"知识点：  有标签 {tagged} 道（{tagged / max(len(problems), 1):.0%}）")
    print("Top 标签：" + "、".join(f"{k}({v})" for k, v in tag_counter.most_common(8)))

    missing = [p for p in problems if not p.get("knowledge_points")]
    if missing:
        print(f"未命中标签的题目 {len(missing)} 道，示例：")
        for problem in missing[:3]:
            print(f"  - {problem['source']}: {problem['question'][:80]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="从 CMC 真题 PDF 构建题库 JSONL")
    parser.add_argument("--papers-dir", default=str(DEFAULT_PAPERS_DIR))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    problems, raw_count = build(args.papers_dir, args.out)
    _report(raw_count, problems, args.out)


if __name__ == "__main__":
    main()
