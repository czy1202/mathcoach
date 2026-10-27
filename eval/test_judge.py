"""eval/judge.py 的自动化测试。

运行方式（在 mathcoach 目录下执行）：
    python -m pytest eval/test_judge.py -v
"""

import pytest

from eval.judge import judge_answer
from schemas import Verdict


# ---------- symbolic：符号等价 ----------

@pytest.mark.parametrize("actual", ["1/2", "0.5", "(1/2)"])
def test_symbolic_pass(actual):
    """与标准答案等价的写法都应通过。"""
    assert judge_answer("1/2", actual, "symbolic") is Verdict.PASS


def test_symbolic_expression_pass():
    """代数等价但形式不同的表达式应通过。"""
    assert judge_answer("x**2 + 2*x + 1", "(x + 1)**2", "symbolic") is Verdict.PASS


def test_symbolic_implicit_multiplication_pass():
    """人类写法（省略乘号、用 ^ 表示指数）也应能解析。"""
    assert judge_answer(
        "2*x*sin(x) + x**2*cos(x)",
        "2x sin x + x^2 cos x",
        "symbolic",
    ) is Verdict.PASS


def test_symbolic_latex_function_adjacent_pass():
    """2x\\sin x 这类函数紧贴变量的 LaTeX 写法不能被并成一个符号。"""
    assert judge_answer(
        "2*x*sin(x) + x**2*cos(x)",
        r"2x\sin x+x^2\cos x",
        "symbolic",
    ) is Verdict.PASS


@pytest.mark.parametrize("actual", [
    "π²/6",            # Unicode 上标
    r"\frac{\pi^2}{6}",  # LaTeX
    r"\frac{\pi^{2}}{6}",
])
def test_symbolic_latex_and_unicode_pass(actual):
    """LaTeX 与 Unicode 写法应能换算成同一表达式。"""
    assert judge_answer("pi**2/6", actual, "symbolic") is Verdict.PASS


def test_symbolic_frac_shorthand_pass():
    """\\frac16 这种简写也应能解析。"""
    assert judge_answer("1/6", r"\frac16", "symbolic") is Verdict.PASS


@pytest.mark.parametrize("actual", ["1/3", "0", "x + 1"])
def test_symbolic_fail(actual):
    """不等价的答案应判为失败。"""
    assert judge_answer("1/2", actual, "symbolic") is Verdict.FAIL


@pytest.mark.parametrize("expect, actual", [
    ("x**2 +", "x**2"),       # 标准答案语法不完整
    ("x**2", "x**2 +"),       # 模型答案语法不完整
    ("", "1"),                # 空字符串
])
def test_symbolic_parse_error_is_uncertain(expect, actual):
    """解析失败不得抛异常，返回 UNCERTAIN。"""
    assert judge_answer(expect, actual, "symbolic") is Verdict.UNCERTAIN


# ---------- numeric：数值容差 ----------

def test_numeric_within_tolerance_pass():
    """误差在 1e-6 以内应通过。"""
    assert judge_answer("1/2", "0.5000001", "numeric") is Verdict.PASS


def test_numeric_out_of_tolerance_fail():
    """超出容差应失败。"""
    assert judge_answer("1/2", "0.6", "numeric") is Verdict.FAIL


def test_numeric_variable_expression_pass():
    """带变量的等价表达式应通过。"""
    assert judge_answer("x**2 + 2*x + 1", "(x + 1)**2", "numeric") is Verdict.PASS


def test_numeric_variable_expression_fail():
    """只在个别点相等不算等价，应失败。"""
    assert judge_answer("x**2", "2*x", "numeric") is Verdict.FAIL


def test_numeric_parse_error_is_uncertain():
    """解析失败返回 UNCERTAIN，不抛异常。"""
    assert judge_answer("x**2", "x**2 +", "numeric") is Verdict.UNCERTAIN


# ---------- exact：精确匹配 ----------

@pytest.mark.parametrize("actual", ["1/2", " 1/2 "])
def test_exact_match_pass(actual):
    """完全相同（允许首尾空白）应通过。"""
    assert judge_answer("1/2", actual, "exact") is Verdict.PASS


@pytest.mark.parametrize("actual", ["0.5", "1 / 2", "1/3"])
def test_exact_match_fail(actual):
    """字面不同就应失败，即使数学上等价。"""
    assert judge_answer("1/2", actual, "exact") is Verdict.FAIL


# ---------- 统一入口的边界情况 ----------

def test_none_answers():
    """标准答案缺失时无法判定；模型答案缺失时判失败。"""
    assert judge_answer(None, "1/2", "symbolic") is Verdict.UNCERTAIN
    assert judge_answer("1/2", None, "symbolic") is Verdict.FAIL


def test_unknown_method_is_uncertain():
    """未知判定方式返回 UNCERTAIN，不抛异常。"""
    assert judge_answer("1/2", "1/2", "manual") is Verdict.UNCERTAIN


def test_never_raises_on_weird_input():
    """各种奇怪输入都不应抛异常。"""
    for expect, actual in [("", ""), ("1/0", "1/0"), ("sin(", "1"), ("None", "None")]:
        result = judge_answer(expect, actual, "symbolic")
        assert isinstance(result, Verdict)
