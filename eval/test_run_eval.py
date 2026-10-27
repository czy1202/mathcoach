"""eval/run_eval.py 的自动化测试。

运行方式（在 mathcoach 目录下执行）：
    python -m pytest eval/test_run_eval.py -v
"""

import json

import pytest

import config
from eval.run_eval import (
    build_client,
    is_refusal,
    load_cases,
    main,
    parse_args,
    run_all,
    self_check,
    summarize,
)
from llm import StubClient
from schemas import Verdict


class FakeAgent:
    """按题干返回预设答案的假 Agent，不依赖模型。"""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers

    def run(self, statement: str, trace=None) -> str:
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
    "题干未给出首项 a_1，需要您补充首项后再求解。",
    "没有给定初始条件，通项公式无法唯一确定。",
    "该命题是未解决的公开难题，无法给出证明。",
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


# ---------- 命令行开关 ----------

def test_parse_args_defaults_to_stub():
    """不传开关时默认离线模式。"""
    args = parse_args([])
    assert args.real is False
    assert isinstance(build_client(args.real), StubClient)


def test_parse_args_real():
    """--real 应打开真实模型模式。"""
    assert parse_args(["--real"]).real is True
    assert parse_args(["--stub"]).real is False


def test_real_and_stub_are_mutually_exclusive():
    """两个开关同时给出时由 argparse 报错退出。"""
    with pytest.raises(SystemExit) as exc:
        parse_args(["--real", "--stub"])
    assert exc.value.code == 2


def test_real_without_api_key_prints_hint(monkeypatch, capsys):
    """缺 OPENAI_API_KEY 时应友好提示并返回 1，而不是抛异常。"""
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")

    assert main(["--real"]) == 1

    out = capsys.readouterr().out
    assert "OPENAI_API_KEY" in out
    assert ".env" in out
    assert "python -m eval.run_eval --real" in out


def test_stub_run_completes(capsys):
    """--stub 应完整跑完并输出评测报告。"""
    assert main(["--stub", "--no-trace"]) == 0

    out = capsys.readouterr().out
    assert "评测报告" in out
    assert "StubClient" in out


def test_parse_args_trace_and_limit():
    """轨迹与冒烟参数应被正确解析。"""
    args = parse_args(["--stub", "--trace-dir", "tmp-traces",
                       "--limit", "3", "--no-trace"])
    assert args.trace_dir == "tmp-traces"
    assert args.limit == 3
    assert args.no_trace is True


def test_limit_runs_subset(capsys):
    """--limit 应只跑前 N 题。"""
    assert main(["--stub", "--limit", "2", "--no-trace"]) == 0

    out = capsys.readouterr().out
    assert "只跑前 2 题" in out
    assert "总题数：2" in out


# ---------- 答案提取接入判分 ----------

def test_extraction_is_used_before_judging():
    """完整讲解 + 最终答案行，应提取后判为 PASS。"""
    agent = FakeAgent({
        "q1": "知识点：极限。\n解题过程：...\n最终答案：1/2",
        "q2": "最终答案：1/3",
    })
    results = run_all(agent, CASES[:2])

    assert results[0].verdict is Verdict.PASS
    assert results[0].extracted == "1/2"
    assert results[1].verdict is Verdict.FAIL


def test_trace_file_written(tmp_path):
    """指定 trace 路径时，每道题写一行 JSONL。"""
    trace_path = tmp_path / "trace.jsonl"
    agent = FakeAgent({"q1": "最终答案：0.5", "q2": "最终答案：1/3"})

    run_all(agent, CASES[:2], trace_path)

    lines = trace_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["case_id"] == "T-01"
    assert first["verdict"] == "pass"
    assert first["extracted"] == "0.5"
    assert first["question"] == "q1"
