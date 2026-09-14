"""`screenshot` 的光标读数与红框标记（`--cursor`）。

契约出处（本文件唯一的行为权威）：
- `core/src/cu/desktop/capture.py` 中 `marker_box` / `paint_marker` / `_mark_cursor` /
  `_finish` 的 docstring 与 `_MARKER_*` 常量；
- `core/src/cu/desktop/base.py` 中 `CaptureResult` 的 `cursor` / `cursor_inside` /
  `cursor_marker` 三个字段注释；
- `core/src/cu/client.py` 中 `screenshot` 的 `--cursor` 与 `render_text` 的截图分支。

两条主线：

1. 光标**在哪**默认就报，与任何参数无关。光标长什么样是图形的事（箭头、输入框里的
   工字梁），它在哪是坐标的事 —— 解析器认不出嵌在版式里的光标，只有坐标能给准数。
2. 红框**默认不画**，只有显式请求才画，而且**光标不在图里就绝不画**。先按原点平移、
   再判是否在场，会把一个图外的光标挪进图里，画出一个并不存在的光标：图是对的，
   框是凭空多出来的 —— 这正是本功能最像「对」的那种错。

**本文件不碰光标、不碰屏幕**：`win32.cursor_pos` / `cursor_size` / `window_rect` /
`extended_frame_bounds` 与两层捕获入口全部被替身接管。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _daemon_shell import FakeDesktop, shell
from cu import client
from cu.desktop import capture as capture_mod
from cu.desktop.base import CaptureResult, WindowInfo

#: 真机实测的一组数：窗口的 DWM 扩展框原点与图像尺寸，光标当时落在窗口左侧。
ORIGIN = (1605, 80)
WIDTH, HEIGHT = 1685, 1210
CURSOR_SIZE = (32, 32)


class _Buffer:
    """帧缓冲的替身：只记切片赋值。真实那个是零拷贝的 ndarray，切片语义一致。"""

    def __init__(self) -> None:
        self.writes: list[tuple] = []

    def __setitem__(self, index, value) -> None:  # noqa: ANN001 - 替身
        self.writes.append((index, value))


class _Frame:
    """一帧的替身。`to_numpy_raises` 用来演「这一层给不出可改写的缓冲」。"""

    def __init__(self, width: int = WIDTH, height: int = HEIGHT, *, buffer=None,
                 to_numpy_raises: bool = False) -> None:
        self.width = width
        self.height = height
        self.frame_buffer = buffer
        self._to_numpy_raises = to_numpy_raises

    def to_numpy(self):
        if self._to_numpy_raises:
            raise RuntimeError("这一层没有可改写的帧缓冲")
        return _Buffer()


def _shot() -> CaptureResult:
    return CaptureResult(path="shot.png", origin=ORIGIN, width=WIDTH, height=HEIGHT, layer="wgc")


# ---------------------------------------------------------------------------
# marker_box：屏幕坐标 → 图像坐标的**唯一**换算处
# ---------------------------------------------------------------------------


def test_the_cursor_outside_the_window_gets_no_box() -> None:
    """窗口截图时这就是「光标不在该窗口内」—— 图里不能有红框。"""
    assert capture_mod.marker_box((1500, 700), ORIGIN, WIDTH, HEIGHT, CURSOR_SIZE) is None


@pytest.mark.parametrize("cursor", [
    (100, 700),                    # 远在窗口左侧：按原样平移就会把假光标画进图里
    (ORIGIN[0] - 1, 700),          # 左边界外一格
    (1600, ORIGIN[1] - 1),         # 上边界外一格
    (ORIGIN[0] + WIDTH, 700),      # 右边界外（右下为开区间）
    (2400, ORIGIN[1] + HEIGHT),    # 下边界外
    (5000, 4000),                  # 象限之外
])
def test_no_box_comes_from_any_direction_outside(cursor: tuple[int, int]) -> None:
    assert capture_mod.marker_box(cursor, ORIGIN, WIDTH, HEIGHT, CURSOR_SIZE) is None


def test_the_image_rectangle_is_half_open() -> None:
    """(0,0) 与 (w-1,h-1) 都在图内，(w,h) 已经出去了。"""
    assert capture_mod.marker_box(ORIGIN, ORIGIN, WIDTH, HEIGHT, CURSOR_SIZE) is not None
    assert capture_mod.marker_box((ORIGIN[0] + WIDTH - 1, ORIGIN[1] + HEIGHT - 1),
                                  ORIGIN, WIDTH, HEIGHT, CURSOR_SIZE) is not None
    assert capture_mod.marker_box((ORIGIN[0] + WIDTH, ORIGIN[1] + HEIGHT - 1),
                                  ORIGIN, WIDTH, HEIGHT, CURSOR_SIZE) is None
    assert capture_mod.marker_box((ORIGIN[0] + WIDTH - 1, ORIGIN[1] + HEIGHT),
                                  ORIGIN, WIDTH, HEIGHT, CURSOR_SIZE) is None


def test_the_box_is_centred_on_the_cursor_in_image_coordinates() -> None:
    assert capture_mod.marker_box((2400, 900), ORIGIN, WIDTH, HEIGHT, CURSOR_SIZE) \
        == (779, 804, 812, 837)


def test_the_box_follows_the_system_cursor_size() -> None:
    """「红框 = 鼠标大小」是口径，所以尺寸来自系统设置，不是写死的常数。"""
    assert capture_mod.marker_box((2400, 900), ORIGIN, WIDTH, HEIGHT, (16, 16)) \
        == (787, 812, 804, 829)


def test_the_box_is_clamped_to_the_image_bounds() -> None:
    """光标贴在图像角上：框要被夹进图内，不能越界写出。"""
    assert capture_mod.marker_box(ORIGIN, ORIGIN, WIDTH, HEIGHT, CURSOR_SIZE) == (0, 0, 17, 17)
    assert capture_mod.marker_box((ORIGIN[0] + WIDTH - 1, ORIGIN[1] + HEIGHT - 1),
                                  ORIGIN, WIDTH, HEIGHT, CURSOR_SIZE) \
        == (1668, 1193, WIDTH, HEIGHT)


# ---------------------------------------------------------------------------
# paint_marker：空心红框
# ---------------------------------------------------------------------------


def test_the_marker_colour_is_red_in_the_buffers_bgra_layout() -> None:
    """帧缓冲是 BGRA。红色是 (0, 0, 255, 255)；写成 (255, 0, 0) 会画成蓝色。"""
    assert capture_mod._MARKER_COLOR == (0, 0, 255, 255)


def test_paint_marker_draws_a_hollow_box() -> None:
    buffer = _Buffer()

    capture_mod.paint_marker(buffer, (10, 20, 30, 40))

    assert [index for index, _ in buffer.writes] == [
        (slice(20, 22), slice(10, 30)),     # 上边
        (slice(38, 40), slice(10, 30)),     # 下边
        (slice(20, 40), slice(10, 12)),     # 左边
        (slice(20, 40), slice(28, 30)),     # 右边
    ]
    assert {value for _, value in buffer.writes} == {capture_mod._MARKER_COLOR}


# ---------------------------------------------------------------------------
# _mark_cursor：这一层画成了没有
# ---------------------------------------------------------------------------


def test_mark_cursor_leaves_an_outside_frame_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = _Buffer()
    monkeypatch.setattr(capture_mod.w, "cursor_pos", lambda: (1500, 700))

    assert capture_mod._mark_cursor(_Frame(buffer=buffer), ORIGIN, CURSOR_SIZE) == "outside"
    assert buffer.writes == []


def test_mark_cursor_draws_when_the_cursor_is_inside(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = _Buffer()
    monkeypatch.setattr(capture_mod.w, "cursor_pos", lambda: (2400, 900))

    assert capture_mod._mark_cursor(_Frame(buffer=buffer), ORIGIN, CURSOR_SIZE) == "drawn"
    assert len(buffer.writes) == 4


def test_mark_cursor_reports_unsupported_instead_of_failing_the_shot(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """画不上不该毁掉这张截图，但必须如实报出来（绝不静默）。"""
    monkeypatch.setattr(capture_mod.w, "cursor_pos", lambda: (2400, 900))

    frame = _Frame(to_numpy_raises=True)

    assert capture_mod._mark_cursor(frame, ORIGIN, CURSOR_SIZE) == "unsupported"


# ---------------------------------------------------------------------------
# _finish：光标读数并进结果
# ---------------------------------------------------------------------------


def test_finish_reports_the_cursor_and_that_it_is_outside(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture_mod.w, "cursor_pos", lambda: (1500, 700))

    result = capture_mod._finish(_shot(), None)

    assert result.cursor == (1500, 700)
    assert result.cursor_inside is False
    assert result.cursor_marker == ""


def test_finish_reports_that_the_cursor_is_inside(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture_mod.w, "cursor_pos", lambda: (2400, 900))

    assert capture_mod._finish(_shot(), None).cursor_inside is True


def test_finish_carries_the_marker_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture_mod.w, "cursor_pos", lambda: (2400, 900))
    mark = capture_mod._CursorMark(origin=ORIGIN, size=CURSOR_SIZE, result="drawn")

    assert capture_mod._finish(_shot(), mark).cursor_marker == "drawn"
    assert capture_mod._finish(_shot(), mark, marker="unsupported").cursor_marker == "unsupported"


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def test_capture_result_omits_the_cursor_fields_unless_reported() -> None:
    payload = CaptureResult(path="shot.png", origin=(0, 0), width=10, height=10,
                            layer="wgc").to_dict()

    assert "cursor" not in payload
    assert "cursor_inside" not in payload
    assert "cursor_marker" not in payload


def test_capture_result_serialises_the_cursor_readout() -> None:
    payload = CaptureResult(path="shot.png", origin=(0, 0), width=10, height=10, layer="wgc",
                            cursor=(5, 6), cursor_inside=False,
                            cursor_marker="outside").to_dict()

    assert payload["cursor"] == [5, 6]
    assert payload["cursor_inside"] is False
    assert payload["cursor_marker"] == "outside"


# ---------------------------------------------------------------------------
# DXGI 窗口层：宁可报 unsupported，也不画错地方
# ---------------------------------------------------------------------------


def test_the_dxgi_window_layer_refuses_to_draw_the_marker(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """DXGI 存的是**整个显示器**，图里没有窗口的坐标系 —— 拿窗口原点去减就是画错地方。"""
    monkeypatch.setattr(capture_mod.w, "window_rect", lambda hwnd: (1605, 80, WIDTH, HEIGHT))
    monkeypatch.setattr(capture_mod.w, "extended_frame_bounds",
                        lambda hwnd: (1605, 80, WIDTH, HEIGHT))
    monkeypatch.setattr(capture_mod.w, "cursor_size", lambda: CURSOR_SIZE)
    monkeypatch.setattr(capture_mod.w, "cursor_pos", lambda: (2400, 900))

    def no_wgc(hwnd, path, mark=None):  # noqa: ANN001 - 替身
        raise RuntimeError("WGC 这一层不可用")

    monkeypatch.setattr(capture_mod, "_capture_wgc_window", no_wgc)
    monkeypatch.setattr(capture_mod, "_capture_dxgi",
                        lambda monitor, crop, path, mark=None: (WIDTH, HEIGHT))

    window = WindowInfo(hwnd=0x1234, title="t", pid=1, process="p.exe",
                        rect=(1605, 80, WIDTH, HEIGHT), monitor=0, is_foreground=False,
                        is_minimized=False, elevated=False)

    result = capture_mod.capture_window(0x1234, tmp_path, 1, window, "png", draw_cursor=True)

    assert result.layer == "dxgi"
    assert result.cursor_marker == "unsupported"
    assert result.cursor == (2400, 900)
    assert result.cursor_inside is True


# ---------------------------------------------------------------------------
# 命令行面：默认值与回显
# ---------------------------------------------------------------------------


def test_the_cursor_marker_is_off_by_default() -> None:
    args = client._resolve_common(client.build_parser().parse_args(["screenshot", "--full"]))

    assert client.to_request(args)[1]["cursor"] is False


def test_the_cursor_marker_can_be_requested_explicitly() -> None:
    args = client._resolve_common(
        client.build_parser().parse_args(["screenshot", "--full", "--cursor"]))

    command, params = client.to_request(args)

    assert command == "desktop.capture"
    assert params["cursor"] is True


def test_the_screenshot_text_shows_the_cursor_readout() -> None:
    text = client.render_text("screenshot", {
        "path": r"C:\tmp\shot.png", "origin": [1605, 80], "width": WIDTH, "height": HEIGHT,
        "layer": "wgc", "cursor": [1500, 700], "cursor_inside": False,
        "cursor_marker": "outside",
    })

    assert "cursor=1500,700" in text
    assert "cursor_inside=false" in text
    assert "cursor_marker=outside" in text


def test_the_screenshot_text_stays_quiet_when_the_cursor_was_not_reported() -> None:
    text = client.render_text("screenshot", {
        "path": r"C:\tmp\shot.png", "origin": [0, 0], "width": 10, "height": 10, "layer": "wgc",
    })

    assert "cursor" not in text


# ---------------------------------------------------------------------------
# daemon 侧：参数透传
# ---------------------------------------------------------------------------


class _CapturingDesktop(FakeDesktop):
    """在共用替身上只加一条：记下 `capture` 收到了什么。"""

    def __init__(self) -> None:
        super().__init__()
        self.seen_capture: list[dict] = []

    def capture(self, **kwargs):  # noqa: ANN003 - 替身
        self.seen_capture.append(kwargs)
        return CaptureResult(path=str(Path(kwargs["out_dir"]) / f"shot-{kwargs['seq']:04d}.png"),
                             origin=(0, 0), width=10, height=10, layer="wgc")


def test_the_daemon_hands_the_cursor_flag_to_the_desktop(tmp_path: Path) -> None:
    desktop = _CapturingDesktop()
    daemon = shell(tmp_path, desktop=desktop)
    session = daemon.sessions.begin()

    daemon._desktop_capture({"session_id": session.session_id, "monitor": 0})
    daemon._desktop_capture({"session_id": session.session_id, "monitor": 0, "cursor": True})

    assert [call["draw_cursor"] for call in desktop.seen_capture] == [False, True]
