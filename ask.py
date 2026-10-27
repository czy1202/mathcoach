"""命令行一问一答入口。

用法：
    python ask.py "计算 lim(x→0) sin x / x"
    python ask.py                      # 连续提问，输入 exit 退出
    python ask.py --stub "1+1"         # 离线假模型，不联网
    python ask.py --verbose "..."      # 打印每一步的工具调用

这是最小的产品壳：复用 Agent 主循环，只负责读题目、调模型、打印回答。
公式目前是 Markdown/LaTeX 源文本，终端不渲染；Web 前端会负责渲染。
"""

import argparse
import sys
import time

import config
from agent import Agent
from llm import OpenAIClient, StubClient
from tracing import TraceRecorder, new_trace_file


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="mathcoach 命令行解题（默认调用真实模型）",
    )
    parser.add_argument(
        "question",
        nargs="*",
        help="题目文本；不传则进入连续提问模式",
    )
    parser.add_argument(
        "--stub",
        action="store_true",
        help="使用离线假模型，不调用 API（用于演示流程）",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="打印每一步的模型响应与工具调用",
    )
    parser.add_argument(
        "--trace-dir",
        default="traces",
        help="轨迹输出目录（默认 traces/）",
    )
    parser.add_argument(
        "--no-trace",
        action="store_true",
        help="不写轨迹文件",
    )
    return parser.parse_args(argv)


def _format_footer(elapsed: float, tracer: TraceRecorder | None) -> str:
    """输出一行运行信息，方便观察耗时和 token 成本。"""
    if tracer is None:
        return f"[用时 {elapsed:.2f}s]"
    return (
        f"[用时 {elapsed:.2f}s | 模型 {tracer.llm_calls} 次 | "
        f"工具 {tracer.tool_calls} 次 | "
        f"tokens {tracer.usage['total_tokens']}]"
    )


def _run_question(
    agent: Agent,
    question: str,
    case_id: str,
    trace_path,
) -> str:
    """回答一道题并打印结果。"""
    tracer = None
    if trace_path is not None:
        tracer = TraceRecorder(case_id=case_id, question=question, path=trace_path)

    start = time.perf_counter()
    try:
        answer = agent.run(question, trace=tracer)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        # 单题异常不退出程序，交互模式下还能继续问下一题
        answer = f"错误：Agent 执行异常（{type(e).__name__}: {e}）"
        if tracer is not None:
            tracer.add("error", kind="agent_exception", message=answer)
    elapsed = time.perf_counter() - start

    if tracer is not None:
        tracer.finish()

    print()
    print(answer)
    print(_format_footer(elapsed, tracer))
    return answer


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    Returns:
        int: 进程退出码。0 表示正常，1 表示配置缺失。
    """
    args = parse_args(argv)

    if args.stub:
        client = StubClient()
    else:
        if not config.OPENAI_API_KEY.strip():
            print("错误：未配置 OPENAI_API_KEY，无法调用真实模型。")
            print("请先在项目根目录 .env 中填写 OPENAI_API_KEY，或用 --stub 离线演示。")
            return 1
        if not config.OPENAI_MODEL.strip():
            print("警告：OPENAI_MODEL 为空，请求可能被接口拒绝。")
        client = OpenAIClient()

    agent = Agent(client=client, verbose=args.verbose)
    trace_path = None if args.no_trace else new_trace_file(args.trace_dir, prefix="ask")

    if args.stub:
        print("提示：当前是 --stub 离线假模型，回答是写死的，不代表真实解题能力。")
    else:
        print(f"模型：{config.OPENAI_MODEL}")

    if args.question:
        _run_question(agent, " ".join(args.question), "ask-1", trace_path)
        if trace_path is not None:
            print(f"轨迹：{trace_path}")
        return 0

    # 管道输入：把 stdin 整体当成一道题，方便脚本调用
    if not sys.stdin.isatty():
        question = sys.stdin.read().strip()
        if not question:
            print("错误：没有从标准输入读到题目。")
            return 1
        _run_question(agent, question, "ask-1", trace_path)
        if trace_path is not None:
            print(f"轨迹：{trace_path}")
        return 0

    print("输入题目后回车；输入 exit 退出。")
    index = 0
    try:
        while True:
            try:
                question = input("\n题目> ").strip()
            except EOFError:
                break
            if question.lower() in {"exit", "quit", "q"}:
                break
            if not question:
                continue
            index += 1
            _run_question(agent, question, f"ask-{index}", trace_path)
    except KeyboardInterrupt:
        print("\n已退出。")

    if trace_path is not None and index:
        print(f"本次轨迹：{trace_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
