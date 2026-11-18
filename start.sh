#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3，请先安装 Python 3.10+。"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "正在创建虚拟环境..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "正在安装依赖..."
pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "已创建 .env，请填写 OPENAI_API_KEY。"
  ${EDITOR:-nano} .env
fi

echo "启动 mathcoach：http://127.0.0.1:8000"
python -m web.app
