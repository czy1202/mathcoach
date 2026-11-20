"""多 Agent 契约的解析测试。"""

from contracts import (
    Plan,
    Review,
    parse_plan,
    parse_review,
)
from schemas import Verdict


def test_parse_plan_from_json():
    plan = parse_plan(
        '{"question_type": "proof", "knowledge_points": ["罗尔定理"], '
        '"missing_info": [], "approach": "构造辅助函数", '
        '"steps_outline": ["构造", "验证"], "retrieval_queries": ["罗尔定理"], '
        '"tools_allowed": ["verify_with_sympy"], "confidence": 0.8}'
    )
    assert isinstance(plan, Plan)
    assert plan.question_type.value == "proof"
    assert plan.knowledge_points == ["罗尔定理"]


def test_parse_plan_accepts_markdown_fence():
    plan = parse_plan('```json\n{"question_type": "solve", "approach": "直接求解"}\n```')
    assert plan is not None
    assert plan.question_type.value == "solve"


def test_parse_plan_invalid_returns_none():
    assert parse_plan("not json") is None
    assert parse_plan("") is None
    assert parse_plan(None) is None


def test_parse_plan_missing_fields_uses_defaults():
    plan = parse_plan('{"question_type": "fill"}')
    assert plan is not None
    assert plan.knowledge_points == []
    assert plan.approach == ""


def test_parse_review_accepts_english_and_chinese_verdict():
    assert parse_review('{"verdict": "PASS"}').verdict is Verdict.PASS
    assert parse_review('{"verdict": "失败"}').verdict is Verdict.FAIL
    assert parse_review('{"verdict": "不确定"}').verdict is Verdict.UNCERTAIN


def test_parse_review_issues():
    review = parse_review(
        '{"verdict": "fail", "issues": ['
        '{"step": 3, "severity": "major", "category": "logic", '
        '"detail": "缺少端点条件"}]}'
    )
    assert isinstance(review, Review)
    assert review.verdict is Verdict.FAIL
    assert review.issues[0].step == 3
    assert review.issues[0].severity.value == "major"
