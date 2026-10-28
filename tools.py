"""工具层：模型可以调用的函数。

本文件只包含工具函数、工具注册表和分派函数，不含 Agent 主循环。

安全提示：sympy.sympify 内部使用 eval，理论上可以执行传入字符串里的代码。
v1 阶段输入来自本地自测，暂时接受这个风险；后期接入真实用户输入前，
应改成受限命名空间的解析方式，或先做输入白名单校验。
"""

import json
import os
import threading
from pathlib import Path
from typing import Any

import sympy as sp

from mathparse import parse_math


def verify_with_sympy(expr_a: Any, expr_b: Any) -> bool:
    """判断两个数学表达式是否等价。

    原理：把两个表达式相减后化简，结果为 0 即认为等价。

    Args:
        expr_a: 第一个表达式，字符串，例如 "sin(x)**2 + cos(x)**2"
        expr_b: 第二个表达式，字符串，例如 "1"

    Returns:
        bool: 等价返回 True；不等价、表达式非法、参数错误一律返回 False。

    注意：
        1. 解析失败与"不等价"都返回 False，调用方无法区分二者。
           需要区分时，应改用返回 CheckResult 的版本（后续步骤再做）。
        2. 符号化简不检查定义域。例如 (x**2-1)/(x-1) 与 x+1 会被判为等价，
           但前者在 x=1 处无定义。
        3. simplify 是启发式算法，极复杂的表达式可能化简不到 0。
    """
    try:
        a = parse_math(expr_a)
        b = parse_math(expr_b)
        return bool(sp.simplify(a - b) == 0)
    except Exception:
        # SympifyError / TypeError / ValueError 等一律归为本次验证失败
        return False


TOOLS: dict[str, dict] = {
    "verify_with_sympy": {
        "函数": verify_with_sympy,
        "描述": "判断两个数学表达式是否等价。等价返回 True，否则返回 False。",
        "参数说明": {
            "expr_a": "第一个表达式，字符串，例如 'sin(x)**2 + cos(x)**2'",
            "expr_b": "第二个表达式，字符串，例如 '1'",
        },
    },
}


# ---------- 题库检索工具 ----------

_INDEX_LOCK = threading.Lock()
_INDEX_CACHE: Any = None
_INDEX_CACHE_PATH: Path | None = None


def index_path() -> Path:
    """题库 JSONL 的位置，可用 CMC_INDEX_PATH 覆盖（测试和换数据时用）。"""
    return Path(os.environ.get("CMC_INDEX_PATH", "data/processed/problems.jsonl"))


def index_available() -> bool:
    """题库是否已构建。调用方据此决定要不要开放检索工具。"""
    return index_path().exists()


def _load_problem_index():
    """懒加载题库索引，避免未使用 RAG 的命令行也读一遍 JSONL。"""
    global _INDEX_CACHE, _INDEX_CACHE_PATH

    path = index_path()
    with _INDEX_LOCK:
        if _INDEX_CACHE is not None and _INDEX_CACHE_PATH == path:
            return _INDEX_CACHE
        if not path.exists():
            raise FileNotFoundError(
                f"题库不存在：{path}。请先运行 python -m rag.build"
            )
        from rag.index import ProblemIndex

        _INDEX_CACHE = ProblemIndex.load(path)
        _INDEX_CACHE_PATH = path
        return _INDEX_CACHE


def search_past_problems(query: str, k: int | str = 3) -> str:
    """检索 CMC 历年真题，返回精简 JSON。

    只返回题干和出处，不返回解答：工具结果会进模型上下文，需要控制体积；
    完整解答留给后续的 get_problem_detail（暂未实现）。
    """
    if not isinstance(query, str) or not query.strip():
        return "[]"

    try:
        limit = max(1, min(int(k), 5))
    except (TypeError, ValueError):
        limit = 3

    index = _load_problem_index()
    hits = index.search(query, k=limit)
    results = [
        {
            "id": hit.problem["id"],
            "source": hit.problem["source"],
            "question": hit.problem["question"][:300],
            "knowledge_points": hit.problem.get("knowledge_points") or [],
            "score": round(hit.score, 3),
        }
        for hit in hits
        if hit.score > 0
    ]
    return json.dumps(results, ensure_ascii=False)


RETRIEVAL_TOOLS: dict[str, dict] = {
    "search_past_problems": {
        "函数": search_past_problems,
        "描述": (
            "在 CMC 非数学类历年真题库中检索同类题，返回题干、知识点和出处。"
            "用于推荐相关真题，不返回解答。每道新题最多调用一次。"
        ),
        "参数说明": {
            "query": "检索内容，通常传当前题目的完整题干或核心表达式",
            "k": "返回条数，字符串形式的整数，默认 3，最多 5",
        },
        "必填": ["query"],
    },
}

ALL_TOOLS: dict[str, dict] = {**TOOLS, **RETRIEVAL_TOOLS}


def run_tool(name: str, args: dict) -> Any:
    """按名字调用工具。

    Args:
        name: 工具名，必须是 ALL_TOOLS 中的键
        args: 参数字典，会以关键字参数形式传给工具函数

    Returns:
        成功时返回工具函数的原始返回值；工具不存在或参数不对时，
        返回一段错误说明字符串。本函数不抛异常，错误一律以文本返回，
        便于上层把错误信息交回模型自行纠正。
    """
    tool = ALL_TOOLS.get(name)
    if tool is None:
        available = "、".join(ALL_TOOLS.keys())
        return f"错误：不存在名为 {name} 的工具。可用工具：{available}"

    if not isinstance(args, dict):
        return f"错误：args 必须是字典，收到的是 {type(args).__name__}"

    try:
        return tool["函数"](**args)
    except TypeError as e:
        return f"错误：参数不匹配（{e}）"
    except Exception as e:
        return f"错误：工具执行失败（{type(e).__name__}: {e}）"
