"""Agent 核心循环。

职责：把"问模型 → 执行工具 → 回填结果 → 再问模型"串起来，
直到模型给出最终文字答案，或触发护栏（超步数 / 超时 / 模型异常）。

本文件只依赖 llm.py 的 chat 接口和 tools.py 的 run_tool，
不关心背后是 StubClient 还是真实的 OpenAIClient，方便第 10 步直接切换。
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

from llm import LLMResponse, RETRIEVAL_TOOL_SCHEMA, StubClient, TOOLS_SCHEMA
from tools import run_tool


# ---------- 常量与系统提示词 ----------

MAX_STEPS = 15          # 单次求解最多问模型 15 轮，防止无限循环烧额度。
                        # 34 道真题实测最长 14 轮（2025-F6、2025-F1）；
                        # 放到 25 轮没有多通过一题，只是更慢。
STEP_TIMEOUT = 30.0     # 单轮模型调用的超时时间（秒）

CANCELLED_MESSAGE = "已停止生成。"


class StreamInterrupted(RuntimeError):
    """流式输出中途失败；此时通常已经向用户显示了一部分内容。"""


class _CancelledStream(Exception):
    """流式输出过程中收到取消信号，用于立刻关闭连接。"""

SYSTEM_PROMPT = r"""你是一名数学竞赛解题助手，请按下面的规则回答：

1. 能写成两个表达式是否相等（恒等式、化简、方程验根）时，优先调用
   verify_with_sympy 验证；极限、积分、级数等问题无法用该工具判定，
   不要硬凑一个等式去“通过验证”，也不要把“无法判定”当成结论错误。
   没有调用工具时，不要声称已经验证。
2. 验证不通过时，根据工具返回的信息重新计算，可以重试。
3. 证明题不需要调用 sympy 验证，但必须给出严谨、完整的推理链。
4. 最终回答依次包含：涉及的知识点、解题思路、分步解题过程、最终答案。
   给出完整解答时，“分步解题过程”必须用三级标题严格分步：
   ### 第一步：<这一步做什么>
   ### 第二步：<...>
   每一步标题后空一行再写正文；不要用序号列表或加粗文字代替步骤标题。
   如果只是回答追问、澄清问题或列出真题，不要强行套用步骤标题。
5. 回答的最后一行必须单独写成：最终答案：<简洁答案>。
   选择题写选项字母；证明题写“得证”或最终结论；不要在这一行写解释，
   也不要在正文其他位置重复这个标记。
6. 如果题干信息不全（例如递推数列没有给首项、函数没有给表达式、
   矩阵没有给元素），必须先要求用户补充，不要引入假设条件后硬答。

数学公式格式（硬性要求，前端用 KaTeX 渲染，写法不规范会显示成乱码）：
- 所有数学表达式必须用标准 LaTeX：行内公式用 $...$ 包裹，
  独立成行的公式用 $$...$$ 包裹；中文说明放在公式外面。
- 极限必须写 $\lim_{x \to 0} f(x)$，禁止写 lim x→0 或 lim(x→0)。
- 上标、下标必须用 ^ 和 _：$x^2$、$y_1$、$e^x$；禁止裸写 x2、ex、y1。
- 积分必须写 $\int_0^1 f(x)\,dx$，禁止写 ∫0^1、Z dt，也不要省略积分号。
- 分数必须写 $\frac{a}{b}$，禁止写 a/b。
- 禁止用纯文本或 Unicode 上下标写数学（例如 x²、eˣ、y₁ 都算违规）；
  除中文标点外，公式内不要混入中文。
- “最终答案：”这一行里的公式同样使用 LaTeX，例如：最终答案：$\frac{1}{2}$。
"""

# 快速模式：用户明确选择“直接看答案”，不追求完整讲解。
FAST_SYSTEM_PROMPT = r"""你是一名数学竞赛解题助手。用户选择了“快速答案”模式，请遵守：

1. 直接给出最终答案和必要的关键步骤，不要写长篇“知识点/解题思路”。
2. 只有当题目能写成两个表达式是否相等（恒等式、化简、方程验根）时，
   才调用 verify_with_sympy；极限、积分、级数、证明题通常无法用这个
   工具判定，不要为了“走流程”硬凑一个等式，也不要把工具返回的
   False 或“无法判定”当成题目结论错误。没有调用工具时，不要声称
   已经验证；可以在回答里写“未工具验证”。
3. 证明题请给简要证明；网页通常会自动把证明题转到详细模式。
4. 如果题干信息不全，先要求用户补充，不要引入假设硬答。
5. 最后一行必须单独写成：最终答案：<简洁答案>。
   选择题写选项字母；证明题写“得证”或最终结论。

数学公式格式：
- 行内公式用 $...$，独立公式用 $$...$$；
- 分数写 $\frac{a}{b}$，积分写 $\int_0^1 f(x)\,dx$，上下标用 ^ 和 _；
- 不要用纯文本或 Unicode 上下标写数学。
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

# 开启题库检索时追加，教模型什么时候检索、怎么用检索结果。
RETRIEVAL_PROMPT = """
当前接入了 CMC 非数学类历年真题库，请额外遵守：
10. 用户直接给出题干提出一道新题时，先调用一次 search_past_problems，query
    传完整题干或本题最核心的表达式，用返回结果判断考点和出题风格；
    同一道题不要重复检索。
11. 检索结果只是题库中其他题目的题干，不是本题的条件。禁止把其中的数字、
    函数或结论搬进本题解答；需要提及时，最多用一句话说明最相关的一道题
    及其相似点，不要在正文里复述题目原文，前端会另行展示相关真题卡片。
12. 当用户要求查看某一届或某一年的完整真题时，调用一次 list_past_problems，
    edition 或 year 二选一；用户提到的 stage / category / supplement / section
    按原话填写，没提到就留空。不要逐题解答，正文只说明找到几道、按初赛/决赛
    如何分组，不要复述题干，客户端会渲染完整列表。用户没有说明届数或年份时
    先询问，不要猜；用户只要求解答其中某一道题时，不要调用这个工具。
13. 当用户不提供题干，而是要求解答某一届/某一年/某类试卷中的某道题
    （例如“第七届初赛压轴题”“2024 年填空题第 3 题”）时：
    a. 先确定 edition 或 year；用户没有说明届数或年份时，先请用户补充，不要猜。
    b. 调用 get_past_problem；stage / category / supplement / section 按用户原话
       填写，number 与 position 二选一，“压轴题/最后一题”用 position="last"，
       “第一题”用 position="first"。
    c. 如果工具返回 candidates（例如初赛和决赛都有第 10 题），先把候选场次
       简短告诉用户并请其确认，不要替用户选一个。
    d. 拿到 question 后，把它当作本题的完整题干，按第 4、5 条完整解答。
       不要让用户重新粘贴题干，也不要把题号、数量等元数据当成题目内容。
14. 第 13 条这种“按届定位并解答”的请求，不要再调用 search_past_problems：
    search 只用于用户已经给出题干、需要找相似题的新题。
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
        retrieval: bool = False,
        system_prompt: str | None = None,
    ) -> None:
        """初始化。

        Args:
            client: 模型客户端，默认用不联网的 StubClient；
                    第 10 步传入 OpenAIClient() 即可切换，循环代码不用改。
            max_steps: 最大循环步数
            step_timeout: 单轮模型调用的超时时间（秒）
            verbose: 是否打印每一步的过程，演示和调试时打开
            retrieval: 是否开放真题库检索工具。默认关闭，
                       这样没构建题库时也能正常跑原有流程。
            system_prompt: 覆盖默认解题提示词。评测的独立复核器用它切换成
                           “阅卷员”角色；网页与命令行仍使用默认提示词。
        """
        self.client = client or StubClient()
        self.max_steps = max_steps
        self.step_timeout = step_timeout
        self.verbose = verbose
        self.retrieval = retrieval
        self.system_prompt = system_prompt
        self.tools_schema = TOOLS_SCHEMA + (
            RETRIEVAL_TOOL_SCHEMA if retrieval else []
        )

    def run(
        self,
        user_input: str,
        trace=None,
        history: list[dict] | None = None,
        cancel_event: threading.Event | None = None,
        on_token=None,
    ) -> str:
        """求解一道题，或基于历史回答一次追问。

        Args:
            user_input: 用户当前输入的问题或追问
            trace: 可选的轨迹记录器，需提供 add(event_type, **fields) 方法。
                   Agent 不依赖具体实现，不传时完全不影响原有行为。
            history: 之前的问答消息，只应包含 user / assistant 的最终文本，
                     不包含工具调用记录。为空时行为和单轮求解完全一致。
            cancel_event: 可选的取消信号。网页端在用户点“停止”后置位；
                          Agent 会在每次模型调用/工具调用之间检查，避免
                         继续消耗后续步骤。已经发出的单次请求无法中途
                         打断，但最多只多等这一次调用。
            on_token: 可选的流式回调，每收到一段模型文本就调用一次。
                      客户端没有 chat_stream 时自动退回非流式调用。

        Returns:
            str: 模型的最终文字答案；触发护栏时返回明确的错误提示。
        """
        history = clean_history(history)
        system_prompt = self.system_prompt or SYSTEM_PROMPT
        if history and self.system_prompt is None:
            system_prompt += FOLLOWUP_PROMPT
        if self.retrieval and self.system_prompt is None:
            system_prompt += RETRIEVAL_PROMPT
        if history or self.retrieval:
            self._emit(
                trace,
                "context",
                history_messages=len(history),
                history_chars=sum(len(m["content"]) for m in history),
                follow_up=bool(history),
                retrieval=self.retrieval,
            )

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": user_input},
        ]

        for step in range(1, self.max_steps + 1):
            if cancel_event is not None and cancel_event.is_set():
                return self._cancelled(trace, step)

            chat_start = time.perf_counter()
            # 网络抖动导致的单次超时很常见，重试一次即可恢复；
            # 连续两次超时才判定该题失败，避免白白丢掉一道能做对的题。
            for attempt in (1, 2):
                try:
                    resp = self._call_model(messages, on_token, cancel_event)
                    break
                except _CancelledStream:
                    return self._cancelled(trace, step)
                except StreamInterrupted as exc:
                    # 已经向用户输出了一部分内容，重试会造成重复文本；
                    # 直接结束，并明确告诉用户这次输出不完整。
                    message = f"错误：第 {step} 步流式输出中断（{exc}），本次回答不完整。"
                    self._emit(
                        trace, "error", step=step, kind="stream_interrupted",
                        message=message,
                    )
                    return message
                except FutureTimeoutError:
                    if attempt == 2:
                        message = (
                            f"错误：第 {step} 步调用模型连续两次超时"
                            f"（每次超过 {self.step_timeout} 秒），"
                            "该步记为失败，已停止。"
                        )
                        self._emit(
                            trace, "error", step=step, kind="timeout", message=message
                        )
                        return message
                    self._emit(
                        trace,
                        "error",
                        step=step,
                        kind="timeout_retry",
                        message=f"第 {step} 步调用模型超时，自动重试一次。",
                    )
                except Exception as e:
                    message = (
                        f"错误：第 {step} 步调用模型失败"
                        f"（{type(e).__name__}: {e}）。"
                    )
                    self._emit(
                        trace, "error", step=step, kind="llm_error", message=message
                    )
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

            # 用户在等模型返回期间点了“停止”：这次请求已经产生费用，
            # 但不要再执行工具或进入下一轮。
            if cancel_event is not None and cancel_event.is_set():
                return self._cancelled(trace, step)

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
                if cancel_event is not None and cancel_event.is_set():
                    return self._cancelled(trace, step)
                if self.verbose:
                    print(f"  → 调用 {call.name}，参数：{call.args}")
                tool_start = time.perf_counter()
                result = run_tool(
                    call.name,
                    call.args,
                    cancel_event=cancel_event,
                )
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

    @classmethod
    def _cancelled(cls, trace, step: int) -> str:
        """记录取消事件并返回统一提示，避免网页把半截过程当成答案。"""
        cls._emit(
            trace,
            "cancelled",
            step=step,
            message=CANCELLED_MESSAGE,
        )
        return CANCELLED_MESSAGE

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

    def _call_model(
        self,
        messages: list[dict],
        on_token=None,
        cancel_event: threading.Event | None = None,
    ) -> LLMResponse:
        """有流式回调且客户端支持时走流式，否则保持原来的阻塞调用。"""
        if on_token is None or not hasattr(self.client, "chat_stream"):
            return self._chat_with_timeout(messages)
        return self._stream_with_timeout(messages, on_token, cancel_event)

    def _stream_with_timeout(
        self,
        messages: list[dict],
        on_token,
        cancel_event: threading.Event | None = None,
    ) -> LLMResponse:
        """消费流式响应，同时保留单步超时护栏。

        超时判断在每收到一段内容时进行；网络本身由客户端自己的
        timeout 参数兜底。已经输出过内容后再超时不能重试，否则前端
        会看到重复文本，因此抛 StreamInterrupted。
        """
        deadline = time.perf_counter() + self.step_timeout
        response: LLMResponse | None = None
        emitted = False
        stream = self.client.chat_stream(
            messages, self.tools_schema, timeout=self.step_timeout
        )
        try:
            for event in stream:
                if cancel_event is not None and cancel_event.is_set():
                    raise _CancelledStream()
                if time.perf_counter() > deadline:
                    if emitted:
                        raise StreamInterrupted("超过单步时限")
                    raise FutureTimeoutError()
                if event.get("type") == "delta":
                    text = event.get("text") or ""
                    if text:
                        emitted = True
                        on_token(text)
                elif event.get("type") == "done":
                    response = event.get("response")
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        if response is None:
            raise StreamInterrupted("连接结束但没有收到完整响应")
        return response

    def _chat_with_timeout(self, messages: list[dict]) -> LLMResponse:
        """带超时地调用一次模型。

        原理：chat 是阻塞调用，放进单线程池里跑，主线程最多等
        step_timeout 秒。超时后线程无法被强行杀掉，只能丢弃它的结果；
        真实模型客户端自身也会设置网络超时，避免线程永久挂起。
        """
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self.client.chat, messages, self.tools_schema)
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
