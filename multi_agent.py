"""Planner → Solver → Verifier 多 Agent 编排。

设计原则：
1. 编排器是确定性状态机，不是 LLM；每个状态都有明确出口和预算。
2. 角色之间只传 contracts.py 里的结构化对象，不传自由文本。
3. Verifier 看不到 Plan、Solver 的工具轨迹和标准答案，保持独立。
4. 第一版最多重解一次；重解只针对 Verifier 指出的 issues。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

from agent import RETRIEVAL_PROMPT, SYSTEM_PROMPT, Agent
from contracts import (
    DEFAULT_ALLOWED_TOOLS,
    Budget,
    Draft,
    Plan,
    QuestionType,
    Review,
    default_plan,
    parse_plan,
    parse_review,
)
from eval.extract import extract_final_answer
from solution import extract_solution_steps
from schemas import Verdict


PLANNER_PROMPT = r"""你是一名数学竞赛解题规划器。你只负责“看懂题、定策略”，不负责解题。

请只输出一个 JSON 对象，不要 Markdown 代码块，不要额外解释：
{
  "question_type": "fill|choice|solve|proof|unknown",
  "knowledge_points": ["..."],
  "missing_info": ["如果题干缺少必要条件，逐条列出；否则为空数组"],
  "approach": "一句话说明推荐解法",
  "steps_outline": ["第一步做什么", "第二步做什么"],
  "retrieval_queries": ["用于检索同类真题的查询，1-3 条"],
  "tools_allowed": ["从 verify_with_sympy / search_past_problems / get_past_problem / list_past_problems 中选择"],
  "answer_form": "最终答案应写成什么形式",
  "confidence": 0.0
}

规则：
- 不要推导公式，不要给最终答案。
- 证明题不要要求 sympy 验证最终答案，可以允许 sympy 验证中间代数步骤。
- 如果题干缺条件（例如递推数列没有首项、函数没有表达式），把缺失项写进 missing_info，不要猜。"""


SOLVER_PROMPT_BASE = (
    SYSTEM_PROMPT
    + r"""

你还会收到一个 Planner 给出的解题计划。请按照计划求解：
- 不要忽略计划里的题型和策略；
- 如果计划明显不适用，可以调整，但要在解答中说明；
- 最终回答仍然按系统要求给出知识点、思路、步骤和最后一行“最终答案：”。
"""
)
SOLVER_PROMPT = SOLVER_PROMPT_BASE + "\n\n" + RETRIEVAL_PROMPT


VERIFIER_PROMPT = r"""你是一名完全独立的数学复核员。你没有标准答案，也不要假设考生答案正确。

复核步骤：
1. 只做逻辑与结论审计，不要从头写完整解答，也不要调用工具；
   数值计算由 Solver 的工具验证负责。
2. 把考生答案与你的结论逐步比较：
   - 结论一致、推导没有实质缺口 -> pass；
   - 答案不同，或证明存在实质缺口 -> fail；
   - 题目信息不足，或你确实无法确定 -> uncertain。
3. 不要为了迎合考生而改判，也不要因为写法不同就判错。
4. 如果工具返回“无法判定”，不要重复同一个调用，换一种等价写法再验证。

只输出一个 JSON 对象，不要 Markdown 代码块，不要额外解释：
{
  "verdict": "pass|fail|uncertain",
  "independent_answer": "你独立得到的简洁答案；证明题写“得证”",
  "issues": [
    {
      "step": 1,
      "severity": "minor|major",
      "category": "logic|calculation|missing_case|format|other",
      "detail": "具体问题"
    }
  ],
  "suggested_fix": "如果需要修改，给出最小修正建议",
  "confidence": 0.0
}"""


def _plan_text(plan: Plan) -> str:
    lines = [
        f"题型：{plan.question_type.value}",
        f"知识点：{'、'.join(plan.knowledge_points) or '未指定'}",
        f"推荐策略：{plan.approach or '直接求解'}",
    ]
    if plan.steps_outline:
        lines.append("步骤大纲：")
        lines.extend(f"- {item}" for item in plan.steps_outline)
    if plan.tools_allowed:
        lines.append(f"允许工具：{'、'.join(plan.tools_allowed)}")
    if plan.answer_form:
        lines.append(f"答案形式：{plan.answer_form}")
    return "\n".join(lines)


def quick_question_type(question: str) -> QuestionType:
    """规则快速判断题型，避免所有题都先跑一次 Planner。"""
    text = question.strip()
    if re.search(r"证明|求证|试证", text):
        return QuestionType.proof
    if "选项" in text or re.search(r"(?m)^\s*[A-D][.、]", text):
        return QuestionType.choice
    if "____" in text or "填空" in text:
        return QuestionType.fill
    return QuestionType.solve


def _draft_from_answer(answer: str) -> Draft:
    steps_data = extract_solution_steps(answer) or {}
    steps = [
        {
            "title": str(step.get("title") or ""),
            "content": str(step.get("markdown") or ""),
        }
        for step in steps_data.get("steps", [])
    ]
    return Draft(
        steps=steps,
        final_answer=extract_final_answer(answer) or "",
        answer_text=answer,
        confidence=0.7,
    )


class Planner:
    """一次规划调用，不开放工具。"""

    def __init__(self, client) -> None:
        self.client = client

    def plan(self, question: str) -> Plan:
        try:
            response = self.client.chat([
                {"role": "system", "content": PLANNER_PROMPT},
                {"role": "user", "content": f"题目：\n{question}"},
            ])
            return parse_plan(response.content) or default_plan()
        except Exception:
            return default_plan()


class Solver:
    """复用现有 Agent 循环执行计划。

    准备两套 Agent：带检索和不带检索。简单填空/选择题不加载 BGE、
    也不多跑一轮检索工具，显著降低首题延迟。
    """

    def __init__(
        self,
        client,
        *,
        retrieval: bool = False,
        max_steps: int = 15,
        step_timeout: float = 60.0,
    ) -> None:
        self.retrieval = retrieval
        self.agent_plain = Agent(
            client=client,
            retrieval=False,
            max_steps=max_steps,
            step_timeout=step_timeout,
            system_prompt=SOLVER_PROMPT_BASE,
        )
        self.agent_retrieval = (
            Agent(
                client=client,
                retrieval=True,
                max_steps=max_steps,
                step_timeout=step_timeout,
                system_prompt=SOLVER_PROMPT_BASE + "\n\n" + RETRIEVAL_PROMPT,
            )
            if retrieval
            else self.agent_plain
        )

    def _agent_for(self, plan: Plan):
        if self.retrieval and "search_past_problems" in plan.tools_allowed:
            return self.agent_retrieval
        return self.agent_plain

    def solve(self, question: str, plan: Plan) -> Draft:
        prompt = (
            f"题目：\n{question}\n\n"
            f"Planner 给出的计划：\n{_plan_text(plan)}\n\n"
            "请按计划完整求解。"
        )
        return _draft_from_answer(self._agent_for(plan).run(prompt))

    def revise(self, question: str, plan: Plan, draft: Draft, review: Review) -> Draft:
        issues = "\n".join(
            f"- 第 {issue.step or '?'} 步 [{issue.severity.value}/{issue.category.value}]：{issue.detail}"
            for issue in review.issues
        ) or "- 复核结论为失败，但没有给出具体步骤"
        prompt = (
            f"题目：\n{question}\n\n"
            f"原计划：\n{_plan_text(plan)}\n\n"
            f"你之前的解答：\n{draft.answer_text}\n\n"
            f"独立复核意见：\n{issues}\n"
            f"建议修正：{review.suggested_fix or '无'}\n\n"
            "请只针对以上问题修正，未涉及的部分保持不动，重新输出完整解答。"
        )
        return _draft_from_answer(self._agent_for(plan).run(prompt))


class Verifier:
    """独立上下文复核，只拿题目和候选解答。

    运行时复核只做一次无工具的快速审计，避免工具调用把延迟拉高；
    评测期仍使用 eval/verify.py 里带工具的复核器。
    """

    def __init__(self, client) -> None:
        self.client = client

    def review(self, question: str, draft: Draft) -> Review:
        candidate = draft.answer_text or draft.final_answer
        prompt = (
            f"题目：\n{question}\n\n"
            f"考生解答：\n{candidate}\n\n"
            "请独立核对并复核，只输出 JSON。"
        )
        try:
            response = self.client.chat([
                {"role": "system", "content": VERIFIER_PROMPT},
                {"role": "user", "content": prompt},
            ])
            return parse_review(response.content) or Review(
                verdict=Verdict.UNCERTAIN,
                suggested_fix="复核器没有按 JSON 格式输出，无法判断。",
            )
        except Exception as exc:
            return Review(
                verdict=Verdict.UNCERTAIN,
                suggested_fix=f"复核失败：{type(exc).__name__}",
            )


@dataclass
class MultiAgentResult:
    answer: str
    plan: Plan | None = None
    draft: Draft | None = None
    review: Review | None = None
    revisions: int = 0
    refused: bool = False
    warning: str = ""
    elapsed: float = 0.0
    stages: list[dict] = field(default_factory=list)

    def to_event(self) -> dict:
        return {
            "type": "final",
            "answer": self.answer,
            "final_answer": (self.draft.final_answer if self.draft else ""),
            "verdict": (self.review.verdict.value if self.review else Verdict.UNCERTAIN.value),
            "revisions": self.revisions,
            "refused": self.refused,
            "warning": self.warning,
            "elapsed": round(self.elapsed, 3),
        }


class Orchestrator:
    """确定性状态机：PLANNING → SOLVING → VERIFYING → REVISING → DONE。"""

    def __init__(
        self,
        planner,
        solver,
        verifier,
        *,
        budget: Budget | None = None,
        emit: Callable[[dict], None] | None = None,
        trace=None,
    ) -> None:
        self.planner = planner
        self.solver = solver
        self.verifier = verifier
        self.budget = budget or Budget()
        self.emit = emit
        self.trace = trace
        self.stages: list[dict] = []

    def _event(self, event_type: str, **fields) -> None:
        event = {"type": event_type, **fields}
        self.stages.append(event)
        if self.emit is not None:
            self.emit(event)
        if self.trace is not None:
            self.trace.add(event_type, **fields)

    def _stage(self, stage: str, status: str, detail: str = "") -> None:
        self._event("stage", stage=stage, status=status, detail=detail)

    def run(self, question: str) -> MultiAgentResult:
        started = time.perf_counter()
        revisions = 0

        quick_type = quick_question_type(question)
        if quick_type in (QuestionType.fill, QuestionType.choice):
            # 填空/选择题通常不需要复杂规划，也不检索真题；直接给默认计划。
            plan = Plan(
                question_type=quick_type,
                approach="直接求解，关键步骤用 sympy 验证。",
                tools_allowed=["verify_with_sympy"],
                confidence=0.6,
            )
            self._stage(
                "planner", "done",
                f"快速识别为 {quick_type.value}，跳过 Planner",
            )
        else:
            self._stage("planner", "running", "正在判断题型和解题策略")
            plan = self.planner.plan(question)
            if plan.missing_info:
                detail = "；".join(plan.missing_info)
                self._stage("planner", "refused", f"题干信息不全：{detail}")
                answer = "题干信息不全，请先补充以下条件：\n" + "\n".join(
                    f"- {item}" for item in plan.missing_info
                )
                result = MultiAgentResult(
                    answer=answer,
                    plan=plan,
                    refused=True,
                    elapsed=time.perf_counter() - started,
                    stages=self.stages,
                )
                self._event("final", **result.to_event())
                return result
            self._stage(
                "planner", "done",
                f"题型：{plan.question_type.value}；策略：{plan.approach or '直接求解'}",
            )

        self._stage("solver", "running", "正在按计划解题")
        try:
            draft = self.solver.solve(question, plan)
        except Exception as exc:
            self._stage("solver", "error", f"解题失败：{type(exc).__name__}")
            result = MultiAgentResult(
                answer=f"错误：解题失败（{type(exc).__name__}: {exc}）",
                plan=plan,
                warning="solver_error",
                elapsed=time.perf_counter() - started,
                stages=self.stages,
            )
            self._event("final", **result.to_event())
            return result
        self._stage("solver", "done", f"已生成解答，最终答案：{draft.final_answer or '未提取'}")

        review: Review | None = None
        warning = ""
        while True:
            if time.perf_counter() - started > self.budget.max_seconds:
                warning = "总耗时超过预算，已停止复核。"
                break
            self._stage("verifier", "running", "正在独立复核")
            try:
                review = self.verifier.review(question, draft)
            except Exception as exc:
                review = Review(
                    verdict=Verdict.UNCERTAIN,
                    suggested_fix=f"复核失败：{type(exc).__name__}",
                )
            self._stage(
                "verifier", "done",
                f"复核结论：{review.verdict.value}"
                + (f"；{review.suggested_fix}" if review.suggested_fix else ""),
            )

            if review.verdict is Verdict.PASS:
                break
            if review.verdict is Verdict.UNCERTAIN:
                warning = "复核未确认正确，结果标记为不确定。"
                break
            if revisions >= self.budget.max_revisions:
                warning = "复核未通过，且已达到最大重解次数。"
                break

            revisions += 1
            self._stage("revision", "running", "正在按复核意见定向重解")
            try:
                draft = self.solver.revise(question, plan, draft, review)
            except Exception as exc:
                warning = f"重解失败：{type(exc).__name__}"
                self._stage("revision", "error", warning)
                break
            self._stage("revision", "done", f"第 {revisions} 次重解完成")

        answer = draft.answer_text or draft.final_answer or "错误：没有生成解答。"
        result = MultiAgentResult(
            answer=answer,
            plan=plan,
            draft=draft,
            review=review,
            revisions=revisions,
            warning=warning,
            elapsed=time.perf_counter() - started,
            stages=self.stages,
        )
        self._stage("done", "done", "多 Agent 流程结束")
        self._event("final", **result.to_event())
        return result
