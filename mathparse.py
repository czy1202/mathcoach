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

_FRAC_BRACED_RE = re.compile(r"\\(?:d|t)?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_FRAC_SHORT_RE = re.compile(
    r"\\(?:d|t)?frac\s*(?:\{([^{}]*)\}|([A-Za-z0-9]))"
    r"\s*(?:\{([^{}]*)\}|([A-Za-z0-9]))"
)

_SQRT_BRACED_RE = re.compile(r"\\sqrt\s*\[([^\]]+)\]\s*\{([^{}]*)\}")
_SQRT_SIMPLE_RE = re.compile(r"\\sqrt\s*\{([^{}]*)\}")

# 需要保留空格边界的函数控制序列。先匹配长名字，避免 \arcsin 被 \sin 截断。
_LATEX_FUNCTIONS = (
    "arcsin", "arccos", "arctan",
    "sinh", "cosh", "tanh",
    "sin", "cos", "tan", "cot", "sec", "csc",
    "ln", "log", "exp",
)


def _frac(match: re.Match) -> str:
    """把 \\frac 的两种写法统一成 ((分子)/(分母))。"""
    groups = match.groups()
    if len(groups) == 2:                      # 花括号写法
        num, den = groups
    else:                                     # 简写写法
        num = groups[0] if groups[0] is not None else groups[1]
        den = groups[2] if groups[2] is not None else groups[3]
    return f"(({num})/({den}))"


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

    # \frac{1}{2} 和 \frac16 两种写法
    while _FRAC_BRACED_RE.search(s):
        s = _FRAC_BRACED_RE.sub(_frac, s)
    while _FRAC_SHORT_RE.search(s):
        s = _FRAC_SHORT_RE.sub(_frac, s)

    # 根号
    s = _SQRT_BRACED_RE.sub(r"((\2)**(1/(\1)))", s)
    s = _SQRT_SIMPLE_RE.sub(r"sqrt(\1)", s)

    # 常见符号
    s = s.replace(r"\pi", "pi")
    s = s.replace(r"\cdot", "*").replace(r"\times", "*")
    s = s.replace(r"\div", "/")
    s = s.replace(r"\le", "<=").replace(r"\ge", ">=")
    s = s.replace(r"\neq", "!=")

    # 函数名要保留两侧的空格：2x\sin x 若直接去掉反斜杠会变成 2xsin x，
    # 被当成一个叫 xsin 的符号。补上空格后是 2x sin x，隐式乘法才能拆开。
    for name in _LATEX_FUNCTIONS:
        s = re.sub(rf"\\{name}\b", f" {name} ", s)

    # 排版指令
    for token in (r"\left", r"\right", r"\displaystyle", r"\textstyle"):
        s = s.replace(token, "")
    for token in (r"\,", r"\!", r"\;", r"\:", r"\quad", r"\qquad"):
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
