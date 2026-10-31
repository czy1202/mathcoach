"""从 CMC 非数学类历年试卷 PDF 中抽取题目。

目前覆盖 `初赛试题（按届数）` 和 `决赛试题（按届数）` 两个目录。
这两批 PDF 是文字版，按 `题目N` 切分很稳定；公式抽取会散架
（∫ -> Z、∑ -> X），所以建库时同时保留两版题干：

- ``question``：压成一行的文本，供检索和模型上下文使用；
- ``question_layout``：保留换行和字号，供 ``rag.latex`` 重建展示用公式。

展示层读的是模型重建后缓存的 ``display_question``，不再直接渲染
PDF 文字层。

2024/2025 的试卷是另一种版式（填空/选择/解答），放在 `parse_recent_text`。
"""

from __future__ import annotations

import re
from pathlib import Path

try:  # PyMuPDF 1.24+ 推荐 pymupdf，旧版本只有 fitz
    import pymupdf as fitz
except ImportError:  # pragma: no cover - 兼容旧环境
    import fitz  # type: ignore

from rag.textnorm import normalize_for_search


_CN_DIGITS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}

_HEADER_RE = re.compile(
    r"^\s*第([一二三四五六七八九十]+)届非数学类\s*"
    r"(初赛|决赛)?\s*(补赛[0-9]*)?\s*(A|B)?\s*类?\s*$",
    re.MULTILINE,
)
_QUESTION_RE = re.compile(r"^\s*题目\s*(\d+)\s*$", re.MULTILINE)
# 题目 id 里带了届数、阶段、类别、补赛和题号，去重后可以据此还原
# “这道题还出现在哪一届的哪个位置”。
_PROBLEM_ID_RE = re.compile(
    r"^cmc-(\d{2})-(prelim|final)(-s\d*)?-([abx])-(\d{2})$"
)

_RECENT_SECTION_RE = re.compile(
    r"^\s*([一二三])、\s*(填空题|选择题|解答题)[^\n]*$",
    re.MULTILINE,
)
_RECENT_ITEM_RE = re.compile(r"^\s*(\d+)\s*[、.](?![0-9])\s*", re.MULTILINE)
_RECENT_MAX_ITEMS = {"填空题": 6, "选择题": 6, "解答题": 5}

_TRAILING_PAGE_NO_RE = re.compile(r"\s+\d{1,2}\s*$")

# 老试卷的数学字体没有 ToUnicode，PyMuPDF 会把 CMEX10 的字形码
# 直接当 ASCII 输出：∫ 变成 Z，∑ 变成 X，大括号变成 \x12/\x13。
# 修正后的文本同时进入 question 和 question_layout；改完题库后需要
# 重新构建向量缓存（python -m rag.build_embeddings）。
_CMEX_GLYPHS = {
    "ZZZ": "∭",
    "ZZ": "∬",
    "Z": "∫",
    "X": "∑",
    "P": "∑",
    "\x12": "(",
    "\x13": ")",
    "\x0e": "[",
    "\x0f": "]",
    "\x10": "{",
    "\x11": "}",
}

_LAYOUT_MARK_RE = re.compile(r"@@([\d.]+),([\d.]+)@@")


def _map_cmex(text: str, font: str) -> str:
    if font != "CMEX10":
        return text
    for source, target in _CMEX_GLYPHS.items():
        text = text.replace(source, target)
    return text


def _cn_to_int(text: str) -> int:
    """把 '十六' / '十' / '三' 之类的届数转成整数。"""
    if text == "十":
        return 10
    if "十" in text:
        tens, ones = text.split("十", 1)
        return (_CN_DIGITS.get(tens, 1) if tens else 1) * 10 + (
            _CN_DIGITS.get(ones, 0) if ones else 0
        )
    return _CN_DIGITS[text]


def _clean_body(raw: str) -> str:
    """把逐行抽取的题目压成一行，去掉页眉页脚和排版噪声。"""
    # _extract_text 会在每行前面附一个字号/坐标标记，供 _clean_layout 使用
    s = re.sub(r"(?m)^@@[\d.,]+@@\s*$", " ", raw)
    s = s.replace("b 站李熵一排版整理", " ").replace("♡", " ")
    # PDF 里偶有控制字符（\x00 等），展示和 JSON 里都没有意义
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    s = re.sub(r"\s+", " ", s).strip()

    # 中文之间的空格是 PDF 逐字抽取造成的，去掉更接近原题
    s = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", s)
    s = re.sub(r"\s+([,.;:!?%)\]])", r"\1", s)
    s = re.sub(r"([(\[])\s+", r"\1", s)

    # 题目末尾孤立的 1-2 位数字通常是页码；短文本不动，避免误删答案
    if len(s) > 40:
        s = _TRAILING_PAGE_NO_RE.sub("", s)
    return s.strip()


def _clean_layout(raw: str) -> str:
    """保留换行的题干原文，供“重建 LaTeX”时恢复分式/上下标结构。

    PDF 的文字层会把分数拆成上下两行、把极限上下限拆到相邻行；
    `_clean_body` 把这些换行压成了空格，模型就再也看不出结构。
    这里只去页眉页脚和噪声，不合并行。
    """
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
    s = s.replace("♡", " ")

    lines: list[str] = []
    size = None
    for line in s.splitlines():
        stripped = line.strip()
        marker = _LAYOUT_MARK_RE.fullmatch(stripped)
        if marker:
            size = float(marker.group(1))
            continue

        line = re.sub(r"[ \t　]+", " ", line).strip()
        if not line:
            continue
        # 页眉“第X届非数学类…”不参与重建
        if re.fullmatch(r"第[一二三四五六七八九十]+届非数学类.*", line):
            continue
        # 字号是判断上下标/分式层级的关键线索：正文 12pt，
        # 上下标和分式的分子分母通常是 8pt。
        prefix = f"[{size:.0f}pt] " if size is not None else ""
        lines.append(prefix + line)
        size = None

    # 末尾孤立的 1-2 位数字是页码；只删最后一行，避免误删分式的分子
    if len(lines) > 2 and re.fullmatch(r"\d{1,2}", lines[-1]):
        lines.pop()
    return "\n".join(lines)


def _supplement_code(supplement: str) -> str:
    if not supplement:
        return ""
    match = re.search(r"(\d+)$", supplement)
    return f"-s{match.group(1)}" if match else "-s"


def _make_id(
    edition: int,
    stage: str,
    category: str,
    supplement: str,
    number: int,
) -> str:
    stage_code = "prelim" if stage == "初赛" else "final"
    category_code = category.lower() if category in ("A", "B") else "x"
    return (
        f"cmc-{edition:02d}-{stage_code}{_supplement_code(supplement)}"
        f"-{category_code}-{number:02d}"
    )


def _make_source(
    edition: int,
    stage: str,
    category: str,
    supplement: str,
    number: int,
) -> str:
    parts = [f"第{edition}届", "非数学类", stage]
    if supplement:
        parts.append(supplement)
    if category in ("A", "B"):
        parts.append(f"{category}类")
    parts.append(f"第{number}题")
    return " ".join(parts)


def make_source(
    edition: int,
    stage: str,
    category: str,
    supplement: str,
    number: int,
) -> str:
    """按试卷归属生成出处，供去重后的别名还原使用。"""
    return _make_source(edition, stage, category, supplement, number)


def parse_problem_id(problem_id: str) -> dict | None:
    """从题目 id 解析出它属于哪一份试卷。

    ``cmc-13-final-x-09`` -> 第 13 届决赛第 9 题。
    去重时被合并的题会保留在 ``aliases`` 里，用这个函数可以把它
    还原到原试卷，否则按届数浏览时会出现“缺第 9 题”的假象。
    """
    match = _PROBLEM_ID_RE.match(str(problem_id or ""))
    if not match:
        return None

    supplement_code = match.group(3) or ""
    supplement = ""
    if supplement_code:
        suffix = supplement_code[2:]
        supplement = f"补赛{suffix}" if suffix else "补赛"

    return {
        "edition": int(match.group(1)),
        "stage": "初赛" if match.group(2) == "prelim" else "决赛",
        "category": {"a": "A", "b": "B", "x": "未分类"}[match.group(4)],
        "supplement": supplement,
        "number": int(match.group(5)),
    }


def parse_retypeset_text(
    text: str,
    *,
    stage_hint: str,
    source_file: str = "",
) -> list[dict]:
    """解析“题目N”版式的试卷文本。

    第16届初赛的标题里没有“初赛”二字，所以 stage 允许缺失，
    用目录名作为 stage_hint 兜底。
    """
    text = text.replace("b 站李熵一排版整理", "").replace("♡", "")
    text = text.replace("　", " ")

    headers = list(_HEADER_RE.finditer(text))
    problems: list[dict] = []

    for index, header in enumerate(headers):
        start = header.end()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        block = text[start:end]

        edition = _cn_to_int(header.group(1))
        stage = header.group(2) or stage_hint
        supplement = header.group(3) or ""
        category = header.group(4) or "未分类"

        questions = list(_QUESTION_RE.finditer(block))
        for q_index, question in enumerate(questions):
            body_start = question.end()
            body_end = (
                questions[q_index + 1].start()
                if q_index + 1 < len(questions)
                else len(block)
            )
            body_raw = block[body_start:body_end]
            body = _clean_body(body_raw)
            if len(body) < 8:
                continue

            number = int(question.group(1))
            problems.append({
                "id": _make_id(edition, stage, category, supplement, number),
                "question": body,
                "question_layout": _clean_layout(body_raw),
                "stage": stage,
                "edition": edition,
                "category": category,
                "supplement": supplement,
                "number": number,
                "source": _make_source(edition, stage, category, supplement, number),
                "source_file": source_file,
                "answer": None,
                "solution": None,
            })

    return problems


def _extract_text(path: Path) -> str:
    """按行抽取文字，并在每行后附 `@@字号,y@@` 标记。

    标记只给 `_clean_layout` 重建 LaTeX 用，`_clean_body` 会先删掉。
    用 dict 而不是纯 text，是因为纯 text 丢掉了字号和基线，
    无法判断上下标；同时可以按字体修正 CMEX10 的字形码。
    """
    document = fitz.open(str(path))
    try:
        lines: list[str] = []
        for page in document:
            data = page.get_text("dict")
            for block in data.get("blocks", []):
                for line in block.get("lines", []):
                    spans = line.get("spans", [])
                    text = "".join(
                        _map_cmex(span.get("text", ""), span.get("font", ""))
                        for span in spans
                    )
                    if not text.strip():
                        continue
                    size = max(
                        (float(span.get("size", 0)) for span in spans),
                        default=0.0,
                    )
                    y = float(line.get("bbox", [0, 0, 0, 0])[1])
                    lines.append(f"@@{size:.1f},{y:.1f}@@")
                    lines.append(text)
        return "\n".join(lines)
    finally:
        document.close()


def parse_recent_text(
    text: str,
    *,
    year: int,
    source_file: str = "",
) -> list[dict]:
    """解析 2024/2025 的“填空/选择/解答”版式。

    这类 PDF 的公式几乎全部散成单字符，正文可读性一般，但题目边界清楚。
    先入库供检索，展示时再考虑用模型重建 LaTeX。
    """
    text = text.replace("　", " ")
    sections = list(_RECENT_SECTION_RE.finditer(text))
    problems: list[dict] = []

    for s_index, section in enumerate(sections):
        start = section.end()
        end = sections[s_index + 1].start() if s_index + 1 < len(sections) else len(text)
        block = text[start:end]
        section_name = section.group(2)

        # 只接受 1,2,3... 连续递增的题号。公式里偶尔会出现形如 "1." 的
        # 小数或编号，如果不校验顺序，会把一道题切成好几段。
        items: list[re.Match] = []
        expected = 1
        max_items = _RECENT_MAX_ITEMS[section_name]
        for match in _RECENT_ITEM_RE.finditer(block):
            if int(match.group(1)) != expected:
                continue
            items.append(match)
            expected += 1
            if expected > max_items:
                break

        for i_index, item in enumerate(items):
            body_start = item.end()
            body_end = items[i_index + 1].start() if i_index + 1 < len(items) else len(block)
            body_raw = block[body_start:body_end]
            body = _clean_body(body_raw)
            if len(body) < 8:
                continue

            number = int(item.group(1))
            # 同一份卷子里填空/选择/解答都从 1 开始编号，id 里带上题型
            section_code = {"填空题": "fill", "选择题": "choice", "解答题": "solve"}[section_name]
            problems.append({
                "id": f"cmc-{year}-{section_code}-{number:02d}",
                "question": body,
                "question_layout": _clean_layout(body_raw),
                "stage": "初赛",
                "edition": None,
                "year": year,
                "category": "A",
                "supplement": "",
                "number": number,
                "section": section_name,
                "source": f"{year} 年 CMC 非数学类 {section_name} 第{number}题",
                "source_file": source_file,
                "answer": None,
                "solution": None,
            })

    return problems


def extract_papers(papers_dir: str | Path) -> list[dict]:
    """扫描目录，返回所有题目的原始记录（未去重、未打标签）。"""
    root = Path(papers_dir)
    if not root.exists():
        raise FileNotFoundError(f"找不到真题目录：{root}")

    problems: list[dict] = []
    for folder, stage_hint in (
        ("初赛试题（按届数）", "初赛"),
        ("决赛试题（按届数）", "决赛"),
    ):
        directory = root / folder
        if not directory.exists():
            continue
        for pdf in sorted(directory.glob("*.pdf")):
            text = _extract_text(pdf)
            problems.extend(
                parse_retypeset_text(
                    text,
                    stage_hint=stage_hint,
                    source_file=str(pdf.relative_to(root)),
                )
            )

    for name, year in (("2024试卷版.pdf", 2024), ("2025（试卷）.pdf", 2025)):
        pdf = root / name
        if pdf.exists():
            problems.extend(
                parse_recent_text(
                    _extract_text(pdf),
                    year=year,
                    source_file=name,
                )
            )

    return problems


def dedupe(problems: list[dict]) -> list[dict]:
    """按归一化题干去重，保留第一条，其余出处合并到 sources。"""
    unique: list[dict] = []
    seen: dict[str, dict] = {}

    for problem in problems:
        key = normalize_for_search(problem["question"], doc_side=True)
        if not key:
            continue
        if key in seen:
            first = seen[key]
            first.setdefault("sources", [first["source"]]).append(problem["source"])
            first.setdefault("aliases", []).append(problem["id"])
            continue
        problem["sources"] = [problem["source"]]
        seen[key] = problem
        unique.append(problem)

    for problem in unique:
        problem["source"] = " / ".join(problem["sources"])
        problem.pop("sources", None)
        if "aliases" not in problem:
            problem["aliases"] = []

    return unique
