"""截图之后的自动解析（DEC-094）。

契约出处（本文件唯一的行为权威）：

- `core/src/cu/daemon/core.py` 中 `Daemon._parse_after_capture` 的 docstring ——
  四种状态 `ok` / `off` / `skipped` / `failed` 各回答什么问题；
- `core/src/cu/desktop/real.py` 中 `_inline_elements` / `MAX_INLINE_ELEMENTS` /
  `INLINE_CONTENT_CHARS` 的注释 —— 内联的是**可交互**元素，其余留在 md 里；
- `core/src/cu/client.py` 中 `--no-parse` 与 `_parse_lines` / `_element_lines`。

三条主线：

1. **默认解析，两个出口关掉它**：`screenshot --no-parse` 关这一次，配置键
   `screenshot_parse` 关整台机器。客户端不读配置 —— 不发这个参数就是「由 daemon 定」。
2. **解析失败不让截图失败**。截图已经成功、图已经在盘上，而 OmniParser 是可选的
   （DEC-002）；让可选项把核心能力一起拖死，等于用 CONSTRAINT-005 去换一个便利功能。
3. **内联的是坐标**。模型读文字没问题，差的是把看到的东西落到精确像素上 ——
   所以索引只收 `interactivity` 为真的元素，其余（以及完整描述）仍在 md 里。

**本文件不碰屏幕、不碰 omni 环境**：桌面层是替身，`omni.available()` 被钉死。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _daemon_shell import FakeDesktop, no_omni, shell
from cu import client
from cu.config import Config
from cu.desktop import omni as omni_bridge
from cu.desktop import real as real_mod
from cu.desktop import uia as uia_mod
from cu.desktop.base import CaptureResult, ParseResult, WindowInfo
from cu.errors import CUError, ErrorCode

HWND = 0x00000001
ORIGIN = (100, 200)

WINDOW = WindowInfo(hwnd=HWND, title="notepad", pid=42, process="notepad.exe",
                    rect=(100, 200, 900, 700), monitor=0, is_foreground=True,
                    is_minimized=False, elevated=False)

#: 一次解析的检测结果：`interactivity` 与 `bbox` 是这份索引真正读的两个字段。
DETECTED = [
    {"type": "Text", "bbox": [10, 10, 200, 40], "interactivity": False, "content": "文件"},
    {"type": "Button", "bbox": [340, 210, 420, 240], "interactivity": True, "content": "保存"},
    {"type": "Text", "bbox": [10, 60, 200, 90], "interactivity": False, "content": "正文"},
    {"type": "Edit", "bbox": [120, 100, 900, 140], "interactivity": True, "content": "a\nb\nc"},
]


def _omni_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(omni_bridge, "available", lambda: (True, ""))


class _ParsingDesktop(FakeDesktop):
    """在共用替身上加两条：能截图、能解析。解析的返回或异常由构造参数给。"""

    def __init__(self, *, result: ParseResult | None = None,
                 error: Exception | None = None) -> None:
        super().__init__()
        self.seen_parse: list[dict] = []
        self._result = result
        self._error = error

    def capture(self, **kwargs):  # noqa: ANN003 - 替身
        seq = kwargs["seq"]
        return CaptureResult(
            path=str(Path(kwargs["out_dir"]) / f"win-0x00000001-notepad-100x200-{seq:04d}.png"),
            origin=ORIGIN, width=900, height=500, layer="wgc",
            kind="window", window=WINDOW, cursor=(5, 6), cursor_inside=True,
        )

    def parse_captured(self, *, shot, ai, out_dir, seq):  # noqa: ANN001 - 替身
        self.seen_parse.append({"shot": shot, "ai": ai, "seq": seq})
        if self._error is not None:
            raise self._error
        if self._result is not None:
            return self._result
        return ParseResult(path=str(Path(out_dir) / "win-0x00000001-notepad-100x200-omni-0001.md"),
                           element_count=4, elements=[])


def _capture(daemon, session_id: str, **params) -> dict:
    return daemon._desktop_capture({"session_id": session_id, "monitor": 0, **params})


# ---------------------------------------------------------------------------
# _inline_elements：索引里收谁
# ---------------------------------------------------------------------------


def test_only_interactive_elements_make_it_into_the_index() -> None:
    # oracle: specified —— real.py：模型读文字没问题，差的是坐标，所以只收能点的。
    elements, truncated = real_mod._inline_elements(DETECTED)

    assert [element["content"] for element in elements] == ["保存", "a b c"]
    assert truncated is False


def test_the_index_keeps_the_row_number_from_the_markdown() -> None:
    # oracle: specified —— `id` 是它在 md 表里的行号，据此回查完整记录。
    elements, _ = real_mod._inline_elements(DETECTED)

    assert [element["id"] for element in elements] == [2, 4]


def test_the_index_carries_the_bbox_as_four_integers() -> None:
    elements, _ = real_mod._inline_elements(DETECTED)

    assert elements[0]["bbox"] == [340, 210, 420, 240]
    assert elements[0]["type"] == "Button"


def test_the_index_is_capped_and_says_so() -> None:
    # oracle: specified —— MAX_INLINE_ELEMENTS 是上限；触顶时返回 truncated，
    #   被截掉的部分仍在 md 里，所以这不是静默丢数据。
    many = [{"type": "Button", "bbox": [i, i, i + 1, i + 1], "interactivity": True,
             "content": str(i)} for i in range(real_mod.MAX_INLINE_ELEMENTS + 5)]

    elements, truncated = real_mod._inline_elements(many)

    assert len(elements) == real_mod.MAX_INLINE_ELEMENTS
    assert truncated is True


def test_long_content_is_clipped_and_newlines_are_flattened() -> None:
    # oracle: specified —— 描述里的换行会把一行内联拆成两行，读的人会以为多了一条。
    long_text = "x" * (real_mod.INLINE_CONTENT_CHARS + 20)
    elements, _ = real_mod._inline_elements([
        {"type": "Button", "bbox": [0, 0, 1, 1], "interactivity": True, "content": long_text},
        {"type": "Edit", "bbox": [0, 0, 1, 1], "interactivity": True, "content": "a\nb"},
    ])

    assert len(elements[0]["content"]) == real_mod.INLINE_CONTENT_CHARS
    assert elements[1]["content"] == "a b"


def test_elements_without_a_name_stay_out_of_the_index() -> None:
    """没有名字的进不了索引 —— 这份索引的用处就是「按名字挑目标」。

    检测器读不出名字的那些（`M0,0L9,0 4.5,` 这类，实测单张图最多 61 条）内容已被清空，
    它们的框仍完整留在 md 里。索引是「能按名字找到的东西」，不是「所有框」。
    """
    # oracle: specified —— real._inline_elements 的注释。
    elements, _ = real_mod._inline_elements([
        {"type": "icon", "bbox": [0, 0, 10, 10], "interactivity": True, "content": ""},
        {"type": "icon", "bbox": [20, 20, 30, 30], "interactivity": True, "content": "   "},
        {"type": "button", "bbox": [40, 40, 50, 50], "interactivity": True, "content": "保存"},
    ])

    assert [element["content"] for element in elements] == ["保存"]


def test_elements_without_a_usable_bbox_are_skipped() -> None:
    elements, _ = real_mod._inline_elements([
        {"type": "Button", "interactivity": True, "content": "没有 bbox"},
        {"type": "Button", "bbox": [1, 2, 3], "interactivity": True, "content": "三个数"},
        "不是字典",
        {"type": "Button", "bbox": [1, 2, 3, 4], "interactivity": True, "content": "好的"},
    ])

    assert [element["content"] for element in elements] == ["好的"]


# ---------------------------------------------------------------------------
# daemon：默认解析与四种状态
# ---------------------------------------------------------------------------


def test_a_screenshot_parses_by_default(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    _omni_ready(monkeypatch)
    desktop = _ParsingDesktop()
    daemon = shell(tmp_path, desktop=desktop)
    session = daemon.sessions.begin()

    payload = _capture(daemon, session.session_id)

    assert payload["parse"]["status"] == "ok"
    assert len(desktop.seen_parse) == 1


def test_the_parse_reads_the_image_that_was_just_returned(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """解析的输入必须是**刚刚返回给调用方的那一张**，不能重新截一张。

    重新截一次拿到的是另一帧，调用方手里的图和元素清单就对不上了 ——
    而它们本来要靠同一套坐标去点。
    """
    _omni_ready(monkeypatch)
    desktop = _ParsingDesktop()
    daemon = shell(tmp_path, desktop=desktop)
    session = daemon.sessions.begin()

    payload = _capture(daemon, session.session_id)

    assert desktop.seen_parse[0]["shot"].path == payload["path"]


def test_the_parse_reuses_the_screenshots_sequence_number(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # oracle: specified —— md 与来源图成对命名，两者同一个 seq，不额外吃一个序号。
    _omni_ready(monkeypatch)
    desktop = _ParsingDesktop()
    daemon = shell(tmp_path, desktop=desktop)
    session = daemon.sessions.begin()

    payload = _capture(daemon, session.session_id)

    assert desktop.seen_parse[0]["seq"] == payload["seq"]
    assert daemon.sessions.next_seq(session.session_id) == payload["seq"] + 1


def test_the_parsed_artifact_is_registered_against_that_screenshot(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _omni_ready(monkeypatch)
    desktop = _ParsingDesktop()
    daemon = shell(tmp_path, desktop=desktop)
    session = daemon.sessions.begin()

    payload = _capture(daemon, session.session_id)

    manifest = daemon.sessions.get(session.session_id).manifest
    assert len(manifest.structured) == 1
    assert manifest.structured[0].source_seq == payload["seq"]
    assert manifest.structured[0].kind == "base"


def test_no_parse_turns_it_off_for_this_call(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    _omni_ready(monkeypatch)
    desktop = _ParsingDesktop()
    daemon = shell(tmp_path, desktop=desktop)
    session = daemon.sessions.begin()

    payload = _capture(daemon, session.session_id, parse=False)

    assert payload["parse"] == {"status": "off", "reason": "no_parse"}
    assert desktop.seen_parse == []


def test_the_config_key_turns_it_off_for_the_machine(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    _omni_ready(monkeypatch)
    desktop = _ParsingDesktop()
    daemon = shell(tmp_path, desktop=desktop)
    daemon.config.screenshot_parse = False
    session = daemon.sessions.begin()

    payload = _capture(daemon, session.session_id)

    assert payload["parse"] == {"status": "off", "reason": "config"}
    assert desktop.seen_parse == []


def test_a_missing_omni_is_reported_not_fatal(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """没装 omni 是可修的，所以带 hint；但截图本身必须照常成功（CONSTRAINT-005）。"""
    no_omni(monkeypatch)
    desktop = _ParsingDesktop()
    daemon = shell(tmp_path, desktop=desktop)
    session = daemon.sessions.begin()

    payload = _capture(daemon, session.session_id)

    assert payload["parse"]["status"] == "skipped"
    assert payload["parse"]["reason"] == "omni_not_installed"
    assert payload["parse"]["hint"] == "computer-use setup omni"
    assert payload["path"] and payload["origin"] == list(ORIGIN)


def test_a_failing_parse_does_not_fail_the_screenshot(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _omni_ready(monkeypatch)
    desktop = _ParsingDesktop(error=CUError(ErrorCode.OMNI_FAILED, "worker 崩了"))
    daemon = shell(tmp_path, desktop=desktop)
    session = daemon.sessions.begin()

    payload = _capture(daemon, session.session_id)

    assert payload["parse"]["status"] == "failed"
    assert payload["parse"]["error_code"] == "omni_failed"
    assert payload["parse"]["message"] == "worker 崩了"
    # 截图那部分一个字段都不少 —— 失败的是解析，不是这次截图。
    assert payload["path"] and payload["layer"] == "wgc"
    assert daemon.sessions.get(session.session_id).manifest.structured == []


def test_an_unexpected_parse_error_is_also_a_status(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """自动路径上任何异常都要变成状态 —— 否则一次解析把整条截图命令带崩。"""
    _omni_ready(monkeypatch)
    desktop = _ParsingDesktop(error=RuntimeError("不该发生"))
    daemon = shell(tmp_path, desktop=desktop)
    session = daemon.sessions.begin()

    payload = _capture(daemon, session.session_id)

    assert payload["parse"]["status"] == "failed"
    assert payload["parse"]["error_code"] == ErrorCode.INTERNAL_ERROR.value


def test_the_worker_reference_is_released_when_the_parse_fails(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # oracle: specified —— 一次都没解析成功的会话没有理由把模型钉在内存里（DEC-014）。
    _omni_ready(monkeypatch)
    desktop = _ParsingDesktop(error=CUError(ErrorCode.OMNI_FAILED, "worker 崩了"))
    daemon = shell(tmp_path, desktop=desktop)
    session = daemon.sessions.begin()

    _capture(daemon, session.session_id)

    assert daemon.omni_refcount == 0
    assert daemon._omni_sessions == set()


def test_a_successful_parse_holds_a_worker_reference(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    # oracle: specified —— 常驻 worker 由引用计数管住：截过图、解析过的会话在场时
    #   不会被停掉，会话结束时才卸载（DEC-014 / DEC-089）。
    _omni_ready(monkeypatch)
    desktop = _ParsingDesktop()
    daemon = shell(tmp_path, desktop=desktop)
    session = daemon.sessions.begin()

    _capture(daemon, session.session_id)

    assert daemon.omni_refcount == 1
    assert daemon._omni_sessions == {session.session_id}


def test_the_auto_parse_never_uses_the_vlm(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """`--ai` 需要配好的端点，没配就报 `vlm_failed`。自动路径不默认踩这个坑。"""
    _omni_ready(monkeypatch)
    desktop = _ParsingDesktop()
    daemon = shell(tmp_path, desktop=desktop)
    session = daemon.sessions.begin()

    _capture(daemon, session.session_id)

    assert desktop.seen_parse[0]["ai"] is False


# ---------------------------------------------------------------------------
# 命令行面：开关与回显
# ---------------------------------------------------------------------------


def test_the_cli_leaves_the_decision_to_the_daemon_by_default() -> None:
    # oracle: specified —— 客户端无状态、不读配置，所以「用户没写」= 不发这个参数。
    args = client._resolve_common(client.build_parser().parse_args(["screenshot", "--full"]))

    assert "parse" not in client.to_request(args)[1]


def test_the_cli_sends_the_flag_only_when_asked() -> None:
    args = client._resolve_common(
        client.build_parser().parse_args(["screenshot", "--full", "--no-parse"]))

    command, params = client.to_request(args)

    assert command == "desktop.capture"
    assert params["parse"] is False


def test_the_screenshot_text_shows_the_element_coordinates() -> None:
    text = client.render_text("screenshot", {
        "path": r"C:\tmp\win-0x00000001-notepad-100x200-0001.png", "origin": [100, 200],
        "width": 900, "height": 500, "layer": "wgc", "seq": 1,
        "parse": {"status": "ok", "path": r"C:\tmp\win-0x00000001-notepad-100x200-omni-0001.md",
                  "element_count": 4, "elements": [
                      {"id": 2, "type": "Button", "bbox": [340, 210, 420, 240], "content": "保存"},
                  ]},
    })

    assert "elements=4" in text
    assert "interactive=1" in text
    assert "2  Button  340,210,420,240  保存" in text


def test_the_screenshot_text_shows_why_there_are_no_elements() -> None:
    """状态先说出来 —— 读的人第一个问题是「这次有没有坐标可用」。"""
    base = {"path": r"C:\tmp\shot.png", "origin": [0, 0], "width": 10, "height": 10,
            "layer": "wgc"}

    assert "parse: off  reason=config" in client.render_text(
        "screenshot", {**base, "parse": {"status": "off", "reason": "config"}})
    assert "parse: off  reason=no_parse" in client.render_text(
        "screenshot", {**base, "parse": {"status": "off", "reason": "no_parse"}})

    skipped = client.render_text("screenshot", {**base, "parse": {
        "status": "skipped", "reason": "omni_not_installed",
        "hint": "computer-use setup omni"}})
    assert "parse: skipped  reason=omni_not_installed" in skipped
    assert "computer-use setup omni" in skipped

    failed = client.render_text("screenshot", {**base, "parse": {
        "status": "failed", "error_code": "omni_failed", "message": "worker 崩了"}})
    assert "parse: failed  error_code=omni_failed" in failed


def test_the_screenshot_text_stays_quiet_when_there_is_no_parse_section() -> None:
    # 老版本 daemon 的返回里没有这一段，渲染不能因此多印一行或崩掉。
    text = client.render_text("screenshot", {
        "path": r"C:\tmp\shot.png", "origin": [0, 0], "width": 10, "height": 10, "layer": "wgc"})

    assert "parse" not in text


def test_the_parse_command_also_shows_the_index() -> None:
    # oracle: derived —— `screenshot` 与 `parse` 返回的是同一种东西，形态必须一致。
    text = client.render_text("parse", {
        "path": r"C:\tmp\img-omni-0007.md", "element_count": 4,
        "elements": [{"id": 2, "type": "Button", "bbox": [1, 2, 3, 4], "content": "保存"}],
    })

    assert "2  Button  1,2,3,4  保存" in text


# ---------------------------------------------------------------------------
# RealDesktop._parse_image：命名与 UIA 的接线（不碰屏幕、不碰 omni 环境）
# ---------------------------------------------------------------------------


def _uia_element(*, rect: tuple[int, int, int, int] = (110, 250, 90, 30),
                 enabled: bool = True) -> uia_mod.UiaElement:
    """用**真的** `UiaElement`，不用替身。

    替身会把这个用例要守的东西绕过去：`bbox` 的语义（角对角）与 `origin` 的四角归一化
    正是它俩之间的事，手写一个 `{"bbox": [...]}` 等于把被测的那一步换成常量。
    """
    return uia_mod.UiaElement(name="保存", control_type="button", rect=rect,
                              interactive=True, enabled=enabled)


class _UiaResult:
    def __init__(self, *, elements=(), reason: str = "") -> None:  # noqa: ANN001 - 替身
        self.elements = list(elements)
        self.reason = reason

    @property
    def usable(self) -> bool:
        return bool(self.elements)


def _record_call_parse(monkeypatch: pytest.MonkeyPatch, out_dir: Path) -> dict:
    """把 `omni.call_parse` 换成记录器 —— 它是唯一跨环境的那一跳。"""
    seen: dict = {}

    def fake_call_parse(**kwargs):  # noqa: ANN003 - 替身
        seen.update(kwargs)
        return {"path": str(out_dir / kwargs["file_name"]), "element_count": 0,
                "model_name": None, "elements": []}

    monkeypatch.setattr(real_mod.omni, "call_parse", fake_call_parse)
    return seen


def _window_shot(out_dir: Path, seq: int = 4) -> CaptureResult:
    return CaptureResult(
        path=str(out_dir / f"win-0x00000001-notepad-100x200-{seq:04d}.png"),
        origin=ORIGIN, width=900, height=500, layer="wgc", kind="window", window=WINDOW)


def test_a_captured_shot_is_parsed_under_the_name_paired_with_its_image(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _record_call_parse(monkeypatch, tmp_path)
    monkeypatch.setattr(real_mod.uia, "read_window", lambda hwnd: _UiaResult())
    desktop = real_mod.RealDesktop(Config(data_dir=str(tmp_path)))

    desktop.parse_captured(shot=_window_shot(tmp_path), ai=False, out_dir=tmp_path, seq=4)

    assert seen["file_name"] == "win-0x00000001-notepad-100x200-omni-0004.md"


def test_an_external_image_gets_the_short_name(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    # oracle: specified —— DEC-025 的 `--image` 形态没有我们自己定的来源图名，
    #   所以没有坐标段可挂，走 `img-` 短模式。
    seen = _record_call_parse(monkeypatch, tmp_path)
    desktop = real_mod.RealDesktop(Config(data_dir=str(tmp_path)))

    desktop.parse(image_path=str(tmp_path / "photo.png"), hwnd=None, ai=False,
                  out_dir=tmp_path, seq=7)

    assert seen["file_name"] == "img-omni-0007.md"


def test_the_ai_variant_gets_its_own_name(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    # oracle: specified —— DEC-011：base 与 ai 是两种工件，同一张图解析两次不能同名。
    seen = _record_call_parse(monkeypatch, tmp_path)
    monkeypatch.setattr(real_mod.uia, "read_window", lambda hwnd: _UiaResult())
    desktop = real_mod.RealDesktop(Config(data_dir=str(tmp_path)))

    desktop.parse_captured(shot=_window_shot(tmp_path), ai=True, out_dir=tmp_path, seq=4)

    assert seen["file_name"] == "win-0x00000001-notepad-100x200-omni_ai-0004.md"


def test_the_uia_elements_are_normalised_by_the_shots_own_origin(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UIA 给的是屏幕坐标、检测器给的是图像坐标。合并前必须减掉**这张图的** origin。

    **四个角都要减。** 只减左上角会让右下角留在屏幕坐标里 —— 一个 32x32 的按钮于是
    变成横跨半个屏幕的一条（真机实测：`后退` 报成 `15,61,1193,424`）。所以这里除了
    断言数值，还断言框没有变得比控件本身大。
    """
    seen = _record_call_parse(monkeypatch, tmp_path)
    monkeypatch.setattr(real_mod.uia, "read_window",
                        lambda hwnd: _UiaResult(elements=[_uia_element()]))
    desktop = real_mod.RealDesktop(Config(data_dir=str(tmp_path)))

    desktop.parse_captured(shot=_window_shot(tmp_path), ai=False, out_dir=tmp_path, seq=4)

    box = seen["extra_elements"][0]["bbox"]
    assert box == [10, 50, 100, 80]          # 90x30 的按钮，屏幕 (110,250) 减 origin (100,200)
    assert box[2] - box[0] == 90 and box[3] - box[1] == 30


def test_a_window_that_exposes_nothing_to_uia_still_parses(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # oracle: specified —— 自绘/Electron 界面拿不到控件树不是错误（CONSTRAINT-005）。
    seen = _record_call_parse(monkeypatch, tmp_path)
    monkeypatch.setattr(real_mod.uia, "read_window",
                        lambda hwnd: _UiaResult(reason="该窗口未向 UIA 暴露带文本的元素"))
    desktop = real_mod.RealDesktop(Config(data_dir=str(tmp_path)))

    result = desktop.parse_captured(shot=_window_shot(tmp_path), ai=False,
                                    out_dir=tmp_path, seq=4)

    assert seen["extra_elements"] is None
    assert result.uia_reason == "该窗口未向 UIA 暴露带文本的元素"


def test_a_full_screen_shot_has_no_window_to_ask_uia_about(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _record_call_parse(monkeypatch, tmp_path)
    monkeypatch.setattr(real_mod.uia, "read_window",
                        lambda hwnd: pytest.fail("全屏截图没有窗口，不该问 UIA"))
    desktop = real_mod.RealDesktop(Config(data_dir=str(tmp_path)))
    shot = CaptureResult(path=str(tmp_path / "full-0x00000000-untitled-0x0-0002.png"),
                         origin=(0, 0), width=3440, height=1440, layer="wgc", kind="full")

    desktop.parse_captured(shot=shot, ai=False, out_dir=tmp_path, seq=2)

    assert seen["file_name"] == "full-0x00000000-untitled-0x0-omni-0002.md"
