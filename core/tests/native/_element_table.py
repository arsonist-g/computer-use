"""读 `-omni-` md 的元素表 —— **按表头认列**，不按列位置。

为什么必须按表头：表头是这份文件的契约，列位置不是。按位置取的话，加一列
（例如 `enabled` 插在 `interactivity` 之后）会让每一处静默错位 —— 把 `enabled`
当成 `content`、把 `source` 当成 `content`，而症状是「诊断脚本给出一个看起来
正常的错结论」。这类错误不会报错，只会让人拿错数据去下判断。

本仓已经有四个脚本抄了同一段按位置取的代码，而它们正是用来判断「UIA 通道好不好用」
的仪器 —— 仪器本身读错列，比没有仪器更糟。

`bbox` 一律是 `[x1, y1, x2, y2]`（图像像素），两种来源同义。
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["read_element_table"]

#: 单元格分隔符。**必须跳过被转义的 `\|`** —— 写的那一侧（`worker._cell`）把内容里的
#: `|` 转义成 `\|` 才不会破坏表结构，而 `line.split("|")` 会把 `a\|b` 切成两格，
#: 于是整行宽度对不上、被当成坏行丢掉。窗口标题里带竖线并不罕见。
_SPLIT = re.compile(r"(?<!\\)\|")


def _cells(line: str) -> list[str]:
    """把一行表格拆成单元格：去掉首尾的空格、还原 `\\|`。"""
    parts = _SPLIT.split(line)
    return [p.strip().replace("\\|", "|") for p in parts[1:-1]]


def _bbox(text: str) -> tuple[int, int, int, int] | None:
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) != 4:
        return None
    try:
        x1, y1, x2, y2 = (int(p) for p in parts)
    except ValueError:
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)


def read_element_table(path: str | Path) -> list[dict]:
    """把一份 `-omni-` md 的元素表读成 dict 列表。

    每项：`id` / `type` / `bbox`（`(x1,y1,x2,y2)`）/ `interactivity` / `enabled`
    （`True` / `False` / `None` = 该列空着，即未知）/ `content` / `source`。

    **只返回框可用的行**：每个消费方要做的第一件事都是「框不合法就跳过」，
    把它放在这里，四个脚本就不用各写一遍（写四遍就是四处会各自漂移的地方）。
    表头认不出来、行宽对不上的行同样跳过 —— 这是诊断工具，宁可少几行，
    也不要把错位的值喂给上面。
    """
    header: list[str] | None = None
    rows: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.startswith("| "):
            continue
        cells = _cells(line)
        if not cells:
            continue
        if all(set(cell) <= {"-"} for cell in cells):
            continue                      # 表头下的分隔行
        if header is None:
            header = cells
            continue
        if len(cells) != len(header):
            continue
        row = dict(zip(header, cells, strict=True))
        box = _bbox(row.get("bbox", ""))
        if box is None:
            continue
        rows.append({
            "id": int(row["#"]) if row.get("#", "").isdigit() else None,
            "type": row.get("type", ""),
            "bbox": box,
            "interactivity": row.get("interactivity") == "y",
            "enabled": {"y": True, "n": False}.get(row.get("enabled", "")),
            "content": row.get("content", ""),
            "source": row.get("source", ""),
        })
    return rows
