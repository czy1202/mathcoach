"""OCR 评测：把 CMC 真题 PDF 裁成单题图片，跑本地识别并算指标。

为什么需要这个脚本
------------------
网页上的“识别成功”只代表接口返回了文本，不代表公式识别正确。这里用
2024 / 2025 两年 CMC 非数学类初赛真题做固定测评集：

    PDF 页面 → 按题号裁成单题图片 → 本地 OCR → 与人工整理的标准题干比较

标准题干取自 eval/cases.jsonl（与解题评测同一份），不采用 PDF 文字层：
真题 PDF 的中文可以正常提取，但公式是 CID 乱码，直接当参考答案会误判。

指标
----
- CJK CER：只比较中文字符的编辑距离，衡量题干文字是否抄对；
- 数字 F1：数字的多重集合 F1，衡量系数、上下标里的数字是否识别正确；
- 符号召回：lim / 积分 / 求和 / 根号 / 分数 / π / 三角函数等关键结构，
  参考答案里出现过、OCR 结果里是否也出现。

这些指标只能定位“哪里可疑”，公式是否真的等价仍需人工抽查；脚本会
同时保存每题的裁剪图和 OCR 原文，方便逐题复核。

运行方式（在 mathcoach 根目录下执行）::

    python -m eval.ocr_eval --year 2024 --year 2025 --degrade none
    python -m eval.ocr_eval --degrade phone      # 模拟手机拍照
    python -m eval.ocr_eval --limit 3            # 先跑几题看看
"""

from __future__ import annotations

import argparse
import io
import json
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean

import pymupdf
from PIL import Image, ImageDraw, ImageFilter

import config
import ocr

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "eval" / "cases.jsonl"
DEFAULT_PDF_DIR = Path.home() / "Desktop" / "数学竞赛分享"
DEFAULT_OUT = ROOT / "eval" / "ocr_results"

PAPERS = {
    2024: "2024试卷版.pdf",
    2025: "2025（试卷）.pdf",
}

SECTION_RE = re.compile(r"^[一二三]、\s*(填空题|选择题|解答题)")
QNUM_RE = re.compile(r"^\s*(\d{1,2})\s*[、.]\s*")
SECTION_KIND = {"填空题": "fill", "选择题": "choice", "解答题": "solve"}
KIND_CASE = {"fill": "F", "choice": "C", "solve": "J"}

PAGE_TOP = 55.0          # 页眉以下
PAGE_BOTTOM = 760.0      # 去掉页脚页码
X0, X1 = 72.0, 545.0
PAD_TOP = 2.0
PAD_END = 0.0

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
DIGIT_RE = re.compile(r"\d+(?:\.\d+)?")

# 关键结构：每项是 (名字, 正则)。命中“有无”而非次数，避免排版差异误判。
SYMBOLS = (
    ("lim", r"lim|\\lim"),
    ("integral", r"∫|\\int"),
    ("sum", r"∑|\\sum"),
    ("sqrt", r"√|\\sqrt"),
    ("frac", r"\\[dt]?frac|/"),
    ("pi", r"π|\\pi"),
    # 不比较 ^ / _ / e^：同一公式可以写成 \sqrt[n]{...} 或 ^(1/n)，
    # 这类差异是排版习惯，不是识别错误，纳入指标会制造假阳性。
    ("trig", r"sin|cos|tan"),
    ("log", r"ln|log"),
    ("infinity", r"∞|\\infty|rightarrow|→"),
)


@dataclass
class Question:
    """一道题的裁剪位置。"""

    id: str
    year: int
    kind: str
    number: int
    page: int          # 0-based
    y0: float
    end_page: int
    end_y: float


@dataclass
class Result:
    """一道题的识别与指标。"""

    id: str
    year: int
    kind: str
    crop: str
    reference: str       # cases.jsonl 的人工题干，用于核对数学结构
    pdf_reference: str   # PDF 同区域文字层，用于 CJK / 数字指标
    ocr_text: str
    cjk_cer: float | None
    digit_f1: float | None
    symbol_recall: float | None
    elapsed: float
    error: str = ""


# ---------- 从 PDF 定位题目 ----------

def collect_questions(doc: pymupdf.Document, year: int) -> list[Question]:
    """按“左边界题号行 + 章节标题”切分题目。

    只依赖版面位置，不依赖会乱码的公式文字。题号行必须在左侧（x0 < 100），
    避免把公式里恰好以数字开头的行当成新题。
    """
    events: list[tuple[int, float, int, object]] = []  # (page, y, type, payload)
    headings: list[tuple[int, float, str]] = []
    starts: list[tuple[int, float, int]] = []

    for pno, page in enumerate(doc):
        page_lines: list[tuple[float, float]] = []  # 每行的 (y0, y1)
        page_starts: list[tuple[int, float, int]] = []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                text = "".join(s["text"] for s in line["spans"]).strip()
                if not text:
                    continue
                x0, y0, _, y1 = line["bbox"]
                page_lines.append((y0, y1))
                match = SECTION_RE.match(text)
                if match and x0 < 120:
                    headings.append((pno, y0, SECTION_KIND[match.group(1)]))
                    continue
                match = QNUM_RE.match(text)
                if match and x0 < 100 and len(text) > 1:
                    page_starts.append((pno, y0, int(match.group(1))))

        # 题号行本身在 y0；比它更高的分数/根号/方程组会向上越过 y0。
        # 取所有与题号行垂直重叠的行的最高点，作为这道题真正的上边界。
        for index, (_, y, number) in enumerate(page_starts):
            tops = [line_y0 for line_y0, line_y1 in page_lines if line_y0 < y < line_y1]
            page_starts[index] = (pno, min(tops + [y]), number)
        starts.extend(page_starts)

    for page, y, kind in headings:
        events.append((page, y, 0, kind))
    for page, y, number in starts:
        events.append((page, y, 1, number))
    events.sort(key=lambda item: (item[0], item[1]))

    questions: list[Question] = []
    kind: str | None = None
    counters: Counter[str] = Counter()
    for index, (page, y, event_type, payload) in enumerate(events):
        if event_type == 0:
            kind = str(payload)
            counters[kind] = 0
            continue
        if kind is None:
            continue
        counters[kind] += 1
        number = int(payload)  # type: ignore[arg-type]
        # 编号必须连续；跳号说明版面识别错了，跳过而不是硬编号
        if number != counters[kind]:
            counters[kind] = number
        if index + 1 < len(events):
            end_page, next_y, _, _ = events[index + 1]
            # next_y 可能是下一题最高公式的上边界；留 3pt 防止把下一题带进来。
            # 跨页时不能和本页坐标取 max，否则会把整页高度当成边界。
            end_y = max(next_y - 3, y + 10) if end_page == page else next_y - 3
        else:
            end_page, end_y = page, PAGE_BOTTOM
        questions.append(
            Question(
                id=f"{year}-{KIND_CASE[kind]}{number}",
                year=year,
                kind=kind,
                number=number,
                page=page,
                y0=y,
                end_page=end_page,
                end_y=end_y,
            )
        )
    return questions


# ---------- 渲染与退化 ----------

def _render_part(doc: pymupdf.Document, page: int, top: float, bottom: float, dpi: int) -> Image.Image:
    """渲染一页的一段，返回 PIL 图像。"""
    pdf_page = doc[page]
    top = max(top, PAGE_TOP)
    bottom = min(bottom, PAGE_BOTTOM)
    if bottom <= top:
        return Image.new("RGB", (1, 1), "white")
    clip = pymupdf.Rect(X0, top, X1, bottom)
    zoom = dpi / 72
    pix = pdf_page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


def render_question(doc: pymupdf.Document, question: Question, dpi: int) -> Image.Image:
    """把一道题渲染成一张图；跨页题目纵向拼接。"""
    parts: list[Image.Image] = []
    if question.page == question.end_page:
        parts.append(
            _render_part(
                doc, question.page, question.y0 - PAD_TOP, question.end_y - PAD_END, dpi
            )
        )
    else:
        parts.append(
            _render_part(doc, question.page, question.y0 - PAD_TOP, PAGE_BOTTOM, dpi)
        )
        for page in range(question.page + 1, question.end_page):
            parts.append(_render_part(doc, page, PAGE_TOP, PAGE_BOTTOM, dpi))
        parts.append(_render_part(doc, question.end_page, PAGE_TOP, question.end_y - PAD_END, dpi))

    parts = [part for part in parts if part.width > 1 and part.height > 1]
    width = max(part.width for part in parts)
    gap = 12
    height = sum(part.height for part in parts) + gap * (len(parts) - 1)
    canvas = Image.new("RGB", (width, height), "white")
    offset = 0
    for part in parts:
        canvas.paste(part, (0, offset))
        offset += part.height + gap
    return canvas


def pdf_text_for_question(doc: pymupdf.Document, question: Question) -> str:
    """提取与裁剪图同一区域的 PDF 文字层。

    中文和数字的提取是可靠的，公式字形是 CID/PUA 乱码；因此这份文本只用于
    CJK CER 和数字 F1，不用于判断数学结构是否识别正确。
    """

    def part(page: int, top: float, bottom: float) -> str:
        clip = pymupdf.Rect(X0, max(top, PAGE_TOP), X1, min(bottom, PAGE_BOTTOM))
        if clip.y1 <= clip.y0:
            return ""
        return doc[page].get_text("text", clip=clip)

    pieces: list[str] = []
    if question.page == question.end_page:
        pieces.append(
            part(question.page, question.y0 - PAD_TOP, question.end_y - PAD_END)
        )
    else:
        pieces.append(part(question.page, question.y0 - PAD_TOP, PAGE_BOTTOM))
        for page in range(question.page + 1, question.end_page):
            pieces.append(part(page, PAGE_TOP, PAGE_BOTTOM))
        pieces.append(part(question.end_page, PAGE_TOP, question.end_y - PAD_END))
    text = "\n".join(piece for piece in pieces if piece.strip())
    return re.sub(r"^\s*\d{1,2}\s*[、.]\s*", "", text, count=1)


def degrade(image: Image.Image, mode: str) -> bytes:
    """输出图片字节；phone 模式模拟手机拍照的常见退化。"""
    if mode == "none":
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    if mode not in ("phone", "phone-hard"):
        raise ValueError(f"未知退化模式：{mode}")

    hard = mode == "phone-hard"
    target_width = 1100 if hard else 1400
    if image.width > target_width:
        ratio = target_width / image.width
        image = image.resize(
            (target_width, max(1, round(image.height * ratio))),
            Image.Resampling.LANCZOS,
        )

    if hard:
        # 轻微透视：模拟手机没有正对纸张
        width, height = image.size
        margin_x = max(4, int(width * 0.02))
        margin_y = max(4, int(height * 0.01))
        quad = (
            margin_x, 0,                          # 左上
            0, height - margin_y,                 # 左下
            width - margin_x, height,             # 右下
            width, margin_y,                      # 右上
        )
        image = image.transform(
            (width, height),
            Image.Transform.QUAD,
            quad,
            resample=Image.Resampling.BICUBIC,
            fillcolor=(255, 255, 255),
        )

    angle = 2.5 if hard else 1.6
    image = image.rotate(angle, expand=True, fillcolor=(255, 255, 255), resample=Image.Resampling.BICUBIC)
    image = image.filter(ImageFilter.GaussianBlur(0.7 if hard else 0.35))
    # 轻微整体压暗，模拟室内光线
    image = Image.eval(image, lambda value: min(255, int(value * (0.88 if hard else 0.94) + 8)))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=55 if hard else 70)
    return buffer.getvalue()


# ---------- 指标 ----------

def clean_ocr_text(text: str) -> str:
    """去掉题号和代码围栏，避免把“1、”当成识别内容。"""
    text = text.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    return re.sub(r"^\s*\d{1,2}\s*[、.]\s*", "", text, count=1)


def lev(a: str, b: str) -> int:
    """编辑距离；题干长度在几百字符内，直接 DP 足够。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (char_a != char_b),
                )
            )
        previous = current
    return previous[-1]


def cjk_cer(reference: str, hypothesis: str) -> float:
    """中文 CER；参考答案没有中文时返回 None。"""
    ref = "".join(CJK_RE.findall(reference))
    hyp = "".join(CJK_RE.findall(hypothesis))
    if not ref:
        return None
    return lev(ref, hyp) / len(ref)


def _multiset_f1(reference: list[str], hypothesis: list[str]) -> float:
    ref_counter = Counter(reference)
    hyp_counter = Counter(hypothesis)
    matched = sum((ref_counter & hyp_counter).values())
    if not ref_counter and not hyp_counter:
        return 1.0
    if not ref_counter or not hyp_counter:
        return 0.0
    precision = matched / len(hypothesis)
    recall = matched / len(reference)
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def digit_f1(reference: str, hypothesis: str) -> float:
    return _multiset_f1(DIGIT_RE.findall(reference), DIGIT_RE.findall(hypothesis))


def symbol_recall(reference: str, hypothesis: str) -> float:
    present = [(name, pattern) for name, pattern in SYMBOLS if re.search(pattern, reference)]
    if not present:
        return 1.0
    hit = sum(1 for _, pattern in present if re.search(pattern, hypothesis))
    return hit / len(present)


# ---------- 主流程 ----------

def load_references() -> dict[str, str]:
    """从 cases.jsonl 取标准题干。"""
    references: dict[str, str] = {}
    for line in CASES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        key = f"{case['year']}-{case['id'].split('-')[-1]}"
        # case id 形如 2024-F1；直接保留
        references[case["id"]] = case["statement"]
        references.setdefault(key, case["statement"])
    return references


def contact_sheet(images: list[tuple[str, Image.Image]], path: Path, per_sheet: int = 6) -> None:
    """每 6 题拼一张带编号的图，避免整页太长、文字被缩得看不清。"""
    if not images:
        return
    label_height = 22
    for start in range(0, len(images), per_sheet):
        chunk = images[start : start + per_sheet]
        width = max(image.width for _, image in chunk)
        height = sum(image.height + label_height for _, image in chunk)
        canvas = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(canvas)
        offset = 0
        for label, image in chunk:
            draw.text((4, offset + 4), label, fill=(180, 0, 0))
            offset += label_height
            canvas.paste(image, (0, offset))
            offset += image.height
        canvas.save(path.with_name(f"{path.stem}-{start // per_sheet + 1:02d}{path.suffix}"))


def run(args: argparse.Namespace) -> int:
    pdf_dir = Path(args.pdf_dir) if args.pdf_dir else DEFAULT_PDF_DIR
    references = load_references()
    questions: list[Question] = []
    for year in args.year:
        pdf_path = pdf_dir / PAPERS[year]
        if not pdf_path.exists():
            print(f"缺少试卷：{pdf_path}")
            return 1
        doc = pymupdf.open(pdf_path)
        found = collect_questions(doc, year)
        found = [q for q in found if q.kind in args.sections]
        questions.extend(found)
        doc.close()

    if args.limit:
        questions = questions[: args.limit]
    if not questions:
        print("没有找到可评测的题目。")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / f"{stamp}-{args.degrade}"
    crop_dir = out_dir / "crops"
    crop_dir.mkdir(parents=True, exist_ok=True)

    config.OCR_PROVIDER = args.provider
    if args.model:
        config.OLLAMA_OCR_MODEL = args.model

    results: list[Result] = []
    images: list[tuple[str, Image.Image]] = []
    for index, question in enumerate(questions, 1):
        doc = pymupdf.open(pdf_dir / PAPERS[question.year])
        image = render_question(doc, question, args.dpi)
        pdf_reference = pdf_text_for_question(doc, question)
        doc.close()
        crop_path = crop_dir / f"{question.id}.png"
        image.save(crop_path)
        images.append((question.id, image))

        reference = references.get(question.id, "")
        data = degrade(image, args.degrade)
        started = time.perf_counter()
        error = ""
        text = ""
        try:
            text = clean_ocr_text(ocr.recognize(data).text)
        except ocr.OcrError as exc:
            error = str(exc)
        elapsed = round(time.perf_counter() - started, 2)

        result = Result(
            id=question.id,
            year=question.year,
            kind=question.kind,
            crop=str(crop_path.relative_to(out_dir)),
            reference=reference,
            pdf_reference=pdf_reference,
            ocr_text=text,
            cjk_cer=None if error else cjk_cer(pdf_reference, text),
            digit_f1=None if error else digit_f1(pdf_reference, text),
            symbol_recall=None if error else symbol_recall(reference, text),
            elapsed=elapsed,
            error=error,
        )
        results.append(result)
        status = (
            "ERR"
            if error
            else f"CER={_fmt(result.cjk_cer)} digitF1={_fmt(result.digit_f1)} "
            f"sym={_fmt(result.symbol_recall)}"
        )
        print(f"[{index:02d}/{len(questions)}] {question.id} {status} {elapsed}s")

    (out_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(asdict(result), ensure_ascii=False) for result in results) + "\n",
        encoding="utf-8",
    )
    contact_sheet(images, out_dir / "contact.png")

    ok = [result for result in results if not result.error]
    lines = [
        f"# OCR 测评 {stamp}",
        "",
        f"- 试卷：{', '.join(str(year) for year in args.year)}",
        f"- 题型：{', '.join(args.sections)}",
        f"- 退化：{args.degrade}，DPI：{args.dpi}，模型：{config.OLLAMA_OCR_MODEL}",
        f"- 题数：{len(results)}，失败：{len(results) - len(ok)}",
        "",
        "| 题目 | CJK CER | 数字 F1 | 符号召回 | 耗时(s) | 备注 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        note = "识别失败" if result.error else ""
        lines.append(
            f"| {result.id} | {_fmt(result.cjk_cer)} | {_fmt(result.digit_f1)} | "
            f"{_fmt(result.symbol_recall)} | {result.elapsed:.1f} | {note} |"
        )
    if ok:
        lines += [
            "",
            f"平均：CJK CER {_fmt(_mean(r.cjk_cer for r in ok))}，"
            f"数字 F1 {_fmt(_mean(r.digit_f1 for r in ok))}，"
            f"符号召回 {_fmt(_mean(r.symbol_recall for r in ok))}",
            "",
            "> CJK CER 只覆盖中文，数字 F1 只覆盖数字，符号召回只检查关键结构是否出现。",
            "> 公式是否真的等价需要结合 crops/ 与 contact.png 人工抽查。",
            "",
        ]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n结果目录：{out_dir}")
    print("\n".join(lines[:12]))
    return 0


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _mean(values) -> float | None:
    """忽略 None 的均值；全为 None 时返回 None。"""
    kept = [value for value in values if value is not None]
    return mean(kept) if kept else None


def main() -> int:
    parser = argparse.ArgumentParser(description="CMC 真题 OCR 测评")
    parser.add_argument("--pdf-dir", default="", help="试卷 PDF 所在目录")
    parser.add_argument(
        "--year",
        action="append",
        type=int,
        choices=sorted(PAPERS),
        help="可重复；默认 2024 和 2025",
    )
    parser.add_argument(
        "--sections",
        default="fill,choice",
        help="逗号分隔：fill / choice / solve（默认 fill,choice）",
    )
    parser.add_argument("--dpi", type=int, default=200, help="渲染 DPI，默认 200")
    parser.add_argument(
        "--degrade",
        choices=("none", "phone", "phone-hard"),
        default="none",
        help="none=清晰渲染；phone=轻度退化；phone-hard=透视/旋转/模糊/低质量 JPEG",
    )
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题")
    parser.add_argument("--provider", default="ollama", help="OCR_PROVIDER，默认 ollama")
    parser.add_argument("--model", default="", help="覆盖 OLLAMA_OCR_MODEL")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    args = parser.parse_args()
    args.year = args.year or [2024, 2025]
    args.sections = {part.strip() for part in args.sections.split(",") if part.strip()}
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
