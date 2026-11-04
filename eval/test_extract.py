"""eval/extract.py 的自动化测试。

运行方式（在 mathcoach 目录下执行）：
    python -m pytest eval/test_extract.py -v
"""

import pytest

from eval.extract import extract_final_answer, extract_final_answer_display


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
    # 2025-F4 的真实回答：$ 只包住等式前半段，后面还跟着附加条件
    (r"最终答案：$f^{(n)}(1)=ne$（$n\ge 1$）", "ne"),
    ("最终答案：f(n)=n^2 (n≥1)", "n^2"),
    # 坐标点末尾的括号是答案本身，不能被当成附加条件删掉
    ("最终答案：(1/2, 1/2)", "(1/2, 1/2)"),
])
def test_cleaning(text, want):
    assert extract_final_answer(text) == want


# ---------- 标记后换行、多个标记、无标记 ----------

def test_answer_on_next_line():
    assert extract_final_answer("最终答案：\n1/2") == "1/2"


def test_last_marker_wins():
    text = "最终答案：1/3\n检查后发现计算有误。\n最终答案：1/2"
    assert extract_final_answer(text) == "1/2"


def test_last_marker_wins_on_same_line():
    """2025-C4 的真实回答：同一行重复标记后再写一次最终答案。"""
    text = (
        "最终答案：B。为符合规则，前面给简短说明？用户说只回答选项字母。"
        "规则要求最后一行“最终答案：B”。我给极少正文。最终答案：B"
    )
    assert extract_final_answer(text) == "B"


def test_fallback_to_last_answer_like_line():
    assert extract_final_answer("经过计算。\n0.5") == "0.5"


def test_fallback_skips_chatter():
    """没有标记时，不应把客套话当成答案。"""
    assert extract_final_answer("解题过程略。\n希望对你有帮助。") is None


# ---------- 空输入 ----------

@pytest.mark.parametrize("text", [None, "", "   \n  "])
def test_empty_returns_none(text):
    assert extract_final_answer(text) is None


def test_keep_assignment_for_parts_judging():
    """多小问判分需要保留 f(0)=-3 的标签。"""
    text = "最终答案：$f(0)=-3$"
    assert extract_final_answer(text) == "-3"
    assert extract_final_answer(text, strip_assignment=False) == "f(0)=-3"


# ---------- 展示用答案 ----------

def test_display_answer_keeps_latex():
    """答案卡片要保留 $...$，不能拿判分器的归一化字符串来展示。"""
    text = "推导略。\n\n最终答案：$\\lim_{n\\to\\infty}x_n=b$"
    assert (
        extract_final_answer_display(text)
        == "$\\lim_{n\\to\\infty}x_n=b$"
    )


def test_display_answer_uses_last_math_on_conclusion_line():
    """没有“最终答案”标记时，结尾结论里的公式仍应完整展示。"""
    text = (
        "推导略。\n\n"
        "最终结论不变：$\\lim_{n\\to\\infty}x_n=b$。"
    )
    assert (
        extract_final_answer_display(text)
        == "$\\lim_{n\\to\\infty}x_n=b$"
    )


def test_display_answer_handles_choice_and_next_line():
    assert extract_final_answer_display("最终答案：B") == "B"
    assert extract_final_answer_display("最终答案：\n$\\frac{1}{2}$") == "$\\frac{1}{2}$"


def test_display_answer_prefers_main_formula_over_condition():
    """$f^{(n)}(1)=ne$（$n\\ge 1$）不能把尾部条件当成答案。"""
    text = r"最终答案：$f^{(n)}(1)=ne$（$n\ge 1$）"
    assert extract_final_answer_display(text) == "$f^{(n)}(1)=ne$"


def test_display_answer_strips_boxed():
    assert (
        extract_final_answer_display(r"最终答案：\boxed{\frac{1}{2}}")
        == r"\frac{1}{2}"
    )


def test_display_answer_respects_no_fallback():
    text = "推导略。\n最终结论：$x=1$"
    assert extract_final_answer_display(text, allow_fallback=False) is None
