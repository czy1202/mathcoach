"""模型客户端封装。

对外只暴露两类东西：
1. 统一响应结构：ToolCall / LLMResponse；
2. chat(messages, tools) 接口及两个实现：OpenAIClient（真实模型）、
   StubClient（写死响应，不联网）。

两个实现的签名完全一致，第 6 步从 stub 切到真实模型时，
agent 主循环的调用代码不需要改。
"""

import json
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

import config
from tools import RETRIEVAL_TOOLS, TOOLS


# ---------- 统一响应结构 ----------

@dataclass
class ToolCall:
    """模型发起的一次工具调用。"""

    name: str                                   # 工具名，对应 TOOLS 的键
    args: dict[str, Any] = field(default_factory=dict)  # 参数字典
    id: str = ""                                # 模型侧调用 id，回填结果时必须原样带回


@dataclass
class Usage:
    """一次模型调用的 token 用量，用于成本统计和 trace。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class LLMResponse:
    """模型一次回复的统一结构。

    要么 content 有文本，要么 tool_calls 非空；两者也可能同时出现。
    """

    content: str = ""                             # 回复文本
    tool_calls: list[ToolCall] = field(default_factory=list)  # 工具调用列表
    usage: Usage = field(default_factory=Usage)   # token 用量，stub 客户端为 0


# ---------- 工具注册表 → OpenAI tools 格式 ----------

def build_tools_schema(registry: dict[str, dict]) -> list[dict]:
    """把 tools.py 的 TOOLS 注册表转成 OpenAI 接口要求的 tools 格式。

    Args:
        registry: 工具注册表，形如 {工具名: {"函数", "描述", "参数说明"}}

    Returns:
        list[dict]: 可直接作为 chat(tools=...) 传入的 schema 列表。

    注意：
        参数类型目前统一按 string 生成，因为 verify_with_sympy 接收的是
        表达式字符串。以后加数值类工具时，需要在注册表里补充类型信息。
        注册表里可以用 "必填" 指定 required 子集；不写则所有参数必填。
    """
    schema: list[dict] = []
    for name, info in registry.items():
        params = info.get("参数说明", {})
        required = info.get("必填", list(params.keys()))
        schema.append({
            "type": "function",
            "function": {
                "name": name,
                "description": info.get("描述", ""),
                "parameters": {
                    "type": "object",
                    "properties": {
                        key: {"type": "string", "description": desc}
                        for key, desc in params.items()
                    },
                    "required": list(required),
                },
            },
        })
    return schema


TOOLS_SCHEMA: list[dict] = build_tools_schema(TOOLS)
RETRIEVAL_TOOL_SCHEMA: list[dict] = build_tools_schema(RETRIEVAL_TOOLS)


def _usage_from(resp: Any) -> Usage:
    """从响应（或流式 chunk）里取 usage，兼容没有该字段的客户端。"""
    raw = getattr(resp, "usage", None)
    return Usage(
        prompt_tokens=getattr(raw, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(raw, "completion_tokens", 0) or 0,
        total_tokens=getattr(raw, "total_tokens", 0) or 0,
    )


# ---------- 真实模型客户端 ----------

class OpenAIClient:
    """走 OpenAI 兼容接口的真实模型客户端。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        """参数为空时回落到 config.py 中读取的 .env 配置。"""
        self.model: str = model or config.OPENAI_MODEL
        self.temperature: float | None = (
            config.OPENAI_TEMPERATURE if temperature is None else temperature
        )
        self.reasoning_effort: str | None = (
            reasoning_effort
            if reasoning_effort is not None
            else (config.OPENAI_REASONING_EFFORT or None)
        )
        self._client = OpenAI(
            api_key=api_key or config.OPENAI_API_KEY,
            base_url=base_url or config.OPENAI_BASE_URL or None,
        )

    def _request_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
    ) -> dict[str, Any]:
        """把 temperature / reasoning / tools 等组装成请求参数。"""
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        if tools:
            kwargs["tools"] = tools
        return kwargs

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """调用模型并解析成统一结构。

        Args:
            messages: OpenAI 格式的消息列表
            tools: OpenAI 格式的工具 schema，通常传 TOOLS_SCHEMA

        Returns:
            LLMResponse: content 与 tool_calls 的统一定义
        """
        kwargs = self._request_kwargs(messages, tools)
        resp = self._client.chat.completions.create(**kwargs)
        message = resp.choices[0].message

        tool_calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                # 参数不是合法 JSON 时交空字典，让 run_tool 返回参数错误，
                # 再作为工具结果回填给模型自行纠正，而不是在这里中断。
                args = {}
            tool_calls.append(ToolCall(
                name=call.function.name,
                args=args,
                id=call.id or "",
            ))

        return LLMResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            usage=_usage_from(resp),
        )

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        timeout: float | None = None,
    ):
        """流式调用：先逐段 yield 文本，最后 yield 完整响应。

        生成器产出两种事件：
        - ``{"type": "delta", "text": "..."}``：模型刚生成的片段；
        - ``{"type": "done", "response": LLMResponse}``：拼装完成的响应。

        工具调用参数会分多条到达，按 index 聚合后再统一解析，
        与 chat() 返回的 LLMResponse 结构完全一致。
        """
        kwargs = self._request_kwargs(messages, tools)
        kwargs["stream"] = True
        # 让服务端在最后一条 chunk 里带上 usage；部分兼容端点不支持这个参数，
        # 报错时退回不带 usage 的普通流式请求（只影响统计，不影响回答）。
        kwargs["stream_options"] = {"include_usage": True}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            stream = self._client.chat.completions.create(**kwargs)
        except Exception:
            if "stream_options" not in kwargs:
                raise
            kwargs.pop("stream_options", None)
            stream = self._client.chat.completions.create(**kwargs)

        content_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage = Usage()

        for chunk in stream:
            raw_usage = getattr(chunk, "usage", None)
            if raw_usage is not None:
                usage = _usage_from(chunk)
            for choice in getattr(chunk, "choices", None) or []:
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                text = getattr(delta, "content", None)
                if text:
                    content_parts.append(text)
                    yield {"type": "delta", "text": text}
                for call in getattr(delta, "tool_calls", None) or []:
                    index = getattr(call, "index", 0) or 0
                    slot = calls.setdefault(
                        index, {"id": "", "name": "", "arguments": ""}
                    )
                    if getattr(call, "id", None):
                        slot["id"] = call.id
                    function = getattr(call, "function", None)
                    if function is not None:
                        if getattr(function, "name", None):
                            slot["name"] = function.name
                        if getattr(function, "arguments", None):
                            slot["arguments"] += function.arguments

        tool_calls: list[ToolCall] = []
        for index in sorted(calls):
            slot = calls[index]
            try:
                args = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(
                name=slot["name"],
                args=args,
                id=slot["id"],
            ))

        yield {
            "type": "done",
            "response": LLMResponse(
                content="".join(content_parts),
                tool_calls=tool_calls,
                usage=usage,
            ),
        }


# ---------- 离线假客户端 ----------

class StubClient:
    """不联网的假客户端，用于第 6 步测试核心循环。

    行为是写死的：消息里还没有工具结果时，返回一次工具调用；
    一旦看到 role == "tool" 的消息（说明工具结果已回填），就返回最终答案。
    这样同一套 chat 接口就能驱动"工具调用 → 回填 → 最终答案"的完整链路。
    """

    def __init__(self) -> None:
        self.call_count: int = 0    # 已调用次数，便于测试断言

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """返回写死的响应，不访问网络。tools 参数仅为保持接口一致。"""
        self.call_count += 1

        if any(m.get("role") == "tool" for m in messages):
            return LLMResponse(
                content="验证通过：两边等价。\n最终答案：1",
            )

        return LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    name="verify_with_sympy",
                    args={
                        "expr_a": "(x + 1)**2",
                        "expr_b": "x**2 + 2*x + 1",
                    },
                    id="call_stub_1",
                )
            ],
        )

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        timeout: float | None = None,
    ):
        """与 chat() 结果一致，只是把文本切成几段推给调用方。

        用于离线测试流式链路：先分块 yield 文本，最后 yield 完整响应。
        """
        response = self.chat(messages, tools)
        text = response.content
        step = max(1, len(text) // 3) if text else 0
        for start in range(0, len(text), step or 1):
            if not text:
                break
            yield {"type": "delta", "text": text[start:start + step]}
        yield {"type": "done", "response": response}
