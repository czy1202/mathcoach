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
