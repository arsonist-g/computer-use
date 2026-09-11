"""操作日志 `ops.md` —— 会话的**真相来源**（data-model.md §3.4 / DEC-006）。

它同时服务两个消费者：人/AI 通读，以及结构化提取。因此锚行格式是固定的：

    ## {entry_no:04d} · {HH:MM:SS} · {command}

其余 `- key: value` 行给语义。时间用**本地时间精确到秒** —— AI 与人都按墙上时间思考，
epoch 整数会逼每次读取都做心算换算。排序不依赖这个时间（`entry_no` 已经有序）。

这个文件只追加、不改写。追加中断的最坏后果是最后一行不完整，
解析方跳过无法解析的尾行即可，其余完好（data-model.md §3.8）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OPS_FILENAME = "ops.md"


@dataclass
class OpsEntry:
    """一条命令记录。字段集是 DEC-006 的落地：必须有**结果**，不只是「命令 + 时间」。"""

    entry_no: int
    at: str                     # 本地时间 HH:MM:SS
    command: str                # 如 `click 850 420 --hwnd 0x0001A2B`
    describe: str = ""
    result: str = "ok"          # ok | error
    error_code: str | None = None
    detail: str | None = None
    artifact: str | None = None # 截图 / 结构化数据路径（含 origin/尺寸等摘要）
    target: str | None = None   # 目标窗口摘要
    extra: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        # 锚行：结构化提取只认这一行。
        lines = [f"## {self.entry_no:04d} · {self.at} · {self.command}"]
        if self.describe:
            lines.append(f"- describe: {self.describe}")
        if self.result == "ok":
            lines.append("- result: ok")
        else:
            code = self.error_code or "internal_error"
            lines.append(f"- result: error · {code}")
        if self.target:
            lines.append(f"- target: {self.target}")
        if self.artifact:
            lines.append(f"- artifact: {self.artifact}")
        if self.detail:
            lines.append(f"- detail: {self.detail}")
        for key, value in self.extra.items():
            lines.append(f"- {key}: {value}")
        return "\n".join(lines) + "\n"


class OpsLog:
    """一个会话一个 `ops.md`。写入是即时的 —— 日志丢了，这一轮操作就无法复盘。"""

    def __init__(self, session_dir: Path) -> None:
        self.path = session_dir / OPS_FILENAME

    def write_header(self, session_id: str, started: str, agent_hint: str | None,
                     display_summary: str) -> None:
        lines = [f"# Session {session_id}", "", f"- started: {started}"]
        if agent_hint:
            lines.append(f"- agent: {agent_hint}")
        lines.append(f"- display: {display_summary}")
        self._append("\n".join(lines) + "\n")

    def append(self, entry: OpsEntry) -> None:
        self._append("\n" + entry.render())

    def _append(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()


def parse_entries(text: str) -> list[dict[str, Any]]:
    """按锚行把 `ops.md` 切开。无法解析的尾行整段跳过（§3.8）。

    这是给复盘与测试用的解析器，不是产品热路径 —— 但它定义了锚行契约，
    所以必须和 `OpsEntry.render` 对照着看。
    """
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                entries.append(current)
            head = line[3:]
            parts = [p.strip() for p in head.split("·")]
            current = {
                "entry_no": int(parts[0]) if parts and parts[0].isdigit() else None,
                "at": parts[1] if len(parts) > 1 else "",
                "command": parts[2] if len(parts) > 2 else "",
                "fields": {},
            }
        elif current is not None and line.startswith("- "):
            key, _, value = line[2:].partition(":")
            current["fields"][key.strip()] = value.strip()
    if current is not None:
        entries.append(current)
    return entries
