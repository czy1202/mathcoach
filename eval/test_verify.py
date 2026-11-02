"""eval/verify.py 的自动化测试。"""

from dataclasses import dataclass

from eval.run_eval import run_case
from eval.verify import (
    Review,
    judge_candidate,
    parse_review,
    resolve_failure,
)
from schemas import Verdict


SYMBOLIC_CASE = {
    "id": "T-SYM",
    "topic": "测试",
    "expect": "solve",
    "statement": "求 1/2 的值。",
    "answer": "1/2",
    "judge": "symbolic",
}

RUBRIC_CASE = {
    "id": "T-RUBRIC",
    "topic": "测试",
    "expect": "solve",
    "statement": "证明命题。",
    "answer": "得证",
    "judge": "rubric",
    "rubric": [{"label": "结论", "any": ["得证"]}],
}


class FakeVerifier:
    def __init__(self, review: Review) -> None:
        self.review_result = review
        self.calls: list[tuple[str, str]] = []

    def review(self, statement: str, candidate: str) -> Review:
        self.calls.append((statement, candidate))
        return self.review_result


class FakeAgent:
    """只返回预设文本，用于检查重解分支。"""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def run(self, user_input: str, trace=None, history=None) -> str:
        self.prompts.append(user_input)
        return self.answer


# ---------- 复核输出解析 ----------

def test_parse_review_standard_format():
    text = (
        "复核结论：PASS\n"
        "复核答案：$\\frac{1}{2}$\n"
        "复核理由：两边相等。"
    )
    review = parse_review(text)

    assert review.verdict is Verdict.PASS
    assert review.answer == r"$\frac{1}{2}$"
    assert review.reason == "两边相等。"


def test_parse_review_chinese_conclusion():
    review = parse_review("复核结论：失败\n复核答案：1\n复核理由：符号相反。")
    assert review.verdict is Verdict.FAIL


def test_parse_review_last_conclusion_wins():
    """模型先写结论又改口时，以最后一次为准。"""
    text = (
        "复核结论：FAIL\n复核答案：1\n复核理由：先看错了。\n"
        "复核结论：PASS\n复核答案：1\n复核理由：重新验证后成立。"
    )
    review = parse_review(text)

    assert review.verdict is Verdict.PASS
    assert review.reason == "重新验证后成立。"


def test_parse_review_malformed_is_uncertain():
    review = parse_review("我觉得这个答案还行。")
    assert review.verdict is Verdict.UNCERTAIN
    assert review.answer is None
    assert "未按格式" in review.reason


def test_parse_review_empty_is_uncertain():
    assert parse_review(None).verdict is Verdict.UNCERTAIN
    assert parse_review("").verdict is Verdict.UNCERTAIN


# ---------- 用评测集判分方式比对复核答案 ----------

def test_judge_candidate_accepts_latex():
    assert judge_candidate(SYMBOLIC_CASE, r"答案：$\frac12$") is Verdict.PASS


def test_judge_candidate_rejects_wrong_answer():
    assert judge_candidate(SYMBOLIC_CASE, "1/3") is Verdict.FAIL


def test_judge_candidate_handles_parts():
    case = {
        "parts": [
            {"labels": ["f(0)"], "answer": "-3", "judge": "symbolic"},
        ],
        "judge": "parts",
    }
    assert judge_candidate(case, "最终答案：f(0)=-3") is Verdict.PASS


def test_judge_candidate_handles_rubric():
    assert judge_candidate(RUBRIC_CASE, "综上，得证。") is Verdict.PASS
    assert judge_candidate(RUBRIC_CASE, "还没证完。") is Verdict.FAIL


# ---------- 复核 + 重解 ----------

def test_review_answer_matching_official_promotes():
    """复核器独立算出的答案命中标准答案，即使它自己判 FAIL 也应改判通过。"""
    verifier = FakeVerifier(Review(
        Verdict.FAIL, answer="1/2", reason="考生答案写法不规范",
    ))
    agent = FakeAgent("不会被调用")

    outcome = resolve_failure(verifier, agent, SYMBOLIC_CASE, "0.5?", Verdict.FAIL)

    assert outcome.verdict is Verdict.PASS
    assert outcome.manual_review is False
    assert "命中标准答案" in outcome.note
    assert agent.prompts == []


def test_rubric_pass_from_reviewer_promotes():
    """证明题的 rubric 容易误杀，复核器认可论证即可通过。

    复核答案故意写成不含 rubric 关键词的形式，确保走的是“复核认可”分支，
    而不是“复核答案命中标准答案”分支。
    """
    verifier = FakeVerifier(Review(Verdict.PASS, answer="原命题成立", reason="替代证明成立"))
    agent = FakeAgent("不会被调用")

    outcome = resolve_failure(verifier, agent, RUBRIC_CASE, "我的证明……", Verdict.FAIL)

    assert outcome.verdict is Verdict.PASS
    assert outcome.manual_review is True
    assert "待人工确认" in outcome.note


def test_review_fail_triggers_one_resolve_and_passes():
    verifier = FakeVerifier(Review(Verdict.FAIL, answer="1/3", reason="应为 1/2"))
    agent = FakeAgent("最终答案：$\\frac{1}{2}$")

    outcome = resolve_failure(verifier, agent, SYMBOLIC_CASE, "1/3", Verdict.FAIL)

    assert outcome.verdict is Verdict.PASS
    assert outcome.second_answer == "最终答案：$\\frac{1}{2}$"
    assert "重解通过" in outcome.note
    assert "应为 1/2" in agent.prompts[0]
    assert SYMBOLIC_CASE["statement"] in agent.prompts[0]


def test_review_fail_and_resolve_fail_stays_fail():
    verifier = FakeVerifier(Review(Verdict.FAIL, answer="1/3", reason="应为 1/2"))
    agent = FakeAgent("最终答案：1/4")

    outcome = resolve_failure(verifier, agent, SYMBOLIC_CASE, "1/3", Verdict.FAIL)

    assert outcome.verdict is Verdict.FAIL
    assert "重解均未通过" in outcome.note


def test_review_uncertain_keeps_first_verdict():
    verifier = FakeVerifier(Review(Verdict.UNCERTAIN, reason="题目有歧义"))
    agent = FakeAgent("最终答案：1/2")

    outcome = resolve_failure(verifier, agent, SYMBOLIC_CASE, "1/3", Verdict.FAIL)

    assert outcome.verdict is Verdict.FAIL
    assert agent.prompts == []


def test_review_uncertain_after_timeout_keeps_uncertain():
    """首轮是“不确定”（超时/撞上限）时，复核也拿不准就维持不确定。"""
    verifier = FakeVerifier(Review(Verdict.UNCERTAIN, reason="复核也超时"))
    agent = FakeAgent("不会被调用")

    outcome = resolve_failure(verifier, agent, SYMBOLIC_CASE, "错误：超时", Verdict.UNCERTAIN)

    assert outcome.verdict is Verdict.UNCERTAIN


# ---------- 与 run_case 的衔接 ----------

def test_run_case_uses_verifier_only_for_failures():
    """首轮通过时不调用复核；首轮失败时按复核结论改判。"""
    passing = FakeVerifier(Review(Verdict.PASS, answer="1/2"))
    result = run_case(FakeAgent("最终答案：1/2"), SYMBOLIC_CASE, verifier=passing)

    assert result.verdict is Verdict.PASS
    assert result.first_verdict is None
    assert passing.calls == []


def test_run_case_records_review_result():
    verifier = FakeVerifier(Review(Verdict.PASS, answer="1/2", reason="复核确认"))
    result = run_case(FakeAgent("最终答案：1/3"), SYMBOLIC_CASE, verifier=verifier)

    assert result.first_verdict is Verdict.FAIL
    assert result.verdict is Verdict.PASS
    assert "复核确认" in result.review_note
    assert verifier.calls


def test_parse_review_tolerates_markdown_bold():
    """复核器偶尔会加粗，结论和答案仍要能读出来。"""
    text = (
        "**复核结论：PASS**\n"
        r"**复核答案：$\frac{1}{2}$**" "\n"
        "**复核理由：两边等价。**"
    )
    review = parse_review(text)

    assert review.verdict is Verdict.PASS
    assert review.answer == r"$\frac{1}{2}$"
    assert review.reason == "两边等价。"


# ---------- 选择题：复核答案带解释 ----------

CHOICE_CASE = {
    "id": "T-CHOICE",
    "topic": "测试",
    "expect": "solve",
    "statement": "选一个。",
    "answer": "B",
    "judge": "exact",
}


def test_choice_letter_with_explanation_passes():
    """复核器写成“B（I2<I1<I3）”时，开头的字母就是选项。"""
    assert judge_candidate(CHOICE_CASE, "B（I2<I1<I3）") is Verdict.PASS
    assert judge_candidate(CHOICE_CASE, "选项 B：理由略") is Verdict.PASS


def test_choice_letter_mismatch_fails():
    assert judge_candidate(CHOICE_CASE, "C（理由略）") is Verdict.FAIL


def test_choice_letter_ignores_words_starting_with_letter():
    """“Because...” 不能因为首字母是 B 就判成选 B。"""
    assert judge_candidate(CHOICE_CASE, "Because the integral is positive") is Verdict.FAIL


def test_choice_letter_after_reasoning_passes():
    """复核器把理由写在前面、最后说“选 B”，也要能认出 B。"""
    assert judge_candidate(
        CHOICE_CASE, "I2<I1<I3，选 B（I1=π，I2≈1.54，I3>π）"
    ) is Verdict.PASS


def test_choice_letter_inside_sentence_is_not_used():
    """没有明确“选/选项/答案为”时，正文里的字母不算答案。"""
    assert judge_candidate(CHOICE_CASE, "A 和 C 明显不对，我再想想") is not Verdict.PASS
