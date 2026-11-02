"""独立复核器：让另一个全新的模型上下文重新做一遍题，再给原答案判卷。

为什么需要它：
    1. rubric 判分靠关键词清单，正确的替代证明经常因为“没写官方那几步”
       被误杀；
    2. 主 Agent 撞步数上限或超时后，其实没有给出答案，判分器只能记失败，
       但题目本身可能并不难。

复核器使用新的消息列表和新的系统提示词，看不到标准答案，因此不会继承
主 Agent 的上下文和已经走偏的推理链。但它仍是同一个模型，可能存在共同
盲区，所以它的结论只是“第二意见”，不能当成正确答案。它只在评测里启用
（--verify），网页端不额外付出这份模型调用成本。

流程（只处理首轮未通过的题）：
    1. 复核器独立求解，给出 PASS / FAIL / UNCERTAIN 和自己的答案；
    2. 非证明题：复核答案与标准答案一致 → 改判通过（有客观答案兜底）；
    3. 证明题：复核结论 PASS → 标记“复核放行”，仍需人工确认。
       证明没有可机械核对的答案，复核器本身也可能看走眼，不能自动算对；
    4. 复核结论为 FAIL → 把复核意见反馈给主 Agent，限一次重解；
    5. 其余情况维持首轮判定。

复核器看不到标准答案，所以第 2 步不会“抄答案”；反馈重解也不会泄题。
"""

import re
from dataclasses import dataclass

from agent import Agent
from eval.extract import extract_final_answer
from eval.judge import judge_answer, judge_parts, judge_rubric
from schemas import Verdict


VERIFIER_PROMPT = r"""你是一名独立的数学阅卷复核员。你没有参考答案，也不要假设考生答案正确。

复核步骤：
1. 先自己独立解一遍题；关键计算调用 verify_with_sympy 验证，不要只靠心算。
2. 再把考生答案与你的结果比较：
   - 结论一致、推导没有缺口 → PASS；
   - 答案不同，或证明存在实质缺口 → FAIL；
   - 题目信息不足，或你确实无法确定 → UNCERTAIN。
3. 不要为了迎合考生而改判，也不要因为写法不同就判错；只看数学是否正确。
4. 如果工具返回“无法判定”，不要重复同一个调用，换一种等价写法再验证。
5. 证明题判 PASS 时，复核理由必须写清你实际检查过的关键推理环节
   （例如“由单调有界得到收敛”“最小值点满足 F=f，与 f≥0 矛盾”），
   不能只写“看起来正确”。

最后三行必须严格按下面的格式输出，每行一项，不要加粗、不要加列表符号：
复核结论：PASS
复核答案：<你独立得到的简洁答案；证明题写“得证”>
复核理由：<不超过 80 字，说明关键依据>
"""


_CONCLUSION_RE = re.compile(
    r"复核结论\s*[:：]\s*\**\s*(PASS|FAIL|UNCERTAIN|通过|失败|不确定)",
    re.IGNORECASE,
)
_ANSWER_RE = re.compile(r"复核答案\s*[:：]\s*\**\s*(.+)")
_REASON_RE = re.compile(r"复核理由\s*[:：]\s*\**\s*(.+)")

_CONCLUSION_MAP = {
    "pass": Verdict.PASS,
    "通过": Verdict.PASS,
    "fail": Verdict.FAIL,
    "失败": Verdict.FAIL,
    "uncertain": Verdict.UNCERTAIN,
    "不确定": Verdict.UNCERTAIN,
}


@dataclass
class Review:
    """复核器的一次结论。"""

    verdict: Verdict
    answer: str | None = None
    reason: str = ""
    raw: str = ""


def parse_review(text: str | None) -> Review:
    """从复核器输出里抽取结论、独立答案和理由。

    格式不对时不猜：返回 UNCERTAIN，调用方会维持首轮判定。
    """
    raw = text or ""
    conclusion = None
    for match in _CONCLUSION_RE.finditer(raw):
        conclusion = match.group(1)
    if conclusion is None:
        return Review(Verdict.UNCERTAIN, reason="复核器未按格式给出结论", raw=raw)

    def last_line(pattern: re.Pattern) -> str | None:
        found = pattern.findall(raw)
        return found[-1].strip() if found else None

    answer = last_line(_ANSWER_RE)
    reason = last_line(_REASON_RE) or ""
    # 去掉模型偶尔多写的 Markdown 标记和句末标点，避免干扰后面的答案比较
    if answer:
        answer = answer.strip().strip("*_`。；; ")
    reason = reason.strip().strip("*_`")
    return Review(
        verdict=_CONCLUSION_MAP[conclusion.lower()],
        answer=answer or None,
        reason=reason,
        raw=raw,
    )


_CHOICE_LETTER_RE = re.compile(
    r"^\s*(?:选项|选择|选)?\s*([A-Da-d])(?![A-Za-z])"
)


def _choice_letter(text: str) -> str | None:
    """从“B（I2<I1<I3）”“I2<I1<I3，选 B”这类文本里取出选项字母。"""
    cleaned = text.strip().strip("$")
    match = _CHOICE_LETTER_RE.match(cleaned)
    if match:
        return match.group(1).upper()

    # 字母不在开头时，只认“选 B”“选项 B”“答案为 B”这种明确表述，
    # 避免把正文里随便出现的 A/B/C/D 当成答案。
    match = re.search(
        r"(?:选|选项|答案(?:应)?为|应选)\s*[:：]?\s*([A-Da-d])(?![A-Za-z])",
        cleaned,
    )
    return match.group(1).upper() if match else None


def judge_candidate(case: dict, text: str | None) -> Verdict:
    """用评测集自带的判分方式，判断一段回答是否命中标准答案。"""
    if not text:
        return Verdict.UNCERTAIN

    if case.get("parts"):
        extracted = extract_final_answer(text, strip_assignment=False)
        return judge_parts(case["parts"], extracted or text)
    if case.get("judge") == "rubric":
        return judge_rubric(case["rubric"], text)

    # 选择题：复核器常写成“B（理由……）”，开头字母就是答案。
    official = str(case.get("answer") or "").strip()
    if case.get("judge") == "exact" and re.fullmatch(r"[A-Da-d]", official):
        letter = _choice_letter(text)
        if letter:
            return (
                Verdict.PASS if letter == official.upper() else Verdict.FAIL
            )

    extracted = extract_final_answer(text)
    return judge_answer(case["answer"], extracted or text, case["judge"])


class Verifier:
    """独立复核器。内部是一个不带检索、步数更少的 Agent。"""

    def __init__(
        self,
        client,
        max_steps: int = 20,
        step_timeout: float = 90.0,
    ) -> None:
        # 复核器只处理失败的题，题目本来就偏难。实测 2024-C6 需要 20 轮
        # 才能绕开无效的符号验证并给出选项；6 轮会直接撞上限、拿不到结论。
        self.agent = Agent(
            client=client,
            max_steps=max_steps,
            step_timeout=step_timeout,
            system_prompt=VERIFIER_PROMPT,
        )

    def review(self, statement: str, candidate: str) -> Review:
        """独立求解并复核考生答案。"""
        prompt = (
            f"题目：\n{statement}\n\n"
            f"考生答案：\n{candidate}\n\n"
            "请按复核规则独立求解，并判断考生答案是否正确。"
        )
        text = self.agent.run(prompt)
        return parse_review(text)


@dataclass
class ResolveOutcome:
    """首轮未通过后，复核 + 重解的最终结果。"""

    verdict: Verdict
    note: str = ""
    review: Review | None = None
    second_answer: str | None = None
    manual_review: bool = False  # 证明题的复核放行只是“建议通过”，需人工确认


_RE_SOLVE_PROMPT = """题目：
{statement}

你之前的答案：
{candidate}

独立复核意见（{conclusion}）：{reason}

请抛开上面的答案，从头独立重新求解。不要假设原答案正确，也不要只改动
原答案中的某一步；按标准格式给出完整解答。"""


def resolve_failure(
    verifier: Verifier,
    agent,
    case: dict,
    candidate: str,
    first_verdict: Verdict,
) -> ResolveOutcome:
    """复核一道首轮未通过的题，必要时带复核意见重解一次。

    Args:
        verifier: 独立复核器
        agent: 主求解器，用于反馈重解
        case: 评测用例
        candidate: 首轮完整回答
        first_verdict: 首轮判定结果

    Returns:
        ResolveOutcome: 最终判定与说明。
    """
    review = verifier.review(case["statement"], candidate)

    if case.get("judge") == "rubric":
        # 证明题没有可比的数值答案，rubric 又只能查关键词；
        # 复核器认可论证即可通过，这是 rubric 误杀的主要出口。
        if review.verdict is Verdict.PASS:
            # 证明题没有可机械核对的答案，复核器本身也可能看走眼。
            # 因此这里只给“建议通过”，最终以人工抽查为准，不悄悄计入严格分。
            return ResolveOutcome(
                Verdict.PASS,
                note=f"复核建议通过（待人工确认）：{review.reason}",
                review=review,
                manual_review=True,
            )
    # 其余题型：复核器自己算出的答案命中标准答案，才算最硬的证据。
    elif review.answer and judge_candidate(case, review.answer) is Verdict.PASS:
        return ResolveOutcome(
            Verdict.PASS,
            note=f"复核独立答案命中标准答案：{review.reason}",
            review=review,
        )

    # 复核没有确认错误：维持首轮判定，避免把不确定当成通过。
    if review.verdict is not Verdict.FAIL:
        return ResolveOutcome(
            first_verdict,
            note=f"复核未确认错误（{review.verdict.value}）：{review.reason}",
            review=review,
        )

    # 复核确认错误：把意见反馈给主 Agent，限一次重解。
    second = agent.run(_RE_SOLVE_PROMPT.format(
        statement=case["statement"],
        candidate=candidate,
        conclusion=review.verdict.value,
        reason=review.reason or "答案不正确",
    ))
    second_verdict = judge_candidate(case, second)
    if second_verdict is Verdict.PASS:
        return ResolveOutcome(
            Verdict.PASS,
            note=f"复核指出错误后重解通过：{review.reason}",
            review=review,
            second_answer=second,
        )
    return ResolveOutcome(
        Verdict.FAIL,
        note=f"复核与重解均未通过：{review.reason}",
        review=review,
        second_answer=second,
    )
