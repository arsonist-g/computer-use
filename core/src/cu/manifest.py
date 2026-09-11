"""会话工件的序列化模型（data-model.md §3.3）。

`session.json` 是**清单缓存，不是真相来源**：
  - 「发生过什么」的真相是 `ops.md`；
  - 「存在哪些文件」的真相是文件系统本身。
不一致时以那两者为准；这个文件可由目录扫描重建（文件名已含 hwnd / 标题 / 坐标 / seq）。

因此这里的读写必须是**宽容的**：字段缺失用默认值补齐，未知字段忽略（前向兼容），
绝不因为清单里有个看不懂的字段就让整个会话打不开。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ids import format_hwnd

SCHEMA_VERSION = 1

SESSION_ACTIVE = "active"
SESSION_ENDED = "ended"
SESSION_ORPHANED = "orphaned"
SESSION_STATUSES = frozenset({SESSION_ACTIVE, SESSION_ENDED, SESSION_ORPHANED})

SCREENSHOT_KINDS = frozenset({"full", "window"})
STRUCTURED_KINDS = frozenset({"base", "ai"})


def _swap(left: Any, right: Any) -> Any:
    """把右侧的值按左侧的类型/形状规整回来。清单可被手改，读的时候不能信。"""
    if left is None:
        return right
    if isinstance(left, bool):
        return bool(right)
    if isinstance(left, int):
        try:
            return int(right)
        except (TypeError, ValueError):
            return left
    if isinstance(left, str):
        return right if isinstance(right, str) else left
    if isinstance(left, list):
        return right if isinstance(right, list) else left
    if isinstance(left, dict):
        return right if isinstance(right, dict) else left
    return right


def _merge(template: Any, raw: Any) -> Any:
    """按模板的键集合从 `raw` 取值；模板里没有的键一律丢弃。"""
    if isinstance(template, dict):
        if not isinstance(raw, dict):
            return dict(template)
        return {k: _merge(v, raw.get(k, v)) for k, v in template.items()}
    if isinstance(template, list):
        if not isinstance(raw, list):
            return list(template)
        item = template[0] if template else None
        if item is None:
            return list(raw)
        return [_merge(item, entry) for entry in raw]
    return _swap(template, raw)


# ---------------------------------------------------------------------------
# 工件记录
# ---------------------------------------------------------------------------


@dataclass
class WindowRef:
    """某窗口在某一时刻的观测。

    刻意不是实体（data-model.md §1.2）：它没有独立生命周期，hwnd 会被系统复用。
    存的是「那一刻看到什么」这一快照事实，不建立引用完整性。
    """

    hwnd: str = ""
    pid: int = 0
    process: str = ""
    title: str = ""
    klass: str = ""
    rect: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    monitor: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd, "pid": self.pid, "process": self.process,
            "title": self.title, "class": self.klass, "rect": list(self.rect),
            "monitor": self.monitor,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> WindowRef:
        data = _merge(cls().to_dict(), raw)
        # `class` 是 Python 关键字，序列化时用 `class`、内存里用 `klass`。
        if isinstance(raw, dict) and "class" in raw:
            data["class"] = raw["class"] if isinstance(raw["class"], str) else ""
        ref = cls()
        ref.hwnd = data["hwnd"]
        ref.pid = data["pid"]
        ref.process = data["process"]
        ref.title = data["title"]
        ref.klass = data["class"]
        ref.rect = [int(v) for v in data["rect"][:4]] if len(data["rect"]) >= 4 else [0, 0, 0, 0]
        ref.monitor = data["monitor"]
        return ref

    @classmethod
    def of(cls, hwnd: int, pid: int, process: str, title: str, klass: str,
           rect: tuple[int, int, int, int] | list[int], monitor: int = 0) -> WindowRef:
        return cls(hwnd=format_hwnd(hwnd), pid=pid, process=process, title=title,
                   klass=klass, rect=list(rect), monitor=monitor)


@dataclass
class ScreenshotRecord:
    seq: int = 0
    kind: str = "window"
    file: str = ""
    format: str = "png"
    width: int = 0
    height: int = 0
    origin: list[int] = field(default_factory=lambda: [0, 0])
    layer: int = 0                       # 走了第几层降级（DEC-007），诊断用
    window: WindowRef | None = None      # 全屏截图无目标窗口

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "seq": self.seq, "kind": self.kind, "file": self.file,
            "format": self.format, "width": self.width, "height": self.height,
            "origin": list(self.origin), "layer": self.layer,
        }
        if self.window is not None:
            out["window"] = self.window.to_dict()
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> ScreenshotRecord:
        data = _merge(cls().to_dict(), raw)
        rec = cls(
            seq=data["seq"], kind=data["kind"] if data["kind"] in SCREENSHOT_KINDS else "window",
            file=data["file"], format=data["format"], width=data["width"],
            height=data["height"], origin=[int(v) for v in data["origin"][:2]] or [0, 0],
            layer=data["layer"],
        )
        if isinstance(raw, dict) and raw.get("window"):
            rec.window = WindowRef.from_dict(raw["window"])
        return rec


@dataclass
class StructuredRecord:
    seq: int = 0
    kind: str = "base"                   # base | ai（DEC-011）
    file: str = ""
    source_seq: int | None = None        # 来源截图的 seq；外部图片为 None
    source_image_path: str | None = None # 仅外部图片（DEC-025 的 --image 形态）
    element_count: int = 0
    model_name: str | None = None        # 仅 ai

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "seq": self.seq, "kind": self.kind, "file": self.file,
            "source_seq": self.source_seq, "source_image_path": self.source_image_path,
            "element_count": self.element_count,
        }
        if self.model_name is not None:
            out["model_name"] = self.model_name
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> StructuredRecord:
        data = _merge(cls().to_dict(), raw)
        seq = data["source_seq"]
        rec = cls(
            seq=data["seq"],
            kind=data["kind"] if data["kind"] in STRUCTURED_KINDS else "base",
            file=data["file"],
            source_seq=int(seq) if isinstance(seq, int) else None,
            source_image_path=data["source_image_path"],
            element_count=data["element_count"],
        )
        if isinstance(raw, dict) and isinstance(raw.get("model_name"), str):
            rec.model_name = raw["model_name"]
        return rec


@dataclass
class MonitorInfo:
    index: int = 0
    rect: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    primary: bool = False
    dpi: int = 96
    scale: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "rect": list(self.rect), "primary": self.primary,
                "dpi": self.dpi, "scale": round(float(self.scale), 4)}

    @classmethod
    def from_dict(cls, raw: Any) -> MonitorInfo:
        data = _merge(cls().to_dict(), raw)
        return cls(index=data["index"], rect=[int(v) for v in data["rect"][:4]] or [0, 0, 0, 0],
                   primary=data["primary"], dpi=data["dpi"], scale=float(data["scale"]))


@dataclass
class DisplayContext:
    """会话开始时观测到的显示器配置。会话级配置，不随每次截图变化。"""

    primary_index: int = 0
    monitors: list[MonitorInfo] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"primary_index": self.primary_index,
                "monitors": [m.to_dict() for m in self.monitors]}

    @classmethod
    def from_dict(cls, raw: Any) -> DisplayContext:
        if not isinstance(raw, dict):
            return cls()
        monitors = [MonitorInfo.from_dict(m) for m in raw.get("monitors", []) if isinstance(m, dict)]
        ctx = cls(primary_index=int(raw.get("primary_index", 0) or 0), monitors=monitors)
        return ctx


# ---------------------------------------------------------------------------
# 会话清单
# ---------------------------------------------------------------------------


@dataclass
class SessionManifest:
    """`session.json` 的完整形状（data-model.md §3.3）。"""

    session_id: str = ""
    created_at: str = ""
    ended_at: str | None = None
    status: str = SESSION_ACTIVE
    agent_hint: str | None = None
    display: DisplayContext = field(default_factory=DisplayContext)
    screenshots: list[ScreenshotRecord] = field(default_factory=list)
    structured: list[StructuredRecord] = field(default_factory=list)
    #: 会话内**全局单调递增**计数器，截图与结构化数据共用（data-model.md §3.2）。
    seq_counter: int = 0
    #: 操作日志条数。冗余字段，仅供快速判断规模，不参与正确性判断（§1.6）。
    op_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "agent_hint": self.agent_hint,
            "display": self.display.to_dict(),
            "screenshots": [s.to_dict() for s in self.screenshots],
            "structured": [s.to_dict() for s in self.structured],
            "seq_counter": self.seq_counter,
            "op_count": self.op_count,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> SessionManifest:
        if not isinstance(raw, dict):
            return cls()
        status = raw.get("status")
        # `status` 来自可被手改的 JSON，可能是 list/dict（不可哈希）——
        # 用 tuple 做成员测试而不是 `in frozenset`，否则这里抛 TypeError，
        # 整个会话就打不开了，与「清单读不坏会话」的设计相反。
        # 非法值一律按 orphaned 处理（data-model.md §3.6）：看不懂的状态意味着
        # 这个会话的记录不可信，宁可让 AI 重建会话。
        if not (isinstance(status, str) and status in SESSION_STATUSES):
            status = SESSION_ORPHANED
        manifest = cls(
            session_id=str(raw.get("session_id", "") or ""),
            created_at=str(raw.get("created_at", "") or ""),
            ended_at=raw.get("ended_at") if isinstance(raw.get("ended_at"), str) else None,
            status=status,
            agent_hint=raw.get("agent_hint") if isinstance(raw.get("agent_hint"), str) else None,
            display=DisplayContext.from_dict(raw.get("display")),
            seq_counter=int(raw.get("seq_counter", 0) or 0),
            op_count=int(raw.get("op_count", 0) or 0),
        )
        manifest.screenshots = [ScreenshotRecord.from_dict(s) for s in raw.get("screenshots", [])
                                if isinstance(s, dict)]
        manifest.structured = [StructuredRecord.from_dict(s) for s in raw.get("structured", [])
                               if isinstance(s, dict)]
        manifest._drop_dangling_source_seq()
        return manifest

    def _drop_dangling_source_seq(self) -> None:
        """`source_seq` 悬空则置 null（data-model.md §3.6 的读取时校验）。"""
        known = {s.seq for s in self.screenshots}
        for record in self.structured:
            if record.source_seq is not None and record.source_seq not in known:
                record.source_seq = None

    def next_seq(self) -> int:
        self.seq_counter += 1
        return self.seq_counter
