"""图片识题：把题目截图转成 Markdown + LaTeX 文本。

支持两个提供方，由 OCR_PROVIDER 选择：

    ollama（默认）
        调用本机 Ollama 的视觉模型（默认 glm-ocr，0.9B，约 2.7 GB）。
        完全免费、图片不出本机；需要先安装 Ollama 并 pull 模型。
    simpletex
        第三方云端 OCR，需要 token 或 APP ID/SECRET；图片会离开本机，
        并按账户额度计费。

设计边界：
1. 只做“图片 → 文本”，不自动解题、不自动提交；识别结果由用户核对后
   再进入正常的 Agent 解题流程；
2. 服务端不主动保存图片、不写 trace；上传解析可能使用框架的临时文件，
   请求结束即释放；
3. 密钥只从 .env 读取，错误信息里不会带出 token / secret；
4. SimpleTex 默认用 simpletex_ocr（整页图文混排），也可以改成 latex_ocr
   只识公式。

Ollama 接口约定：
    POST http://127.0.0.1:11434/api/generate
    JSON: {"model": "glm-ocr", "prompt": "Text Recognition:",
           "images": ["<base64>"], "stream": false}
    返回: {"response": "<Markdown>"}

SimpleTex 接口约定（官方开放平台）：
    POST https://server.simpletex.cn/api/{mode}
    multipart 字段名：file
    整页识别额外参数：rec_mode=document，并用
        inline_formula_wrapper / isolated_formula_wrapper 指定 $ / $$ 分隔符，
        这样前端可以直接按现有 KaTeX 管线渲染。
    返回：{"status": true, "res": {"content" 或 "latex": "..."}, ...}

鉴权二选一：
    token 头              —— 用户中心生成的授权令牌，本地自用最省事；
    app-id/random-str/
    timestamp/sign        —— 开放平台应用签名，sign = md5(...&secret=...)；
                             签名要把表单参数一起按 key 排序后计算。
"""

from __future__ import annotations

import base64
import hashlib
import io
import secrets
import string
import time
from dataclasses import dataclass

import httpx
from PIL import Image, ImageOps

import config


MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB，足够一张手机照片
REQUEST_TIMEOUT = 60.0

# GLM-OCR 的整页识别提示。模型按任务前缀训练，改写成自然语言可能降低
# 稳定性；需要换模型时通过 OLLAMA_OCR_PROMPT 覆盖。
DEFAULT_OLLAMA_PROMPT = "Text Recognition:"

# 小图放大。模型内部会把图片缩到固定的视觉分辨率，整页大图再放大没有
# 收益；但单题裁剪图（约 1300px 宽）里的小符号会认错，实测 2025 年第 1
# 道选择题的 e^{∛x} 被读成 e^{√x}：清晰图 1700px 宽即可恢复，手机退化图
# 要到 1800px，继续放大到 3000px 反而又认错。统一取 2000px，给退化留余量。
# 面积上限用来排除整页截图，避免白白增加计算量。
_UPSCALE_WIDTH = 2000
_UPSCALE_MAX_AREA = 1_500_000
_UPSCALE_MAX_FACTOR = 2.5

# SimpleTex 文档写的是 jpg / png / bmp，不支持的格式宁可本地拒绝，
# 也不要上传后换来一个含糊的远端错误。
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"BM", "bmp", "image/bmp"),
)

_ALLOWED_MODES = {"simpletex_ocr", "latex_ocr", "latex_ocr_turbo"}

# 不同模式优先从哪个字段取文本。文档模式返回 res.content（新版）或
# res.info（旧版），公式模式返回 res.latex；其余字段是版本变化的兜底。
_MODE_FIELDS = {
    # 新版文档模式返回 res.content；旧版 / .cn 返回 res.info，两者都兼容
    "simpletex_ocr": ("content", "info", "latex", "text", "markdown"),
    "latex_ocr": ("latex", "info", "text"),
    "latex_ocr_turbo": ("latex", "info", "text"),
}


class OcrError(Exception):
    """可以直接展示给用户的识别失败原因。"""


@dataclass
class OcrResult:
    """一次成功的整页识别。"""

    text: str
    mode: str
    confidence: float | None = None


def get_provider() -> str:
    """当前图片识别提供方；未知值原样返回，由 recognize 报配置错误。"""
    return config.OCR_PROVIDER.strip().lower() or "ollama"


def is_configured() -> bool:
    """当前提供方是否具备可用配置。

    Ollama 是本地服务，模型是否真的下载好无法在不发请求的情况下判断，
    所以这里只检查提供方名字；缺模型时由 recognize 给出 pull 提示。
    """
    provider = get_provider()
    if provider == "ollama":
        return True
    if provider == "simpletex":
        return bool(config.SIMPLETEX_TOKEN.strip()) or bool(
            config.SIMPLETEX_APP_ID.strip() and config.SIMPLETEX_APP_SECRET.strip()
        )
    return False


def auth_headers(
    *,
    token: str,
    app_id: str,
    app_secret: str,
    params: dict[str, str] | None = None,
    timestamp: str | None = None,
    random_str: str | None = None,
) -> dict[str, str]:
    """构造鉴权头。token 优先；否则用 app id + secret 做 MD5 签名。

    官方签名规则会把表单参数和三个鉴权字段一起按 key 排序，再追加 secret。
    params 传将要随 multipart 发送的文本字段（不含 file）。

    timestamp / random_str 允许调用方传入，测试里据此得到可复现的签名。
    """
    token = token.strip()
    if token:
        return {"token": token}

    app_id = app_id.strip()
    app_secret = app_secret.strip()
    if not app_id or not app_secret:
        raise OcrError(
            "服务端未配置 SimpleTex：请设置 SIMPLETEX_TOKEN，"
            "或同时设置 SIMPLETEX_APP_ID 与 SIMPLETEX_APP_SECRET。"
        )

    timestamp = timestamp or str(int(time.time()))
    random_str = random_str or "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(16)
    )
    fields = {
        "app-id": app_id,
        "random-str": random_str,
        "timestamp": timestamp,
    }
    for key, value in (params or {}).items():
        if key != "file":
            fields[str(key)] = str(value)

    raw = "&".join(f"{key}={fields[key]}" for key in sorted(fields))
    raw += f"&secret={app_secret}"
    return {
        "app-id": app_id,
        "random-str": random_str,
        "timestamp": timestamp,
        "sign": hashlib.md5(raw.encode("utf-8")).hexdigest(),
    }


def detect_image_type(data: bytes) -> tuple[str, str] | None:
    """按文件头识别图片格式，返回 (扩展名, MIME)；不认识返回 None。"""
    for signature, extension, mime in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return extension, mime
    return None


def validate_image(
    data: bytes,
    allowed: set[str] | None = None,
) -> tuple[str, str]:
    """检查大小和格式，返回 (扩展名, MIME)。

    Args:
        allowed: 允许的扩展名集合；None 表示 PNG / JPG / BMP 都行。
    """
    if not data:
        raise OcrError("图片内容为空，请重新选择。")
    if len(data) > MAX_IMAGE_BYTES:
        limit_mb = MAX_IMAGE_BYTES // (1024 * 1024)
        raise OcrError(f"图片超过 {limit_mb} MB，请压缩或裁剪后再上传。")

    detected = detect_image_type(data)
    if detected is None:
        raise OcrError("只支持 PNG / JPG / BMP 图片，请换一种格式。")
    if allowed is not None and detected[0] not in allowed:
        # 固定按 PNG / JPG / BMP 的顺序展示，避免字母序把提示写得别扭
        names = " / ".join(
            name.upper() for name in ("png", "jpg", "bmp") if name in allowed
        )
        raise OcrError(f"本地识别只支持 {names} 图片，请换一种格式。")
    return detected


def _find_text(value: object) -> str:
    """从 res 的任意嵌套结构里找出第一段非空文本。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_find_text(item) for item in value]
        return "\n\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("content", "info", "latex", "text", "markdown"):
            text = _find_text(value.get(key))
            if text:
                return text
        for item in value.values():
            text = _find_text(item)
            if text:
                return text
    return ""


def _error_message(payload: dict) -> str:
    """从失败响应里取一句可展示的原因，限制长度。"""
    for source in (payload, payload.get("err_info")):
        if not isinstance(source, dict):
            continue
        for key in ("err_msg", "message", "msg", "error", "detail", "reason"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
        nested = _find_text(source)
        if nested:
            return nested[:200]
    res = payload.get("res")
    nested = _find_text(res)
    return nested[:200]


def _friendly_detail(detail: str) -> str:
    """把最常见的英文鉴权错误翻成可操作的中文提示。"""
    lowered = detail.lower()
    if "invalid credentials" in lowered or "unauthorized" in lowered:
        return (
            "鉴权失败：请检查 SIMPLETEX_TOKEN（或 APP ID / SECRET）"
            "是否有效、是否已过期。"
        )
    return detail


def _confidence(res: object) -> float | None:
    if not isinstance(res, dict):
        return None
    for key in ("conf", "confidence", "score"):
        value = res.get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _clean_ocr_text(text: str) -> str:
    """去掉模型偶尔包在外面的 Markdown 代码围栏。"""
    cleaned = text.strip()
    if not cleaned.startswith("```"):
        return cleaned

    lines = cleaned.splitlines()
    if len(lines) < 2 or not lines[-1].strip().startswith("```"):
        return cleaned

    body = lines[1:-1]
    return "\n".join(body).strip()


def _prepare_image(image: bytes, extension: str) -> bytes:
    """按 EXIF 摆正方向，并把小图放大到约 2000px 宽。

    浏览器展示图片时会按 EXIF 自动转正，但 llama.cpp 不会；竖着拍的手机
    照片如果原样发送，模型看到的是横躺的整页。小图放大则是对策：模型内部
    的视觉分辨率是固定的，单题小图里 ∛ 这类细符号容易被压没。

    只在确实需要时重新编码，避免把一张大照片转成几十 MB 的 PNG。
    """
    try:
        with Image.open(io.BytesIO(image)) as source:
            orientation = source.getexif().get(0x0112, 1)
            img = ImageOps.exif_transpose(source)
            rotated = orientation != 1

            factor = 1.0
            width, height = img.size
            if width < _UPSCALE_WIDTH and width * height <= _UPSCALE_MAX_AREA:
                factor = min(_UPSCALE_MAX_FACTOR, _UPSCALE_WIDTH / width)
            if factor >= 1.05:
                img = img.resize(
                    (round(width * factor), round(height * factor)),
                    Image.Resampling.LANCZOS,
                )
            elif not rotated:
                return image

            buffer = io.BytesIO()
            if extension == "jpg":
                # 手机照片本身有损，再存 PNG 只会成倍变大
                img.convert("RGB").save(buffer, format="JPEG", quality=92)
            else:
                img.save(buffer, format="PNG")
            return buffer.getvalue()
    except Exception:
        # 图片能通过文件头校验但 Pillow 解不开时，交给模型自己判断
        return image


def _recognize_ollama(image: bytes) -> OcrResult:
    """调用本机 Ollama 的视觉模型做整页识别。"""
    # llama.cpp 的图片解码对 BMP 支持不稳定，本地路线只收 PNG / JPG；
    # 手机照片和截图几乎都是这两种。
    extension, _ = validate_image(image, allowed={"png", "jpg"})
    image = _prepare_image(image, extension)

    model = config.OLLAMA_OCR_MODEL.strip() or "glm-ocr"
    prompt = config.OLLAMA_OCR_PROMPT.strip() or DEFAULT_OLLAMA_PROMPT
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [base64.b64encode(image).decode("ascii")],
        "stream": False,
        "options": {
            "temperature": 0,
            # 不设 num_ctx 时 Ollama 用很小的默认上下文，整页图片会把窗口
            # 占满，输出到第一两题就因 done_reason=length 被截断。
            "num_ctx": config.OLLAMA_OCR_NUM_CTX,
            # GLM-OCR 默认输出上限偏小，整页多题时容易被截断
            "num_predict": 8192,
        },
    }
    url = f"{config.OLLAMA_HOST.rstrip('/')}/api/generate"

    try:
        response = httpx.post(
            url,
            json=payload,
            timeout=config.OLLAMA_OCR_TIMEOUT,
        )
    except httpx.ConnectError as exc:
        raise OcrError(
            f"无法连接本地 Ollama（{config.OLLAMA_HOST}）。"
            "请先启动 Ollama 应用，或在终端运行 ollama serve。"
        ) from exc
    except httpx.TimeoutException as exc:
        raise OcrError(
            "本地识别超时。首次加载模型会比较慢，请稍后重试；"
            "也可以裁剪成单页或换一张更小的图片。"
        ) from exc
    except httpx.HTTPError as exc:
        raise OcrError(f"调用本地 Ollama 失败：{type(exc).__name__}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise OcrError("本地识别返回了无法解析的内容。") from exc
    if not isinstance(data, dict):
        raise OcrError("本地识别返回格式异常。")

    error = str(data.get("error") or "").strip()
    if response.status_code == 404 or "not found" in error.lower():
        raise OcrError(
            f"本地模型 {model} 还没下载：请在终端运行 ollama pull {model}，"
            "下载完成后再试。"
        )
    if response.status_code >= 400 or error:
        detail = error or response.reason_phrase
        raise OcrError(f"本地识别失败：{detail}")

    text = _clean_ocr_text(str(data.get("response") or ""))
    if not text:
        raise OcrError("没有识别到文字，请换一张更清晰、方向端正的图片。")
    if data.get("done_reason") == "length":
        raise OcrError(
            "图片内容太多或分辨率过高，识别结果被截断。"
            "请裁成单题/单页，或调大 OLLAMA_OCR_NUM_CTX 后重试。"
        )

    return OcrResult(text=text, mode=f"ollama:{model}")


def _recognize_simpletex(image: bytes) -> OcrResult:
    """调用 SimpleTex 云端 OCR（第三方，需要鉴权）。"""
    extension, mime = validate_image(image)

    mode = config.SIMPLETEX_MODE.strip() or "simpletex_ocr"
    if mode not in _ALLOWED_MODES:
        raise OcrError(
            "SIMPLETEX_MODE 配置无效，可选 simpletex_ocr / latex_ocr / "
            "latex_ocr_turbo。"
        )

    # 整页识别显式要求文档模式，并指定 $ / $$ 分隔符，避免返回一大段
    # 没有定界符的 LaTeX，前端无法渲染，用户也无从核对。
    data: dict[str, str] = {}
    if mode == "simpletex_ocr":
        data = {
            "rec_mode": "document",
            "inline_formula_wrapper": '["$","$"]',
            "isolated_formula_wrapper": '["$$","$$"]',
        }

    headers = auth_headers(
        token=config.SIMPLETEX_TOKEN,
        app_id=config.SIMPLETEX_APP_ID,
        app_secret=config.SIMPLETEX_APP_SECRET,
        params=data,
    )
    url = f"{config.SIMPLETEX_BASE_URL.rstrip('/')}/{mode}"

    try:
        response = httpx.post(
            url,
            headers=headers,
            data=data,
            files={"file": (f"problem.{extension}", image, mime)},
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        raise OcrError("图片识别超时，请稍后重试或换一张更小的图片。") from exc
    except httpx.HTTPError as exc:
        raise OcrError("无法连接图片识别服务，请检查网络后重试。") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise OcrError("图片识别服务返回了无法解析的内容。") from exc

    if not isinstance(payload, dict):
        raise OcrError("图片识别服务返回格式异常。")
    if response.status_code >= 400:
        detail = _friendly_detail(
            _error_message(payload) or response.reason_phrase
        )
        raise OcrError(f"图片识别服务返回 HTTP {response.status_code}：{detail}")
    if payload.get("status") is False:
        detail = _friendly_detail(_error_message(payload) or "未知原因")
        raise OcrError(f"图片识别失败：{detail}")

    res = payload.get("res")
    text = ""
    if isinstance(res, str):
        text = res.strip()
    elif isinstance(res, dict):
        for key in _MODE_FIELDS[mode]:
            text = _find_text(res.get(key))
            if text:
                break
        if not text:
            text = _find_text(res)
    elif isinstance(res, list):
        text = _find_text(res)

    if not text:
        raise OcrError("没有识别到文字，请换一张更清晰、方向端正的图片。")

    return OcrResult(text=text, mode=mode, confidence=_confidence(res))


def recognize(image: bytes) -> OcrResult:
    """把一张题目图片识别成 Markdown + LaTeX 文本。

    Args:
        image: 图片原始字节。浏览器给的文件名和 MIME 都不可信，这里按
               文件头判断格式。

    Raises:
        OcrError: 配置无效、格式不支持、模型/服务失败或返回空文本。
    """
    provider = get_provider()
    if provider == "ollama":
        return _recognize_ollama(image)
    if provider == "simpletex":
        return _recognize_simpletex(image)
    raise OcrError(
        f"OCR_PROVIDER 配置无效（{config.OCR_PROVIDER!r}），"
        "可选 ollama / simpletex。"
    )
