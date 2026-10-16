"""数据结构定义。

本文件只定义"数据长什么样"，不含任何业务逻辑。
"""

from dataclasses import dataclass, field
from enum import Enum


class Verdict(Enum):
    """解题结论的三种状态。"""

    PASS = "pass"
    FAIL = "fail"
    UNCERTAIN = "uncertain"


@dataclass
class Problem:
    """一道待求解的题目。"""

    statement: str          # 题干
    topic: str = ""         # 知识点，例如 "极限"
    difficulty: str = ""    # 难度，例如 "中"
    source: str = ""        # 来源，例如 "第16届初赛"


@dataclass
class Solution:
    """一次求解的产出。"""

    steps: list[str] = field(default_factory=list)        # 解题步骤，按顺序
    final_answer: str = ""                                # 最终答案
    tools_used: list[str] = field(default_factory=list)   # 本次用到的工具名


@dataclass
class CheckResult:
    """一次答案验证的结果。"""

    passed: bool = False    # 是否通过验证
    method: str = ""        # 验证方式：symbolic / numeric / exact
    evidence: str = ""      # 验证依据，例如 "simplify(left - right) == 0"
