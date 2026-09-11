"""JSON → markdown —— 结构化数据的落盘格式（DEC-015 / D8）。

**这个模块不得 import `cu` 包里的任何东西。** 它跑在 `venv-omni` 里，
与 base 环境**零代码共享**（架构 §1.5 第 3 条）：两边的关系只有管道与文件。

为什么用 markdown 而不是 JSON：检测器的原始输出是元素数组，每个元素带着
同一组键名（type / bbox / content / interactivity / source）。几十上百个元素
意味着那些键名重复几十上百遍，把 token 预算全花在了键名上。markdown 的表格
让键名只出现一次。

**但只换格式不裁信息**（DEC-015）：元素一个不删，坐标一位不少。
省下的是键名，不是内容。AI 需要精确坐标去点击，裁掉坐标等于把功能裁掉。
"""

from __future__ import annotations

from typing import Any

#: 元素表头。顺序即列序 —— 改它等于改契约。
COLUMNS = ("#", "type", "bbox", "interactivity", "content")


def _bbox_text(raw: Any) -> str:
    """`[x1, y1, x2, y2]` → `x1,y1,x2,y2`。

    逗号而不是空格：markdown 表格用 `|` 分隔，bbox 里再用空格会让「一个 bbox」
    在读表的人眼里变成四个单元格。逗号是紧凑且不会被误读的那个选择。
    """
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        return ",".join(str(int(v)) for v in raw)
    return ""


def _cell(text: Any) -> str:
    """把任意文本塞进一个表格单元格而不破坏表结构。"""
    out = str(text if text is not None else "")
    # `|` 会提前结束单元格，换行会结束整行 —— 两者都必须转义。
    return out.replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def _element_row(index: int, element: dict) -> str:
    return "| " + " | ".join([
        str(index),
        _cell(element.get("type")),
        _bbox_text(element.get("bbox")),
        _cell(_interactivity(element.get("interactivity"))),
        _cell(element.get("content")),
    ]) + " |"


def _interactivity(raw: Any) -> str:
    """可交互性是布尔；渲染成 `y` / `n` 而不是 `true` / `false`。

    这是 token 预算最直接的体现：一张表里出现 137 次 `false`，换成 `n` 省掉一半。
    语义不受影响，因为表头已经交代了这一列的含义。
    """
    if raw is None:
        return ""
    return "y" if raw else "n"


def to_markdown(payload: dict, *, source: str = "", model: str | None = None) -> str:
    """把一次解析的完整结果渲染成 markdown。

    `payload` 的形状是检测器的输出：`{"elements": [...]}`，可能额外带一个
    由多模态端点优化过的 `content` 字段（DEC-011）。
    """
    elements = payload.get("elements") or []
    lines: list[str] = []

    lines.append("# 界面元素解析")
    lines.append("")
    if source:
        lines.append(f"- source: {source}")
    if model:
        # 记下是哪个模型改写的：复盘时才知道该信任到什么程度（DEC-011）。
        lines.append(f"- description model: {model}")
    lines.append(f"- elements: {len(elements)}")
    lines.append("")
    lines.append(
        "> 坐标为图像像素，原点在图像左上角。"
        "换算到屏幕坐标需加上截图返回的 `origin`（DEC-001）。"
    )
    lines.append("")

    if not elements:
        lines.append("（未检测到元素）")
        return "\n".join(lines) + "\n"

    lines.append("| " + " | ".join(COLUMNS) + " |")
    lines.append("|" + "|".join(["---"] * len(COLUMNS)) + "|")
    for index, element in enumerate(elements, start=1):
        if not isinstance(element, dict):
            continue
        lines.append(_element_row(index, element))

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# AI 优化契约的校验（DEC-011）
# ---------------------------------------------------------------------------


class ContractViolation(Exception):
    """优化结果违反了冻结契约。"""


def validate_optimized(original: dict, optimized: dict) -> dict:
    """校验多模态端点返回的结果是否守约，守约则返回它，违约则抛错。

    契约（DEC-011）只有两条，但两条都是硬要求：

    1. **bbox 冻结**：优化只改描述，不改位置。模型的理解力比定位力强，
       让它碰坐标等于用它的弱项去覆盖检测器的强项。
    2. **不得新增元素**：模型不能凭空造出检测器没找到的元素，否则 AI 会去点
       一个不存在的东西。**允许减少** —— 模型可能识别出某个元素其实是噪声。

    违约时抛错而不是「尽力修正」：静默接受一份被改过坐标的结果，会让 AI
    点到错误的位置，而它没有任何办法察觉。
    """
    original_elements = original.get("elements") or []
    optimized_elements = optimized.get("elements")
    if not isinstance(optimized_elements, list):
        raise ContractViolation("优化结果的 elements 不是数组")
    if len(optimized_elements) > len(original_elements):
        raise ContractViolation(
            f"优化结果新增了元素：原 {len(original_elements)} 个，优化后 "
            f"{len(optimized_elements)} 个（契约禁止新增）"
        )

    # bbox 用「多重集合」比对而不是按下标：模型可能重排元素，但**集合**必须一致。
    # 按下标比对会把「顺序变了」误判成「坐标被改」。
    def bbox_multiset(elements: list) -> list:
        return sorted(
            tuple(int(v) for v in element.get("bbox", []))
            for element in elements
            if isinstance(element, dict) and len(element.get("bbox", [])) == 4
        )

    before = bbox_multiset(original_elements)
    after = bbox_multiset(optimized_elements)
    # 允许减少：优化后的 bbox 集合必须是原集合的子集。
    remaining = list(before)
    for box in after:
        if box not in remaining:
            raise ContractViolation(f"优化结果出现原始结果中没有的 bbox：{box}（契约冻结 bbox）")
        remaining.remove(box)

    return optimized


def merge_descriptions(original: dict, optimized: dict) -> dict:
    """把优化后的描述并回原始元素，**保留原始 bbox**。

    按 bbox 配对而不是按顺序：位置是检测器给出的事实，描述是模型给的判断，
    两者靠坐标对齐最可靠。
    """
    descriptions: dict[tuple, str] = {}
    for element in optimized.get("elements") or []:
        if not isinstance(element, dict):
            continue
        box = element.get("bbox")
        if isinstance(box, list) and len(box) == 4:
            descriptions[tuple(int(v) for v in box)] = str(element.get("content") or "")

    merged = []
    for element in original.get("elements") or []:
        if not isinstance(element, dict):
            continue
        item = dict(element)
        box = item.get("bbox")
        if isinstance(box, list) and len(box) == 4:
            better = descriptions.get(tuple(int(v) for v in box))
            if better:
                item["content"] = better
        merged.append(item)
    return {**original, "elements": merged}
