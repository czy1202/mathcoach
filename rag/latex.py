"""把 PDF 抽取的题干重建为 Markdown + LaTeX。

为什么不在前端直接渲染题库原文：

- PDF 的文字层会把分数压成「分子 分母」两段、把上下标压成普通字符、
  把老试卷的 ∫/∑ 输出成 Z/X。前端拿到的是已经丢失结构的字符串，
  再高明的正则也补不回原来的排版；
- 但换行顺序、字号层级（12pt 正文 / 8pt 上下标）仍保留在
  ``question_layout`` 里，模型可以据此恢复分式、指数、积分上下限。

所以这里在构建阶段调用一次模型，把题干重建成 LaTeX 并缓存到本地，
运行时只读缓存，不会给每次提问增加模型调用。

用法：
    python -m rag.latex
    python -m rag.latex --limit 20 --batch-size 4
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

DEFAULT_INDEX = Path("data/processed/problems.jsonl")
DEFAULT_CACHE = Path("data/processed/question_latex.json")
DEFAULT_BATCH_SIZE = 8
MAX_RETRIES = 2
PROMPT_VERSION = "v1"


SYSTEM_PROMPT = """你是数学公式排版修复器。用户给的是从 PDF 提取的竞赛题干：
- text 是压成一行后的文本，公式结构可能已经丢失；
- layout 保留 PDF 原来的换行顺序，并带有字号标记：正文通常 12pt，
  上下标和分式的分子/分母通常是 8pt。

请以 layout 为主、text 为辅，在不改变题意、不增删条件、不改变任何数字的前提下，
把题干重建为 Markdown + 标准 LaTeX。

结构提示：
- PDF 的分数会拆成上下两行；上下相邻且宽度相近的行通常构成 \\frac{}{}；
- 紧跟在分式右侧、字号更小的短行可能是上标/指数；
- 极限、积分、求和的上下限会出现在符号的上一行或下一行；
- 大括号、方括号可能被拆成单独的行，应配对包住区间；
- 如果结构确实无法判断，优先保留原意，不要凭空增加条件。

格式要求：行内公式用 $...$，独立公式用 $$...$$；极限写 \\lim_{x \\to 0}，
积分写 \\int_a^b，求和写 \\sum_{k=1}^n，分数写 \\frac{}{}，根号写 \\sqrt{}；
中文说明放在公式外面；只输出题干本身，不要加“题干：”之类的前缀。

输入是 JSON 数组，输出也必须是 JSON 数组，每项格式为 {"id": "...", "latex": "..."}，
不要输出 Markdown 代码围栏或其他解释。"""


def question_hash(problem: dict) -> str:
    """题目内容变化或提示词升级时让缓存失效。"""
    payload = "\n".join([
        PROMPT_VERSION,
        str(problem.get("id", "")),
        str(problem.get("question", "")),
        str(problem.get("question_layout", "")),
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_response(text: str) -> list[dict]:
    """从模型回复里取出 JSON 数组；容忍代码围栏和前后说明。"""
    cleaned = _strip_fences(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return []

    if isinstance(data, dict):
        data = data.get("results") or data.get("items") or []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


_CJK_PERIOD_RE = re.compile(r"\s*\\text\{。\}\s*(\$\$|\$)")


def clean_latex(latex: str) -> str:
    """把公式末尾的 ``\\text{。}`` 移到公式外面。

    模型常把填空题末尾的句号写进公式；KaTeX 能渲染，但字体和间距
    都不对。这里只处理紧贴结束分隔符的情况，正文里的句号不动。
    """
    return _CJK_PERIOD_RE.sub(r"\1。", latex).strip()


def _digit_counts(text: str) -> Counter:
    """统计每个数字字符出现的次数。

    不能用“数字串顺序一致”来判断：PDF 里积分上下限常按
    “∫ 上限 / 下限” 的顺序抽取，LaTeX 却是 ``\\int_下限^上限``；
    矩阵、分式也可能调换阅读顺序。按字符多重集比较可以容忍这种
    合法的重排，同时仍能发现被改写或漏掉的数字。
    """
    return Counter(re.findall(r"\d", text))


def validate_latex(raw: str, latex: object) -> tuple[bool, str]:
    """返回 (是否可用, 原因)。宁可回退原文，也不展示明显错误的公式。"""
    if not isinstance(latex, str) or not latex.strip():
        return False, "空内容"
    latex = latex.strip()
    if len(latex) > 4000:
        return False, "内容过长"
    if "```" in latex:
        return False, "包含代码围栏"

    raw_counts = _digit_counts(raw)
    tex_counts = _digit_counts(latex)
    missing = sum(
        max(0, count - tex_counts[digit]) for digit, count in raw_counts.items()
    )
    extra = sum(
        max(0, count - raw_counts[digit]) for digit, count in tex_counts.items()
    )

    # 同时缺数字又多数字，说明某个数值被改写（2 -> 3），直接拒绝。
    if missing and extra:
        return False, "数字不一致"
    # PDF 页码会混进题干，通常是 1-2 位；允许模型略去，但超过 2 位
    # 就可能真的漏了题干条件。
    if missing > 2:
        return False, "数字缺失"
    return True, ""


class LatexStore:
    """题目 id -> 重建后的 LaTeX，按内容哈希校验。"""

    def __init__(self, path: str | Path = DEFAULT_CACHE, entries: dict | None = None):
        self.path = Path(path)
        self.entries: dict[str, dict] = entries or {}

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CACHE) -> "LatexStore":
        path = Path(path)
        if not path.exists():
            return cls(path, {})
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return cls(path, {})
        return cls(path, data if isinstance(data, dict) else {})

    def get(self, problem: dict) -> str | None:
        entry = self.entries.get(str(problem.get("id", "")))
        if not isinstance(entry, dict):
            return None
        if entry.get("hash") != question_hash(problem):
            return None
        latex = entry.get("latex")
        return clean_latex(latex) if isinstance(latex, str) and latex.strip() else None

    def set(self, problem: dict, latex: str, model: str = "") -> None:
        self.entries[str(problem.get("id", ""))] = {
            "hash": question_hash(problem),
            "latex": clean_latex(latex),
            "model": model,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.entries, handle, ensure_ascii=False, indent=2)
        tmp.replace(self.path)


def _chunks(items: list[dict], size: int) -> Iterable[list[dict]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _request_batch(client, batch: list[dict]) -> list[dict]:
    payload = [
        {
            "id": problem.get("id", ""),
            "text": problem.get("question", ""),
            "layout": problem.get("question_layout", ""),
        }
        for problem in batch
    ]
    response = client.chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ])
    return parse_response(response.content or "")


def build_latex(
    problems: list[dict],
    *,
    client,
    cache_path: str | Path = DEFAULT_CACHE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    force: bool = False,
    show_progress: bool = True,
) -> dict:
    """为缺失的题目生成 LaTeX，返回统计信息。

    每处理完一批就落盘，中途失败或中断不会丢掉已完成的部分。
    """
    if batch_size <= 0:
        raise ValueError("batch_size 必须大于 0")

    store = LatexStore.load(cache_path)
    todo = [
        problem for problem in problems
        if force or store.get(problem) is None
    ]
    if limit is not None:
        todo = todo[: max(0, int(limit))]

    model = getattr(client, "model", "")
    stats = {
        "total": len(problems),
        "pending": len(todo),
        "ok": 0,
        "failed": 0,
        "skipped": len(problems) - len(todo),
    }
    batches = list(_chunks(todo, batch_size))

    for index, batch in enumerate(batches, start=1):
        by_id = {str(problem.get("id", "")): problem for problem in batch}
        results: list[dict] = []
        for attempt in range(MAX_RETRIES + 1):
            try:
                results = _request_batch(client, batch)
            except Exception:
                results = []
            if results:
                break

        if not results and len(batch) > 1:
            # 整批 JSON 失败时退化为逐题请求，避免一道题拖垮一批
            results = []
            for problem in batch:
                try:
                    results.extend(_request_batch(client, [problem]))
                except Exception:
                    continue

        seen: set[str] = set()
        handled: set[str] = set()
        for item in results:
            problem_id = str(item.get("id", ""))
            problem = by_id.get(problem_id)
            if problem is None or problem_id in handled:
                continue
            handled.add(problem_id)
            ok, reason = validate_latex(
                problem.get("question", ""), item.get("latex")
            )
            if not ok:
                stats["failed"] += 1
                if show_progress:
                    print(f"  - 跳过 {problem_id}: {reason}")
                continue
            seen.add(problem_id)
            store.set(problem, item["latex"].strip(), model=model)
            stats["ok"] += 1

        # 模型整条漏掉、或返回了无效内容的题都算失败，但只计一次
        missing = [
            problem for problem in batch
            if str(problem.get("id", "")) not in handled
        ]
        stats["failed"] += len(missing)
        store.save()

        if show_progress:
            print(
                f"[{index}/{len(batches)}] 本批成功 {len(seen)}/{len(batch)}"
                f"，累计 {stats['ok']} 条"
            )

    return stats


def main() -> None:
    import argparse

    from dotenv import load_dotenv

    load_dotenv(".env")

    from llm import OpenAIClient
    from rag.index import ProblemIndex

    parser = argparse.ArgumentParser(description="用模型把题库题干重建为 LaTeX")
    parser.add_argument("--index", default=str(DEFAULT_INDEX))
    parser.add_argument("--out", default=str(DEFAULT_CACHE))
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="忽略缓存，全部重新生成")
    args = parser.parse_args()

    index = ProblemIndex.load(args.index)
    client = OpenAIClient()
    stats = build_latex(
        index.problems,
        client=client,
        cache_path=args.out,
        batch_size=args.batch_size,
        limit=args.limit,
        force=args.force,
    )
    print(
        f"完成：总题数 {stats['total']}，本次待生成 {stats['pending']}，"
        f"成功 {stats['ok']}，失败 {stats['failed']}，"
        f"已有缓存 {stats['skipped']}"
    )


if __name__ == "__main__":
    main()
