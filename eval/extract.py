"""从模型回答中提取最终答案。

模型被要求在回答的最后一行写：

    最终答案：<简洁答案>

本模块负责把这一行找出来，并做基本的格式清洗（Markdown、LaTeX、
"x = ..." 前缀、选择题字母等），让判分器能拿到一个可解析的表达式。

设计原则：
1. 只做机械抽取，不判断答案对错；
2. 找不到就返回 None，由调用方决定如何记录，绝不猜答案；
3. 清洗规则全部是纯函数，可以单独测试。
"""

import re

from mathparse import normalize_math_text


# 答案标记：最终答案 / 答案 / final answer，后面可以跟冒号，也可以跟“是/为”。
# 兼容 Markdown 粗体：**最终答案**：1/2
_MARKER_RE = re.compile(
    r"(?:\*\*|__)?\s*(?:最终\s*答案|答案|final\s*answer|answer)"
    r"\s*(?:\*\*|__)?\s*(?:[:：]|是|为)\s*(?P<ans>[^\n]*)",
    re.IGNORECASE,
)

# 选择题答案：B / 选项B / 选B / 选 B
_CHOICE_RE = re.compile(r"^(?:选项|选择|选)?\s*([A-Da-d])\s*[.。]?$")

# 单个变量/函数名做左值的等式，例如 x = 1/2、a_n = ...、f(x) = ...、y' = ...
_EQ_RE = re.compile(
    r"^\s*[A-Za-z][A-Za-z0-9_'′]*(?:\s*\([^()]*\))?\s*=\s*(?P<rhs>.+)$"
)

# 结尾的解释性从句，例如 “1/2，故选 B”
_TRAILING_CLAUSE_RE = re.compile(r"[，,]\s*(?:故|所以|因此|即|选|答案).*$")

# 判断某一行是否“像答案”，只用于无标记时的兜底，避免把客套话当答案
_ANSWER_HINT_RE = re.compile(r"[0-9A-Za-z=+\-*/^()\[\]{}<>√π]|得证|证毕|无解|不存在")


def _strip_wrappers(text: str) -> str:
    """去掉 Markdown 强调、公式外壳和句末标点。"""
    s = text.strip()
    s = s.strip("*_`")

    if len(s) >= 2 and s.startswith("$") and s.endswith("$"):
        s = s[1:-1]
    if s.startswith(r"\(") and s.endswith(r"\)"):
        s = s[2:-2]
    if s.startswith(r"\[") and s.endswith(r"\]"):
        s = s[2:-2]
    if s.startswith(r"\boxed{") and s.endswith("}"):
        s = s[len(r"\boxed{"):-1]

    return s.strip().rstrip(" \t。．.；;,，").strip()


def _normalize_latex(s: str) -> str:
    """把常见的 LaTeX 写法转成 sympy 能解析的形式。"""
    return normalize_math_text(s)


def _clean(raw: str) -> str:
    """把一段候选文本清洗成答案字符串。"""
    s = _strip_wrappers(raw)
    s = _TRAILING_CLAUSE_RE.sub("", s).strip()
    s = _strip_wrappers(s)
    s = _normalize_latex(s)
    s = re.sub(r"\s+", " ", s).strip()   # LaTeX 函数替换可能留下连续空格
    s = _strip_wrappers(s)

    # 选择题：B / 选项 B / 选B
    m = _CHOICE_RE.match(s)
    if m:
        return m.group(1).upper()
    m = re.fullmatch(r"(?:选项|选择|选)\s*([A-Da-d])", s)
    if m:
        return m.group(1).upper()

    # x = 1/2 这类带左值的答案，只保留右边
    m = _EQ_RE.match(s)
    if m and "=" not in m.group("rhs"):
        s = m.group("rhs").strip()

    return s.strip()


def _looks_like_answer(text: str) -> bool:
    """无标记兜底时的启发式：短、且包含数学字符或固定结论词。"""
    return bool(text) and len(text) <= 80 and bool(_ANSWER_HINT_RE.search(text))


def extract_final_answer(text: str | None) -> str | None:
    """从整段回答中提取最终答案。

    Args:
        text: 模型的完整回答。

    Returns:
        str | None: 清洗后的答案；找不到任何可用内容时返回 None。

    规则：
        1. 从后往前找最后一个答案标记，取该行标记之后的内容；
        2. 标记后为空时，取下一行非空内容；
        3. 完全没有标记时，退化为最后一行“像答案”的文本；
        4. 仍找不到则返回 None。
    """
    if not text or not text.strip():
        return None

    lines = text.splitlines()

    for idx in range(len(lines) - 1, -1, -1):
        match = _MARKER_RE.search(lines[idx])
        if not match:
            continue

        candidate = match.group("ans")
        if not candidate.strip():
            candidate = next((line for line in lines[idx + 1:] if line.strip()), "")

        result = _clean(candidate)
        if result:
            return result

    for line in reversed(lines):
        cleaned = _clean(line)
        if _looks_like_answer(cleaned):
            return cleaned

    return None
