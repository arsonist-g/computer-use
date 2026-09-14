"""鼠标落点的事实来源：光标**现在**在哪。

契约出处（本文件唯一的行为权威）：
- `core/src/cu/desktop/input.py` 中 `cursor_now` / `cursor_snapshot` / `move_cursor` /
  `click` 的 docstring；
- `core/src/cu/desktop/real.py` 中 `move` 的返回值。

这条链要防的那个缺陷：`move_cursor` 拿「上次我把它放哪儿了」当轨迹起点。人一动鼠标
这个缓存就过期，于是「移动到 (x, y)」退化成一次静默空操作 —— `moved_ms=0`、光标没动，
紧接着按钮事件落在光标**实际**所在处（`SendInput` 的按钮事件不带坐标）。光标位置因此
必须由**现读**的 `GetCursorPos` 给出，且每次移动/点击后都回报一次。

**本文件不碰光标、不碰屏幕**：`_send` / `cursor_now` / `_virtual_screen` 全部被替身接管。
"""

from __future__ import annotations

from pathlib import Path

from cu.config import Config
from cu.desktop import input as input_mod
from cu.desktop import real as real_mod
from cu.desktop import win32 as w

TARGET = (1500, 700)
ELSEWHERE = (200, 150)
VIRTUAL_SCREEN = (0, 0, 3440, 1440)


class _SendSpy:
    """`input._send` 的替身：只记录，绝不调用真实 SendInput。"""

    def __init__(self) -> None:
        self.events: list[w.INPUT] = []

    def __call__(self, *inputs: w.INPUT) -> None:
        self.events.extend(inputs)

    def mouse_flags(self) -> list[int]:
        """按派发顺序列出鼠标事件的 `dwFlags`。"""
        return [event.mi.dwFlags for event in self.events if event.type == w.INPUT_MOUSE]


def _install(monkeypatch, positions, *, virtual=VIRTUAL_SCREEN):
    """装好替身：光标位置按 `positions` 依次给出（最后一个会被重复读）。"""
    sends = _SendSpy()
    queue = list(positions)
    monkeypatch.setattr(input_mod, "_send", sends)
    monkeypatch.setattr(input_mod, "_virtual_screen", lambda: virtual)
    monkeypatch.setattr(input_mod, "cursor_now",
                        lambda: queue.pop(0) if len(queue) > 1 else queue[0])
    return sends


def test_move_cursor_is_not_a_no_op_when_the_previous_call_aimed_at_the_same_point(
        monkeypatch) -> None:
    """同一个目标点连点两次：第二次仍必须真的派发事件。

    旧实现把「上次落点」当起点，第二次直接短路 —— 而人早就把鼠标挪走了。
    """
    sends = _install(monkeypatch, [ELSEWHERE, (900, 900)])

    input_mod.move_cursor(*TARGET, step_ms=0, max_points=5)
    after_first = len(sends.events)
    input_mod.move_cursor(*TARGET, step_ms=0, max_points=5)

    assert after_first > 0
    assert len(sends.events) > after_first


def test_move_cursor_short_circuits_only_when_the_real_cursor_is_already_there(
        monkeypatch) -> None:
    """已经在目标点上就不必动 —— 这条短路要看**真实**光标，不是缓存。"""
    sends = _install(monkeypatch, [TARGET])

    assert input_mod.move_cursor(*TARGET, step_ms=0, max_points=5) == 0
    assert sends.events == []


def test_the_trajectory_starts_from_the_real_cursor(monkeypatch) -> None:
    """起点错了，中间点整段就画在错误的位置上（终点仍对，轨迹是假的）。"""
    seen: dict = {}
    real_trajectory = input_mod.trajectory

    def spy(x0, y0, x1, y1, max_points):  # noqa: ANN001 - 替身
        seen["start"] = (x0, y0)
        return real_trajectory(x0, y0, x1, y1, max_points)

    monkeypatch.setattr(input_mod, "trajectory", spy)
    _install(monkeypatch, [ELSEWHERE])

    input_mod.move_cursor(*TARGET, step_ms=0, max_points=5)

    assert seen["start"] == ELSEWHERE


def test_click_moves_the_cursor_before_it_presses(monkeypatch) -> None:
    """目标与光标当前位置不一致时，必须先移动再点击 —— 按钮事件落在光标实际所在处。"""
    sends = _install(monkeypatch, [ELSEWHERE, TARGET])

    input_mod.click(*TARGET, step_ms=0, max_points=5)

    flags = sends.mouse_flags()
    assert flags, "click 一个事件都没派发"
    assert flags[0] & w.MOUSEEVENTF_MOVE
    assert not flags[0] & (w.MOUSEEVENTF_LEFTDOWN | w.MOUSEEVENTF_LEFTUP)
    assert flags[-1] & w.MOUSEEVENTF_LEFTUP


def test_click_reports_the_cursor_it_landed_on(monkeypatch) -> None:
    _install(monkeypatch, [ELSEWHERE, TARGET])

    assert input_mod.click(*TARGET, step_ms=0, max_points=5).detail["cursor"] == list(TARGET)


def test_the_cursor_snapshot_is_a_live_read(monkeypatch) -> None:
    _install(monkeypatch, [(42, 24)])

    assert input_mod.cursor_snapshot() == {"cursor": [42, 24]}


def test_the_real_desktop_move_reports_the_cursor(tmp_path: Path, monkeypatch) -> None:
    """`move` 的读数要一路走到结果里，不能只停在输入层。"""
    _install(monkeypatch, [ELSEWHERE, TARGET])
    desktop = real_mod.RealDesktop(Config(data_dir=str(tmp_path)), controller=object())

    result = desktop.move(*TARGET)

    assert result.ok is True
    assert result.detail["cursor"] == list(TARGET)
