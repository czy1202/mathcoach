"""配置读取。

从项目根目录的 .env 读取 OpenAI 兼容接口配置，集中对外暴露。

设计要点：
1. .env 缺失、变量未填时，对应值为空字符串，不抛异常。
   这样没有真实密钥也能用 StubClient 离线跑通后续步骤。
2. 真实密钥只放在被 .gitignore 忽略的 .env 里，仓库中只提交 .env.example。
"""

import os

from dotenv import load_dotenv

# 文件不存在时 load_dotenv 静默跳过，不会报错
load_dotenv()

OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "")
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "")

# 默认不传 temperature，沿用接口默认值。
# 实测把温度设为 0 会让模型反复重试同一个失败的 sympy 验证，反而撞步数上限。
_temperature = os.getenv("OPENAI_TEMPERATURE", "").strip()
OPENAI_TEMPERATURE: float | None = float(_temperature) if _temperature else None

# 图片识别提供方：
#   ollama    —— 本地开源模型，免费、图片不出本机（默认）
#   simpletex —— 第三方云端 OCR，需要 token 或 APP ID/SECRET
OCR_PROVIDER: str = os.getenv("OCR_PROVIDER", "ollama").strip().lower() or "ollama"

# 本地识别：Ollama 已安装并启动后，先 ollama pull glm-ocr（约 2.7 GB）。
OLLAMA_HOST: str = (
    os.getenv("OLLAMA_HOST", "").strip() or "http://127.0.0.1:11434"
).rstrip("/")
OLLAMA_OCR_MODEL: str = (
    os.getenv("OLLAMA_OCR_MODEL", "").strip() or "glm-ocr"
)
# 留空用 GLM-OCR 的整页识别提示；换成 Qwen3-VL 等通用模型时可自定义。
OLLAMA_OCR_PROMPT: str = os.getenv("OLLAMA_OCR_PROMPT", "").strip()
_ollama_timeout = os.getenv("OLLAMA_OCR_TIMEOUT", "").strip()
OLLAMA_OCR_TIMEOUT: float = float(_ollama_timeout) if _ollama_timeout else 240.0

# 图片识别（SimpleTex）。两种鉴权方式任选其一：
# 1. SIMPLETEX_TOKEN：用户中心生成的授权令牌（AT 开头），本地自用最省事；
# 2. SIMPLETEX_APP_ID + SIMPLETEX_APP_SECRET：开放平台应用，服务端签名。
# 都为空时网页仍可用，只是“拍照识题”按钮会提示未配置。
SIMPLETEX_TOKEN: str = os.getenv("SIMPLETEX_TOKEN", "")
SIMPLETEX_APP_ID: str = os.getenv("SIMPLETEX_APP_ID", "")
SIMPLETEX_APP_SECRET: str = os.getenv("SIMPLETEX_APP_SECRET", "")

# 默认整页识别（中文题干 + 公式混排）。latex_ocr 更擅长纯公式，
# 但不会把题号、中文说明一起识别出来。
SIMPLETEX_MODE: str = os.getenv("SIMPLETEX_MODE", "simpletex_ocr").strip() or "simpletex_ocr"

# 只给测试或自建代理用，正常不需要改。
SIMPLETEX_BASE_URL: str = (
    os.getenv("SIMPLETEX_BASE_URL", "").strip()
    or "https://server.simpletex.cn/api"
).rstrip("/")
