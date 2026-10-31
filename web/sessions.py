"""Web 多轮会话的内存存储。

设计边界：
1. 只存在进程内存里，服务重启就清空；本地演示够用；
2. 每轮只保存“用户问题 + 最终回答”，不保存工具调用记录；
3. 送给模型的上下文有数量和字符上限，避免多轮对话把 token 拖爆；
4. 会话 id 由服务端生成，客户端只负责带回，不能自己编造历史。
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field


# 服务端生成的 id 是 uuid4().hex，固定 32 位小写十六进制
SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")

ELLIPSIS = "\n…（中间省略）…\n"

DEFAULT_MAX_SESSIONS = 100
DEFAULT_TTL_SECONDS = 3600
DEFAULT_MAX_TURNS = 20
DEFAULT_MAX_HISTORY_TURNS = 3
DEFAULT_MAX_HISTORY_CHARS = 12000
DEFAULT_MAX_ASSISTANT_CHARS = 2000


def truncate_middle(text: str, limit: int) -> str:
    """截断长文本，保留开头和结尾。

    历史里的助手回答通常“开头是思路、结尾是最终答案”，截中间比截尾
    更有用。截断点尽量落在换行处，避免把一段话切得太碎。
    """
    if limit <= 0 or len(text) <= limit:
        return text
    if limit <= len(ELLIPSIS) + 20:
        return text[-limit:]

    head_len = int(limit * 0.6)
    tail_len = limit - head_len - len(ELLIPSIS)

    head = text[:head_len]
    cut = head.rfind("\n")
    if cut >= head_len // 2:
        head = head[:cut]

    tail = text[-tail_len:]
    newline = tail.find("\n")
    if 0 <= newline < len(tail) - 1:
        tail = tail[newline + 1:]

    return head + ELLIPSIS + tail


@dataclass
class Session:
    """一次会话。turns 只保存最终问答，不保存工具调用。"""

    id: str
    created_at: float
    updated_at: float
    original_question: str = ""
    turns: list[dict[str, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class SessionStore:
    """带过期、容量上限和上下文裁剪的会话存储。"""

    def __init__(
        self,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_history_turns: int = DEFAULT_MAX_HISTORY_TURNS,
        max_history_chars: int = DEFAULT_MAX_HISTORY_CHARS,
        max_assistant_chars: int = DEFAULT_MAX_ASSISTANT_CHARS,
    ) -> None:
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds
        self.max_turns = max_turns
        self.max_history_turns = max_history_turns
        self.max_history_chars = max_history_chars
        self.max_assistant_chars = max_assistant_chars

        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    # ---------- 会话生命周期 ----------

    def get_or_create(self, session_id: str | None = None) -> Session:
        """按 id 取会话；不存在或 id 非法时新建一个。

        服务重启后旧 id 会失效，此时返回新会话比抛 404 更适合本地演示：
        用户不需要手动清缓存，下一条消息会自动开始新对话。
        """
        now = time.monotonic()
        with self._lock:
            self._cleanup(now)

            if session_id and SESSION_ID_RE.fullmatch(session_id):
                session = self._sessions.get(session_id)
                if session is not None:
                    return session

            session = Session(
                id=uuid.uuid4().hex,
                created_at=now,
                updated_at=now,
            )
            self._sessions[session.id] = session
            self._evict_if_needed()
            return session

    def get(self, session_id: str | None) -> Session | None:
        """按 id 取会话；不存在、已过期或 id 非法时返回 None。

        和 ``get_or_create`` 不同，这里不会新建会话。步骤追问必须挂在一个
        仍然存在的主对话上，否则就没有原题和解答可以引用。
        """
        if not session_id or not SESSION_ID_RE.fullmatch(session_id):
            return None
        now = time.monotonic()
        with self._lock:
            self._cleanup(now)
            return self._sessions.get(session_id)

    def reset(self, session_id: str) -> bool:
        """删除一个会话；返回是否真的删除了。"""
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def clear(self) -> None:
        """清空所有会话，主要给测试用。"""
        with self._lock:
            self._sessions.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _cleanup(self, now: float) -> None:
        expired = [
            sid for sid, session in self._sessions.items()
            if now - session.updated_at > self.ttl_seconds
        ]
        for sid in expired:
            self._sessions.pop(sid, None)

    def _evict_if_needed(self) -> None:
        while len(self._sessions) > self.max_sessions:
            oldest = min(self._sessions.values(), key=lambda s: s.updated_at)
            self._sessions.pop(oldest.id, None)

    # ---------- 上下文 ----------

    def append_turn(self, session: Session, question: str, answer: str) -> str:
        """追加一轮最终问答，返回稳定的 turn_id。

        调用方需持有 session.lock。turn_id 不随轮次裁剪变化，步骤追问
        用它定位原回答；``turn`` 序号只作为旧客户端/旧数据的兜底。
        """
        if not session.original_question:
            session.original_question = question
        turn_id = uuid.uuid4().hex
        session.turns.append({
            "id": turn_id,
            "question": question,
            "answer": answer,
        })
        if len(session.turns) > self.max_turns:
            # 原始问题单独保存在 original_question，裁掉旧轮次后仍能置顶
            session.turns = session.turns[-self.max_turns:]
        session.updated_at = time.monotonic()
        return turn_id

    def get_turn_by_id(
        self,
        session: Session,
        turn_id: str | None,
    ) -> dict | None:
        """按 turn_id 取一轮问答。调用方需持有 session.lock。"""
        if not turn_id:
            return None
        for turn in session.turns:
            if turn.get("id") == turn_id:
                return turn
        return None

    def get_turn(
        self,
        session: Session,
        turn: int | None,
    ) -> dict | None:
        """按 1 起的轮次序号取问答。调用方需持有 session.lock。

        轮次被裁剪后旧序号会失效，因此这只是 turn_id 缺失时的兼容路径。
        """
        if turn is None or turn < 1 or turn > len(session.turns):
            return None
        return session.turns[turn - 1]

    def history_for(self, session: Session) -> list[dict[str, str]]:
        """构造送给模型的历史消息。

        调用方需持有 session.lock。规则：
        - 轮次不多时全部带上；
        - 轮次超过上限时，固定保留第一轮原题和它的回答，再带最近几轮；
        - 超过字符预算时先丢较旧的轮次，最后再截断助手回答。
        """
        turns = session.turns
        if not turns:
            return []

        start = max(0, len(turns) - self.max_history_turns)
        pairs: list[tuple[str, str, bool]] = []

        if start > 0:
            first = turns[0]
            pairs.append((
                session.original_question or first["question"],
                first["answer"],
                True,
            ))

        pairs.extend(
            (turn["question"], turn["answer"], False)
            for turn in turns[start:]
        )

        messages = self._build_messages(pairs)
        while self._char_count(messages) > self.max_history_chars:
            if pairs[0][2]:
                # 置顶轮次必须保留；至少再留一轮最近问答
                if len(pairs) <= 2:
                    break
                pairs.pop(1)
            else:
                if len(pairs) <= 1:
                    break
                pairs.pop(0)
            messages = self._build_messages(pairs)

        if self._char_count(messages) > self.max_history_chars:
            messages = self._fit_budget(messages)

        return messages

    def _build_messages(
        self,
        pairs: list[tuple[str, str, bool]],
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for question, answer, _pinned in pairs:
            messages.append({"role": "user", "content": question})
            messages.append({
                "role": "assistant",
                "content": truncate_middle(answer, self.max_assistant_chars),
            })
        return messages

    @staticmethod
    def _char_count(messages: list[dict[str, str]]) -> int:
        return sum(len(message["content"]) for message in messages)

    def _fit_budget(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        """最后一层保险：按剩余预算进一步截断助手回答。"""
        assistants = [m for m in messages if m["role"] == "assistant"]
        if not assistants:
            return messages

        user_chars = sum(
            len(m["content"]) for m in messages if m["role"] == "user"
        )
        available = max(
            50 * len(assistants),
            self.max_history_chars - user_chars,
        )
        per_message = max(50, available // len(assistants))

        for message in assistants:
            message["content"] = truncate_middle(message["content"], per_message)
        return messages
