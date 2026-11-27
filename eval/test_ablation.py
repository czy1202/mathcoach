"""消融脚本的离线测试，不访问网络。"""

import json

from eval import ablation
from llm import StubClient


def test_stub_run_writes_report(tmp_path):
    out = tmp_path / "ablation.json"

    code = ablation.main([
        "--stub",
        "--configs", "A,C",
        "--limit", "1",
        "--no-trace",
        "--out", str(out),
    ])

    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["real"] is False
    assert len(data["cases"]) == 1
    assert {s["config"] for s in data["summary"]} == {"A", "C"}
    for summary in data["summary"]:
        assert summary["total"] == 1


def test_multi_agent_adapter_uses_more_calls_than_single():
    """多 Agent 至少要多跑规划和复核两次调用。"""
    case = {"statement": "验证 (x+1)**2 与 x**2+2*x+1 是否等价"}

    single = ablation.run_one("A", {**case, "id": "x", "expect": "solve",
                                    "answer": "1", "judge": "symbolic"},
                              StubClient(), None)
    multi = ablation.run_one("C", {**case, "id": "x", "expect": "solve",
                                   "answer": "1", "judge": "symbolic"},
                             StubClient(), None)

    assert multi.calls > single.calls


def test_counting_client_accumulates_usage():
    client = ablation.CountingClient(StubClient())

    client.chat([{"role": "user", "content": "hi"}])

    assert client.calls == 1
    # StubClient 不返回 usage，累计值保持 0，但不应报错
    assert client.total_tokens == 0
