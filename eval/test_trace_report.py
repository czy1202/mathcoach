"""eval/trace_report.py 的自动化测试。"""

import json

from eval.trace_report import load_traces, summarize_traces


def _record(case_id, verdict, elapsed, usage, llm_calls=1, tool_calls=0,
            tool_errors=0, events=None):
    return {
        "case_id": case_id,
        "verdict": verdict,
        "elapsed": elapsed,
        "usage": usage,
        "llm_calls": llm_calls,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "events": events or [],
    }


def test_summarize_traces():
    records = [
        _record("T-01", "pass", 1.0,
                {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                llm_calls=2, tool_calls=1),
        _record("T-02", "fail", 3.0,
                {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                llm_calls=1, tool_calls=1, tool_errors=1,
                events=[{"type": "error", "kind": "timeout"}]),
        _record("T-03", "uncertain", 2.0,
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
    ]

    s = summarize_traces(records)

    assert s["total"] == 3
    assert s["pass"] == 1
    assert s["fail"] == 1
    assert s["uncertain"] == 1
    assert s["elapsed_total"] == 6.0
    assert s["elapsed_avg"] == 2.0
    assert s["total_tokens"] == 45
    assert s["llm_calls"] == 4
    assert s["tool_calls"] == 2
    assert s["tool_errors"] == 1
    assert s["tool_error_rate"] == 0.5
    assert s["error_kinds"] == {"timeout": 1}


def test_load_traces_skips_bad_lines(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(
        json.dumps({"case_id": "T-01"}) + "\n"
        + "not json\n"
        + "\n"
        + json.dumps({"case_id": "T-02"}) + "\n",
        encoding="utf-8",
    )

    records = load_traces(path)

    assert [r["case_id"] for r in records] == ["T-01", "T-02"]


def test_summarize_empty():
    s = summarize_traces([])
    assert s["total"] == 0
    assert s["elapsed_avg"] == 0.0
    assert s["tool_error_rate"] == 0.0
