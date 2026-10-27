"""多轮会话存储的单元测试。"""

from web.sessions import SESSION_ID_RE, SessionStore, truncate_middle


# ---------- 会话生命周期 ----------

def test_new_session_has_no_history():
    store = SessionStore()
    session = store.get_or_create()

    assert SESSION_ID_RE.fullmatch(session.id)
    assert store.history_for(session) == []
    assert store.size() == 1


def test_append_turn_builds_user_assistant_history():
    store = SessionStore()
    session = store.get_or_create()

    store.append_turn(session, "求极限", "极限是 1")
    history = store.history_for(session)

    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[0]["content"] == "求极限"
    assert history[1]["content"] == "极限是 1"


def test_invalid_session_id_starts_new_session():
    store = SessionStore()
    session = store.get_or_create("not-a-valid-id")

    assert SESSION_ID_RE.fullmatch(session.id)
    assert session.id != "not-a-valid-id"

    unknown = "0" * 32
    assert store.get_or_create(unknown).id != unknown


def test_reset_removes_session():
    store = SessionStore()
    session = store.get_or_create()

    assert store.reset(session.id) is True
    assert store.reset(session.id) is False
    assert store.get_or_create(session.id).id != session.id


def test_oldest_session_is_evicted_at_capacity():
    store = SessionStore(max_sessions=2)
    first = store.get_or_create()
    store.get_or_create()
    store.get_or_create()

    assert store.size() == 2
    assert store.get_or_create(first.id).id != first.id


# ---------- 上下文裁剪 ----------

def test_original_question_is_pinned():
    store = SessionStore(max_history_turns=2)
    session = store.get_or_create()

    for i in range(1, 6):
        store.append_turn(session, f"q{i}", f"a{i}")

    history = store.history_for(session)
    contents = [m["content"] for m in history]

    assert contents[0] == "q1"
    assert contents[1] == "a1"
    assert "q3" not in contents
    assert "q4" in contents
    assert "q5" in contents
    assert [m["role"] for m in history] == [
        "user", "assistant", "user", "assistant", "user", "assistant",
    ]


def test_history_respects_char_budget():
    store = SessionStore(
        max_history_turns=10,
        max_history_chars=200,
        max_assistant_chars=1000,
    )
    session = store.get_or_create()

    for i in range(1, 4):
        store.append_turn(session, f"q{i}", "答" * 500)

    history = store.history_for(session)
    contents = [m["content"] for m in history]

    assert sum(len(c) for c in contents) <= 200
    assert contents[0] == "q3"      # 旧轮次被丢掉，最近一轮保留


def test_truncate_middle_keeps_head_and_final_answer():
    text = "开头" * 100 + "\n最终答案：42"
    result = truncate_middle(text, 200)

    assert len(result) <= 200
    assert result.startswith("开头")
    assert "最终答案：42" in result
    assert "中间省略" in result


def test_long_assistant_answer_is_truncated_in_history():
    store = SessionStore(max_assistant_chars=200)
    session = store.get_or_create()
    answer = "开头" * 100 + "\n最终答案：42"

    store.append_turn(session, "题目", answer)
    history = store.history_for(session)

    assert len(history[1]["content"]) <= 200
    assert "最终答案：42" in history[1]["content"]
