"""多 Agent 状态机测试，全部使用假角色，不访问网络。"""

from contracts import Budget, Draft, Plan, Review
from multi_agent import Orchestrator
from schemas import Verdict


def _draft(answer: str = "1/2") -> Draft:
    return Draft(
        steps=[{"title": "求值", "content": "过程"}],
        final_answer=answer,
        answer_text=f"解答过程\n最终答案：{answer}",
        confidence=0.8,
    )


class FakePlanner:
    def __init__(self, plan: Plan) -> None:
        self.value = plan
        self.calls = 0

    def plan(self, question: str) -> Plan:
        self.calls += 1
        return self.value


class FakeSolver:
    def __init__(self, drafts: list[Draft]) -> None:
        self.drafts = list(drafts)
        self.solve_calls = 0
        self.revise_calls = 0

    def solve(self, question: str, plan: Plan) -> Draft:
        self.solve_calls += 1
        return self.drafts.pop(0)

    def revise(self, question: str, plan: Plan, draft: Draft, review: Review) -> Draft:
        self.revise_calls += 1
        return self.drafts.pop(0)


class FakeVerifier:
    def __init__(self, reviews: list[Review]) -> None:
        self.reviews = list(reviews)
        self.calls: list[tuple[str, Draft]] = []

    def review(self, question: str, draft: Draft) -> Review:
        self.calls.append((question, draft))
        return self.reviews.pop(0)


def _plan(**kwargs) -> Plan:
    data = {
        "question_type": "solve",
        "approach": "直接求解",
        "tools_allowed": ["verify_with_sympy"],
    }
    data.update(kwargs)
    return Plan(**data)


def test_pass_path_emits_stage_and_final_events():
    events: list[dict] = []
    planner = FakePlanner(_plan())
    solver = FakeSolver([_draft("1/2")])
    verifier = FakeVerifier([Review(verdict=Verdict.PASS)])

    result = Orchestrator(
        planner, solver, verifier, emit=events.append
    ).run("求 1/2")

    assert result.revisions == 0
    assert result.answer.endswith("1/2")
    assert [e["stage"] for e in events if e["type"] == "stage"] == [
        "planner", "planner", "solver", "solver",
        "verifier", "verifier", "done",
    ]
    assert events[-1]["type"] == "final"
    assert events[-1]["verdict"] == "pass"


def test_fill_question_skips_planner_and_retrieval_tools():
    """填空/选择题走快速路径，不调用 Planner，也不要求检索工具。"""
    events: list[dict] = []
    planner = FakePlanner(_plan())
    solver = FakeSolver([_draft("3")])
    verifier = FakeVerifier([Review(verdict=Verdict.PASS)])

    result = Orchestrator(
        planner, solver, verifier, emit=events.append
    ).run("计算极限 ____")

    assert planner.calls == 0
    assert result.plan.question_type.value == "fill"
    assert result.plan.tools_allowed == ["verify_with_sympy"]
    assert any("跳过 Planner" in str(e.get("detail")) for e in events)


def test_missing_info_refuses_before_solving():
    events: list[dict] = []
    planner = FakePlanner(_plan(missing_info=["缺少首项 a_1"]))
    solver = FakeSolver([])
    verifier = FakeVerifier([])

    result = Orchestrator(
        planner, solver, verifier, emit=events.append
    ).run("递推数列求通项")

    assert result.refused is True
    assert solver.solve_calls == 0
    assert verifier.calls == []
    assert "缺少首项" in result.answer
    assert any(e.get("status") == "refused" for e in events)


def test_fail_then_revision_pass():
    events: list[dict] = []
    planner = FakePlanner(_plan())
    solver = FakeSolver([_draft("1/3"), _draft("1/2")])
    verifier = FakeVerifier([
        Review(verdict=Verdict.FAIL, suggested_fix="重新计算"),
        Review(verdict=Verdict.PASS),
    ])

    result = Orchestrator(
        planner, solver, verifier, emit=events.append
    ).run("求极限")

    assert result.revisions == 1
    assert solver.revise_calls == 1
    assert result.review.verdict is Verdict.PASS
    assert result.answer.endswith("1/2")
    assert any(e.get("stage") == "revision" and e.get("status") == "done" for e in events)


def test_uncertain_stops_without_revision():
    planner = FakePlanner(_plan())
    solver = FakeSolver([_draft("1/2")])
    verifier = FakeVerifier([Review(verdict=Verdict.UNCERTAIN)])

    result = Orchestrator(planner, solver, verifier).run("题")

    assert result.revisions == 0
    assert "不确定" in result.warning


def test_max_revisions_stops():
    planner = FakePlanner(_plan())
    solver = FakeSolver([_draft("1/3"), _draft("1/4")])
    verifier = FakeVerifier([
        Review(verdict=Verdict.FAIL),
        Review(verdict=Verdict.FAIL),
    ])

    result = Orchestrator(
        planner, solver, verifier, budget=Budget(max_revisions=1)
    ).run("题")

    assert result.revisions == 1
    assert "最大重解次数" in result.warning


def test_budget_exhaustion_skips_verification():
    planner = FakePlanner(_plan())
    solver = FakeSolver([_draft("1/2")])
    verifier = FakeVerifier([Review(verdict=Verdict.PASS)])

    result = Orchestrator(
        planner,
        solver,
        verifier,
        budget=Budget(max_seconds=0),
    ).run("题")

    assert verifier.calls == []
    assert "预算" in result.warning


def test_verifier_only_receives_question_and_draft():
    planner = FakePlanner(_plan(approach="这个计划不应该出现在 Verifier 里"))
    solver = FakeSolver([_draft("1/2")])
    verifier = FakeVerifier([Review(verdict=Verdict.PASS)])

    Orchestrator(planner, solver, verifier).run("题")

    question, draft = verifier.calls[0]
    assert question == "题"
    assert isinstance(draft, Draft)
