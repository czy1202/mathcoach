"""统一的数学文本清洗与表达式解析。

模型写出的答案经常是“人类写法”：

    2x sin x + x^2 cos x      省略乘号
    \\frac16                      LaTeX 简写
    pi²/6                         Unicode 上标
    2^{n-1}                       带花括号的指数

sympy 默认的 `sympify` 处理不了这些，所以这里先做统一的文本归一化，
再用带转换规则的 `parse_expr` 解析：

    convert_xor                        x^2 -> x**2
    implicit_multiplication_application 2x -> 2*x，sin x -> sin(x)

安全提示：`parse_expr` 和 `sympify` 一样最终会执行传入字符串里的表达式。
v1 只处理本地评测集与模型输出，接入真实用户输入前应换成受限命名空间。
"""

import re

import sympy as sp
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

TRANSFORMS = standard_transformations + (
    convert_xor,
    implicit_multiplication_application,
)

# 上标/下标字符映射
_SUPERSCRIPT_MAP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺ⁿ", "0123456789-+n")
_SUBSCRIPT_MAP = str.maketrans("₀₁₂₃₄₅₆₇₈₉ₙ", "0123456789n")

_SUPERSCRIPT_RE = re.compile(r"([A-Za-z0-9_)\]}]+)([⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺ⁿ]+)")
_SUBSCRIPT_RE = re.compile(r"([A-Za-z])_?([₀₁₂₃₄₅₆₇₈₉ₙ]+)")

_FRAC_TOKEN_RE = re.compile(r"\\(?:d|t)?frac\s*")
_SQRT_TOKEN_RE = re.compile(r"\\sqrt\s*")

# 需要保留空格边界的函数控制序列。先匹配长名字，避免 \arcsin 被 \sin 截断。
_LATEX_FUNCTIONS = (
    "arcsin", "arccos", "arctan",
    "sinh", "cosh", "tanh",
    "sin", "cos", "tan", "cot", "sec", "csc",
    "ln", "log", "exp",
)


def _balanced_group(text: str, start: int) -> tuple[str, int] | None:
    """读取从 start 开始的 {...} 花括号组。

    Returns:
        (组内文本, 右花括号后的位置)；花括号不配对时返回 None。
    """
    if start >= len(text) or text[start] != "{":
        return None

    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
    return None


def _read_arg(text: str, start: int) -> tuple[str | None, int]:
    """读取一个 LaTeX 参数：{...} 组，或单个字符。

    Returns:
        (参数文本, 新位置)；无法读取时返回 (None, 原位置)。
    """
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text):
        return None, start

    if text[start] == "{":
        group = _balanced_group(text, start)
        return group if group is not None else (None, start)

    return text[start], start + 1


def _replace_fracs(text: str) -> str:
    """把 \\frac 的两种写法统一成 ((分子)/(分母))，支持嵌套花括号。"""
    out: list[str] = []
    pos = 0

    while True:
        match = _FRAC_TOKEN_RE.search(text, pos)
        if match is None:
            out.append(text[pos:])
            break

        num, after_num = _read_arg(text, match.end())
        if num is None:
            # 参数不完整时不冒险改写，原样输出
            out.append(text[pos:match.end()])
            pos = match.end()
            continue

        den, after_den = _read_arg(text, after_num)
        if den is None:
            out.append(text[pos:match.end()])
            pos = match.end()
            continue

        out.append(text[pos:match.start()])
        out.append(f"(({num})/({den}))")
        pos = after_den

    return "".join(out)


def _replace_sqrts(text: str) -> str:
    """把 \\sqrt[n]{x} / \\sqrt{x} / \\sqrt2 统一成幂或 sqrt(...)。"""
    out: list[str] = []
    pos = 0

    while True:
        match = _SQRT_TOKEN_RE.search(text, pos)
        if match is None:
            out.append(text[pos:])
            break

        cursor = match.end()
        index: str | None = None

        if cursor < len(text) and text[cursor] == "[":
            close = text.find("]", cursor + 1)
            if close != -1:
                index = text[cursor + 1:close]
                cursor = close + 1

        arg, after_arg = _read_arg(text, cursor)
        if arg is None:
            out.append(text[pos:match.end()])
            pos = match.end()
            continue

        out.append(text[pos:match.start()])
        if index is None:
            out.append(f"sqrt({arg})")
        else:
            out.append(f"(({arg})**(1/({index})))")
        pos = after_arg

    return "".join(out)


def _superscript(match: re.Match) -> str:
    base, sup = match.groups()
    return f"{base}**({sup.translate(_SUPERSCRIPT_MAP)})"


def _subscript(match: re.Match) -> str:
    base, sub = match.groups()
    return f"{base}_{sub.translate(_SUBSCRIPT_MAP)}"


def normalize_math_text(text: object) -> str:
    """把人类写法 / LaTeX 写法归一化成 parse_expr 能处理的文本。"""
    s = str(text)

    # 先把带花括号的指数转掉，避免 \frac{\pi^{2}}{6} 里的嵌套花括号
    # 干扰后面的分数正则。
    s = re.sub(r"\^\s*\{([^{}]*)\}", r"**(\1)", s)

    # \frac{1}{2} 和 \frac16 两种写法；循环处理 \frac{\frac{1}{2}}{3}
    while _FRAC_TOKEN_RE.search(s):
        replaced = _replace_fracs(s)
        if replaced == s:
            break
        s = replaced

    # 根号：\sqrt[n]{x}、\sqrt{x}、\sqrt2 都要能解析
    while _SQRT_TOKEN_RE.search(s):
        replaced = _replace_sqrts(s)
        if replaced == s:
            break
        s = replaced

    # 常见符号
    s = s.replace(r"\pi", "pi")
    s = s.replace(r"\cdot", "*").replace(r"\times", "*")
    s = s.replace(r"\div", "/")
    # 用负向前瞻避免把 \left / \leq / \geq 的前缀截断
    s = re.sub(r"\\leq(?![A-Za-z])", "<=", s)
    s = re.sub(r"\\geq(?![A-Za-z])", ">=", s)
    s = re.sub(r"\\le(?![A-Za-z])", "<=", s)
    s = re.sub(r"\\ge(?![A-Za-z])", ">=", s)
    s = re.sub(r"\\neq(?![A-Za-z])", "!=", s)

    # 函数名要保留两侧的空格：2x\sin x 若直接去掉反斜杠会变成 2xsin x，
    # 被当成一个叫 xsin 的符号。补上空格后是 2x sin x，隐式乘法才能拆开。
    for name in _LATEX_FUNCTIONS:
        s = re.sub(rf"\\{name}\b", f" {name} ", s)

    # 排版指令
    for token in (r"\left", r"\right", r"\displaystyle", r"\textstyle"):
        s = s.replace(token, "")
    for token in (r"\,", r"\!", r"\;", r"\:", r"\quad", r"\qquad", r"\ "):
        s = s.replace(token, " ")

    # \text{...} / \mathrm{...} / \mathbf{...} / \operatorname{...} 只保留内容
    s = re.sub(r"\\(?:text|mathrm|mathbf|operatorname)\s*\{([^{}]*)\}", r"\1", s)

    # 其余控制序列：\sin -> sin，\alpha -> alpha
    s = re.sub(r"\\([A-Za-z]+)", r"\1", s)

    # 根号（先处理，避免 √x² 被上标规则拆错）
    s = re.sub(r"√\s*\(([^()]*)\)", r"sqrt(\1)", s)
    s = re.sub(r"√\s*([A-Za-z0-9]+)", r"sqrt(\1)", s)

    # Unicode 数学符号要先替换，π² 才能被识别成 pi²
    s = (s.replace("π", "pi").replace("×", "*").replace("÷", "/")
         .replace("−", "-").replace("·", "*"))

    # Unicode 上下标
    s = _SUPERSCRIPT_RE.sub(_superscript, s)
    s = _SUBSCRIPT_RE.sub(_subscript, s)

    return s


def parse_math(expr: object) -> sp.Expr:
    """把字符串或 sympy 表达式解析成 sympy 表达式。

    Raises:
        Exception: 解析失败时向上抛出，由调用方决定如何归类
                   （判分器归为 UNCERTAIN，工具层归为 False）。
    """
    if isinstance(expr, sp.Basic):
        return expr
    if expr is None:
        raise ValueError("表达式不能为 None")

    return parse_expr(normalize_math_text(expr), transformations=TRANSFORMS)
