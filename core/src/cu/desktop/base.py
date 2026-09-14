"""桌面层的接口定义。

daemon 只依赖这里的形状，不直接碰 Win32 —— 这样桌面层可以在没有桌面的环境里
（CI、开发机）被替换掉，daemon 的路由与会话逻辑因此可被单元测试覆盖。

**绝不静默失败**（入口文档约束 4）：接口缺失时返回的是显式错误（`capture_failed` /
`omni_not_installed` / `window_not_found`），而不是空列表或零尺寸图片。
空结果会被调用方误读成「桌面上没有窗口」，那比报错危险得多。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..errors import CUError, ErrorCode


@dataclass(frozen=True)
class WindowIdentity:
    """写操作前置要比对的「这个 hwnd 应该是谁」（DEC-013 第 1 层的落地）。

    由 daemon 侧从会话记录里解析出来（只有它持有 `Sessions`）：该窗口**最近一次
    截图**记下的 pid 与窗口类，就是这次写操作要求它在场的身份。

    **没有记录就不传**（`None`）：无从比对，保守放行 ——
    见 `Daemon._expected_identity`。
    """

    pid: int | None = None
    klass: str | None = None


@dataclass
class WindowInfo:
    """一次窗口枚举的观测结果（api-contract.md §1.2 的字段契约，顺序即输出顺序）。"""

    hwnd: int
    title: str
    pid: int
    process: str
    rect: tuple[int, int, int, int]      # x, y, w, h（屏幕绝对坐标，物理像素）
    monitor: int
    is_foreground: bool
    is_minimized: bool
    elevated: bool
    klass: str = ""
    is_topmost: bool = False
    zorder: int = 0

    def to_dict(self, verbose: bool = False) -> dict[str, Any]:
        from ..ids import format_hwnd

        out: dict[str, Any] = {
            "hwnd": format_hwnd(self.hwnd),
            "title": self.title,
            "pid": self.pid,
            "process": self.process,
            "rect": list(self.rect),
            "monitor": self.monitor,
            "is_foreground": self.is_foreground,
            "is_minimized": self.is_minimized,
            "elevated": self.elevated,
        }
        if verbose:
            out["class"] = self.klass
            out["is_topmost"] = self.is_topmost
            out["zorder"] = self.zorder
        return out


@dataclass
class CaptureResult:
    """一次截图的结果。

    `origin` 是契约的一部分（api-contract.md §1.2）：窗口截图返回该窗口屏幕矩形的
    左上角，全屏截图返回 (0,0)。AI 据此换算点击坐标 `screen_x = origin_x + image_x`
    —— 这条直接落实 CONSTRAINT-003。

    spike 已实测：WGC 截出的图**包含标题栏**，即图像 (0,0) 精确对应
    `GetWindowRect()` 的 (left, top)，无偏移。
    """

    path: str
    origin: tuple[int, int]
    width: int
    height: int
    layer: str                  # 走了第几层降级：wgc / printwindow / dxgi / —
    kind: str = "window"        # window | full
    window: WindowInfo | None = None
    inline_base64: str | None = None
    #: 光标位置（屏幕绝对物理像素）。**默认就报**，不是可选：光标长什么样是图形的事
    #: （箭头 / 输入框里的工字梁），它在哪是坐标的事 —— 后者必须给准数。
    cursor: tuple[int, int] | None = None
    #: 光标是否落在**这张图覆盖的矩形**里。窗口截图时即「是否在该窗口内」。
    cursor_inside: bool | None = None
    #: 请求在图上画光标红框时，这一张的结果：`drawn` / `outside`（光标不在图内，
    #: 按约定不画）/ `unsupported`（这一层给不出能改写的帧缓冲）。没请求时为空串。
    cursor_marker: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.path,
            "origin": list(self.origin),
            "width": self.width,
            "height": self.height,
            "layer": self.layer,
        }
        if self.cursor is not None:
            out["cursor"] = list(self.cursor)
        if self.cursor_inside is not None:
            out["cursor_inside"] = self.cursor_inside
        if self.cursor_marker:
            out["cursor_marker"] = self.cursor_marker
        if self.inline_base64 is not None:
            out["base64"] = self.inline_base64
        return out


@dataclass
class ParseResult:
    path: str
    element_count: int
    model_name: str | None = None
    inline_markdown: str | None = None
    #: UIA 文本通道没参与的原因。**不是错误**：自绘/Electron 界面拿不到控件树
    #: 是应用的属性，那走检测器路径即可。有值时说明这次结果里没有 UIA 的精确文本。
    uia_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.path, "element_count": self.element_count}
        if self.model_name:
            out["model_name"] = self.model_name
        if self.inline_markdown is not None:
            out["markdown"] = self.inline_markdown
        if self.uia_reason:
            out["uia_note"] = self.uia_reason
        return out


@dataclass
class InputResult:
    ok: bool = True
    moved_ms: int = 0
    total_ms: int = 0
    warning: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": self.ok, "moved_ms": self.moved_ms, "total_ms": self.total_ms}
        if self.warning:
            out["warning"] = self.warning
        out.update(self.detail)
        return out


class Desktop(Protocol):
    """桌面层的形状。实现可以是真实 Win32，也可以是明确的「不可用」。"""

    def list_windows(self, all_windows: bool = False) -> list[WindowInfo]: ...

    def display_context(self): ...

    def capture(self, *, hwnd: int | None, monitor: int | None, image_format: str,
                out_dir, seq: int, draw_cursor: bool = False) -> CaptureResult: ...

    def parse(self, *, hwnd: int | None, image_path: str | None, ai: bool,
              out_dir, seq: int) -> ParseResult: ...

    # 写方法都接受一个可选的 `expect`：daemon 解析出的期望身份。桌面层**不自己
    # 推断**它是谁 —— 那张 pid/class 的底稿在会话记录里，只有 daemon 有。

    def click(self, x: int, y: int, *, button: str, count: int,
              hwnd: int | None = None, expect: WindowIdentity | None = None) -> InputResult: ...

    def move(self, x: int, y: int, *, hwnd: int | None = None,
             expect: WindowIdentity | None = None) -> InputResult: ...

    def drag(self, x1: int, y1: int, x2: int, y2: int, *, button: str,
             hwnd: int | None = None, expect: WindowIdentity | None = None) -> InputResult: ...

    def scroll(self, dx: int, dy: int, *, at: tuple[int, int] | None = None) -> InputResult: ...

    def type_text(self, text: str, *, hwnd: int | None = None,
                  expect: WindowIdentity | None = None) -> InputResult: ...

    def key(self, combo: str, *, hwnd: int | None = None, force: bool = False,
            expect: WindowIdentity | None = None) -> InputResult: ...


class UnavailableDesktop:
    """没有桌面时的实现：每个方法都**显式报错**。

    刻意不返回空列表或零尺寸图片 —— 「桌面上没有窗口」与「桌面层不可用」
    必须是两种不同的结果，否则调用方会把故障当成事实。
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def _fail(self, code: ErrorCode, what: str) -> CUError:
        return CUError(code, f"{what}不可用：{self.reason}")

    def list_windows(self, all_windows: bool = False) -> list[WindowInfo]:
        raise self._fail(ErrorCode.INTERNAL_ERROR, "窗口枚举")

    def display_context(self):
        from ..manifest import DisplayContext

        return DisplayContext()

    def capture(self, **_kwargs):
        raise self._fail(ErrorCode.CAPTURE_FAILED, "截图")

    def parse(self, **_kwargs):
        raise self._fail(ErrorCode.OMNI_NOT_INSTALLED, "结构化解析")

    def click(self, *_args, **_kwargs) -> InputResult:
        raise self._fail(ErrorCode.INTERNAL_ERROR, "鼠标输入")

    def move(self, *_args, **_kwargs) -> InputResult:
        raise self._fail(ErrorCode.INTERNAL_ERROR, "鼠标移动")

    def drag(self, *_args, **_kwargs) -> InputResult:
        raise self._fail(ErrorCode.INTERNAL_ERROR, "拖拽")

    def scroll(self, *_args, **_kwargs) -> InputResult:
        raise self._fail(ErrorCode.INTERNAL_ERROR, "滚轮")

    def type_text(self, *_args, **_kwargs) -> InputResult:
        raise self._fail(ErrorCode.INTERNAL_ERROR, "文本输入")

    def key(self, *_args, **_kwargs) -> InputResult:
        raise self._fail(ErrorCode.INTERNAL_ERROR, "按键")
