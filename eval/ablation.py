"""多 Agent 消融：同一批真题，用不同配置求解并对比。

用法：
    python -m eval.ablation --stub                       # 离线跑通流程
    python -m eval.ablation --real --limit 6             # 小样本真实模型
    python -m eval.ablation --real --configs A,C --runs 1

配置：
    A  单 Agent，首轮直接判分（与 run_eval --real 一致）
    B  单 Agent + 失败题独立复核 + 重解（与 run_eval --real --verify 一致）
    C  Planner → Solver → Verifier 多 Agent 编排器

判分口径完全复用 eval/run_eval.py 里的 judge_answer / judge_parts /
judge_rubric，避免两套标准。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import config
from agent import Agent
from contracts import Budget
from eval.extract import extract_final_answer
from eval.judge import judge_answer, judge_parts, judge_rubric
from eval.run_eval import DEFAULT_CASES, load_cases, self_check
from eval.verify import Verifier as EvalVerifier
from llm import OpenAIClient, StubClient
from multi_agent import Orchestrator, Planner, Solver, Verifier
from schemas import Verdict
from tracing import TraceRecorder, new_trace_file


# ---------- 客户端包装：统计调用次数与 token ----------

class CountingClient:
    """给任意客户端套一层，累计本次运行花了多少次调用、多少 token。"""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.reset()

    def reset(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        response = self.inner.chat(messages, tools)
        usage = getattr(response, "usage", None)
        self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        self.total_tokens += getattr(usage, "total_tokens", 0) or 0
        return response

    def chat_stream(self, messages, tools=None, timeout=None):
        # 消融不需要流式，直接走阻塞调用，避免两条路径统计不一致。
        yield {"type": "done", "response": self.chat(messages, tools)}


# ---------- 配置 C：多 Agent 适配器 ----------

class MultiAgentAdapter:
    """把 Orchestrator 包装成与 Agent 相同的 run(question, trace) 接口。"""

    def __init__(self, client, step_timeout: float = 30.0) -> None:
        self.client = client
        self.step_timeout = step_timeout
        self.last: dict = {}

    def run(self, question: str, trace=None) -> str:
        orchestrator = Orchestrator(
            Planner(self.client),
            Solver(self.client, retrieval=False, step_timeout=self.step_timeout),
            Verifier(self.client),
            budget=Budget(),
            trace=trace,
        )
        result = orchestrator.run(question)
        draft = result.draft.answer_text if result.draft else ""
        self.last = {
            "draft_answer": draft,
            "verdict": result.review.verdict.value if result.review else "",
            "revisions": result.revisions,
            "refused": result.refused,
            "warning": result.warning,
        }
        return result.answer


# ---------- 判分 ----------

def judge_case(case: dict, answer: str) -> Verdict:
    """与 run_eval.run_case 相同的判分逻辑。"""
    extracted = extract_final_answer(
        answer, strip_assignment=not case.get("parts")
    )
    answer_for_judge = extracted if extracted is not None else answer
    if case.get("parts"):
        return judge_parts(case["parts"], answer_for_judge)
    if case.get("judge") == "rubric":
        return judge_rubric(case["rubric"], answer)
    return judge_answer(case["answer"], answer_for_judge, case["judge"])


# ---------- 单题结果 ----------

@dataclass
class CaseRun:
    case_id: str
    kind: str
    final_verdict: Verdict
    first_verdict: Verdict
    answer: str
    elapsed: float
    calls: int
    tokens: int


@dataclass
class ConfigResult:
    name: str
    label: str
    runs: list[CaseRun] = field(default_factory=list)

    def summary(self) -> dict:
        total = len(self.runs)
        passed = sum(1 for r in self.runs if r.final_verdict is Verdict.PASS)
        first = sum(1 for r in self.runs if r.first_verdict is Verdict.PASS)
        return {
            "config": self.name,
            "label": self.label,
            "total": total,
            "pass": passed,
            "first_pass": first,
            "pass_rate": passed / total if total else 0.0,
            "first_pass_rate": first / total if total else 0.0,
            "avg_elapsed": statistics.mean([r.elapsed for r in self.runs]) if total else 0.0,
            "avg_tokens": statistics.mean([r.tokens for r in self.runs]) if total else 0.0,
            "avg_calls": statistics.mean([r.calls for r in self.runs]) if total else 0.0,
        }


def run_one(config_name: str, case: dict, client, trace_path: Path | None) -> CaseRun:
    """跑一道题。单题失败记为 FAIL，不中断整批。"""
    counting = CountingClient(client)
    tracer = (
        TraceRecorder(case_id=case["id"], question=case["statement"], path=trace_path)
        if trace_path is not None
        else None
    )

    if config_name == "A":
        solver = Agent(client=counting)
        verifier = None
    elif config_name == "B":
        solver = Agent(client=counting)
        verifier = EvalVerifier(counting)
    elif config_name == "C":
        solver = MultiAgentAdapter(counting)
        verifier = None
    else:
        raise ValueError(f"未知配置：{config_name}")

    start = time.perf_counter()
    try:
        answer = solver.run(case["statement"], trace=tracer)
    except Exception as exc:
        answer = f"错误：求解失败（{type(exc).__name__}: {exc}）"
    elapsed = time.perf_counter() - start

    first_verdict = judge_case(case, answer)
    final_verdict = first_verdict

    if verifier is not None and first_verdict is not Verdict.PASS:
        from eval.verify import resolve_failure

        try:
            outcome = resolve_failure(verifier, solver, case, answer, first_verdict)
        except Exception:
            outcome = None
        if outcome is not None:
            final_verdict = outcome.verdict
            elapsed = time.perf_counter() - start

    if config_name == "C":
        draft = getattr(solver, "last", {}).get("draft_answer") or ""
        if draft:
            first_verdict = judge_case(case, draft)

    if tracer is not None:
        tracer.finish(
            verdict=final_verdict.value,
            first_verdict=first_verdict.value,
            case_elapsed=round(elapsed, 4),
            calls=counting.calls,
            tokens=counting.total_tokens,
        )

    return CaseRun(
        case_id=case["id"],
        kind=case.get("kind", ""),
        final_verdict=final_verdict,
        first_verdict=first_verdict,
        answer=answer,
        elapsed=elapsed,
        calls=counting.calls,
        tokens=counting.total_tokens,
    )


CONFIG_LABELS = {
    "A": "单 Agent",
    "B": "单 Agent + 失败复核",
    "C": "多 Agent（Plan/Solve/Verify）",
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="多 Agent 消融评测")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--real", action="store_true", help="调用真实模型")
    mode.add_argument("--stub", action="store_true", help="离线假模型（默认）")
    parser.add_argument("--configs", default="A,C", help="逗号分隔，例如 A,C")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题")
    parser.add_argument(
        "--kinds", default="", help="只跑指定题型，逗号分隔，如 填空,选择"
    )
    parser.add_argument("--runs", type=int, default=1, help="每种配置重复次数")
    parser.add_argument("--trace-dir", type=Path, default=Path("traces"))
    parser.add_argument("--no-trace", action="store_true")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--out", type=Path, default=Path("traces/ablation.json"))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    real = args.real

    if real and not config.OPENAI_API_KEY.strip():
        print("错误：--real 需要 OPENAI_API_KEY，请先在 .env 中填写。")
        return 1

    cases = load_cases(args.cases)
    if args.kinds:
        wanted = [k.strip() for k in args.kinds.split(",") if k.strip()]
        cases = [c for c in cases if c.get("kind") in wanted]
    if args.limit > 0:
        cases = cases[: args.limit]
    if not cases:
        print("错误：筛选后没有题目。")
        return 1

    bad = self_check(cases)
    if bad:
        print(f"标准答案自检失败：{'、'.join(bad)}")
        return 1

    configs = [c.strip().upper() for c in args.configs.split(",") if c.strip()]
    unknown = [c for c in configs if c not in CONFIG_LABELS]
    if unknown:
        print(f"错误：未知配置 {'、'.join(unknown)}，可选 A/B/C。")
        return 1

    client = OpenAIClient() if real else StubClient()
    model_name = getattr(client, "model", "stub")
    print(
        f"配置 {'/'.join(configs)} · {len(cases)} 题 × {args.runs} 轮 · "
        f"模型 {model_name}"
    )

    results: list[ConfigResult] = []
    for name in configs:
        result = ConfigResult(name=name, label=CONFIG_LABELS[name])
        for run_index in range(args.runs):
            for case in cases:
                trace_path = (
                    None
                    if args.no_trace
                    else new_trace_file(args.trace_dir, prefix=f"abl-{name}")
                )
                run = run_one(name, case, client, trace_path)
                result.runs.append(run)
                mark = "✓" if run.final_verdict is Verdict.PASS else "✗"
                print(
                    f"  [{name}{run_index + 1}] {case['id']:<10} {mark} "
                    f"{run.elapsed:5.1f}s {run.tokens:>6} tok {run.calls} 次调用"
                )
        results.append(result)

    summaries = [r.summary() for r in results]
    print("\n| 配置 | 首轮通过 | 最终通过 | 平均耗时 | 平均 tokens | 平均调用 |")
    print("|---|---|---|---|---|---|")
    for s in summaries:
        print(
            f"| {s['label']} | {s['first_pass']}/{s['total']} "
            f"({s['first_pass_rate']:.1%}) | {s['pass']}/{s['total']} "
            f"({s['pass_rate']:.1%}) | {s['avg_elapsed']:.1f}s | "
            f"{s['avg_tokens']:.0f} | {s['avg_calls']:.1f} |"
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "real": real,
                "model": model_name,
                "cases": [c["id"] for c in cases],
                "runs_per_config": args.runs,
                "summary": summaries,
                "details": [
                    {
                        "config": r.name,
                        "cases": [
                            {
                                "id": run.case_id,
                                "final": run.final_verdict.value,
                                "first": run.first_verdict.value,
                                "elapsed": round(run.elapsed, 3),
                                "tokens": run.tokens,
                                "calls": run.calls,
                            }
                            for run in r.runs
                        ],
                    }
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n结果已写入：{args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
