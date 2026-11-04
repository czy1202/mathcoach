"""Web 测试的公共隔离。

线上默认把会话写到 data/sessions.db；测试必须用独立的内存存储，
否则一次 `clear()` 就会删掉本地真实会话。
"""

import pytest

import web.app as web_app
from web.sessions import SessionStore


@pytest.fixture(autouse=True)
def isolate_session_stores(monkeypatch):
    """每个测试使用全新的内存会话，不触碰真实 SQLite 文件。"""
    monkeypatch.setattr(web_app, "SESSIONS", SessionStore())
    monkeypatch.setattr(
        web_app,
        "STEP_SESSIONS",
        SessionStore(
            max_sessions=200,
            max_history_turns=4,
            max_history_chars=24000,
            max_assistant_chars=4000,
        ),
    )
    with web_app._CANCEL_LOCK:
        web_app._CANCEL_EVENTS.clear()
    yield
    with web_app._CANCEL_LOCK:
        web_app._CANCEL_EVENTS.clear()
