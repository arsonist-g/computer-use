"""写操作前置接上「期望身份」（Q-024）。

oracle: specified —— DEC-013 第 1 层：hwnd 仍存在，且 `pid` 与窗口类与**记录**一致
（hwnd 会被系统复用，仅凭 hwnd 会打到别的窗口上）。
那份「记录」就是 `Sessions.last_screenshot_of` 里的 window ref。

端到端那条（期望身份不符时真的返回 `window_stale`）在真机上跑：
`tests/native/acceptance_preflight.py` —— 它篡改 `session.json` 里记下的 pid，
模拟「hwnd 被复用给了别的进程」。这里是纯逻辑层。
"""

from __future__ import annotations

from pathlib import Path

from _daemon_shell import FakeDesktop, record_window_screenshot, shell
from cu.config import Config
from cu.desktop import real as real_mod
from cu.desktop.base import InputResult, WindowIdentity

SESSION_HWND = 0x1A2B
OTHER_HWND = 0x3C4D
SESSION_HWND_TEXT = "0x00001A2B"


def test_identity_comes_from_the_recorded_screenshot(tmp_path: Path) -> None:
    """有该 hwnd 的历史截图 ⟹ 用它的 pid/class 当期望身份。"""
    daemon = shell(tmp_path)
    session = daemon.sessions.begin()
    record_window_screenshot(daemon, session.session_id, hwnd=SESSION_HWND, pid=4242,
                             klass="Notepad")

    identity = daemon._expected_identity(session.session_id, SESSION_HWND)

    assert identity is not None
    assert (identity.pid, identity.klass) == (4242, "Notepad")


def test_identity_is_none_without_a_record(tmp_path: Path) -> None:
    """没有历史记录就不校验 —— 宁可不拦，也不要因为「这个会话没截过图」拒绝一次合法点击。"""
    daemon = shell(tmp_path)
    session = daemon.sessions.begin()

    assert daemon._expected_identity(session.session_id, SESSION_HWND) is None
    assert daemon._expected_identity(session.session_id, None) is None


def test_identity_is_none_when_the_only_record_is_for_another_window(tmp_path: Path) -> None:
    """只认**该 hwnd** 的记录 —— 别的窗口的截图不能拿来当基准。"""
    daemon = shell(tmp_path)
    session = daemon.sessions.begin()
    record_window_screenshot(daemon, session.session_id, hwnd=OTHER_HWND, pid=4242,
                             klass="Notepad")

    assert daemon._expected_identity(session.session_id, SESSION_HWND) is None


def test_write_hands_the_identity_to_the_desktop(tmp_path: Path) -> None:
    """`_write` 把解析出的身份交给桌面层 —— 这条链断了，`window_stale` 就还是发不出来。

    这是 Q-024 的核心：校验参数早就存在，缺的一直是「谁来传」。
    """
    desktop = FakeDesktop()
    daemon = shell(tmp_path, desktop=desktop)
    session = daemon.sessions.begin()
    record_window_screenshot(daemon, session.session_id, hwnd=SESSION_HWND, pid=4242,
                             klass="Notepad")

    daemon._input_click({"x": 10, "y": 20, "describe": "点一下",
                         "session_id": session.session_id, "hwnd": SESSION_HWND_TEXT})

    assert len(desktop.seen_expect) == 1
    identity = desktop.seen_expect[0]
    assert identity is not None
    assert (identity.pid, identity.klass) == (4242, "Notepad")


def test_write_hands_nothing_when_there_is_no_record(tmp_path: Path) -> None:
    """没有记录就交 None：桌面层据此跳过身份比对，其余前置照旧（约束 4）。"""
    desktop = FakeDesktop()
    daemon = shell(tmp_path, desktop=desktop)
    session = daemon.sessions.begin()

    daemon._input_click({"x": 10, "y": 20, "describe": "点一下",
                         "session_id": session.session_id, "hwnd": SESSION_HWND_TEXT})

    assert desktop.seen_expect == [None]


def test_expectation_treats_empty_values_as_unknown() -> None:
    """`pid=0` / `class=""` 是「有记录却比对不了」—— 与没有记录等价，都要放行。

    否则清单里一条早期记录会让此后**每一次**合法点击都被判成 `window_stale`。
    """
    assert real_mod._expectation(None) == (None, None)
    assert real_mod._expectation(WindowIdentity(pid=0, klass="")) == (None, None)
    assert real_mod._expectation(WindowIdentity(pid=4242, klass="Notepad")) == (4242, "Notepad")
    assert real_mod._expectation(WindowIdentity(pid=4242)) == (4242, None)


def test_preflight_forwards_the_identity_to_check_hwnd(tmp_path: Path,
                                                      monkeypatch) -> None:
    """前置校验把身份真的传给了 `check_hwnd`（`expect_pid` / `expect_class`）。

    这两个参数此前**全仓没有调用点传过** —— 这条测试盯的就是那个缺口。
    """
    seen: dict = {}

    def fake_check(hwnd, expect_pid=None, expect_class=None):  # noqa: ANN001 - 替身
        seen.update(hwnd=hwnd, pid=expect_pid, klass=expect_class)

    monkeypatch.setattr(real_mod.windows_mod, "check_hwnd", fake_check)
    # 只走 `_preflight`：给它一个不建窗口的替身控制器（真实那个会碰 Win32）。
    desktop = real_mod.RealDesktop(Config(data_dir=str(tmp_path)), controller=object())

    desktop._preflight(SESSION_HWND, WindowIdentity(pid=4242, klass="Notepad"),
                       foreground=False)

    assert seen == {"hwnd": SESSION_HWND, "pid": 4242, "klass": "Notepad"}


def test_preflight_skips_everything_without_an_hwnd(tmp_path: Path, monkeypatch) -> None:
    """没有 hwnd 就完全跳过前置 —— 契约允许「就在当前前台窗口上操作」。"""
    calls: list = []
    monkeypatch.setattr(real_mod.windows_mod, "check_hwnd", lambda *a, **k: calls.append(a))
    desktop = real_mod.RealDesktop(Config(data_dir=str(tmp_path)), controller=object())

    desktop._preflight(None, WindowIdentity(pid=4242, klass="Notepad"))

    assert calls == []


def test_where_the_foreground_is_stolen_and_where_it_is_not(tmp_path: Path,
                                                           monkeypatch) -> None:
    """带 `--hwnd` 的写命令里，哪些会先把目标窗口提到前台。

    按键与点击、输入一样，落点是**发命令那一刻的前台窗口**：`key ctrl+a` 不抢前台，
    就等于把这一串键投给用户手上那个窗口（api-contract.md §1.3 把 `--hwnd` 定义为
    写操作的前置校验）。`move` 例外 —— 悬停响应看光标落在哪个窗口上，与前台无关。
    """
    stolen: list[int] = []
    monkeypatch.setattr(real_mod, "BRING_TO_FOREGROUND", True)
    monkeypatch.setattr(real_mod.windows_mod, "check_hwnd", lambda *a, **k: None)
    monkeypatch.setattr(real_mod.windows_mod, "bring_to_foreground", stolen.append)
    for name in ("click", "drag", "type_text", "key"):
        monkeypatch.setattr(real_mod.input_mod, name, lambda *a, **k: None)
    monkeypatch.setattr(real_mod.input_mod, "move_cursor", lambda *a, **k: 0)
    monkeypatch.setattr(real_mod.input_mod, "scroll", lambda *a, **k: InputResult())

    desktop = real_mod.RealDesktop(Config(data_dir=str(tmp_path)), controller=object())

    desktop.click(1, 2, hwnd=SESSION_HWND)
    desktop.drag(1, 2, 3, 4, hwnd=SESSION_HWND)
    desktop.type_text("hi", hwnd=SESSION_HWND)
    desktop.key("ctrl+a", hwnd=SESSION_HWND)
    assert stolen == [SESSION_HWND] * 4

    stolen.clear()
    desktop.move(1, 2, hwnd=SESSION_HWND)
    desktop.scroll(0, -1)                      # 契约里滚动只有 `--at`，没有 `--hwnd`
    assert stolen == []
