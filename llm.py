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
from tools import TOOLS


# ---------- 统一响应结构 ----------

@dataclass
class ToolCall:
    """模型发起的一次工具调用。"""

    name: str                                   # 工具名，对应 TOOLS 的键
    args: dict[str, Any] = field(default_factory=dict)  # 参数字典


@dataclass
class LLMResponse:
    """模型一次回复的统一结构。

    要么 content 有文本，要么 tool_calls 非空；两者也可能同时出现。
    """

    content: str = ""                             # 回复文本
    tool_calls: list[ToolCall] = field(default_factory=list)  # 工具调用列表


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
    """
    schema: list[dict] = []
    for name, info in registry.items():
        params = info.get("参数说明", {})
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
                    "required": list(params.keys()),
                },
            },
        })
    return schema


TOOLS_SCHEMA: list[dict] = build_tools_schema(TOOLS)


# ---------- 真实模型客户端 ----------

class OpenAIClient:
    """走 OpenAI 兼容接口的真实模型客户端。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        """参数为空时回落到 config.py 中读取的 .env 配置。"""
        self.model: str = model or config.OPENAI_MODEL
        self._client = OpenAI(
            api_key=api_key or config.OPENAI_API_KEY,
            base_url=base_url or config.OPENAI_BASE_URL or None,
        )

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
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools

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
            tool_calls.append(ToolCall(name=call.function.name, args=args))

        return LLMResponse(content=message.content or "", tool_calls=tool_calls)


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
            return LLMResponse(content="验证通过：两边等价，最终答案 1。")

        return LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    name="verify_with_sympy",
                    args={
                        "expr_a": "(x + 1)**2",
                        "expr_b": "x**2 + 2*x + 1",
                    },
                )
            ],
        )
