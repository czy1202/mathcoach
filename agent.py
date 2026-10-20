"""Agent 核心循环。

职责：把"问模型 → 执行工具 → 回填结果 → 再问模型"串起来，
直到模型给出最终文字答案，或触发护栏（超步数 / 超时 / 模型异常）。

本文件只依赖 llm.py 的 chat 接口和 tools.py 的 run_tool，
不关心背后是 StubClient 还是真实的 OpenAIClient，方便第 10 步直接切换。
"""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

from llm import LLMResponse, StubClient, TOOLS_SCHEMA
from tools import run_tool


# ---------- 常量与系统提示词 ----------

MAX_STEPS = 10          # 单次求解最多问模型 10 轮，防止无限循环烧额度
STEP_TIMEOUT = 30.0     # 单轮模型调用的超时时间（秒）

SYSTEM_PROMPT = """你是一名数学竞赛解题助手，请按下面的规则回答：

1. 必须调用工具验证答案，不得自行心算后直接下结论。
2. 验证不通过时，根据工具返回的信息重新计算，可以重试。
3. 无法验证时，明确标注"结果不确定"，不要编造结论。
4. 最终回答依次包含：涉及的知识点、解题思路、分步解题过程、最终答案。
5. 如果题干信息不全，先要求用户补充条件，不要硬答。
"""


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

    def run(self, user_input: str) -> str:
        """求解一道题。

        Args:
            user_input: 用户输入的题目文本

        Returns:
            str: 模型的最终文字答案；触发护栏时返回明确的错误提示。
        """
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_input},
        ]

        for step in range(1, self.max_steps + 1):
            try:
                resp = self._chat_with_timeout(messages)
            except FutureTimeoutError:
                # 超时视为该步失败并立即停止，避免连续超时累计等待
                return (
                    f"错误：第 {step} 步调用模型超时"
                    f"（超过 {self.step_timeout} 秒），该步记为失败，已停止。"
                )
            except Exception as e:
                return f"错误：第 {step} 步调用模型失败（{type(e).__name__}: {e}）。"

            # 没有工具调用，说明模型这次是在"说话"，也就是最终答案
            if not resp.tool_calls:
                if not resp.content.strip():
                    return "错误：模型既没有调用工具，也没有返回文字答案。"
                if self.verbose:
                    print(f"[第 {step} 步] 模型给出最终答案")
                return resp.content

            # 模型这次是在"点单"：逐个执行工具，把结果回填进消息列表。
            # run_tool 保证异常也以文本返回，所以这里不用 try/except，
            # 模型能看到错误信息并自行纠正。
            if self.verbose:
                print(f"[第 {step} 步] 模型请求调用 {len(resp.tool_calls)} 个工具")

            for call in resp.tool_calls:
                if self.verbose:
                    print(f"  → 调用 {call.name}，参数：{call.args}")
                result = run_tool(call.name, call.args)
                if self.verbose:
                    print(f"  ← 工具结果：{result}")
                messages.append({"role": "tool", "content": str(result)})

            # continue：带着工具结果进入下一轮，再问模型

        return (
            f"错误：已达到最大步数 {self.max_steps}，"
            "模型仍未给出最终答案，已停止。"
        )

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

    agent = Agent(verbose=True)
    answer = agent.run(question)

    print("-" * 40)
    print("Agent：", answer)
