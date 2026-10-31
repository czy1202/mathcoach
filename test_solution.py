"""solution.py 的步骤解析测试。"""

from solution import extract_solution_steps


def test_extract_three_sections_and_steps():
    answer = """## 涉及的知识点

- 分部积分
- 极限

## 解题思路

先化简，再换元。

## 分步解题过程

### 第一步：化简被积函数

由 $e^x$ 的展开式可得。

### 第二步：换元并合并积分

令 $u=x^2$，则 $du=2x\\,dx$。

### 第三步：计算定积分

代入上下限。

## 最终答案

最终答案：$\\frac{1}{2}$
"""

    parsed = extract_solution_steps(answer)

    assert parsed is not None
    assert "分部积分" in parsed["intro"]
    assert "分步解题过程" in parsed["intro"]
    assert [step["index"] for step in parsed["steps"]] == [1, 2, 3]
    assert [step["label"] for step in parsed["steps"]] == [
        "第一步", "第二步", "第三步",
    ]
    assert [step["title"] for step in parsed["steps"]] == [
        "化简被积函数", "换元并合并积分", "计算定积分",
    ]
    assert "令 $u=x^2$" in parsed["steps"][1]["markdown"]
    assert "## 最终答案" not in parsed["steps"][-1]["markdown"]
    assert "## 最终答案" in parsed["outro"]
    assert "最终答案：" in parsed["outro"]


def test_extract_arabic_numbers_and_bold_headings():
    answer = """**第1步：求导**

$y' = 2x\\sin x + x^2\\cos x$。

**第2步：代入**

所以结果为 $0$。
"""

    parsed = extract_solution_steps(answer)

    assert parsed is not None
    assert len(parsed["steps"]) == 2
    assert parsed["steps"][0]["label"] == "第1步"
    assert parsed["steps"][0]["title"] == "求导"
    assert "2x" in parsed["steps"][0]["markdown"]
    assert parsed["steps"][1]["title"] == "代入"
    assert parsed["intro"] == ""
    assert parsed["outro"] == ""


def test_extract_returns_none_without_steps():
    assert extract_solution_steps("## 解题过程\n\n直接计算即可。") is None
    assert extract_solution_steps("") is None
    assert extract_solution_steps(None) is None


def test_extract_ignores_step_like_text_in_code_fence():
    answer = """### 第一步：真正的一步

```text
### 第二步：代码里的假标题
```

### 第二步：真正的第二步

继续计算。
"""

    parsed = extract_solution_steps(answer)

    assert parsed is not None
    assert len(parsed["steps"]) == 2
    assert "代码里的假标题" in parsed["steps"][0]["markdown"]
    assert parsed["steps"][1]["title"] == "真正的第二步"


def test_extract_renumbers_by_order_when_model_skips_numbers():
    answer = """### 第一步：第一次

内容一。

### 第三步：模型写错了编号

内容二。
"""

    parsed = extract_solution_steps(answer)

    assert parsed is not None
    assert [step["index"] for step in parsed["steps"]] == [1, 2]
    assert [step["label"] for step in parsed["steps"]] == ["第一步", "第三步"]


def test_last_step_stops_at_same_level_section_heading():
    answer = """### 第一步：计算

内容。

## 最终答案

最终答案：$1$
"""

    parsed = extract_solution_steps(answer)

    assert parsed is not None
    assert parsed["steps"][0]["markdown"] == "内容。"
    assert parsed["outro"].startswith("## 最终答案")
