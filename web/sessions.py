"""Web 多轮会话存储。

设计边界：
1. 默认是进程内存；传入 ``persist_path`` 后会额外落到 SQLite，服务重启
   仍能恢复最近会话。SQLite 只是本地单文件，不引入外部服务；
2. 每轮只保存“用户问题 + 最终回答”，不保存工具调用记录；
3. 送给模型的上下文有数量和字符上限，避免多轮对话把 token 拖爆；
4. 会话 id 由服务端生成，客户端只负责带回，不能自己编造历史。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path


logger = logging.getLogger(__name__)

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
    # 正在处理这次会话的请求 id。取消或结束后清空，客户端可以立刻追问。
    active_request_id: str | None = field(default=None, repr=False)
    # “新对话”删除会话后，仍在后台跑的旧请求不能再把结果写回来。
    closed: bool = field(default=False, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class SessionStore:
    """带过期、容量上限、上下文裁剪和可选 SQLite 持久化的会话存储。"""

    def __init__(
        self,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_history_turns: int = DEFAULT_MAX_HISTORY_TURNS,
        max_history_chars: int = DEFAULT_MAX_HISTORY_CHARS,
        max_assistant_chars: int = DEFAULT_MAX_ASSISTANT_CHARS,
        persist_path: str | Path | None = None,
        namespace: str = "main",
    ) -> None:
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds
        self.max_turns = max_turns
        self.max_history_turns = max_history_turns
        self.max_history_chars = max_history_chars
        self.max_assistant_chars = max_assistant_chars

        self.persist_path = Path(persist_path) if persist_path is not None else None
        self.namespace = namespace

        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

        if self.persist_path is not None:
            self._load_from_disk()

    @property
    def persistent(self) -> bool:
        return self.persist_path is not None

    # ---------- 会话生命周期 ----------

    def get_or_create(self, session_id: str | None = None) -> Session:
        """按 id 取会话；不存在或 id 非法时新建一个。

        持久化开启时，重启后旧 id 仍能从 SQLite 恢复；确实不存在时
        新建会话，下一条消息会自动开始新对话。
        """
        now = time.time()
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
            self._write_session(session)
            return session

    def get(self, session_id: str | None) -> Session | None:
        """按 id 取会话；不存在、已过期或 id 非法时返回 None。

        和 ``get_or_create`` 不同，这里不会新建会话。步骤追问必须挂在一个
        仍然存在的主对话上，否则就没有原题和解答可以引用。
        """
        if not session_id or not SESSION_ID_RE.fullmatch(session_id):
            return None
        now = time.time()
        with self._lock:
            self._cleanup(now)
            return self._sessions.get(session_id)

    def reset(self, session_id: str) -> bool:
        """删除一个会话；返回是否真的删除了。"""
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return False
            session.closed = True
            session.active_request_id = None
            self._delete_ids([session_id])
            return True

    def clear(self) -> None:
        """清空所有会话，主要给测试用。"""
        with self._lock:
            for session in self._sessions.values():
                session.closed = True
                session.active_request_id = None
            self._sessions.clear()
            self._clear_disk()

    def size(self) -> int:
        with self._lock:
            return len(self._sessions)

    def begin_request(self, session: Session, request_id: str) -> bool:
        """标记会话正在处理某个请求；已有其他请求时返回 False。

        调用方需持有 ``session.lock``。这里不持久化 active_request_id：
        进程重启后没有仍在跑的请求，恢复出来的会话应该直接可追问。
        """
        if session.closed:
            return False
        if session.active_request_id not in (None, request_id):
            return False
        session.active_request_id = request_id
        return True

    def end_request(self, session: Session, request_id: str) -> bool:
        """结束请求；如果它已经被取消或被新请求取代，返回 False。"""
        if session.active_request_id != request_id:
            return False
        session.active_request_id = None
        return True

    def _cleanup(self, now: float) -> None:
        expired = [
            sid for sid, session in self._sessions.items()
            if now - session.updated_at > self.ttl_seconds
        ]
        for sid in expired:
            session = self._sessions.pop(sid, None)
            if session is not None:
                session.closed = True
        if expired:
            self._delete_ids(expired)

    def _evict_if_needed(self) -> None:
        evicted: list[str] = []
        while len(self._sessions) > self.max_sessions:
            oldest = min(self._sessions.values(), key=lambda s: s.updated_at)
            self._sessions.pop(oldest.id, None)
            oldest.closed = True
            evicted.append(oldest.id)
        if evicted:
            self._delete_ids(evicted)

    # ---------- 上下文 ----------

    def append_turn(self, session: Session, question: str, answer: str) -> str:
        """追加一轮最终问答，返回稳定的 turn_id。

        调用方需持有 session.lock。turn_id 不随轮次裁剪变化，步骤追问
        用它定位原回答；``turn`` 序号只作为旧客户端/旧数据的兜底。
        """
        if session.closed:
            raise ValueError("会话已结束，不能继续追加问答")
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
        session.updated_at = time.time()
        self._write_session(session)
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

    # ---------- SQLite 持久化 ----------

    def _connect(self) -> sqlite3.Connection:
        """打开 SQLite 并确保表存在。调用方负责 close。"""
        assert self.persist_path is not None
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.persist_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                namespace TEXT NOT NULL,
                id TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                original_question TEXT NOT NULL DEFAULT '',
                turns_json TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (namespace, id)
            )
            """
        )
        return conn

    def _load_from_disk(self) -> None:
        """启动时从 SQLite 恢复未过期会话；损坏或缺文件时静默跳过。"""
        assert self.persist_path is not None
        if not self.persist_path.exists():
            return

        now = time.time()
        loaded: list[Session] = []
        expired: list[str] = []
        conn = None
        try:
            conn = self._connect()
            rows = conn.execute(
                "SELECT id, created_at, updated_at, original_question, turns_json "
                "FROM sessions WHERE namespace = ?",
                (self.namespace,),
            ).fetchall()
            for sid, created_at, updated_at, original, raw_turns in rows:
                if not isinstance(sid, str) or not SESSION_ID_RE.fullmatch(sid):
                    continue
                try:
                    updated = float(updated_at)
                except (TypeError, ValueError):
                    updated = now
                if now - updated > self.ttl_seconds:
                    expired.append(sid)
                    continue

                turns = self._decode_turns(raw_turns)
                session = Session(
                    id=sid,
                    created_at=float(created_at or updated),
                    updated_at=updated,
                    original_question=str(original or ""),
                    turns=turns,
                )
                if not session.original_question and turns:
                    session.original_question = str(turns[0].get("question") or "")
                loaded.append(session)

            # 超过容量时只保留最近更新的会话，避免旧库把内存撑大
            loaded.sort(key=lambda item: item.updated_at)
            keep = loaded[-self.max_sessions:] if self.max_sessions > 0 else []
            dropped = [item.id for item in loaded[:max(0, len(loaded) - len(keep))]]
            for session in keep:
                self._sessions[session.id] = session
            stale = expired + dropped
            if stale:
                self._delete_ids(stale, conn=conn)
        except (sqlite3.Error, OSError, ValueError) as exc:
            logger.warning("会话持久化文件读取失败，将只使用内存：%s", exc)
        finally:
            if conn is not None:
                conn.close()

    def _decode_turns(self, raw: object) -> list[dict[str, str]]:
        try:
            data = json.loads(str(raw or "[]"))
        except (TypeError, ValueError):
            return []
        if not isinstance(data, list):
            return []

        turns: list[dict[str, str]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            question = item.get("question")
            answer = item.get("answer")
            if not isinstance(question, str) or not isinstance(answer, str):
                continue
            turn_id = item.get("id")
            turns.append({
                "id": str(turn_id) if turn_id else uuid.uuid4().hex,
                "question": question,
                "answer": answer,
            })
        return turns[-self.max_turns:]

    def _write_session(self, session: Session) -> None:
        if self.persist_path is None:
            return
        conn = None
        try:
            conn = self._connect()
            with conn:
                conn.execute(
                    """
                    INSERT INTO sessions
                        (namespace, id, created_at, updated_at,
                         original_question, turns_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(namespace, id) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        original_question = excluded.original_question,
                        turns_json = excluded.turns_json
                    """,
                    (
                        self.namespace,
                        session.id,
                        session.created_at,
                        session.updated_at,
                        session.original_question,
                        json.dumps(session.turns, ensure_ascii=False),
                    ),
                )
        except (sqlite3.Error, OSError) as exc:
            logger.warning("会话持久化写入失败，保留内存数据：%s", exc)
        finally:
            if conn is not None:
                conn.close()

    def _delete_ids(
        self,
        ids: list[str],
        conn: sqlite3.Connection | None = None,
    ) -> None:
        if self.persist_path is None or not ids:
            return
        own_conn = conn is None
        if conn is None:
            try:
                conn = self._connect()
            except (sqlite3.Error, OSError) as exc:
                logger.warning("会话持久化删除失败：%s", exc)
                return
        try:
            placeholders = ",".join("?" for _ in ids)
            with conn:
                conn.execute(
                    f"DELETE FROM sessions WHERE namespace = ? AND id IN ({placeholders})",
                    (self.namespace, *ids),
                )
        except (sqlite3.Error, OSError) as exc:
            logger.warning("会话持久化删除失败：%s", exc)
        finally:
            if own_conn:
                conn.close()

    def _clear_disk(self) -> None:
        if self.persist_path is None:
            return
        conn = None
        try:
            conn = self._connect()
            with conn:
                conn.execute(
                    "DELETE FROM sessions WHERE namespace = ?",
                    (self.namespace,),
                )
        except (sqlite3.Error, OSError) as exc:
            logger.warning("会话持久化清空失败：%s", exc)
        finally:
            if conn is not None:
                conn.close()
