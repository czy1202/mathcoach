"""检索用的文本归一化、分词与知识点识别。

和 mathparse.normalize_math_text 的区别：
- mathparse 的目标是让 sympy 能解析，会把分数改写成 ((a)/(b))；
- 这里的目标是“让 BM25 能匹配”，保留人类可读的写法，只做符号统一。

PDF 抽取出来的文本有两类噪声：
1. 常见数学符号被抽成别的字符：∫ -> Z、∑ -> X/P、√ -> p；
2. 2024/2025 的试卷用了 Symbol 字体，提取出来是私用区码位（\\uf0f2 等）。
两类都在 normalize 里统一成英文记号，索引和查询走同一套规则。
"""

from __future__ import annotations

import re

# 2024/2025 PDF 的 Symbol 字体码位。只映射确定见过的，其余私用区字符直接丢弃。
_GLYPH_MAP = {
    "\uf02a": "*",
    "\uf02b": "+",
    "\uf02d": "-",
    "\uf02f": "/",
    "\uf03d": "=",
    "\uf03e": ">",
    "\uf03c": "<",
    "\uf0a3": "<=",
    "\uf0b3": ">=",
    "\uf0b9": "!=",
    "\uf0ae": "->",
    "\uf0a5": "inf",
    "\uf0f2": "int",
    "\uf0ce": "in",
    "\uf070": "pi",
    "\uf072": "rho",
    "\uf071": "theta",
    "\uf066": "phi",
    "\uf06c": "lambda",
    "\uf0b1": "+-",
    # 大括号的上下碎片，没有语义，直接丢掉
    "\uf0e6": "",
    "\uf0e7": "",
    "\uf0e8": "",
    "\uf0f6": "",
    "\uf0f7": "",
    "\uf0f8": "",
}

# 人类写法与 LaTeX 写法统一成同一组英文记号
_SYMBOL_MAP = str.maketrans({
    "∫": " int ",
    "∬": " iint ",
    "∭": " iiint ",
    "∮": " oint ",
    "∑": " sum ",
    "∏": " prod ",
    "√": " sqrt ",
    "∞": " inf ",
    "→": " -> ",
    "⇒": " => ",
    "≤": " <= ",
    "≥": " >= ",
    "≠": " != ",
    "π": " pi ",
    "±": " +- ",
    "∂": " partial ",
    "′": "'",
    "’": "'",
    "−": "-",
    "–": "-",
    "—": "-",
    "×": "*",
    "÷": "/",
    "·": "*",
    "（": "(",
    "）": ")",
    "，": ",",
    "。": ".",
    "；": ";",
    "：": ":",
    "【": "[",
    "】": "]",
    "％": "%",
})

# PDF 里常见的抽取替换：只在文档侧使用，查询侧不动，避免把变量 z 当积分号
_DOC_ARTIFACTS = (
    (re.compile(r"\bZ\s*Z\s*Z\b"), " iiint "),
    (re.compile(r"\bZ\s*Z\b"), " iint "),
    (re.compile(r"\bZ\b"), " int "),
    (re.compile(r"\bX\b"), " sum "),
    (re.compile(r"\bP\b"), " sum "),
)

_LATEX_KEEP = (
    "lim", "sin", "cos", "tan", "cot", "sec", "csc",
    "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh",
    "ln", "log", "exp", "max", "min", "sup", "inf",
    "int", "iint", "iiint", "oint", "sum", "prod", "sqrt", "partial",
)

# 同一语义的 LaTeX 写法统一成一个记号，避免查询写 \infty、文档写 ∞ 时匹配不上
_LATEX_ALIASES = {
    "infty": " inf ",
    "to": " -> ",
    "rightarrow": " -> ",
    "Rightarrow": " => ",
    "leq": " <= ",
    "le": " <= ",
    "geq": " >= ",
    "ge": " >= ",
    "neq": " != ",
    "cdot": " * ",
    "times": " * ",
    "div": " / ",
    "pm": " +- ",
}
_LATEX_ALIAS_RE = re.compile(
    r"\\(" + "|".join(sorted(_LATEX_ALIASES, key=len, reverse=True)) + r")(?![A-Za-z])"
)

# 用 (?![A-Za-z]) 而不是 \b：\int_0^1 里 int 后面是下划线，
# \b 不成立，会导致整个积分号被后面的 _LATEX_ANY_RE 删掉。
_LATEX_KEEP_RE = re.compile(
    r"\\(?:" + "|".join(sorted(_LATEX_KEEP, key=len, reverse=True)) + r")(?![A-Za-z])"
)
_LATEX_DROP_RE = re.compile(r"\\(?:frac|dfrac|tfrac|left|right|displaystyle|textstyle)\b")
_LATEX_ANY_RE = re.compile(r"\\[A-Za-z]+")

_PRIVATE_USE_RE = re.compile(r"[\ue000-\uf8ff]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_RE = re.compile(r"[a-z]+|[0-9]+(?:\.[0-9]+)?")

# 知识点受控词表。命中即打标签，一个问题可以有多个标签。
# 这些词同时用于查询侧，用来给检索结果做轻量加权。
TAG_PATTERNS: dict[str, tuple[str, ...]] = {
    "极限": ("极限", "lim", "无穷小", "无穷大", "等价无穷", "夹逼", "单调有界"),
    # 只收“间断点/连续性”这类真正考连续性的题；“连续函数”多半只是题设条件
    "连续": ("间断点", "可去间断", "跳跃间断", "连续性"),
    "导数": ("导数", "求导", "dy/dx", "y'", "高阶导"),
    "微分": ("微分", "全微分"),
    # 裸的“拉格朗日”也会命中拉格朗日乘数法，必须限定为中值定理
    "中值定理": ("中值定理", "罗尔", "柯西中值"),
    "泰勒展开": ("泰勒", "taylor", "麦克劳林"),
    "不定积分": ("不定积分", "原函数"),
    "定积分": ("定积分", "积分上限", "积分下限"),
    # dxdy 在曲面积分里也会出现，这里只认明确写“二重积分”的题
    "二重积分": ("二重积分",),
    "三重积分": ("三重积分", "dxdydz"),
    "曲线积分": ("曲线积分", "格林公式", "第一型曲线", "第二型曲线", "oint"),
    "曲面积分": ("曲面积分", "高斯公式", "斯托克斯", "第一型曲面", "第二型曲面", "dydz", "dzdx"),
    "级数": ("级数", "幂级数", "傅里叶级数", "收敛半径", "收敛域"),
    "数列": ("数列", "递推", "a1", "an+1"),
    "微分方程": ("微分方程", "通解", "特解", "初始条件", "dy dx", "dy/dx"),
    "多元微分": ("偏导", "方向导数", "梯度", "多元函数"),
    "空间解析几何": ("切平面", "法平面", "法线", "柱面", "锥面", "旋转曲面"),
    "几何应用": ("面积", "体积", "弧长", "切线", "曲率", "形心", "表面积"),
    "极值最值": ("极值", "最值", "最大值", "最小值", "驻点"),
    "渐近线": ("渐近线",),
    "拐点": ("拐点", "凹凸"),
    "旋转体": ("旋转体", "旋转一周"),
    # “证明”太宽，几乎所有证明题都会命中；只保留真正的不等式信号
    "不等式": ("不等式", "<=", ">="),
    "线性代数": ("矩阵", "行列式", "特征值", "特征向量", "二次型", "正定", "秩"),
    "概率论": ("概率", "期望", "方差", "分布", "随机变量"),
}


def _strip_latex(s: str) -> str:
    """去掉 LaTeX 外壳，保留可读内容。

    \\lim_{x\\to 0}\\frac{a}{b} -> lim x -> 0 a b
    """
    # LaTeX 的细间距命令（\, \; \!）不是数学内容，直接换成空格
    for spacing in ("\\,", "\\;", "\\!", "\\ "):
        s = s.replace(spacing, " ")

    s = _LATEX_ALIAS_RE.sub(lambda m: _LATEX_ALIASES[m.group(1)], s)
    s = _LATEX_KEEP_RE.sub(lambda m: f" {m.group(0)[1:]} ", s)
    s = _LATEX_DROP_RE.sub(" ", s)
    s = _LATEX_ANY_RE.sub(" ", s)
    s = s.replace("{", " ").replace("}", " ").replace("$", " ")
    return s


def normalize_for_search(text: object, *, doc_side: bool = False) -> str:
    """把题目文本归一化成适合检索的一行字符串。

    Args:
        text: 原始文本
        doc_side: 是否按 PDF 抽取结果处理（会额外把 Z/X/P 还原成积分号和求和号）
    """
    s = str(text or "")

    for src, dst in _GLYPH_MAP.items():
        s = s.replace(src, dst)

    s = s.translate(_SYMBOL_MAP)
    s = _PRIVATE_USE_RE.sub(" ", s)

    if doc_side:
        for pattern, replacement in _DOC_ARTIFACTS:
            s = pattern.sub(replacement, s)

    s = _strip_latex(s)
    s = s.lower()

    # 只保留有检索意义的字符：中文、英文、数字、常见数学符号
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff<>!=+\-*/^().,'\[\]% ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def tokenize(text: object, *, doc_side: bool = False) -> list[str]:
    """中英混合分词。

    中文用字符二元组（bigram）：不依赖词典，'二重积分' -> 二重/重积/积分，
    对数学术语和错别字都更稳。英文和数字按连续串切分。
    """
    s = normalize_for_search(text, doc_side=doc_side)
    tokens = [m.group(0) for m in _ASCII_RE.finditer(s)]
    for match in _CJK_RE.finditer(s):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def _matches_keyword(key: str, text: str) -> bool:
    """判断关键词是否命中。

    中文直接做子串匹配。含字母的英文记号要检查词边界，否则
    “point” 会命中曲线积分的 `oint`，“slim” 会命中 `lim`。
    有边界的检查只看字母，不看数字：`dxdy1` 这类写法仍应算命中。
    """
    if key.isascii() and any(ch.isalpha() for ch in key):
        return re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", text) is not None
    return key in text


def detect_tags(text: object) -> list[str]:
    """按受控词表识别知识点。

    关键词之外，补充一层积分类型推断：PDF 里 ∫∫ 常被抽成 ZZ，
    normalize 后会变成 iint；这类信号比“二重积分”四个字更常见。
    """
    s = normalize_for_search(text, doc_side=True)
    tags: list[str] = []
    for tag, patterns in TAG_PATTERNS.items():
        for pattern in patterns:
            key = normalize_for_search(pattern, doc_side=True)
            if key and _matches_keyword(key, s):
                tags.append(tag)
                break

    # 积分类型：按符号判断，避免只认“二重积分”四个字
    has_double = "iint" in s or "dxdy" in s
    has_triple = "iiint" in s or "dxdydz" in s
    has_surface = "dydz" in s or "dzdx" in s or "曲面积分" in s
    if has_triple:
        tags.append("三重积分")
    elif has_surface and "dxdy" in s:
        tags.append("曲面积分")
    elif has_double:
        tags.append("二重积分")
    elif re.search(r"\bint\b", s):
        tags.append("定积分")

    # 去重并保持稳定顺序
    return list(dict.fromkeys(tags))
