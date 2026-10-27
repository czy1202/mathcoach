"""ask.py 命令行入口的自动化测试。

运行方式（在 mathcoach 目录下执行）：
    python -m pytest test_ask.py -v
"""

import io
import sys

import config
from ask import main, parse_args


def test_parse_args_question_and_flags():
    """题目文本与开关应被正确解析。"""
    args = parse_args(["计算 1+1", "--stub", "--no-trace"])

    assert args.question == ["计算 1+1"]
    assert args.stub is True
    assert args.no_trace is True


def test_stub_single_question(capsys):
    """--stub 应离线跑完一次问答并打印最终答案。"""
    assert main(["--stub", "--no-trace", "验证恒等式"]) == 0

    out = capsys.readouterr().out
    assert "最终答案：1" in out
    assert "离线假模型" in out


def test_missing_api_key_returns_hint(monkeypatch, capsys):
    """缺少密钥时应提示配置方式，而不是抛异常。"""
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")

    assert main(["--no-trace", "1+1"]) == 1

    out = capsys.readouterr().out
    assert "OPENAI_API_KEY" in out
    assert "--stub" in out


def test_piped_stdin_is_one_question(monkeypatch, capsys):
    """从管道输入时应把 stdin 整体当成一道题。"""
    monkeypatch.setattr(sys, "stdin", io.StringIO("验证恒等式\n"))

    assert main(["--stub", "--no-trace"]) == 0

    out = capsys.readouterr().out
    assert "最终答案：1" in out
