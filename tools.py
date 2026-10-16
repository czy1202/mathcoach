"""工具层：模型可以调用的函数。

本文件只包含工具函数、工具注册表和分派函数，不含 Agent 主循环。

安全提示：sympy.sympify 内部使用 eval，理论上可以执行传入字符串里的代码。
v1 阶段输入来自本地自测，暂时接受这个风险；后期接入真实用户输入前，
应改成受限命名空间的解析方式，或先做输入白名单校验。
"""

from typing import Any

import sympy as sp


def verify_with_sympy(expr_a: Any, expr_b: Any) -> bool:
    """判断两个数学表达式是否等价。

    原理：把两个表达式相减后化简，结果为 0 即认为等价。

    Args:
        expr_a: 第一个表达式，字符串，例如 "sin(x)**2 + cos(x)**2"
        expr_b: 第二个表达式，字符串，例如 "1"

    Returns:
        bool: 等价返回 True；不等价、表达式非法、参数错误一律返回 False。

    注意：
        1. 解析失败与"不等价"都返回 False，调用方无法区分二者。
           需要区分时，应改用返回 CheckResult 的版本（后续步骤再做）。
        2. 符号化简不检查定义域。例如 (x**2-1)/(x-1) 与 x+1 会被判为等价，
           但前者在 x=1 处无定义。
        3. simplify 是启发式算法，极复杂的表达式可能化简不到 0。
    """
    try:
        a = sp.sympify(expr_a)
        b = sp.sympify(expr_b)
        return bool(sp.simplify(a - b) == 0)
    except Exception:
        # SympifyError / TypeError / ValueError 等一律归为本次验证失败
        return False


TOOLS: dict[str, dict] = {
    "verify_with_sympy": {
        "函数": verify_with_sympy,
        "描述": "判断两个数学表达式是否等价。等价返回 True，否则返回 False。",
        "参数说明": {
            "expr_a": "第一个表达式，字符串，例如 'sin(x)**2 + cos(x)**2'",
            "expr_b": "第二个表达式，字符串，例如 '1'",
        },
    },
}


def run_tool(name: str, args: dict) -> Any:
    """按名字调用工具。

    Args:
        name: 工具名，必须是 TOOLS 中的键
        args: 参数字典，会以关键字参数形式传给工具函数

    Returns:
        成功时返回工具函数的原始返回值；工具不存在或参数不对时，
        返回一段错误说明字符串。本函数不抛异常，错误一律以文本返回，
        便于上层把错误信息交回模型自行纠正。
    """
    tool = TOOLS.get(name)
    if tool is None:
        available = "、".join(TOOLS.keys())
        return f"错误：不存在名为 {name} 的工具。可用工具：{available}"

    if not isinstance(args, dict):
        return f"错误：args 必须是字典，收到的是 {type(args).__name__}"

    try:
        return tool["函数"](**args)
    except TypeError as e:
        return f"错误：参数不匹配（{e}）"
    except Exception as e:
        return f"错误：工具执行失败（{type(e).__name__}: {e}）"
