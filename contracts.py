"""多 Agent 的结构化契约。

Planner / Solver / Verifier 之间不传自由文本，而是传这些经过校验的对象。
所有解析函数在失败时返回 None 或默认对象，调用方负责回退，不抛异常。
"""

from __future__ import annotations

import json
import re
from enum import Enum

from pydantic import BaseModel, Field

from schemas import Verdict


class QuestionType(str, Enum):
    fill = "fill"
    choice = "choice"
    solve = "solve"
    proof = "proof"
    unknown = "unknown"


class IssueSeverity(str, Enum):
    minor = "minor"
    major = "major"


class IssueCategory(str, Enum):
    logic = "logic"
    calculation = "calculation"
    missing_case = "missing_case"
    format = "format"
    other = "other"


class Issue(BaseModel):
    step: int = 0
    severity: IssueSeverity = IssueSeverity.minor
    category: IssueCategory = IssueCategory.other
    detail: str = ""


class Plan(BaseModel):
    question_type: QuestionType = QuestionType.unknown
    knowledge_points: list[str] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)
    approach: str = ""
    steps_outline: list[str] = Field(default_factory=list)
    retrieval_queries: list[str] = Field(default_factory=list)
    tools_allowed: list[str] = Field(default_factory=list)
    answer_form: str = ""
    confidence: float = 0.0


class Draft(BaseModel):
    steps: list[dict] = Field(default_factory=list)
    final_answer: str = ""
    answer_text: str = ""
    confidence: float = 0.0
    tools_used: list[str] = Field(default_factory=list)
    open_issues: list[str] = Field(default_factory=list)


class Review(BaseModel):
    verdict: Verdict = Verdict.UNCERTAIN
    independent_answer: str = ""
    issues: list[Issue] = Field(default_factory=list)
    suggested_fix: str = ""
    confidence: float = 0.0


class Budget(BaseModel):
    max_solver_steps: int = 15
    max_verifier_steps: int = 20
    max_revisions: int = 1
    max_seconds: float = 180.0
    max_llm_calls: int = 12


DEFAULT_ALLOWED_TOOLS = [
    "verify_with_sympy",
    "search_past_problems",
    "get_past_problem",
    "list_past_problems",
]


def parse_json_object(text: str | None) -> dict | None:
    """从模型输出里取第一个 JSON 对象，兼容 Markdown 代码围栏。"""
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(cleaned[start:end + 1])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def parse_plan(text: str | None) -> Plan | None:
    data = parse_json_object(text)
    if data is None:
        return None
    try:
        return Plan.model_validate(data)
    except Exception:
        return None


_VERDICT_ALIASES = {
    "pass": Verdict.PASS,
    "通过": Verdict.PASS,
    "正确": Verdict.PASS,
    "fail": Verdict.FAIL,
    "失败": Verdict.FAIL,
    "错误": Verdict.FAIL,
    "uncertain": Verdict.UNCERTAIN,
    "不确定": Verdict.UNCERTAIN,
}


def _coerce_verdict(value: object) -> Verdict:
    key = str(value or "").strip().lower()
    return _VERDICT_ALIASES.get(key, Verdict.UNCERTAIN)


def parse_review(text: str | None) -> Review | None:
    data = parse_json_object(text)
    if data is None:
        return None
    data = dict(data)
    data["verdict"] = _coerce_verdict(data.get("verdict"))
    try:
        return Review.model_validate(data)
    except Exception:
        return None


def default_plan() -> Plan:
    """Planner 失败时的兜底计划：不阻塞解题，退化为单 Agent。"""
    return Plan(
        question_type=QuestionType.unknown,
        approach="直接求解，必要时调用工具验证。",
        tools_allowed=list(DEFAULT_ALLOWED_TOOLS),
        confidence=0.0,
    )
