"""跑批脚本：逐题调用 Agent，判分并输出评测报告。

运行方式（在 mathcoach 根目录下执行）：
    python -m eval.run_eval

判定规则：
    expect="solve"   调 judge_answer 比较模型答案与标准答案
    expect="refuse"  检查回答里是否出现"不确定 / 信息不足"等拒答标志，
                     出现算正确拒答，否则算未拒答

注意：当前 Agent 默认使用 StubClient 假模型，跑出来的分数只用于验证
评测流程是否通畅，不代表真实模型水平。第 10 步换成 OpenAIClient 后，
这份报告才有评估意义。
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path

from agent import Agent
from eval.judge import judge_answer
from schemas import Verdict


DEFAULT_CASES = Path(__file__).resolve().parent / "cases.jsonl"

# 拒答标志：回答里出现任意一个，就算模型明确表示"答不了"
# 这是 v1 的关键词匹配，简单但会有误判，后续可换成更可靠的判定
REFUSAL_MARKERS = (
    "不确定", "无法确定", "无法验证", "无法判断", "无法作答", "不能作答",
    "信息不足", "信息不全", "条件不足", "条件不全", "缺少条件", "不足以",
    "拒答", "请补充", "需要补充",
    "uncertain", "insufficient information", "cannot determine", "unable to verify",
)


# ---------- 结果结构 ----------

@dataclass
class CaseResult:
    """一道题的评测结果。"""

    id: str
    topic: str
    expect: str             # solve 或 refuse
    verdict: Verdict        # 通过 / 失败 / 不确定
    answer: str             # 模型给出的答案（用于摘要展示）
    elapsed: float = 0.0    # 该题耗时（秒）


@dataclass
class EvalSummary:
    """整批评测的汇总指标。"""

    total: int = 0
    solve_total: int = 0
    solve_pass: int = 0
    solve_fail: int = 0
    solve_uncertain: int = 0
    refuse_total: int = 0
    refuse_pass: int = 0

    @property
    def refuse_fail(self) -> int:
        return self.refuse_total - self.refuse_pass

    @property
    def overall_pass(self) -> int:
        return self.solve_pass + self.refuse_pass

    @property
    def overall_rate(self) -> float:
        return self.overall_pass / self.total if self.total else 0.0


# ---------- 读取与判定 ----------

def load_cases(path: Path | None = None) -> list[dict]:
    """逐行读取 JSONL 评测集。"""
    path = path or DEFAULT_CASES
    cases: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path} 第 {line_no} 行不是合法 JSON：{e}") from e
    return cases


def is_refusal(text: str | None) -> bool:
    """判断模型回答是否明确表示无法作答。"""
    if not text or not text.strip():
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in REFUSAL_MARKERS)


def self_check(cases: list[dict]) -> list[str]:
    """预检：所有 solve 题的标准答案必须能被判分器判为 PASS。

    注意：自检只能发现"答案格式无法解析 / 判定方式配错"这类问题，
    不能发现"答案本身算错"。数学正确性要在录题时独立复核。

    Returns:
        list[str]: 自检失败的题目 id；为空表示全部通过。
    """
    bad: list[str] = []
    for case in cases:
        if case["expect"] != "solve":
            continue
        result = judge_answer(case["answer"], case["answer"], case["judge"])
        if result is not Verdict.PASS:
            bad.append(case["id"])
    return bad


def run_case(agent, case: dict) -> CaseResult:
    """跑一道题并判定。"""
    start = time.perf_counter()
    actual = agent.run(case["statement"])
    elapsed = time.perf_counter() - start

    if case["expect"] == "solve":
        verdict = judge_answer(case["answer"], actual, case["judge"])
    elif case["expect"] == "refuse":
        verdict = Verdict.PASS if is_refusal(actual) else Verdict.FAIL
    else:
        raise ValueError(f"{case['id']} 的 expect 字段非法：{case['expect']!r}")

    return CaseResult(
        id=case["id"],
        topic=case["topic"],
        expect=case["expect"],
        verdict=verdict,
        answer=actual or "",
        elapsed=elapsed,
    )


def run_all(agent, cases: list[dict]) -> list[CaseResult]:
    """逐题跑完整批评测。"""
    return [run_case(agent, case) for case in cases]


def summarize(results: list[CaseResult]) -> EvalSummary:
    """汇总指标。"""
    summary = EvalSummary(total=len(results))
    for r in results:
        if r.expect == "solve":
            summary.solve_total += 1
            if r.verdict is Verdict.PASS:
                summary.solve_pass += 1
            elif r.verdict is Verdict.FAIL:
                summary.solve_fail += 1
            else:
                summary.solve_uncertain += 1
        else:
            summary.refuse_total += 1
            if r.verdict is Verdict.PASS:
                summary.refuse_pass += 1
    return summary


# ---------- 报告输出 ----------

def _rate(passed: int, total: int) -> str:
    return f"{passed / total * 100:.1f}%" if total else "-"


def _summary(text: str, limit: int = 50) -> str:
    """把模型答案压成一行短摘要。"""
    text = " ".join(text.split())
    if not text:
        return "(空)"
    return text if len(text) <= limit else text[:limit] + "…"


def _status(result: CaseResult) -> str:
    """把人看的判定结果显示成中文。"""
    if result.expect == "refuse":
        return "正确拒答" if result.verdict is Verdict.PASS else "未拒答"
    return {
        Verdict.PASS: "通过",
        Verdict.FAIL: "失败",
        Verdict.UNCERTAIN: "不确定",
    }[result.verdict]


def print_report(results: list[CaseResult], client_name: str) -> None:
    """打印逐题结果与汇总指标。"""
    summary = summarize(results)

    print("=" * 72)
    print(f"mathcoach 评测报告（模型客户端：{client_name}）")
    if client_name == "StubClient":
        print("注意：当前是 StubClient 假模型，分数仅用于验证流程，不代表真实水平。")
    print("=" * 72)

    for r in results:
        print(f"{r.id:5s} | {r.topic:6s} | {r.expect:6s} | {_status(r):6s} | "
              f"{_summary(r.answer)}")

    total_elapsed = sum(r.elapsed for r in results)
    failed = [r.id for r in results if r.verdict is not Verdict.PASS]

    print("-" * 72)
    print(f"总题数：{summary.total}")
    print(f"solve：{summary.solve_pass}/{summary.solve_total} 通过"
          f"（{_rate(summary.solve_pass, summary.solve_total)}），"
          f"失败 {summary.solve_fail}，不确定 {summary.solve_uncertain}")
    print(f"refuse：{summary.refuse_pass}/{summary.refuse_total} 正确拒答"
          f"（{_rate(summary.refuse_pass, summary.refuse_total)}），"
          f"未拒答 {summary.refuse_fail}")
    print(f"总通过率：{summary.overall_pass}/{summary.total}"
          f"（{_rate(summary.overall_pass, summary.total)}）")
    if results:
        print(f"总耗时：{total_elapsed:.2f} 秒，"
              f"平均每题 {total_elapsed / len(results):.2f} 秒")
    else:
        print("总耗时：0 秒")
    print(f"未通过题目：{'、'.join(failed) if failed else '无'}")


# ---------- 入口 ----------

def main() -> None:
    cases = load_cases()

    # 先确认评测集本身没写错，否则后面的分数没有意义
    bad = self_check(cases)
    if bad:
        print(f"标准答案自检失败：{'、'.join(bad)} 无法被判分器判为 PASS，已中止。")
        return
    solve_count = sum(1 for c in cases if c["expect"] == "solve")
    print(f"标准答案自检通过：{solve_count}/{solve_count} 道 solve 题均判为 PASS")

    agent = Agent()    # 默认 StubClient；第 10 步可改为 Agent(client=OpenAIClient())
    results = run_all(agent, cases)
    print_report(results, type(agent.client).__name__)


if __name__ == "__main__":
    main()
