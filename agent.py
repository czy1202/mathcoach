"""Agent 核心循环。

职责：把"问模型 → 执行工具 → 回填结果 → 再问模型"串起来，
直到模型给出最终文字答案，或触发护栏（超步数 / 超时 / 模型异常）。

本文件只依赖 llm.py 的 chat 接口和 tools.py 的 run_tool，
不关心背后是 StubClient 还是真实的 OpenAIClient，方便第 10 步直接切换。
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

from llm import LLMResponse, StubClient, TOOLS_SCHEMA
from tools import run_tool


# ---------- 常量与系统提示词 ----------

MAX_STEPS = 10          # 单次求解最多问模型 10 轮，防止无限循环烧额度
STEP_TIMEOUT = 30.0     # 单轮模型调用的超时时间（秒）

SYSTEM_PROMPT = """你是一名数学竞赛解题助手，请按下面的规则回答：

1. 计算题优先调用工具验证关键步骤，不得自行心算后直接下结论。
2. 验证不通过时，根据工具返回的信息重新计算，可以重试。
3. 证明题不需要调用 sympy 验证，但必须给出严谨、完整的推理链。
4. 最终回答依次包含：涉及的知识点、解题思路、分步解题过程、最终答案。
5. 回答的最后一行必须单独写成：最终答案：<简洁答案>。
   选择题写选项字母；证明题写“得证”或最终结论；不要在这一行写解释，
   也不要在正文其他位置重复这个标记。
6. 如果题干信息不全（例如递推数列没有给首项、函数没有给表达式、
   矩阵没有给元素），必须先要求用户补充，不要引入假设条件后硬答。
"""

# 只有携带上文时才追加。追问和新题的处理方式不同，否则用户问
# “这一步为什么”也会收到一整篇知识点 + 完整解答。
FOLLOWUP_PROMPT = """
当前对话包含之前的问题和解答。请额外遵守：
7. 如果用户是在追问上一题（例如“这一步为什么”“再详细一点”
   “还有别的方法吗”），结合上一轮的题目和解答直接回答被问到的部分；
   不必重复完整的四段格式，也不必在最后重复“最终答案：”行，
   除非用户明确要求给出完整解答。
8. 如果用户提出一道全新题目，仍按第 4、5 条输出完整格式。
9. 不要编造上一轮没有出现过的条件或步骤；如果“这一步”“这个结果”
   等指代不明确，先复述你的理解，再请用户确认。
"""


def clean_history(history: list[dict] | None) -> list[dict]:
    """只保留合法的 user / assistant 文本消息。

    历史由服务端会话提供，正常情况下已经是干净数据；这里再过滤一次，
    避免调用方误传 system、tool 消息或空内容，破坏消息顺序。
    """
    if not isinstance(history, list):
        return []

    cleaned: list[dict] = []
    for message in history:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        content = content.strip()
        if content:
            cleaned.append({"role": role, "content": content})
    return cleaned


# ---------- Agent ----------

class Agent:
    """一次题目求解的执行器。"""

    def __init__(
        self,
        client=None,
        max_steps: int = MAX_STEPS,
        step_timeout: float = STEP_TIMEOUT,
        verbose: bool = False,
    ) -> None:
        """初始化。

        Args:
            client: 模型客户端，默认用不联网的 StubClient；
                    第 10 步传入 OpenAIClient() 即可切换，循环代码不用改。
            max_steps: 最大循环步数
            step_timeout: 单轮模型调用的超时时间（秒）
            verbose: 是否打印每一步的过程，演示和调试时打开
        """
        self.client = client or StubClient()
        self.max_steps = max_steps
        self.step_timeout = step_timeout
        self.verbose = verbose

    def run(
        self,
        user_input: str,
        trace=None,
        history: list[dict] | None = None,
    ) -> str:
        """求解一道题，或基于历史回答一次追问。

        Args:
            user_input: 用户当前输入的问题或追问
            trace: 可选的轨迹记录器，需提供 add(event_type, **fields) 方法。
                   Agent 不依赖具体实现，不传时完全不影响原有行为。
            history: 之前的问答消息，只应包含 user / assistant 的最终文本，
                     不包含工具调用记录。为空时行为和单轮求解完全一致。

        Returns:
            str: 模型的最终文字答案；触发护栏时返回明确的错误提示。
        """
        history = clean_history(history)
        system_prompt = SYSTEM_PROMPT
        if history:
            system_prompt += FOLLOWUP_PROMPT
            self._emit(
                trace,
                "context",
                history_messages=len(history),
                history_chars=sum(len(m["content"]) for m in history),
                follow_up=True,
            )

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": user_input},
        ]

        for step in range(1, self.max_steps + 1):
            chat_start = time.perf_counter()
            try:
                resp = self._chat_with_timeout(messages)
            except FutureTimeoutError:
                # 超时视为该步失败并立即停止，避免连续超时累计等待
                message = (
                    f"错误：第 {step} 步调用模型超时"
                    f"（超过 {self.step_timeout} 秒），该步记为失败，已停止。"
                )
                self._emit(trace, "error", step=step, kind="timeout", message=message)
                return message
            except Exception as e:
                message = f"错误：第 {step} 步调用模型失败（{type(e).__name__}: {e}）。"
                self._emit(trace, "error", step=step, kind="llm_error", message=message)
                return message

            self._emit(
                trace,
                "llm",
                step=step,
                elapsed=round(time.perf_counter() - chat_start, 4),
                content=resp.content,
                tool_calls=[
                    {"id": call.id, "name": call.name, "args": call.args}
                    for call in resp.tool_calls
                ],
                usage=self._usage_dict(resp),
            )

            # 没有工具调用，说明模型这次是在"说话"，也就是最终答案
            if not resp.tool_calls:
                if not resp.content.strip():
                    message = "错误：模型既没有调用工具，也没有返回文字答案。"
                    self._emit(
                        trace, "error", step=step, kind="empty_response", message=message
                    )
                    return message
                if self.verbose:
                    print(f"[第 {step} 步] 模型给出最终答案")
                self._emit(trace, "final", step=step, content=resp.content)
                return resp.content

            # 模型这次是在"点单"：逐个执行工具，把结果回填进消息列表。
            # run_tool 保证异常也以文本返回，所以这里不用 try/except，
            # 模型能看到错误信息并自行纠正。
            if self.verbose:
                print(f"[第 {step} 步] 模型请求调用 {len(resp.tool_calls)} 个工具")

            # OpenAI 兼容接口要求：下一条 tool 消息必须能对应上
            # 前一条 assistant 消息里的 tool_calls，所以这里要先把模型这次
            # 的调用原样写回消息列表，并保留每一条的 id。
            call_ids = [
                call.id or f"call_{step}_{i}"
                for i, call in enumerate(resp.tool_calls, 1)
            ]
            messages.append({
                "role": "assistant",
                "content": resp.content or None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                call.args, ensure_ascii=False, default=str
                            ),
                        },
                    }
                    for call, call_id in zip(resp.tool_calls, call_ids)
                ],
            })

            for call, call_id in zip(resp.tool_calls, call_ids):
                if self.verbose:
                    print(f"  → 调用 {call.name}，参数：{call.args}")
                tool_start = time.perf_counter()
                result = run_tool(call.name, call.args)
                elapsed = round(time.perf_counter() - tool_start, 6)
                if self.verbose:
                    print(f"  ← 工具结果：{result}")
                self._emit(
                    trace,
                    "tool",
                    step=step,
                    tool_call_id=call_id,
                    name=call.name,
                    args=call.args,
                    result=result,
                    ok=not (
                        isinstance(result, str) and result.startswith("错误：")
                    ),
                    elapsed=elapsed,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": call.name,
                    "content": str(result),
                })

            # continue：带着工具结果进入下一轮，再问模型

        message = (
            f"错误：已达到最大步数 {self.max_steps}，"
            "模型仍未给出最终答案，已停止。"
        )
        self._emit(trace, "error", step=self.max_steps, kind="max_steps", message=message)
        return message

    @staticmethod
    def _emit(trace, event_type: str, **fields) -> None:
        """向轨迹记录器发一个事件；没有记录器时什么都不做。"""
        if trace is not None:
            trace.add(event_type, **fields)

    @staticmethod
    def _usage_dict(resp: LLMResponse) -> dict[str, int]:
        """从响应里取 usage，兼容没有 usage 字段的自定义客户端。"""
        usage = getattr(resp, "usage", None)
        if hasattr(usage, "as_dict"):
            return usage.as_dict()
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def _chat_with_timeout(self, messages: list[dict]) -> LLMResponse:
        """带超时地调用一次模型。

        原理：chat 是阻塞调用，放进单线程池里跑，主线程最多等
        step_timeout 秒。超时后线程无法被强行杀掉，只能丢弃它的结果；
        真实模型客户端自身也会设置网络超时，避免线程永久挂起。
        """
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self.client.chat, messages, TOOLS_SCHEMA)
        try:
            return future.result(timeout=self.step_timeout)
        finally:
            # 不等待线程结束，避免超时后主线程继续被阻塞
            executor.shutdown(wait=False, cancel_futures=True)


# ---------- 演示 ----------

if __name__ == "__main__":
    question = "验证 (x + 1)**2 与 x**2 + 2*x + 1 是否等价，并给出结论。"
    print("用户：", question)
    print("-" * 40)

    agent = Agent(verbose=False)
    answer = agent.run(question)

    print("-" * 40)
    print("Agent：", answer)
