"""多 Agent 流式接口的离线测试。"""

import json

from fastapi.testclient import TestClient

import config
import web.app as web_app
from llm import StubClient

client = TestClient(web_app.app)


def test_multi_stream_with_stub(monkeypatch, tmp_path):
    """StubClient 也能跑完阶段流，并返回 final 事件。"""
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(web_app, "get_client", lambda: StubClient())
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    with client.stream(
        "POST",
        "/api/ask-multi",
        json={"question": "验证 (x+1)**2 与 x**2+2*x+1 是否等价"},
    ) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())

    events = []
    for line in text.splitlines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:].strip()))

    assert events
    assert any(event.get("type") == "stage" for event in events)
    assert any(event.get("type") == "final" for event in events)
    final = next(event for event in events if event.get("type") == "final")
    assert final["answer"]
    assert "display_answer" in final
    # 最终回答写入会话，步骤追问依赖这两个字段
    assert final["session_id"]
    assert final["turn_id"]
