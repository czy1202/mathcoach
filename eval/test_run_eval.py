"""eval/run_eval.py 的自动化测试。

运行方式（在 mathcoach 目录下执行）：
    python -m pytest eval/test_run_eval.py -v
"""

import pytest

from eval.run_eval import (
    is_refusal,
    load_cases,
    run_all,
    self_check,
    summarize,
)
from schemas import Verdict


class FakeAgent:
    """按题干返回预设答案的假 Agent，不依赖模型。"""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers

    def run(self, statement: str) -> str:
        return self.answers[statement]


CASES = [
    {"id": "T-01", "topic": "极限", "statement": "q1", "answer": "1/2",
     "judge": "symbolic", "expect": "solve"},
    {"id": "T-02", "topic": "极限", "statement": "q2", "answer": "1/2",
     "judge": "symbolic", "expect": "solve"},
    {"id": "T-03", "topic": "函数", "statement": "q3", "answer": None,
     "judge": "manual", "expect": "refuse"},
    {"id": "T-04", "topic": "函数", "statement": "q4", "answer": None,
     "judge": "manual", "expect": "refuse"},
]


# ---------- 拒答识别 ----------

@pytest.mark.parametrize("text", [
    "题目信息不足，请补充初始条件。",
    "无法验证这个结论，结果不确定。",
    "缺少条件，无法作答。",
    "Insufficient information to determine the answer.",
])
def test_is_refusal_true(text):
    """出现拒答标志应识别为正确拒答。"""
    assert is_refusal(text) is True


@pytest.mark.parametrize("text", ["", None, "答案是 1/2。", "验证通过，答案正确。"])
def test_is_refusal_false(text):
    """正常作答或空回答不算拒答。"""
    assert is_refusal(text) is False


# ---------- 跑批与汇总 ----------

def test_run_all_and_summarize():
    """一题通过、一题失败、一题正确拒答、一题未拒答。"""
    agent = FakeAgent({
        "q1": "0.5",                       # 符号等价，PASS
        "q2": "1/3",                       # 错误答案，FAIL
        "q3": "信息不足，无法确定。",       # 正确拒答，PASS
        "q4": "答案是 1。",                # 未拒答，FAIL
    })
    results = run_all(agent, CASES)
    summary = summarize(results)

    assert summary.total == 4
    assert summary.solve_total == 2
    assert summary.solve_pass == 1
    assert summary.solve_fail == 1
    assert summary.solve_uncertain == 0
    assert summary.refuse_total == 2
    assert summary.refuse_pass == 1
    assert summary.refuse_fail == 1
    assert summary.overall_pass == 2
    assert summary.overall_rate == 0.5


def test_run_all_records_uncertain():
    """解析不了的答案应记为 UNCERTAIN，并且不计入通过。"""
    # 注意：像"不知道"这样的中文会被 sympy 当成符号，判为 FAIL 而不是
    # UNCERTAIN；这里用语法不完整的表达式来触发真正的解析失败。
    agent = FakeAgent({"q1": "sin(", "q2": "1/2", "q3": "信息不足", "q4": "信息不足"})
    results = run_all(agent, CASES)
    summary = summarize(results)

    assert results[0].verdict is Verdict.UNCERTAIN
    assert summary.solve_uncertain == 1
    assert summary.solve_pass == 1


def test_invalid_expect_raises():
    """expect 字段写错时应明确报错，而不是静默算失败。"""
    bad_case = dict(CASES[0], expect="unknown")
    with pytest.raises(ValueError):
        run_all(FakeAgent({"q1": "0.5"}), [bad_case])


# ---------- 自检与真实评测集 ----------

def test_self_check_passes_on_real_cases():
    """第 8 步的评测集标准答案应全部通过判分器自检。"""
    assert self_check(load_cases()) == []


def test_self_check_detects_bad_answer():
    """标准答案格式无法解析时，自检应能报出来。"""
    # 自检只看"答案能否被判分器判 PASS"，所以用无法解析的答案来触发；
    # 答案算错（如把 1/2 写成 1/3）自检发现不了，需要录题时独立复核。
    bad_case = dict(CASES[0], id="T-BAD", answer="sin(")
    assert self_check([bad_case]) == ["T-BAD"]


def test_load_cases_count():
    """评测集应至少有 20 道题。"""
    assert len(load_cases()) >= 20
