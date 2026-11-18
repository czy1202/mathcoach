"""图片识题模块的测试。

运行方式（在 mathcoach 目录下执行）：
    python -m pytest test_ocr.py -v

所有测试都不访问真实网络：httpx.post 被替换成假函数。
"""

import base64
import hashlib
import io

import httpx
import pytest
from PIL import Image

import config
import ocr


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16
JPG = b"\xff\xd8\xff\xe0" + b"x" * 16
BMP = b"BM" + b"x" * 16


@pytest.fixture
def simpletex_env(monkeypatch):
    """SimpleTex 配置：使用 token，整页识别，指向一个假地址。"""
    monkeypatch.setattr(config, "OCR_PROVIDER", "simpletex")
    monkeypatch.setattr(config, "SIMPLETEX_TOKEN", "AT-test-token")
    monkeypatch.setattr(config, "SIMPLETEX_APP_ID", "")
    monkeypatch.setattr(config, "SIMPLETEX_APP_SECRET", "")
    monkeypatch.setattr(config, "SIMPLETEX_MODE", "simpletex_ocr")
    monkeypatch.setattr(config, "SIMPLETEX_BASE_URL", "https://ocr.test/api")
    return config


@pytest.fixture
def ollama_env(monkeypatch):
    """本地 Ollama 配置：不发真实请求，httpx.post 由测试替换。"""
    monkeypatch.setattr(config, "OCR_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_HOST", "http://127.0.0.1:11434")
    monkeypatch.setattr(config, "OLLAMA_OCR_MODEL", "glm-ocr")
    monkeypatch.setattr(config, "OLLAMA_OCR_PROMPT", "")
    monkeypatch.setattr(config, "OLLAMA_OCR_TIMEOUT", 240.0)
    monkeypatch.setattr(config, "OLLAMA_OCR_NUM_CTX", 16384)
    return config


@pytest.fixture
def deepseek_env(monkeypatch):
    """DeepSeek 视觉配置：复用 OpenAI Key，不发真实请求。"""
    monkeypatch.setattr(config, "OCR_PROVIDER", "deepseek")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setattr(config, "OPENAI_VISION_MODEL", "deepseek-flash")
    return config


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeCompletionResponse:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


class FakeCompletions:
    def __init__(self, content="题目：$\\frac{1}{2}$", exc=None):
        self.content = content
        self.exc = exc
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return FakeCompletionResponse(self.content)


class FakeOpenAI:
    last = None

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.completions = FakeCompletions()
        self.chat = type("Chat", (), {"completions": self.completions})()
        FakeOpenAI.last = self


def install_deepseek(monkeypatch, content="题目：$\\frac{1}{2}$", exc=None):
    client = FakeOpenAI()
    client.completions.content = content
    client.completions.exc = exc
    monkeypatch.setattr(ocr, "OpenAI", lambda **kwargs: client)
    return client


class FakeResponse:
    def __init__(self, payload, status_code=200, reason_phrase="OK"):
        self._payload = payload
        self.status_code = status_code
        self.reason_phrase = reason_phrase

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def install_post(monkeypatch, response):
    """替换 httpx.post，并记录调用参数。"""
    calls = []

    def fake_post(url, headers=None, data=None, json=None, files=None,
                  timeout=None):
        calls.append({"url": url, "headers": headers, "data": data,
                      "json": json, "files": files, "timeout": timeout})
        return response

    monkeypatch.setattr(ocr.httpx, "post", fake_post)
    return calls


# ---------- 鉴权 ----------

def test_auth_prefers_token(simpletex_env):
    headers = ocr.auth_headers(
        token="AT-token",
        app_id="",
        app_secret="",
    )

    assert headers == {"token": "AT-token"}


def test_auth_signs_app_credentials():
    headers = ocr.auth_headers(
        token="",
        app_id="app-123",
        app_secret="secret-456",
        timestamp="1700000000",
        random_str="AbCdEf1234567890",
    )

    raw = (
        "app-id=app-123&random-str=AbCdEf1234567890"
        "&timestamp=1700000000&secret=secret-456"
    )
    assert headers == {
        "app-id": "app-123",
        "random-str": "AbCdEf1234567890",
        "timestamp": "1700000000",
        "sign": hashlib.md5(raw.encode()).hexdigest(),
    }


def test_auth_signature_includes_form_params():
    """官方签名要求把表单字段和鉴权字段一起按 key 排序。"""
    headers = ocr.auth_headers(
        token="",
        app_id="app-123",
        app_secret="secret-456",
        params={"rec_mode": "document", "inline_formula_wrapper": '["$","$"]'},
        timestamp="1700000000",
        random_str="AbCdEf1234567890",
    )

    raw = (
        'app-id=app-123&inline_formula_wrapper=["$","$"]'
        "&random-str=AbCdEf1234567890&rec_mode=document"
        "&timestamp=1700000000&secret=secret-456"
    )
    assert headers["sign"] == hashlib.md5(raw.encode()).hexdigest()


def test_auth_requires_credentials(simpletex_env):
    with pytest.raises(ocr.OcrError) as exc:
        ocr.auth_headers(token="", app_id="", app_secret="")

    assert "SIMPLETEX_TOKEN" in str(exc.value)


# ---------- 图片格式 ----------

@pytest.mark.parametrize(
    ("data", "extension", "mime"),
    [(PNG, "png", "image/png"),
     (JPG, "jpg", "image/jpeg"),
     (BMP, "bmp", "image/bmp")],
)
def test_detect_image_type(data, extension, mime):
    assert ocr.detect_image_type(data) == (extension, mime)


def test_validate_rejects_unknown_format(simpletex_env):
    with pytest.raises(ocr.OcrError) as exc:
        ocr.validate_image(b"GIF89a")

    assert "PNG" in str(exc.value)


def test_validate_rejects_empty(simpletex_env):
    with pytest.raises(ocr.OcrError):
        ocr.validate_image(b"")


def test_validate_rejects_oversize(simpletex_env, monkeypatch):
    monkeypatch.setattr(ocr, "MAX_IMAGE_BYTES", 4)

    with pytest.raises(ocr.OcrError) as exc:
        ocr.validate_image(PNG)

    assert "MB" in str(exc.value)


# ---------- 识别 ----------

def test_recognize_whole_page_uses_info(simpletex_env, monkeypatch):
    calls = install_post(monkeypatch, FakeResponse({
        "status": True,
        "res": {"info": "1. 计算 $\\int_0^1 x\\,dx$", "conf": 0.93},
        "request_id": "tr_test",
    }))

    result = ocr.recognize(PNG)

    assert result.text == "1. 计算 $\\int_0^1 x\\,dx$"
    assert result.mode == "simpletex_ocr"
    assert result.confidence == pytest.approx(0.93)
    assert calls[0]["url"] == "https://ocr.test/api/simpletex_ocr"
    assert calls[0]["headers"] == {"token": "AT-test-token"}
    assert calls[0]["data"]["rec_mode"] == "document"
    assert calls[0]["data"]["inline_formula_wrapper"] == '["$","$"]'
    assert calls[0]["data"]["isolated_formula_wrapper"] == '["$$","$$"]'
    filename, data, mime = calls[0]["files"]["file"]
    assert filename == "problem.png"
    assert data == PNG
    assert mime == "image/png"


def test_recognize_document_mode_uses_content(simpletex_env, monkeypatch):
    """新版文档识别返回 res.content（Markdown），也要能取到。"""
    install_post(monkeypatch, FakeResponse({
        "status": True,
        "res": {"content": "1. 求 $\\int_0^1 x\\,dx$"},
    }))

    result = ocr.recognize(PNG)

    assert result.text == "1. 求 $\\int_0^1 x\\,dx$"


def test_recognize_latex_mode_uses_latex(simpletex_env, monkeypatch):
    monkeypatch.setattr(config, "SIMPLETEX_MODE", "latex_ocr")
    install_post(monkeypatch, FakeResponse({
        "status": True,
        "res": {"latex": "\\frac{1}{2}"},
    }))

    result = ocr.recognize(JPG)

    assert result.text == "\\frac{1}{2}"
    assert result.mode == "latex_ocr"


def test_recognize_joins_list_result(simpletex_env, monkeypatch):
    install_post(monkeypatch, FakeResponse({
        "status": True,
        "res": [{"info": "第一题"}, {"info": "第二题"}],
    }))

    result = ocr.recognize(PNG)

    assert result.text == "第一题\n\n第二题"


def test_recognize_status_false_reports_reason(simpletex_env, monkeypatch):
    install_post(monkeypatch, FakeResponse({
        "status": False,
        "message": "token 无效",
    }))

    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(PNG)

    assert "token 无效" in str(exc.value)
    # 错误信息里不能带出密钥本身
    assert "AT-test-token" not in str(exc.value)


def test_recognize_status_false_reads_err_info(simpletex_env, monkeypatch):
    """官方新接口把原因放在 err_info.err_msg。"""
    install_post(monkeypatch, FakeResponse({
        "status": False,
        "err_info": {"err_msg": "invalid credentials", "err_type": "req_unauthorized"},
    }))

    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(PNG)

    assert "鉴权失败" in str(exc.value)
    assert "SIMPLETEX_TOKEN" in str(exc.value)


def test_recognize_http_error(simpletex_env, monkeypatch):
    install_post(
        monkeypatch,
        FakeResponse({"message": "额度不足"}, status_code=402,
                     reason_phrase="Payment Required"),
    )

    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(PNG)

    assert "402" in str(exc.value)
    assert "额度不足" in str(exc.value)


def test_recognize_invalid_json(simpletex_env, monkeypatch):
    install_post(monkeypatch, FakeResponse(ValueError("bad json")))

    with pytest.raises(ocr.OcrError):
        ocr.recognize(PNG)


def test_recognize_timeout(simpletex_env, monkeypatch):
    def timeout(*args, **kwargs):
        raise httpx.TimeoutException("too slow")

    monkeypatch.setattr(ocr.httpx, "post", timeout)

    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(PNG)

    assert "超时" in str(exc.value)


def test_recognize_connection_error(simpletex_env, monkeypatch):
    def broken(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(ocr.httpx, "post", broken)

    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(PNG)

    assert "无法连接" in str(exc.value)


def test_recognize_empty_text(simpletex_env, monkeypatch):
    install_post(monkeypatch, FakeResponse({
        "status": True,
        "res": {"info": "   "},
    }))

    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(PNG)

    assert "没有识别到文字" in str(exc.value)


def test_recognize_rejects_invalid_mode(simpletex_env, monkeypatch):
    monkeypatch.setattr(config, "SIMPLETEX_MODE", "not_a_mode")
    install_post(monkeypatch, FakeResponse({"status": True, "res": {}}))

    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(PNG)

    assert "SIMPLETEX_MODE" in str(exc.value)


def test_is_configured(simpletex_env, monkeypatch):
    assert ocr.is_configured() is True

    monkeypatch.setattr(config, "SIMPLETEX_TOKEN", "")
    assert ocr.is_configured() is False

    monkeypatch.setattr(config, "SIMPLETEX_APP_ID", "app")
    assert ocr.is_configured() is False
    monkeypatch.setattr(config, "SIMPLETEX_APP_SECRET", "secret")
    assert ocr.is_configured() is True


# ---------- 本地 Ollama ----------

def test_ollama_recognize_sends_image(ollama_env, monkeypatch):
    calls = install_post(monkeypatch, FakeResponse({
        "response": "1. 求 $\\int_0^1 x^2\\,dx$",
        "done": True,
    }))

    result = ocr.recognize(PNG)

    assert result.text == "1. 求 $\\int_0^1 x^2\\,dx$"
    assert result.mode == "ollama:glm-ocr"
    assert result.confidence is None

    call = calls[0]
    assert call["url"] == "http://127.0.0.1:11434/api/generate"
    payload = call["json"]
    assert payload["model"] == "glm-ocr"
    assert payload["prompt"] == "Text Recognition:"
    assert payload["stream"] is False
    assert payload["options"]["num_predict"] == 8192
    # 不传 num_ctx 时整页图片会把 Ollama 默认上下文占满，输出被截断
    assert payload["options"]["num_ctx"] == 16384
    assert base64.b64decode(payload["images"][0]) == PNG


def test_ollama_custom_num_ctx(ollama_env, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_OCR_NUM_CTX", 32768)
    calls = install_post(monkeypatch, FakeResponse({"response": "题目"}))

    ocr.recognize(PNG)

    assert calls[0]["json"]["options"]["num_ctx"] == 32768


def _image_bytes(size, fmt="PNG", exif=None):
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format=fmt, exif=exif)
    return buffer.getvalue()


def _sent_image(calls):
    return Image.open(io.BytesIO(
        base64.b64decode(calls[0]["json"]["images"][0])
    ))


def test_ollama_upscales_small_crop(ollama_env, monkeypatch):
    """单题小图放大到 2000px 宽，避免小符号被模型压没。"""
    calls = install_post(monkeypatch, FakeResponse({"response": "题目"}))

    ocr.recognize(_image_bytes((800, 200)))

    sent = _sent_image(calls)
    assert sent.size == (2000, 500)


def test_ollama_keeps_whole_page_size(ollama_env, monkeypatch):
    """整页面积超过放大上限，不应再放大。"""
    data = _image_bytes((1654, 2339))
    calls = install_post(monkeypatch, FakeResponse({"response": "题目"}))

    ocr.recognize(data)

    assert base64.b64decode(calls[0]["json"]["images"][0]) == data


def test_ollama_applies_exif_orientation(ollama_env, monkeypatch):
    """手机竖拍照片依赖 EXIF 方向；模型不会自己转正。"""
    exif = Image.Exif()
    exif[0x0112] = 6  # 顺时针转 90° 才是正方向
    data = _image_bytes((2000, 1000), fmt="JPEG", exif=exif)
    calls = install_post(monkeypatch, FakeResponse({"response": "题目"}))

    ocr.recognize(data)

    assert _sent_image(calls).size == (1000, 2000)


def test_ollama_custom_prompt(ollama_env, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_OCR_PROMPT", "自定义提示")
    calls = install_post(monkeypatch, FakeResponse({"response": "题目"}))

    ocr.recognize(PNG)

    assert calls[0]["json"]["prompt"] == "自定义提示"


def test_ollama_cleans_code_fence(ollama_env, monkeypatch):
    install_post(monkeypatch, FakeResponse({
        "response": "```markdown\n1. 题目\n```",
    }))

    assert ocr.recognize(PNG).text == "1. 题目"


def test_ollama_missing_model(ollama_env, monkeypatch):
    install_post(monkeypatch, FakeResponse(
        {"error": "model 'glm-ocr' not found, try pulling it first"},
        status_code=404,
    ))

    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(PNG)

    assert "ollama pull glm-ocr" in str(exc.value)


def test_ollama_connection_error(ollama_env, monkeypatch):
    def broken(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(ocr.httpx, "post", broken)

    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(PNG)

    assert "ollama serve" in str(exc.value)


def test_ollama_timeout(ollama_env, monkeypatch):
    def timeout(*args, **kwargs):
        raise httpx.TimeoutException("too slow")

    monkeypatch.setattr(ocr.httpx, "post", timeout)

    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(PNG)

    assert "超时" in str(exc.value)


def test_ollama_empty_response(ollama_env, monkeypatch):
    install_post(monkeypatch, FakeResponse({"response": "  "}))

    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(PNG)

    assert "没有识别到文字" in str(exc.value)


def test_ollama_truncated_response(ollama_env, monkeypatch):
    install_post(monkeypatch, FakeResponse({
        "response": "1. 题目",
        "done_reason": "length",
    }))

    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(PNG)

    assert "截断" in str(exc.value)


def test_ollama_rejects_bmp(ollama_env, monkeypatch):
    def should_not_run(*args, **kwargs):
        raise AssertionError("BMP 应在本地校验阶段被拒绝")

    monkeypatch.setattr(ocr.httpx, "post", should_not_run)

    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(BMP)

    assert "PNG / JPG" in str(exc.value)


def test_ollama_is_configured(ollama_env):
    assert ocr.get_provider() == "ollama"
    assert ocr.is_configured() is True


def test_invalid_provider(monkeypatch):
    monkeypatch.setattr(config, "OCR_PROVIDER", "unknown")

    assert ocr.is_configured() is False
    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(PNG)

    assert "OCR_PROVIDER" in str(exc.value)


# ---------- DeepSeek 视觉 ----------

def test_deepseek_is_configured_and_available(deepseek_env, monkeypatch):
    assert ocr.get_provider() == "deepseek"
    assert ocr.is_configured() is True
    assert ocr.is_available() is True

    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    assert ocr.is_configured() is False
    assert ocr.is_available() is False


def test_deepseek_recognize_sends_image(deepseek_env, monkeypatch):
    client = install_deepseek(monkeypatch, content="  ```markdown\n题目 $x$\n```  ")

    result = ocr.recognize(PNG)

    assert result.text == "题目 $x$"
    assert result.mode == "deepseek:deepseek-flash"
    call = client.completions.calls[0]
    assert call["model"] == "deepseek-flash"
    assert call["messages"][0]["role"] == "system"
    user = call["messages"][1]
    assert user["content"][0]["type"] == "text"
    image = user["content"][1]
    assert image["type"] == "image_url"
    assert image["image_url"]["url"].startswith("data:image/png;base64,")


def test_deepseek_rejects_bmp(deepseek_env, monkeypatch):
    client = install_deepseek(monkeypatch)

    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(BMP)

    assert "PNG / JPG" in str(exc.value)
    assert client.completions.calls == []


class AuthenticationError(Exception):
    pass


class APIConnectionError(Exception):
    pass


def test_deepseek_authentication_error(deepseek_env, monkeypatch):
    install_deepseek(monkeypatch, exc=AuthenticationError("bad key"))

    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(PNG)

    assert "鉴权失败" in str(exc.value)
    assert "sk-test" not in str(exc.value)


def test_deepseek_connection_error(deepseek_env, monkeypatch):
    install_deepseek(monkeypatch, exc=APIConnectionError("offline"))

    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(PNG)

    assert "无法连接 DeepSeek API" in str(exc.value)


def test_deepseek_empty_response(deepseek_env, monkeypatch):
    install_deepseek(monkeypatch, content="   ")

    with pytest.raises(ocr.OcrError) as exc:
        ocr.recognize(PNG)

    assert "没有返回识别结果" in str(exc.value)


def test_ollama_is_available_probes_server(ollama_env, monkeypatch):
    monkeypatch.setattr(
        ocr.httpx,
        "get",
        lambda *args, **kwargs: FakeResponse({}, status_code=200),
    )
    assert ocr.is_available() is True

    def offline(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(ocr.httpx, "get", offline)
    assert ocr.is_available() is False
