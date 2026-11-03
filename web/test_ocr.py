"""网页端图片识题接口的测试。全部使用假 OCR，不访问网络。

运行方式（在 mathcoach 目录下执行）：
    python -m pytest web/test_ocr.py -v
"""

import pytest
import config
from fastapi.testclient import TestClient

import ocr
import web.app as web_app


client = TestClient(web_app.app)
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16


@pytest.fixture(autouse=True)
def _reset_ocr_lock():
    """上一个测试若异常退出，确保锁不会一直占着。"""
    if web_app._ocr_lock.locked():
        web_app._ocr_lock.release()
    yield
    if web_app._ocr_lock.locked():
        web_app._ocr_lock.release()


@pytest.fixture
def configured(monkeypatch):
    """走 SimpleTex 分支，便于测试“未配置”等云端相关路径。"""
    monkeypatch.setattr(config, "OCR_PROVIDER", "simpletex")
    monkeypatch.setattr(config, "SIMPLETEX_TOKEN", "AT-test-token")
    monkeypatch.setattr(config, "SIMPLETEX_APP_ID", "")
    monkeypatch.setattr(config, "SIMPLETEX_APP_SECRET", "")
    return "AT-test-token"


def upload(data=PNG, filename="problem.png", content_type="image/png"):
    return client.post(
        "/api/ocr",
        files={"file": (filename, data, content_type)},
    )


# ---------- 健康检查 ----------

def test_health_reports_ocr_configured(configured):
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["ocr_configured"] is True
    assert response.json()["ocr_provider"] == "simpletex"


def test_health_reports_local_ocr(monkeypatch):
    """默认的本地 Ollama 不需要密钥，始终视为已配置。"""
    monkeypatch.setattr(config, "OCR_PROVIDER", "ollama")

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["ocr_configured"] is True
    assert response.json()["ocr_provider"] == "ollama"


def test_health_reports_ocr_missing(monkeypatch):
    monkeypatch.setattr(config, "OCR_PROVIDER", "simpletex")
    monkeypatch.setattr(config, "SIMPLETEX_TOKEN", "")
    monkeypatch.setattr(config, "SIMPLETEX_APP_ID", "")
    monkeypatch.setattr(config, "SIMPLETEX_APP_SECRET", "")

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["ocr_configured"] is False


# ---------- 识别接口 ----------

def test_ocr_returns_text_without_solving(configured, monkeypatch):
    """只返回识别文本，不产生会话、不调用解题模型。"""
    def fake_recognize(data):
        assert data == PNG
        return ocr.OcrResult(text="1. 求 $\\int_0^1 x\\,dx$", mode="simpletex_ocr",
                             confidence=0.88)

    monkeypatch.setattr(web_app.ocr, "recognize", fake_recognize)

    response = upload()

    assert response.status_code == 200
    data = response.json()
    assert data["text"] == "1. 求 $\\int_0^1 x\\,dx$"
    assert data["mode"] == "simpletex_ocr"
    assert data["confidence"] == pytest.approx(0.88)
    assert web_app.SESSIONS.size() == 0


def test_ocr_requires_configuration(monkeypatch):
    monkeypatch.setattr(config, "OCR_PROVIDER", "simpletex")
    monkeypatch.setattr(config, "SIMPLETEX_TOKEN", "")
    monkeypatch.setattr(config, "SIMPLETEX_APP_ID", "")
    monkeypatch.setattr(config, "SIMPLETEX_APP_SECRET", "")

    response = upload()

    assert response.status_code == 503
    assert "SIMPLETEX_TOKEN" in response.json()["detail"]


def test_ocr_rejects_unsupported_format(configured, monkeypatch):
    def should_not_run(data):
        raise AssertionError("格式校验失败时不应调用 OCR 服务")

    monkeypatch.setattr(web_app.ocr, "recognize", should_not_run)

    response = upload(data=b"GIF89a" + b"x" * 20)

    assert response.status_code == 415
    assert "PNG" in response.json()["detail"]


def test_ocr_rejects_bmp_on_local_provider(monkeypatch):
    monkeypatch.setattr(config, "OCR_PROVIDER", "ollama")

    response = upload(data=b"BM" + b"x" * 20, filename="problem.bmp",
                      content_type="image/bmp")

    assert response.status_code == 415
    assert "PNG / JPG" in response.json()["detail"]


def test_ocr_rejects_oversize(configured, monkeypatch):
    monkeypatch.setattr(ocr, "MAX_IMAGE_BYTES", 8)

    response = upload()

    assert response.status_code == 413
    assert "MB" in response.json()["detail"]


def test_ocr_rejects_empty_file(configured):
    response = upload(data=b"")

    assert response.status_code == 400


def test_ocr_maps_upstream_failure_to_502(configured, monkeypatch):
    def fail(data):
        raise ocr.OcrError("图片识别失败：额度不足")

    monkeypatch.setattr(web_app.ocr, "recognize", fail)

    response = upload()

    assert response.status_code == 502
    assert "额度不足" in response.json()["detail"]


def test_ocr_rejects_concurrent_upload(configured, monkeypatch):
    """同一时间只允许一张图片识别，避免重复消耗额度。"""
    web_app._ocr_lock.acquire()

    response = upload()

    assert response.status_code == 429
