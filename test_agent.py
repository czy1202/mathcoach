"""agent.py 的自动化测试。

运行方式（在 mathcoach 目录下执行）：
    pytest -v
"""

from agent import Agent
from llm import LLMResponse, ToolCall
from tracing import TraceRecorder


class RecordingClient:
    """记录每次请求的消息列表，用于检查回填格式。"""

    def __init__(self, tool_call_id: str = "call_test_1") -> None:
        self.tool_call_id = tool_call_id
        self.requests: list[list[dict]] = []

    def chat(self, messages, tools=None):
        self.requests.append([dict(m) for m in messages])
        if len(self.requests) == 1:
            return LLMResponse(tool_calls=[ToolCall(
                name="verify_with_sympy",
                args={"expr_a": "(x + 1)**2", "expr_b": "x**2 + 2*x + 1"},
                id=self.tool_call_id,
            )])
        return LLMResponse(content="最终答案：1/2")


def _find(messages: list[dict], role: str) -> dict:
    return next(m for m in messages if m["role"] == role)


def test_tool_call_round_trip_keeps_id():
    """第二轮请求必须带上 assistant 的 tool_calls 和对应的 tool_call_id。"""
    client = RecordingClient()
    answer = Agent(client=client).run("测试题")

    assert answer == "最终答案：1/2"

    second = client.requests[1]
    assistant = _find(second, "assistant")
    tool = _find(second, "tool")

    assert assistant["tool_calls"][0]["id"] == "call_test_1"
    assert assistant["tool_calls"][0]["function"]["name"] == "verify_with_sympy"
    assert tool["tool_call_id"] == "call_test_1"
    assert tool["content"] == "True"


def test_missing_tool_call_id_gets_generated():
    """模型没给 id 时生成一个，保证 assistant 与 tool 两边仍然对得上。"""
    client = RecordingClient(tool_call_id="")
    Agent(client=client).run("测试题")

    second = client.requests[1]
    assistant = _find(second, "assistant")
    tool = _find(second, "tool")

    generated = assistant["tool_calls"][0]["id"]
    assert generated
    assert tool["tool_call_id"] == generated


# ---------- 轨迹记录 ----------

def test_agent_emits_trace_events(tmp_path):
    """一次工具调用应记录 llm→tool→llm→final 四类事件。"""
    client = RecordingClient()
    tracer = TraceRecorder(
        case_id="T-TRACE",
        question="测试题",
        path=tmp_path / "trace.jsonl",
    )

    Agent(client=client).run("测试题", trace=tracer)
    record = tracer.finish(verdict="pass")

    assert [e["type"] for e in record["events"]] == [
        "llm", "tool", "llm", "final",
    ]
    assert record["llm_calls"] == 2
    assert record["tool_calls"] == 1
    assert record["tool_errors"] == 0
    assert record["final_answer"] == "最终答案：1/2"
    assert record["verdict"] == "pass"
    assert (tmp_path / "trace.jsonl").exists()
