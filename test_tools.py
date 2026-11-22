"""tools.py 的自动化测试。

运行方式（在 mathcoach 目录下执行）：
    pytest -v
"""

import pytest

import config
from tools import TOOLS, run_python, run_tool, verify_with_sympy


# ---------- verify_with_sympy：等价（应返回 True）----------

@pytest.mark.parametrize(
    "expr_a, expr_b",
    [
        ("(x + 1)**2", "x**2 + 2*x + 1"),
        ("sin(x)**2 + cos(x)**2", "1"),
        ("(x**2 - 1)/(x - 1)", "x + 1"),
        ("2*x*sin(x) + x**2*cos(x)", "2x sin x + x^2 cos x"),
        ("(x + 1)**2", "x^2 + 2x + 1"),
        ("pi**2/6", "π²/6"),
        ("1/6", r"\frac16"),
    ],
)
def test_equivalent_expressions_return_true(expr_a, expr_b):
    """代数等价的表达式应返回 True。"""
    assert verify_with_sympy(expr_a, expr_b) is True


# ---------- verify_with_sympy：不等价（应返回 False）----------

@pytest.mark.parametrize(
    "expr_a, expr_b",
    [
        ("x**2", "2*x"),
        ("sin(x)", "x"),
    ],
)
def test_non_equivalent_expressions_return_false(expr_a, expr_b):
    """不等价的表达式应返回 False。"""
    assert verify_with_sympy(expr_a, expr_b) is False


# ---------- verify_with_sympy：化简不动但数值相等 ----------

def test_numerically_equal_integrals_return_true():
    """2024-F5 的真实卡点：两个积分都等于 1/3，但 simplify 化不归零。

    旧实现此时返回 False，模型会以为等式错了，反复重试同一个调用直到撞步数上限。
    """
    assert verify_with_sympy(
        "integrate(x*log(1+exp(x)),(x,-1,1))",
        "integrate(x**2/2,(x,-1,1))",
    ) is True
    assert verify_with_sympy(
        "integrate(x*log(1+exp(x)),(x,-1,1))",
        "1/3",
    ) is True


def test_numerically_different_integrals_return_false():
    """数值比较也要能明确判出不等价。"""
    assert verify_with_sympy(
        "integrate(exp(x**2),(x,0,1))",
        "integrate(exp(x**2),(x,0,2))",
    ) is False


def test_unsettled_identity_returns_explanation_not_false():
    """抽样一致但符号化简失败时，应返回“无法判定”文本，而不是 False。

    这样模型知道是工具算不动，不会把“没验出来”当成“等式错了”。
    """
    result = verify_with_sympy("sqrt(1-cos(x)**2)", "sin(x)")
    assert isinstance(result, str)
    assert result.startswith("无法判定")
    assert "不要重复" in TOOLS["verify_with_sympy"]["描述"]


# ---------- verify_with_sympy：非法输入（应返回 False，不抛异常）----------

@pytest.mark.parametrize(
    "expr_a, expr_b",
    [
        ("x**2 +", "x**2"),   # 语法不完整
        ("", "1"),            # 空字符串
        (None, "1"),          # 参数类型不对
    ],
)
def test_invalid_input_returns_false_without_raising(expr_a, expr_b):
    """非法输入不得抛异常，一律返回 False。"""
    assert verify_with_sympy(expr_a, expr_b) is False


# ---------- run_tool：正常分派 ----------

def test_run_tool_dispatches_to_function():
    """run_tool 应把参数转交给对应工具并返回其结果。"""
    result_ok = run_tool(
        "verify_with_sympy",
        {"expr_a": "(x + 1)**2", "expr_b": "x**2 + 2*x + 1"},
    )
    result_bad = run_tool(
        "verify_with_sympy",
        {"expr_a": "x", "expr_b": "x + 1"},
    )
    assert result_ok is True
    assert result_bad is False


# ---------- run_tool：错误分支（应返回错误文本，不抛异常）----------

def test_run_tool_unknown_name_returns_error_text():
    """工具名拼错时应返回错误文本，而不是抛异常。"""
    result = run_tool("verify_with_sympyy", {"expr_a": "x", "expr_b": "x"})
    assert isinstance(result, str)
    assert "不存在" in result
    assert "verify_with_sympy" in result


def test_run_tool_wrong_arg_name_returns_error_text():
    """参数名写错时应返回错误文本，而不是抛异常。"""
    result = run_tool("verify_with_sympy", {"wrong_arg": 1})
    assert isinstance(result, str)
    assert "参数不匹配" in result


def test_run_tool_args_not_dict_returns_error_text():
    """args 不是字典时应返回错误文本，而不是抛异常。"""
    result = run_tool("verify_with_sympy", "not a dict")
    assert isinstance(result, str)
    assert "args 必须是字典" in result


# ---------- 注册表本身 ----------

def test_tools_registry_contains_verify_with_sympy():
    """注册表里应登记 verify_with_sympy，且对应的是可调用对象。"""
    assert "verify_with_sympy" in TOOLS
    assert callable(TOOLS["verify_with_sympy"]["函数"])


# ---------- 表达式解析安全边界 ----------

def test_verify_rejects_code_execution_payload():
    """sympy 解析不能执行任意 Python；恶意输入只算验证失败。"""
    payload = "__import__('os').system('echo hacked')"
    assert verify_with_sympy(payload, "0") is False


def test_verify_still_supports_integrate():
    """限制命名空间后，常用的 integrate 仍必须可用。"""
    assert verify_with_sympy(
        "integrate(x, (x, 0, 1))",
        "1/2",
    ) is True


def test_run_tool_timeout_returns_error_text(monkeypatch):
    """工具执行超时不能拖死整个请求，应返回可读的错误文本。"""
    import time

    import tools

    monkeypatch.setitem(
        tools.ALL_TOOLS,
        "slow_test_tool",
        {"函数": lambda: time.sleep(0.5), "描述": "", "参数说明": {}},
    )

    result = tools.run_tool("slow_test_tool", {}, timeout=0.1)

    assert isinstance(result, str)
    assert "超时" in result
    assert "slow_test_tool" in result


# ---------- run_python：子进程执行与安全护栏 ----------

def test_run_python_prints_result():
    result = run_python("print(1 + 1)")

    assert "[stdout]" in result
    assert "2" in result
    assert "[退出码] 0" in result


def test_run_python_reports_exception():
    result = run_python("print(1 / 0)")

    assert "ZeroDivisionError" in result
    assert "退出码" in result


def test_run_python_timeout_kills_process():
    """死循环必须在超时后终止，而不是拖住整个请求。"""
    result = run_python("while True:\n    pass", timeout=0.8)

    assert "超时" in result
    assert "已终止" in result


def test_run_python_blocks_network_client_import():
    """requests 这类网络客户端直接禁止导入，给出明确原因。"""
    result = run_python("import requests\nprint('ok')")

    assert "沙箱禁用模块" in result
    assert "requests" in result


def test_run_python_blocks_network_connect():
    """socket 本身允许导入（科学计算库依赖），但真正建连必须失败。"""
    result = run_python(
        "import urllib.request\n"
        "urllib.request.urlopen('http://example.com')\n"
    )

    assert "PermissionError" in result
    assert "沙箱禁止" in result


def test_run_python_scrubs_api_keys(monkeypatch):
    """子进程只能看到白名单环境变量，密钥不会被打印出来。"""
    monkeypatch.setenv("MATHCOACH_SECRET_KEY", "should-not-leak")

    result = run_python(
        "import os\n"
        "print(os.environ.get('MATHCOACH_SECRET_KEY', 'missing'))\n"
    )

    assert "should-not-leak" not in result
    assert "missing" in result


def test_run_python_truncates_output():
    result = run_python("print('x' * 5000)", max_output=100)

    assert "输出已截断" in result
    assert len(result) < 1000


def test_run_python_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "RUN_PYTHON_ENABLED", False)

    result = run_python("print(1)")

    assert "未启用" in result


def test_run_python_is_registered_as_tool():
    assert "run_python" in TOOLS
    assert "code" in TOOLS["run_python"]["参数说明"]


def test_run_python_blocks_reading_project_files():
    """模型代码不能读项目里的 .env 等文件，避免密钥被打印出来。"""
    import pathlib

    target = pathlib.Path(".env").resolve()
    result = run_python(f"print(open({str(target)!r}).read())")

    assert "沙箱禁止访问该路径" in result


def test_run_python_allows_writes_in_workdir():
    """临时工作目录内允许创建文件，方便写中间结果。"""
    result = run_python(
        "open('out.txt', 'w').write('ok')\n"
        "print(open('out.txt').read())\n"
    )

    assert "ok" in result
    assert "[退出码] 0" in result
