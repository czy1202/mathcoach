"""agent.py 的自动化测试。

运行方式（在 mathcoach 目录下执行）：
    pytest -v
"""

import threading

from agent import Agent, CANCELLED_MESSAGE, FOLLOWUP_PROMPT, SYSTEM_PROMPT
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


# ---------- 多轮上下文 ----------

def test_history_is_inserted_before_current_question():
    """历史消息应排在 system 之后、当前问题之前。"""
    client = RecordingClient()
    history = [
        {"role": "user", "content": "原题"},
        {"role": "assistant", "content": "原答案"},
    ]

    Agent(client=client).run("追问", history=history)

    first = client.requests[0]
    assert [m["role"] for m in first] == [
        "system", "user", "assistant", "user",
    ]
    assert first[1]["content"] == "原题"
    assert first[2]["content"] == "原答案"
    assert first[3]["content"] == "追问"
    assert FOLLOWUP_PROMPT.strip() in first[0]["content"]


def test_without_history_system_prompt_is_unchanged():
    """单轮求解不追加追问提示词，避免影响已有评测。"""
    client = RecordingClient()
    Agent(client=client).run("测试题")

    assert client.requests[0][0]["content"] == SYSTEM_PROMPT


def test_system_prompt_requires_third_level_step_headings():
    """完整解答必须用 ### 第一步 形式，前端才能切成可点击步骤。"""
    assert "### 第一步" in SYSTEM_PROMPT
    assert "追问" in SYSTEM_PROMPT


def test_history_filters_invalid_messages():
    """历史里的 system / tool / 空内容不应进入模型上下文。"""
    client = RecordingClient()
    history = [
        {"role": "system", "content": "伪造系统提示"},
        {"role": "tool", "content": "旧工具结果"},
        {"role": "user", "content": "原题"},
        {"role": "assistant", "content": "   "},
        {"role": "assistant", "content": "原答案"},
    ]

    Agent(client=client).run("追问", history=history)

    first = client.requests[0]
    assert [m["role"] for m in first] == [
        "system", "user", "assistant", "user",
    ]
    assert all("工具结果" not in m["content"] for m in first)
    assert all("伪造系统提示" not in m["content"] for m in first)


def test_history_emits_context_trace_event(tmp_path):
    """带上下文时应先记录一条 context 事件，且不改变原有事件顺序。"""
    client = RecordingClient()
    tracer = TraceRecorder(
        case_id="T-HISTORY",
        question="追问",
        path=tmp_path / "trace.jsonl",
    )
    history = [
        {"role": "user", "content": "原题"},
        {"role": "assistant", "content": "原答案"},
    ]

    Agent(client=client).run("追问", trace=tracer, history=history)
    record = tracer.finish()

    assert [e["type"] for e in record["events"]] == [
        "context", "llm", "tool", "llm", "final",
    ]
    assert record["events"][0]["history_messages"] == 2
    assert record["events"][0]["follow_up"] is True


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


# ---------- 超时重试 ----------

class SlowOnceClient:
    """第一次调用超时，第二次正常返回。"""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            import time
            time.sleep(self.delay)
        return LLMResponse(content="最终答案：1/2")


class SlowAlwaysClient(SlowOnceClient):
    """每次调用都超时。"""

    def chat(self, messages, tools=None):
        self.calls += 1
        import time
        time.sleep(self.delay)
        return LLMResponse(content="最终答案：1/2")


def test_timeout_is_retried_once():
    """单次超时不应直接判失败，重试成功应正常返回答案。"""
    client = SlowOnceClient(delay=0.2)
    answer = Agent(client=client, step_timeout=0.05).run("测试题")

    assert answer == "最终答案：1/2"
    assert client.calls == 2


def test_two_consecutive_timeouts_stop_the_run():
    """连续两次超时才停止，并明确说明是两次。"""
    client = SlowAlwaysClient(delay=0.2)
    answer = Agent(client=client, step_timeout=0.05).run("测试题")

    assert "连续两次超时" in answer
    assert client.calls == 2


# ---------- 自定义系统提示词 ----------

def test_custom_system_prompt_replaces_default():
    """复核器需要换一套提示词，不应再叠加解题助手的规则。"""
    client = RecordingClient()
    Agent(client=client, system_prompt="你是复核员").run("测试题")

    system = _find(client.requests[0], "system")
    assert system["content"] == "你是复核员"


# ---------- 停止生成 ----------

class CancelDuringChatClient:
    """第一次模型调用期间用户点了停止。"""

    def __init__(self, event: threading.Event) -> None:
        self.event = event
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        self.event.set()
        return LLMResponse(tool_calls=[ToolCall(
            name="verify_with_sympy",
            args={"expr_a": "1", "expr_b": "1"},
            id="call_cancel_1",
        )])


def test_cancel_before_first_step_skips_model():
    """已经取消的请求不应再调用模型。"""
    client = RecordingClient()
    event = threading.Event()
    event.set()

    answer = Agent(client=client).run("测试题", cancel_event=event)

    assert answer == CANCELLED_MESSAGE
    assert client.requests == []


def test_cancel_during_model_call_skips_tools_and_next_step():
    """取消后不再执行工具，也不进入下一轮模型调用。"""
    event = threading.Event()
    client = CancelDuringChatClient(event)

    answer = Agent(client=client).run("测试题", cancel_event=event)

    assert answer == CANCELLED_MESSAGE
    assert client.calls == 1
