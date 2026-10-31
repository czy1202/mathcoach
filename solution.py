"""把完整的 Markdown 解答切成可单独引用的步骤。

模型在“分步解题过程”里使用 ``### 第一步：...`` 这样的三级标题。Web 层
据此把每一步渲染成独立卡片，并把步骤原文传给侧栏追问。解析失败时返回
``None``，调用方回退到原来的整段回答，不影响主流程。
"""

from __future__ import annotations

import re


# 步骤标题：### 第一步：化简 / ## 第 2 步 / #### 第十一步
_STEP_HEADING_RE = re.compile(
    r"^\s{0,3}(?P<hashes>#{2,4})\s*第\s*"
    r"(?P<num>[0-9一二三四五六七八九十百零〇两]+)\s*步\s*(?P<rest>.*)$"
)

# 兼容模型偶尔写成 **第一步：化简** 的情况
_STEP_BOLD_RE = re.compile(
    r"^\s*(?:\*\*|__)\s*第\s*"
    r"(?P<num>[0-9一二三四五六七八九十百零〇两]+)\s*步\s*"
    r"(?P<rest>.*?)(?:\*\*|__)\s*$"
)

_SECTION_HEADING_RE = re.compile(r"^\s{0,3}(?P<hashes>#{2,4})\s+")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_TITLE_SEPARATOR_RE = re.compile(r"^\s*[：:、.\-—]\s*")


def _clean_title(rest: str) -> str:
    return _TITLE_SEPARATOR_RE.sub("", rest.strip()).strip()


def _match_step(line: str) -> tuple[int, str, str] | None:
    """返回 (标题级别, 题号原文, 标题)；不是步骤标题时返回 None。"""
    match = _STEP_HEADING_RE.match(line)
    if match:
        return len(match.group("hashes")), match.group("num"), _clean_title(
            match.group("rest")
        )

    match = _STEP_BOLD_RE.match(line)
    if match:
        return 3, match.group("num"), _clean_title(match.group("rest"))

    return None


def extract_solution_steps(answer: str) -> dict | None:
    """把回答拆成 intro / steps / outro。

    Returns:
        ``{"intro": str, "steps": [...], "outro": str}``；没有识别到步骤时
        返回 ``None``。``steps`` 的 ``index`` 按出现顺序重新编号，不信任
        模型写出的数字，避免跳号导致前端按钮和内容错位。
    """
    if not isinstance(answer, str) or not answer.strip():
        return None

    lines = answer.splitlines()

    # 先记录所有步骤标题的位置，跳过代码块里的同名文本。
    marks: list[dict] = []
    in_fence = False
    for line_no, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        matched = _match_step(line)
        if matched:
            level, number, title = matched
            marks.append({
                "line": line_no,
                "level": level,
                "number": number,
                "title": title,
            })

    if not marks:
        return None

    first_level = marks[0]["level"]
    entries: list[dict] = []

    for position, mark in enumerate(marks):
        start = mark["line"] + 1
        end = marks[position + 1]["line"] if position + 1 < len(marks) else len(lines)

        # 步骤正文遇到同级或更高级的 Markdown 标题就结束，例如
        # “## 最终答案”不应被算进最后一步。
        in_fence = False
        for line_no in range(start, end):
            line = lines[line_no]
            if _FENCE_RE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue

            section = _SECTION_HEADING_RE.match(line)
            if not section:
                continue
            if _match_step(line):
                continue
            if len(section.group("hashes")) <= first_level:
                end = line_no
                break

        entries.append({
            "index": position + 1,
            "label": f"第{mark['number']}步",
            "title": mark["title"],
            "markdown": "\n".join(lines[start:end]).strip(),
            "end": end,
        })

    first_line = marks[0]["line"]
    intro = "\n".join(lines[:first_line]).strip()
    outro = "\n".join(lines[entries[-1]["end"]:]).strip()

    steps = [
        {
            "index": entry["index"],
            "label": entry["label"],
            "title": entry["title"],
            "markdown": entry["markdown"],
        }
        for entry in entries
    ]
    return {"intro": intro, "steps": steps, "outro": outro}
