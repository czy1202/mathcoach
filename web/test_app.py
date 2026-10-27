"""Web MVP 的自动化测试。

运行方式（在 mathcoach 目录下执行）：
    python -m pytest web/test_app.py -v
"""

import config
from fastapi.testclient import TestClient

import web.app as web_app
from llm import StubClient


client = TestClient(web_app.app)


# ---------- 页面与健康检查 ----------

def test_index_returns_page():
    """根路径应返回单页应用。"""
    response = client.get("/")

    assert response.status_code == 200
    assert "mathcoach" in response.text
    assert "app.js" in response.text


def test_health_does_not_expose_key():
    """健康检查只报告是否配置了密钥，不返回密钥本身。"""
    response = client.get("/api/health")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert "key_set" in data
    assert "OPENAI_API_KEY" not in data


# ---------- 求解接口 ----------

def test_ask_with_stub_client(monkeypatch, tmp_path):
    """用 StubClient 跑通一次问答，并返回轨迹摘要。"""
    monkeypatch.setattr(web_app, "_client", StubClient())
    monkeypatch.setattr(web_app, "TRACE_DIR", tmp_path)

    response = client.post("/api/ask", json={"question": "验证恒等式"})

    assert response.status_code == 200
    data = response.json()
    assert data["final_answer"] == "1"
    assert "最终答案：1" in data["answer"]
    assert "最终答案：" not in data["display_answer"]
    assert data["llm_calls"] == 2
    assert data["tool_calls"] == 1
    assert data["tools"][0]["name"] == "verify_with_sympy"
    assert data["steps"]
    assert (tmp_path / data["trace_file"]).exists()


def test_ask_rejects_blank_question():
    """只有空白的题目应被拒绝。"""
    response = client.post("/api/ask", json={"question": "   "})

    assert response.status_code == 400
    assert "不能为空" in response.json()["detail"]


def test_ask_rejects_too_long_question():
    """超长题目由请求模型直接拒绝。"""
    response = client.post("/api/ask", json={"question": "x" * 5001})

    assert response.status_code == 422


def test_ask_without_api_key(monkeypatch):
    """未配置密钥时应返回明确的 503，而不是 500。"""
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(web_app, "_client", None)

    response = client.post("/api/ask", json={"question": "1+1"})

    assert response.status_code == 503
    assert "OPENAI_API_KEY" in response.json()["detail"]


# ---------- 展示辅助函数 ----------

def test_normalize_math_delimiters():
    """LaTeX 分隔符应统一成美元符号，同时保护换行写法。"""
    assert web_app.normalize_math_delimiters(r"\(x^2\)") == "$x^2$"
    assert web_app.normalize_math_delimiters(r"\[x^2\]") == "$$x^2$$"
    assert web_app.normalize_math_delimiters(r"\\[2pt]") == r"\\[2pt]"


def test_strip_answer_marker():
    """展示文本应去掉最后一行答案标记。"""
    text = "解题过程\n\n最终答案：1/2"
    assert web_app.strip_answer_marker(text) == "解题过程"


def test_strip_answer_marker_keeps_body_when_missing():
    """没有答案标记时不应改动正文。"""
    assert web_app.strip_answer_marker("计算结果是 1/2。") == "计算结果是 1/2。"


def test_strip_answer_marker_removes_empty_trailing_heading():
    """去掉答案标记后，结尾空标题不应留下。"""
    text = "解题过程\n\n## 最终答案\n\n最终答案：1/2"
    assert web_app.strip_answer_marker(text) == "解题过程"
