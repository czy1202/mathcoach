"""运行轨迹（trace）记录。

一次求解 = 一条 trace，包含每一步的模型调用、工具调用、token 用量、
耗时和最终判定。评测跑批时每个 case 写一行 JSONL，方便事后回放和汇总。

设计原则：
1. 记录器只收集数据，不改变 Agent 的行为；
2. 写入失败不能影响求解（trace 是观测手段，不是业务逻辑）；
3. 所有字段都能 JSON 序列化，无法序列化的对象转成字符串。
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_TEXT = 8000     # 单个文本字段最多保留多少字符，避免 trace 文件过大


def _safe(value: Any) -> Any:
    """把值转成可 JSON 序列化的形式，并截断过长的文本。"""
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "…[truncated]"
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def new_trace_file(directory: Path | str, prefix: str = "eval") -> Path:
    """生成一个带时间戳的 trace 文件路径，并确保目录存在。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return directory / f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}.jsonl"


@dataclass
class TraceRecorder:
    """收集一次求解的事件，并在结束时写出一行 JSONL。"""

    case_id: str = ""
    question: str = ""
    path: Path | None = None
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    events: list[dict] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    })
    llm_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    final_answer: str | None = None
    error: str | None = None
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    started: float = field(default_factory=time.perf_counter)

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path = Path(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---------- 记录事件 ----------

    def add(self, event_type: str, **fields: Any) -> None:
        """追加一个事件，并更新聚合计数。"""
        event = {"type": event_type, "t": round(time.perf_counter() - self.started, 4)}
        event.update({k: _safe(v) for k, v in fields.items()})
        self.events.append(event)

        if event_type == "llm":
            self.llm_calls += 1
            usage = fields.get("usage") or {}
            for key in self.usage:
                self.usage[key] += int(usage.get(key, 0) or 0)
        elif event_type == "tool":
            self.tool_calls += 1
            if not fields.get("ok", True):
                self.tool_errors += 1
        elif event_type == "final":
            self.final_answer = fields.get("content")
        elif event_type == "error":
            self.error = fields.get("message") or fields.get("kind")

    # ---------- 结束并落盘 ----------

    def finish(self, **fields: Any) -> dict:
        """结束本次记录，写出 JSONL，并返回完整 record。"""
        record = {
            "run_id": self.run_id,
            "case_id": self.case_id,
            "question": _safe(self.question),
            "started_at": self.started_at,
            "elapsed": round(time.perf_counter() - self.started, 4),
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "usage": self.usage,
            "final_answer": _safe(self.final_answer),
            "error": _safe(self.error),
            "events": self.events,
        }
        record.update({k: _safe(v) for k, v in fields.items()})

        if self.path is not None:
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            except OSError:
                # trace 写入失败不应影响评测结果
                pass

        return record
