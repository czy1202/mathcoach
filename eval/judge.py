"""自动判分器。

职责：比较"模型给出的答案"和"标准答案"，返回 Verdict。
支持六种判定方式：

    symbolic  符号等价：用 sympy 化简两边之差，结果为 0 算通过
    numeric   数值容差：把随机取样的数值代入两边，在容差内一致算通过
    exact     精确匹配：字符串完全相同算通过
    point     坐标点：两个分量分别符号等价才算通过
    parts     多小问：每个小问分别抽取、分别判分
    rubric    证明题：按关键步骤清单匹配（启发式）

统一入口是 judge_answer(expect, actual, method) -> Verdict。
所有解析失败、未知判定方式都返回 UNCERTAIN（或 FAIL），绝不抛异常。

说明：
    1. 为什么不直接用 tools.verify_with_sympy？因为它把"解析失败"和
       "不等价"都归为 False，判分器需要区分这两种情况，所以在这里
       单独解析表达式。
    2. sympy.sympify 内部使用 eval，v1 的输入来自本地评测集，暂时接受
       这个风险；接入真实用户输入前需要做输入白名单或受限解析。
"""

import math
import random
import re

import sympy as sp

from mathparse import normalize_math_text, parse_math
from schemas import Verdict


# ---------- 判定参数 ----------

DEFAULT_TOL = 1e-6      # 数值判定的绝对/相对容差
SAMPLE_COUNT = 6        # 数值判定取几组样本
SAMPLE_SEED = 20261021  # 固定随机种子，保证每次判分结果可复现
SAMPLE_LOW = 0.3        # 取样范围下限，避开 0 和负数的常见奇点
SAMPLE_HIGH = 2.7       # 取样范围上限


# ---------- 三种判定实现 ----------

def _judge_symbolic(expect: str, actual: str) -> Verdict:
    """符号等价判定：化简 expect - actual，等于 0 算通过。"""
    try:
        left = parse_math(expect)
        right = parse_math(actual)
    except Exception:
        # 任一表达式解析失败，无法判断对错
        return Verdict.UNCERTAIN

    try:
        return Verdict.PASS if sp.simplify(left - right) == 0 else Verdict.FAIL
    except Exception:
        # 化简过程本身出错（极少数复杂表达式），同样无法判定
        return Verdict.UNCERTAIN


def _numeric_value(expr: sp.Expr, point: dict) -> float | None:
    """把一个表达式在给定取值下算成实数。

    Returns:
        float: 可比较的实数值；结果不是有限实数时返回 None，
               调用方据此跳过这个样本点。
    """
    try:
        value = sp.N(expr.subs(point))
        if value.is_real is not True:
            return None
        number = float(value)
    except Exception:
        # 除零、复数、无法数值化等情况都跳过这个样本点
        return None

    return number if math.isfinite(number) else None


def _judge_numeric(expect: str, actual: str, tol: float = DEFAULT_TOL) -> Verdict:
    """数值容差判定：多组取样都一致算通过，任一不一致算失败。

    注意：
        这是启发式判定。两个表达式若只在样本点之外不同，可能被误判为
        通过；某一组样本全部落在无定义点时会返回 UNCERTAIN。
    """
    try:
        left = parse_math(expect)
        right = parse_math(actual)
    except Exception:
        return Verdict.UNCERTAIN

    symbols = sorted(left.free_symbols | right.free_symbols, key=str)
    rng = random.Random(SAMPLE_SEED)

    compared = 0    # 实际成功比较的样本点数
    for _ in range(SAMPLE_COUNT):
        # 每个符号取不同的随机值，避免 x == y 造成的偶然相等
        point = {s: sp.Float(round(rng.uniform(SAMPLE_LOW, SAMPLE_HIGH), 6))
                 for s in symbols}
        a = _numeric_value(left, point)
        b = _numeric_value(right, point)
        if a is None or b is None:
            continue

        compared += 1
        if not math.isclose(a, b, rel_tol=tol, abs_tol=tol):
            return Verdict.FAIL

    if compared == 0:
        # 所有样本点都因无定义/非实数被跳过，无法判断
        return Verdict.UNCERTAIN
    return Verdict.PASS


def _judge_exact(expect: str, actual: str) -> Verdict:
    """精确匹配判定：只忽略首尾空白，其余字符必须完全相同。"""
    if expect is None or actual is None:
        return Verdict.FAIL
    return Verdict.PASS if str(expect).strip() == str(actual).strip() else Verdict.FAIL


def _split_top_level(text: str) -> list[str]:
    """按顶层逗号切分，不切开 sqrt(...)、f(...) 里面的逗号。"""
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch in "([{（":
            depth += 1
        elif ch in ")]}）":
            depth -= 1
        elif ch in ",，" and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _parse_point(text: str) -> tuple[sp.Expr, sp.Expr] | None:
    """把 "(1/√2, 1/2)" 或 "x=1/√2, y=1/2" 解析成两个坐标。"""
    s = normalize_math_text(text).strip().strip("$")
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]

    parts = _split_top_level(s)
    if len(parts) != 2:
        return None

    values: list[sp.Expr] = []
    for part in parts:
        part = part.strip()
        if "=" in part:
            part = part.split("=", 1)[1].strip()
        values.append(parse_math(part))

    return values[0], values[1]


def _judge_point(expect: str, actual: str) -> Verdict:
    """坐标点判定：两个分量分别符号等价才通过。"""
    try:
        expected = _parse_point(expect)
        got = _parse_point(actual)
    except Exception:
        return Verdict.UNCERTAIN

    if expected is None or got is None:
        return Verdict.UNCERTAIN

    try:
        return (
            Verdict.PASS
            if all(sp.simplify(a - b) == 0 for a, b in zip(expected, got))
            else Verdict.FAIL
        )
    except Exception:
        return Verdict.UNCERTAIN


# ---------- 统一入口 ----------

_JUDGES = {
    "symbolic": _judge_symbolic,
    "numeric": _judge_numeric,
    "exact": _judge_exact,
    "point": _judge_point,
}


def judge_answer(expect: str | None, actual: str | None, method: str = "symbolic") -> Verdict:
    """比较标准答案与模型答案。

    Args:
        expect: 标准答案。为 None 表示这道题没有可判定的标准答案
                （例如拒答类、人工判定类），返回 UNCERTAIN。
        actual: 模型给出的答案。
        method: 判定方式，取值 "symbolic" / "numeric" / "exact" / "point"。
                parts 与 rubric 由 judge_parts / judge_rubric 单独调用。

    Returns:
        Verdict: PASS 通过、FAIL 不通过、UNCERTAIN 无法判定。
                 解析失败、未知判定方式都归为 UNCERTAIN，不抛异常。
    """
    if expect is None:
        return Verdict.UNCERTAIN
    if actual is None:
        return Verdict.FAIL

    judge = _JUDGES.get(method)
    if judge is None:
        return Verdict.UNCERTAIN

    try:
        return judge(expect, actual)
    except Exception:
        # 最后一道保险：任何意外都不让判分器中断整批评测
        return Verdict.UNCERTAIN


# ---------- 多小问与证明题判定 ----------

# 标签后面可能还跟着下标/上标或括号说明，例如
# (S_1+S_2)_{min}、S_{min}、V_x。允许这些修饰词后再匹配 "="。
_LABEL_QUALIFIER = (
    r"(?:\s*\)?\s*[_^]\s*(?:\{[^{}]{0,20}\}|[A-Za-z0-9]{1,10}))?"
)


def _label_pattern(label: str) -> str:
    """把标签转成可在压缩空白后的答案文本里匹配的正则。"""
    normalized = re.sub(r"\s+", "", normalize_math_text(label))
    pattern = re.escape(normalized) + _LABEL_QUALIFIER
    if re.fullmatch(r"[A-Za-z]", normalized):
        # 单字母标签（如 a）不能匹配到单词内部
        pattern = rf"(?<![A-Za-z]){pattern}(?![A-Za-z])"
    return pattern


def extract_labeled_value(text: str, labels: list[str]) -> str | None:
    """从答案文本里找出 "标签 = 值" / "标签为 值" 中的值。

    只做机械抽取，找不到返回 None，由调用方判为 UNCERTAIN。
    """
    if not text:
        return None

    # 去掉空白，避免模型写成 f(0) = -3 / f (0) = -3 时漏匹配
    normalized = re.sub(r"\s+", "", normalize_math_text(text))

    for label in labels:
        if not label:
            continue
        pattern = _label_pattern(label)
        match = re.search(
            pattern + r"(?:=|:|：|为)([^,，;；、\n]+)",
            normalized,
        )
        if not match and re.search(r"[一-鿿]", label):
            # 中文标签常直接跟数值而不写等号，例如“最小值 1/3-√2/6”。
            # 只接受以数字或数学符号开头的值，避免把后面的 “V=...” 吞进来。
            match = re.search(
                pattern + r"([0-9+\\\[({√π.\-][^,，;；、\n]*)",
                normalized,
            )
        if match:
            value = match.group(1).strip().strip("。.$")
            if value:
                return value

    return None


def judge_parts(parts: list[dict], actual: str | None) -> Verdict:
    """多小问判定：每个小问分别抽取并判分，全部通过才算通过。

    Args:
        parts: 每项形如 {"labels": ["f(0)"], "answer": "-3", "judge": "symbolic"}
        actual: 模型回答中的最终答案文本
    """
    if not actual:
        return Verdict.FAIL

    verdicts: list[Verdict] = []
    for part in parts:
        labels = part.get("labels") or ([part["label"]] if part.get("label") else [])
        value = extract_labeled_value(actual, labels)
        if value is None:
            verdicts.append(Verdict.UNCERTAIN)
            continue
        verdicts.append(
            judge_answer(part.get("answer"), value, part.get("judge", "symbolic"))
        )

    if not verdicts:
        return Verdict.UNCERTAIN
    if any(v is Verdict.FAIL for v in verdicts):
        return Verdict.FAIL
    if all(v is Verdict.PASS for v in verdicts):
        return Verdict.PASS
    return Verdict.UNCERTAIN


def judge_rubric(checks: list[dict], actual: str | None) -> Verdict:
    """证明题关键步骤清单判定。

    这是启发式判定：每项 check 只要有一个正则命中就算该步骤出现，
    全部 check 命中才算 PASS。它能检查"关键结论有没有写出来"，
    但不能替代人工阅卷判断证明是否严谨。
    """
    if not actual or not str(actual).strip():
        return Verdict.FAIL
    if not checks:
        return Verdict.UNCERTAIN

    # 保留大小写：F/G 是辅助函数，f 是原函数，不能混为一谈
    normalized = re.sub(r"\s+", "", normalize_math_text(actual))

    for check in checks:
        patterns = check.get("any", []) if isinstance(check, dict) else [check]
        if not patterns:
            return Verdict.UNCERTAIN
        if not any(re.search(p, normalized) for p in patterns):
            return Verdict.FAIL

    return Verdict.PASS


# ---------- 演示 ----------

if __name__ == "__main__":
    demo = [
        # method,      expect,       actual,       期望结果
        ("symbolic", "1/2",        "0.5",         Verdict.PASS),
        ("symbolic", "1/2",        "1/3",         Verdict.FAIL),
        ("symbolic", "x**2+2*x+1", "(x+1)**2",    Verdict.PASS),
        ("symbolic", "x**2",       "x**2 +",      Verdict.UNCERTAIN),
        ("numeric",  "1/2",        "0.5000001",   Verdict.PASS),
        ("numeric",  "1/2",        "0.6",         Verdict.FAIL),
        ("numeric",  "x**2",       "2*x",         Verdict.FAIL),
        ("exact",    "1/2",        "1/2",         Verdict.PASS),
        ("exact",    "1/2",        "0.5",         Verdict.FAIL),
        ("exact",    "1/2",        None,          Verdict.FAIL),
    ]

    for method, expect, actual, want in demo:
        got = judge_answer(expect, actual, method)
        mark = "OK" if got is want else "!!"
        print(f"[{mark}] {method:8s} expect={expect!r:12s} actual={actual!r:12s} "
              f"-> {got.value}")
