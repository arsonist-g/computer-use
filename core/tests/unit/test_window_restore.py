"""`window restore` —— 显式的「把最小化窗口还原出来」命令（DEC-101）。

契约出处：`core/src/cu/desktop/windows.py` 的 `restore_window`、
`core/src/cu/daemon/core.py` 的 `_desktop_window_restore`、
`core/src/cu/client.py` 的 `window restore` 子命令与它的渲染。

为什么需要这条命令：写命令（`click` / `type` / `key`）遇到最小化窗口报
`window_minimized` —— 那是 DEC-013 前置校验的既有语义，DEC-101 刻意不动它 ——
而在此之前命令面里**没有任何还原窗口的命令**，AI 遇到了只能请人去点。
这条命令补的就是那个缺口：先 `window restore --hwnd H`，再发写命令。

两条性质是这条命令的要点，各有用例钉住：

- **不抢前台**：走 `SW_SHOWNOACTIVATE` 而不是 `SW_RESTORE`（后者按 Win32 文档是
  「激活并显示」，实测会把 `fg` 抢过去）。命令名里没有「activate」，行为里也不该有。
- **幂等**：窗口本来就可见时什么都不做，`restored=False`，且**不是失败** ——
  调用方可以无条件地把它当「确保这个窗口可用」的第一步。

**本文件一次都不碰真实桌面**：`w` 命名空间换成替身，daemon 侧用
`_daemon_shell.shell` 的假桌面。

oracle 类别逐条标注：specified（契约原文或 Win32 外部规范）/ derived（由契约推出）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _daemon_shell import shell
from cu.client import _resolve_common, build_parser, render_text, to_request
from cu.desktop import windows as windows_mod
from cu.desktop.base import WindowRestore
from cu.errors import HINTS, CUError, ErrorCode

# ---------------------------------------------------------------------------
# 手算的期望值：出处是 Win32 文档，与被测实现的取值无关
# ---------------------------------------------------------------------------

WIN_SW_SHOWNOACTIVATE = 4     # 显示但不激活
WIN_SW_RESTORE = 9            # 激活并显示 —— 会抢走前台，这条命令**刻意不用**
WIN_NOT_MINIMIZED = 0
WIN_MINIMIZED = 1
TARGET_HWND = 0x0001A2B4


class _FakeUser32:
    def __init__(self, desktop: _FakeWindow) -> None:
        self._desktop = desktop

    def IsIconic(self, hwnd: int) -> int:
        return self._desktop.is_iconic(hwnd)

    def IsWindow(self, hwnd: int) -> int:
        return self._desktop.is_window(hwnd)

    def ShowWindow(self, hwnd: int, command: int) -> bool:
        self._desktop.show_window(hwnd, command)
        return True


class _FakeWindow:
    """`windows.py` 里 `w` 命名空间的替身。"""

    SW_SHOWNOACTIVATE = WIN_SW_SHOWNOACTIVATE
    SW_RESTORE = WIN_SW_RESTORE

    def __init__(self, *, exists: bool = True, minimized: bool = True,
                 title: str = "某个窗口") -> None:
        self.exists = exists
        self.minimized = minimized
        self.title = title
        self.events: list[tuple[object, ...]] = []
        self.user32 = _FakeUser32(self)

    def is_window(self, hwnd: int) -> int:
        return 1 if self.exists else 0

    def is_iconic(self, hwnd: int) -> int:
        # 还原之后立刻解除 —— 等待循环正常退出，用例不必睡满预算。
        return WIN_MINIMIZED if self.minimized else WIN_NOT_MINIMIZED

    def show_window(self, hwnd: int, command: int) -> None:
        self.events.append(("ShowWindow", hwnd, command))
        self.minimized = False

    def window_text(self, hwnd: int) -> str:
        return self.title


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeWindow) -> _FakeWindow:
    monkeypatch.setattr(windows_mod, "w", fake)
    assert windows_mod.w is fake, "替身没装上：再往下走就会碰真实桌面"
    return fake


# ---------------------------------------------------------------------------
# 还原：做一次、不抢前台、已可见时不动
# ---------------------------------------------------------------------------


def test_a_minimized_window_is_restored_without_taking_the_foreground(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # oracle: specified —— 命令名里没有「activate」，行为里也不该有。
    # `SW_RESTORE` 按 Win32 文档是「激活并显示」，实测会把 fg 抢过去。
    assert WIN_SW_SHOWNOACTIVATE != WIN_SW_RESTORE, "两个命令不能写成同一个数，否则这条用例失去意义"
    fake = _install(monkeypatch, _FakeWindow(minimized=True))
    result = windows_mod.restore_window(TARGET_HWND)
    assert fake.events == [("ShowWindow", TARGET_HWND, WIN_SW_SHOWNOACTIVATE)]
    assert result.restored is True


def test_a_visible_window_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    # oracle: specified —— 幂等：本来就可见时什么都不做，且**不是失败**。
    fake = _install(monkeypatch, _FakeWindow(minimized=False))
    result = windows_mod.restore_window(TARGET_HWND)
    assert fake.events == []
    assert result.restored is False


def test_an_unknown_handle_is_reported_not_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    # oracle: specified —— 与其它窗口命令同一个码（`window_not_found`），
    # 调用方据此重新枚举；静默成功会让它以为窗口还在。
    fake = _install(monkeypatch, _FakeWindow(exists=False))
    with pytest.raises(CUError) as caught:
        windows_mod.restore_window(TARGET_HWND)
    assert caught.value.code is ErrorCode.WINDOW_NOT_FOUND
    assert caught.value.detail["hwnd"] == "0x0001A2B4"
    assert fake.events == []


def test_the_result_carries_the_handle_the_title_and_whether_it_moved(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # oracle: specified —— 线协议形状（api-contract 的 Delta）。
    _install(monkeypatch, _FakeWindow(minimized=True, title="未命名 - 记事本"))
    assert windows_mod.restore_window(TARGET_HWND).to_dict() == {
        "hwnd": "0x0001A2B4", "title": "未命名 - 记事本", "restored": True,
    }


# ---------------------------------------------------------------------------
# daemon 侧：路由与必填参数
# ---------------------------------------------------------------------------


class _FakeDesktop:
    """只实现 `restore_window` 的桌面层替身。"""

    def __init__(self) -> None:
        self.seen: list[int] = []

    def restore_window(self, hwnd: int) -> WindowRestore:
        self.seen.append(hwnd)
        return WindowRestore(hwnd=hwnd, title="某个窗口", restored=True)


def test_the_daemon_route_parses_the_handle(tmp_path: Path) -> None:
    desktop = _FakeDesktop()
    daemon = shell(tmp_path, desktop=desktop)  # type: ignore[arg-type]
    assert daemon._desktop_window_restore({"hwnd": "0x1A2B"})["restored"] is True
    assert desktop.seen == [0x1A2B]


def test_the_daemon_route_rejects_a_missing_handle(tmp_path: Path) -> None:
    # oracle: specified —— 这条命令的全部输入就是那个窗口，没有它没有意义。
    daemon = shell(tmp_path, desktop=_FakeDesktop())  # type: ignore[arg-type]
    with pytest.raises(CUError) as caught:
        daemon._desktop_window_restore({})
    assert caught.value.code is ErrorCode.INVALID_PARAMS


def test_the_daemon_needs_no_session(tmp_path: Path) -> None:
    # oracle: specified —— 它不产出工件、不注入输入，与 `windows` 同类，
    # 所以没有 `session_id` 也必须能跑（`screenshot` 那条路会要求会话）。
    desktop = _FakeDesktop()
    daemon = shell(tmp_path, desktop=desktop)  # type: ignore[arg-type]
    daemon._desktop_window_restore({"hwnd": "0x1A2B"})
    assert desktop.seen == [0x1A2B]


# ---------------------------------------------------------------------------
# 客户端侧：命令行到线协议、以及人读输出
# ---------------------------------------------------------------------------


def test_the_cli_maps_to_the_route_and_keeps_the_handle_as_written() -> None:
    # oracle: specified —— 客户端不解析 hwnd（`parse_hwnd` 的权威在服务端），
    # 原样透传，两种写法都接受。
    args = _resolve_common(build_parser().parse_args(["window", "restore", "--hwnd", "0x1A2B"]))
    assert to_request(args) == ("desktop.windowRestore", {"hwnd": "0x1A2B"})


def test_the_cli_requires_a_handle() -> None:
    # oracle: specified —— `--hwnd` 是必填，缺了就该在本地拦下，
    # 而不是发一条没有目标的请求上去。
    # 注意本项目的解析器把 argparse 的 SystemExit 换成了 CUError（`client._Parser`），
    # 所以这里断言的是 CUError 而不是 SystemExit —— 那正是退出码 2 的来源。
    with pytest.raises(CUError) as caught:
        build_parser().parse_args(["window", "restore"])
    assert caught.value.code is ErrorCode.INVALID_PARAMS


@pytest.mark.parametrize(("restored", "expected"), [
    (True, '已还原  0x0001A2B4  "未命名 - 记事本"'),
    (False, '本来就可见  0x0001A2B4  "未命名 - 记事本"'),
])
def test_the_text_output_says_whether_it_moved(restored: bool, expected: str) -> None:
    # oracle: derived —— 两种都是成功行（「本来就可见」不是失败），
    # 但必须分得开：读的人要知道这次到底有没有动过那个窗口。
    text = render_text("window", {"hwnd": "0x0001A2B4", "title": "未命名 - 记事本",
                                  "restored": restored})
    assert text == expected


def test_the_minimized_error_points_at_this_command() -> None:
    # oracle: specified —— hint 是 AI 的行动指令，不是给人看的安慰话。
    # 写路径仍然拒绝最小化窗口（DEC-101 刻意不动那条语义），所以提示语必须告诉它
    # **现在有办法了**；否则它只能回头去请人 —— 那正是这条命令要消掉的场景。
    assert "window restore" in HINTS[ErrorCode.WINDOW_MINIMIZED]
