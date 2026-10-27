"""eval/extract.py 的自动化测试。

运行方式（在 mathcoach 目录下执行）：
    python -m pytest eval/test_extract.py -v
"""

import pytest

from eval.extract import extract_final_answer


# ---------- 标准写法 ----------

@pytest.mark.parametrize("text, want", [
    ("最终答案：1/2", "1/2"),
    ("解：原式 = 1/2。\n最终答案：1/2", "1/2"),
    ("**最终答案：** 1/2", "1/2"),
    ("**最终答案**：1/2", "1/2"),
    ("**最终答案**：$\\frac{1}{2}$", "((1)/(2))"),
    ("最终答案：B", "B"),
    ("最终答案：选B", "B"),
    ("最终答案：选项 C。", "C"),
])
def test_standard_answers(text, want):
    assert extract_final_answer(text) == want


# ---------- LaTeX 与格式清洗 ----------

@pytest.mark.parametrize("text, want", [
    (r"最终答案：$\frac{1}{2}$", "((1)/(2))"),
    (r"最终答案：\boxed{\frac{1}{2}}", "((1)/(2))"),
    (r"最终答案：\boxed{\frac16}", "((1)/(6))"),
    (r"最终答案：\(\frac16\)", "((1)/(6))"),
    ("最终答案：π²/6", "pi**(2)/6"),
    (r"最终答案：\sqrt{2}", "sqrt(2)"),
    (r"最终答案：\frac{\pi}{4}", "((pi)/(4))"),
    ("最终答案：x = 1/2", "1/2"),
    ("最终答案：f(x) = x**2 + 1", "x**2 + 1"),
    ("最终答案：y' = 2x sin x + x^2 cos x", "2x sin x + x^2 cos x"),
    ("最终答案：1/2，故选 B", "1/2"),
    ("最终答案：1/2。", "1/2"),
    (r"最终答案：\(y'=2x\sin x+x^2\cos x\)", "2x sin x+x^2 cos x"),
    (r"最终答案：\(\operatorname{Var}(X)=\dfrac{1}{12}\)", "((1)/(12))"),
])
def test_cleaning(text, want):
    assert extract_final_answer(text) == want


# ---------- 标记后换行、多个标记、无标记 ----------

def test_answer_on_next_line():
    assert extract_final_answer("最终答案：\n1/2") == "1/2"


def test_last_marker_wins():
    text = "最终答案：1/3\n检查后发现计算有误。\n最终答案：1/2"
    assert extract_final_answer(text) == "1/2"


def test_fallback_to_last_answer_like_line():
    assert extract_final_answer("经过计算。\n0.5") == "0.5"


def test_fallback_skips_chatter():
    """没有标记时，不应把客套话当成答案。"""
    assert extract_final_answer("解题过程略。\n希望对你有帮助。") is None


# ---------- 空输入 ----------

@pytest.mark.parametrize("text", [None, "", "   \n  "])
def test_empty_returns_none(text):
    assert extract_final_answer(text) is None
