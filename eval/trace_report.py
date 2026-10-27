"""轨迹汇总报告。

读取 eval/run_eval.py 写出的 JSONL 轨迹，输出：
总题数、判定分布、耗时、token、模型调用/工具调用次数、错误分布，
以及逐题明细。用于判断一次评测到底是"模型不行"还是"流程有问题"。

运行方式：
    python -m eval.trace_report traces/eval-20261027-153000-abc123.jsonl
    python -m eval.trace_report            # 自动选 traces/ 下最新的文件
"""

import json
import sys
from collections import Counter
from pathlib import Path


DEFAULT_TRACE_DIR = Path("traces")


def load_traces(path: Path) -> list[dict]:
    """逐行读取轨迹文件。坏行给出提示后跳过，不影响其余记录。"""
    records: list[dict] = []
    with Path(path).open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"警告：{path} 第 {line_no} 行不是合法 JSON，已跳过",
                      file=sys.stderr)
    return records


def latest_trace(directory: Path = DEFAULT_TRACE_DIR) -> Path | None:
    """返回目录下最新的 trace 文件。"""
    files = sorted(Path(directory).glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[index]


def summarize_traces(records: list[dict]) -> dict:
    """把一批 trace 记录压成汇总指标。"""
    verdicts = Counter(r.get("verdict", "unknown") for r in records)
    elapsed = [float(r.get("elapsed", 0) or 0) for r in records]

    usage = Counter()
    llm_calls = tool_calls = tool_errors = 0
    error_kinds = Counter()

    for record in records:
        for key, value in (record.get("usage") or {}).items():
            usage[key] += int(value or 0)
        llm_calls += int(record.get("llm_calls", 0) or 0)
        tool_calls += int(record.get("tool_calls", 0) or 0)
        tool_errors += int(record.get("tool_errors", 0) or 0)
        for event in record.get("events") or []:
            if event.get("type") == "error":
                error_kinds[event.get("kind", "unknown")] += 1

    total = len(records)
    return {
        "total": total,
        "pass": verdicts.get("pass", 0),
        "fail": verdicts.get("fail", 0),
        "uncertain": verdicts.get("uncertain", 0),
        "unknown": verdicts.get("unknown", 0),
        "elapsed_total": sum(elapsed),
        "elapsed_avg": sum(elapsed) / total if total else 0.0,
        "elapsed_p95": _p95(elapsed),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "tokens_avg": usage.get("total_tokens", 0) / total if total else 0.0,
        "llm_calls": llm_calls,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "tool_error_rate": tool_errors / tool_calls if tool_calls else 0.0,
        "error_kinds": dict(error_kinds),
    }


def print_report(records: list[dict], path: Path) -> None:
    """打印汇总指标和逐题明细。"""
    s = summarize_traces(records)

    print("=" * 78)
    print(f"轨迹报告：{path}")
    print("=" * 78)
    print(f"题数：{s['total']}　通过 {s['pass']}　失败 {s['fail']}　"
          f"不确定 {s['uncertain']}　未知 {s['unknown']}")
    print(f"耗时：总计 {s['elapsed_total']:.2f}s，平均 {s['elapsed_avg']:.2f}s/题，"
          f"p95 {s['elapsed_p95']:.2f}s")
    print(f"token：总计 {s['total_tokens']}（输入 {s['prompt_tokens']} / "
          f"输出 {s['completion_tokens']}），平均 {s['tokens_avg']:.0f}/题")
    print(f"调用：模型 {s['llm_calls']} 次，工具 {s['tool_calls']} 次，"
          f"工具错误 {s['tool_errors']} 次"
          f"（{s['tool_error_rate'] * 100:.1f}%）")
    if s["error_kinds"]:
        detail = "，".join(f"{k}={v}" for k, v in sorted(s["error_kinds"].items()))
        print(f"错误分布：{detail}")
    else:
        print("错误分布：无")

    failed = [r for r in records if r.get("verdict") != "pass"]
    if failed:
        print("-" * 78)
        print("未通过明细：")
        for r in failed:
            print(f"  {r.get('case_id', '?'):6s} | {r.get('verdict', '?'):9s} | "
                  f"提取={r.get('extracted') or '未提取到'} | "
                  f"错误={r.get('error') or '-'}")

    print("-" * 78)
    print(f"{'ID':6s} {'判定':10s} {'模型':>4s} {'工具':>4s} "
          f"{'token':>8s} {'耗时':>8s}  提取")
    for r in records:
        print(f"{r.get('case_id', '?'):6s} {r.get('verdict', '?'):10s} "
              f"{int(r.get('llm_calls', 0) or 0):4d} "
              f"{int(r.get('tool_calls', 0) or 0):4d} "
              f"{int((r.get('usage') or {}).get('total_tokens', 0) or 0):8d} "
              f"{float(r.get('elapsed', 0) or 0):7.2f}s  "
              f"{r.get('extracted') or '-'}")


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        path = Path(argv[0])
    else:
        path = latest_trace()
        if path is None:
            print(f"错误：{DEFAULT_TRACE_DIR}/ 下没有轨迹文件，请先运行评测。")
            return 1

    if not path.exists():
        print(f"错误：轨迹文件不存在：{path}")
        return 1

    records = load_traces(path)
    if not records:
        print(f"错误：{path} 里没有有效记录。")
        return 1

    print_report(records, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
