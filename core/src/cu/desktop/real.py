"""桌面层的真实实现 —— 把 win32 / windows / capture / input / controller 接起来。

daemon 只依赖 `base.Desktop` 的形状；这个类是实现那个形状的那一个。
每个方法要么返回真实结果，要么抛带 `error_code` 的 `CUError` —— 没有中间态。
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..errors import CUError, ErrorCode
from ..ids import artifact_name, parsed_name_for
from . import capture as capture_mod
from . import input as input_mod
from . import omni, uia
from . import windows as windows_mod
from .base import CaptureResult, InputResult, ParseResult, WindowIdentity, WindowInfo
from .controller import WriteSequenceController

#: 写操作前置的自动前台开关。DEC-013 记录了「前置是否可关闭」这一未决项 ——
#: 默认开启（安全优先），需要「就在当前前台窗口上点击」的场景可以关掉。
BRING_TO_FOREGROUND = True

#: 内联元素索引最多带多少条。这是**上限而不是目标** —— 正常界面上的可交互元素
#: 只有几十个，这个数只用来挡住「某个界面把整屏都标成可点」时把返回撑爆。
#: 触顶时返回 `elements_truncated`，被截掉的部分一条不少地留在 md 里。
MAX_INLINE_ELEMENTS = 100

#: 内联 `content` 的字符上限。检测器给出的描述偶尔是一整句，而内联要回答的是
#: 「这是什么东西」，完整描述在 md 里一个字没少。
INLINE_CONTENT_CHARS = 80


def _inline_elements(elements: list) -> tuple[list[dict], bool]:
    """把检测器产出的元素列表压成「可交互元素的紧凑索引」（DEC-094）。

    只取 `interactivity` 为真的元素，理由见 `ParseResult.elements` 的注释：
    模型读文字没问题，差的是把看到的东西落到精确坐标上，所以内联的是能点的那些。
    `id` 是它在 md 表里的行号，调用方据此回查完整记录。
    """
    picked: list[dict] = []
    truncated = False
    for index, element in enumerate(elements, start=1):
        if not isinstance(element, dict) or not element.get("interactivity"):
            continue
        box = element.get("bbox")
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        content = _clip(str(element.get("content") or ""))
        if not content:
            # 没有名字的进不了索引 —— 这份索引的用处就是「按名字挑目标」。
            # 检测器读不出名字的那些（`M0,0L9,0 4.5,` 这类，实测单张图最多 61 条）
            # 内容已被清空，它们的框仍完整留在 md 里，位置也能在图上看到。
            continue
        if len(picked) >= MAX_INLINE_ELEMENTS:
            truncated = True
            break
        picked.append({
            "id": index,
            "type": str(element.get("type") or ""),
            "bbox": [int(value) for value in box],
            "content": content,
        })
    return picked, truncated


def _clip(text: str) -> str:
    """折叠空白并截断 —— 描述里的换行会把一行内联拆成两行，读的人会以为多了一条。"""
    flat = " ".join(text.split())
    if len(flat) <= INLINE_CONTENT_CHARS:
        return flat
    return flat[:INLINE_CONTENT_CHARS - 1] + "…"


class RealDesktop:
    def __init__(self, config: Config, controller: WriteSequenceController | None = None) -> None:
        self.config = config
        self.controller = controller or WriteSequenceController()

    # ---- 只读 ----

    def list_windows(self, all_windows: bool = False) -> list[WindowInfo]:
        monitors = windows_mod.display_context().monitors
        return windows_mod.enumerate_windows(monitors, all_windows=all_windows)

    def display_context(self):
        return windows_mod.display_context()

    def capture(self, *, hwnd: int | None, monitor: int | None, image_format: str,
                out_dir: Path, seq: int, draw_cursor: bool = False) -> CaptureResult:
        if hwnd is None:
            return capture_mod.capture_full(
                monitor if monitor is not None else 0, out_dir, seq, image_format,
                draw_cursor=draw_cursor)
        # 窗口信息要写进清单（`Screenshot.window` 是复盘时的现场证据，DEC-006）。
        monitors = windows_mod.display_context().monitors
        info = _find_window(hwnd, monitors)
        if info is None:
            from ..ids import format_hwnd

            raise CUError(ErrorCode.WINDOW_NOT_FOUND,
                          f"窗口不存在：{format_hwnd(hwnd)}", {"hwnd": format_hwnd(hwnd)})
        restored = False
        if info.is_minimized:
            # 最小化的窗口没有可截的合成面，降级链必然全败（见 `restore_minimized`）。
            restored = windows_mod.restore_minimized(hwnd)
            # 还原之后**重新枚举一次**：`rect` 与 `monitor` 都变了，而 DXGI 那条路
            # 按 `monitor` 去显示器上裁 —— 拿还原前的值裁，裁到的是它原来的位置。
            info = _find_window(hwnd, monitors) or info
        # 红框与坐标换算都留在 capture_window 里做：窗口原点是它算出来的
        # （DWM 扩展框），在外面二次换算等于把那条已经踩过的偏移再踩一遍。
        result = capture_mod.capture_window(hwnd, out_dir, seq, info, image_format,
                                            draw_cursor=draw_cursor)
        # 只读命令动了用户的窗口，必须让它出现在返回值里（见 `restore_minimized`）。
        result.window_restored = restored
        return result

    def parse(self, *, hwnd: int | None, image_path: str | None, ai: bool,
              out_dir: Path, seq: int) -> ParseResult:
        """把一张图解析成结构化数据。

        OmniParser 跑在独立环境（DEC-037），与 base **零代码共享** —— 那条通道由
        `omni.py` 以「拉起子进程 + NDJSON」的方式提供，base 这边不 import 它任何东西。
        未安装时**显式报错**，不静默降级（DEC-002）。

        `--hwnd` 形态要先自己截一张图：OmniParser 只吃图片路径，不接受窗口句柄。
        这也是 DEC-025 把 `--hwnd` 与 `--image` 并列的原因 —— 前者是我们的能力，
        后者是解析器真正需要的东西。
        """
        if image_path:
            return self._parse_image(Path(image_path), ai=ai, out_dir=out_dir, seq=seq)
        if hwnd is None:
            raise CUError(ErrorCode.INVALID_PARAMS, "必须提供 hwnd 或 image 之一")
        shot = self.capture(hwnd=hwnd, monitor=None,
                            image_format=self.config.image_format,
                            out_dir=out_dir, seq=seq)
        return self.parse_captured(shot=shot, ai=ai, out_dir=out_dir, seq=seq)

    def parse_captured(self, *, shot: CaptureResult, ai: bool,
                       out_dir: Path, seq: int) -> ParseResult:
        """解析一张**已经截好**的图（DEC-094）。

        与 `parse(hwnd=…)` 的差别只有一点：不重新截图。截图后的自动解析必须解析
        「刚刚返回给调用方的那一张」—— 重新截一次拿到的是另一帧，调用方手里的图
        和元素清单就对不上了，而它们本来要靠同一套坐标去点。
        """
        return self._parse_image(Path(shot.path), ai=ai, out_dir=out_dir, seq=seq,
                                 source_shot=shot)

    def _parse_image(self, source: Path, *, ai: bool, out_dir: Path, seq: int,
                     source_shot: CaptureResult | None = None) -> ParseResult:
        window_ref = source_shot.window if source_shot is not None else None
        origin = source_shot.origin if source_shot is not None else None
        uia_elements: list[dict] = []
        uia_note = ""
        if window_ref is not None and origin is not None:
            # UIA 文本通道（可选增强）。在 base 读、把结果交给 worker 合并 ——
            # UIA 是 Win32 调用，属于这一侧；worker 只管「图片 → 结构化数据」。
            # **拿不到不算失败**：Electron/自绘界面本来就不暴露控件树，
            # 那走原来的检测器路径即可（CONSTRAINT-005）。
            uia_result = uia.read_window(window_ref.hwnd)
            if uia_result.usable:
                # UIA 给的是**屏幕坐标**，检测器给的是**图像坐标**。先减掉 origin
                # 归一化，否则合并时两个坐标系对不上，表现是「UIA 好像没生效」。
                #
                # **四个角都要减**：`bbox` 是角对角（`UiaElement.to_dict` 保证），
                # 只减左上角会让右下角留在屏幕坐标里 —— 一个 32x32 的按钮于是变成
                # 横跨半个屏幕的一条（实测 `后退` 成了 1178x363）。
                uia_elements = [element.to_dict() for element in uia_result.elements]
                for element in uia_elements:
                    box = element["bbox"]
                    box[0] -= origin[0]
                    box[1] -= origin[1]
                    box[2] -= origin[0]
                    box[3] -= origin[1]
            else:
                uia_note = uia_result.reason

        suffix = "omni_ai" if ai else "omni"
        if source_shot is not None:
            # 与来源图成对命名。从**图片文件名**推而不是把 kind/hwnd/title/origin
            # 再拼一遍：解析的输入就是那张图，名字必须成对（见 `parsed_name_for`）。
            name = parsed_name_for(Path(source_shot.path).name, suffix)
        else:
            # 外部图片（DEC-025 的 `--image` 形态）：没有我们自己定的来源图名，
            # 因此没有坐标段可挂，走更短的模式。
            name = artifact_name("img", seq, suffix=suffix, ext="md")

        result = omni.call_parse(
            image_path=str(source), out_dir=out_dir, file_name=name, ai=ai,
            vlm={"base_url": self.config.vlm.base_url,
                 "api_key": self.config.vlm.api_key,
                 "model_name": self.config.vlm.model_name,
                 "user_agent": self.config.vlm.user_agent},
            extra_elements=uia_elements or None,
        )
        elements, truncated = _inline_elements(result.get("elements") or [])
        return ParseResult(
            path=str(result.get("path") or (out_dir / name)),
            element_count=int(result.get("element_count") or 0),
            model_name=result.get("model_name"),
            uia_reason=uia_note,
            elements=elements,
            elements_truncated=truncated,
        )

    # ---- 写 ----

    def click(self, x: int, y: int, *, button: str = "left", count: int = 1,
              hwnd: int | None = None, expect: WindowIdentity | None = None) -> InputResult:
        self._preflight(hwnd, expect)
        return input_mod.click(x, y, button=button, count=count,
                               step_ms=self.config.mouse_step_ms,
                               max_points=self.config.mouse_max_points)

    def move(self, x: int, y: int, *, hwnd: int | None = None,
             expect: WindowIdentity | None = None) -> InputResult:
        # 只移光标**不抢前台**：悬停响应看光标落在哪个窗口上，与前台无关；
        # 而移动光标本身不该把用户手上的窗口顶下去。
        self._preflight(hwnd, expect, foreground=False)
        moved = input_mod.move_cursor(x, y, step_ms=self.config.mouse_step_ms,
                                      max_points=self.config.mouse_max_points)
        return InputResult(ok=True, moved_ms=moved, total_ms=moved,
                           detail=input_mod.cursor_snapshot())

    def drag(self, x1: int, y1: int, x2: int, y2: int, *, button: str = "left",
             hwnd: int | None = None, expect: WindowIdentity | None = None) -> InputResult:
        self._preflight(hwnd, expect)
        return input_mod.drag(x1, y1, x2, y2, button=button,
                              step_ms=self.config.mouse_step_ms,
                              max_points=self.config.mouse_max_points)

    def scroll(self, dx: int, dy: int, *, at: tuple[int, int] | None = None) -> InputResult:
        return input_mod.scroll(dx, dy, at=at)

    def type_text(self, text: str, *, hwnd: int | None = None,
                  expect: WindowIdentity | None = None) -> InputResult:
        self._preflight(hwnd, expect)
        return input_mod.type_text(text)

    def key(self, combo: str, *, hwnd: int | None = None, force: bool = False,
            expect: WindowIdentity | None = None) -> InputResult:
        # 按键的落点是**发键那一刻的前台窗口**，所以带 `--hwnd` 时必须先抢前台：
        # 不抢就等于把 `ctrl+a` / `delete` 投给用户手上那个窗口（api-contract.md §1.3，
        # `--hwnd` 用于写操作前置校验）。
        self._preflight(hwnd, expect)
        return input_mod.key(combo, force=force,
                             danger_keys=frozenset(self.config.danger_keys))

    # ---- 前置校验（DEC-013）----

    def _preflight(self, hwnd: int | None, expect: WindowIdentity | None = None,
                   foreground: bool = True) -> None:
        """写操作的三层前置：身份校验 → 自动前台 → （漂移检查在 daemon 侧）。

        没有 hwnd 时跳过 —— 契约允许「就在当前前台窗口上操作」（api-contract.md
        §1.3：`--hwnd` 用于前置校验，**不改变坐标语义**）。

        `expect` 是 daemon 从会话记录里解析出来的期望身份（Q-024），**只在这里消费**：
        比对基准必须来自「我们此前看到的那个窗口」，而不是调用方现编的参数 ——
        否则这条检查挡不住 hwnd 复用，也就发不出 `window_stale`。
        """
        if hwnd is None:
            return
        expect_pid, expect_class = _expectation(expect)
        windows_mod.check_hwnd(hwnd, expect_pid=expect_pid, expect_class=expect_class)
        if foreground and BRING_TO_FOREGROUND:
            windows_mod.bring_to_foreground(hwnd)


def _find_window(hwnd: int, monitors) -> WindowInfo | None:
    """这一次枚举里该 hwnd 的窗口信息（不在就 None）。

    抽出来是因为 `capture` 要枚举两次：还原最小化窗口之后必须重来一次，
    `rect` 与 `monitor` 都变了。两处用同一个函数，才不会出现「第二次忘了清缓存」
    这种只在一半路径上错的问题。
    """
    return next((w for w in windows_mod.enumerate_windows(monitors) if w.hwnd == hwnd), None)


def _expectation(expect: WindowIdentity | None) -> tuple[int | None, str | None]:
    """把期望身份拆成 `check_hwnd` 的两个参数，并把空值当成「不知道」。

    清单里的 `pid` / `class` 可能是 `0` / `""`（早期记录，或当时取不到窗口类）。
    把它们当成「要求 pid == 0」会让一次合法点击被拒 —— 有记录却没法比对，
    与没有记录等价，都应当放行（约束：`window_stale` 只在真有可比对的身份时才判）。
    """
    if expect is None:
        return None, None
    return (expect.pid or None), (expect.klass or None)


def captured_name(kind: str, seq: int, **kwargs) -> str:
    """留给调用方复用的命名入口（与 capture 内部保持同一个函数）。"""
    return artifact_name(kind, seq, **kwargs)
