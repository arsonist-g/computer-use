"""`type` / `key` 的输入派发契约。

契约出处（本文件唯一的行为权威）：

- `core/src/cu/desktop/input.py` 中 `_has_line_break` / `type_text` / `_type_via_clipboard` /
  `_parse_combo` / `key` 的 docstring。
- `core/src/cu/desktop/clipboard.py` 中 `get_text` / `set_text` 的 docstring（剪贴板是两个
  函数组成的缝：读不到文本返回 `None`，写失败抛 `CUError`）。
- `skill/computer-use/SKILL.md` 与 `skill-zh.md` 的 `type` / `key` 两行：含换行的文本走剪贴板
  粘贴插入，要按 Enter 用 `key enter`。

两条主线：`type` 只插入文本（含换行时经剪贴板粘贴，绝不合成 Enter 按键；不含换行时逐字走
Unicode），按键一律走 `key`（单键与组合键同一套写法）。

剪贴板走 Win32 `CF_UNICODETEXT` 而不是子进程，是因为 2026-09-14 实测两条子进程路子都会动坏内容：
`clip.exe` 按控制台代码页解码 UTF-16LE 字节流（`中文A` 变 `-N锟斤拷eA`），`Get-Clipboard -Raw`
反向读会多带一个行尾 `\r\n`。含换行的 `type` 现在默认走这条路，所以必须验「写进去的就是原文本」。

**本文件不注入任何真实输入、不碰真实剪贴板**：`input._send` 与 `input.clipboard` 的
`get_text` / `set_text` 在每个用例里都被换成只记录的替身。不碰桌面、窗口、焦点。

覆盖判据：`_has_line_break` / `type_text` / `_type_via_clipboard` / `_parse_combo` / `key` 的
**分支（decision）全覆盖**。项目未配置覆盖率工具（无 pytest-cov / coverage），因此这是声明值而非实测值。

oracle 类别逐条标注：specified（契约原文或 Win32 外部规范）/ derived（由契约推出）/ implicit（不变量）。
"""

from __future__ import annotations

import pytest

from cu.desktop import input as input_mod
from cu.desktop import win32 as w
from cu.errors import CUError, ErrorCode

# Win32 虚拟键码与 KEYBDINPUT.dwFlags 位：手算的字面量，出处是 Win32 文档，与被测实现无关。
# 本文件的断言用的就是这些值，另有 `test_win32_key_event_flags_match_the_documented_values`
# 把 win32 模块里的常量也钉在同一组值上。
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_ESCAPE = 0x1B
VK_L = 0x4C
VK_S = 0x53
VK_V = 0x56
VK_LWIN = 0x5B
VK_F10 = 0x79
VK_F13 = 0x7C

UNICODE_FLAG = 0x0004      # KEYEVENTF_UNICODE：事件投递的是字面量字符，不是虚拟键
KEYUP_FLAG = 0x0002        # KEYEVENTF_KEYUP：抬起

#: 替身里「用户原来就有的剪贴板内容」，随便挑的一段文本。
ORIGINAL_CLIPBOARD = "剪贴板原有内容"


class _SendSpy:
    """`input._send` 的替身：只记录，绝不调用真实 SendInput。`events` 是**真正投递出去**的事件
    （调用失败时 SendInput 一个事件都没送到，那一批只留在 `calls` 里）。

    `fail_on_unicode=True` 时，凡带 `KEYEVENTF_UNICODE` 的调用都抛 `CUError` ——
    真实的 SendInput 失败在单测里无法复现，用这个开关把无换行路径逼进剪贴板降级。
    """

    def __init__(self, *, fail_on_unicode: bool = False) -> None:
        self.fail_on_unicode = fail_on_unicode
        self.calls: list[tuple[w.INPUT, ...]] = []
        self.events: list[w.INPUT] = []

    def __call__(self, *inputs: w.INPUT) -> None:
        self.calls.append(inputs)
        if self.fail_on_unicode and any(item.ki.dwFlags & UNICODE_FLAG for item in inputs):
            raise CUError(ErrorCode.INTERNAL_ERROR, "SendInput 只投递了 0/2 个事件")
        self.events.extend(inputs)


class _ClipboardSpy:
    """`cu.desktop.clipboard` 的替身：剪贴板变成内存里的一个字符串，绝不碰真实剪贴板。

    `current` 是剪贴板里的文本（`None` = 里面没有文本，`get_text` 契约里的那一种）；
    `fail_writes` 是**第几次** `set_text` 调用（0 起）会抛 `CUError`，用来复现「剪贴板打不开
    或被别的进程占住」。`writes` 只记成功的写入，`write_calls` 记全部尝试。
    """

    def __init__(self, current: str | None = ORIGINAL_CLIPBOARD, fail_writes: set[int] | None = None) -> None:
        self.current = current
        self.fail_writes = set(fail_writes or ())
        self.writes: list[str] = []
        self.write_calls = 0
        self.reads = 0

    def get_text(self) -> str | None:
        self.reads += 1
        return self.current

    def set_text(self, text: str) -> None:
        index = self.write_calls
        self.write_calls += 1
        if index in self.fail_writes:
            raise CUError(ErrorCode.INTERNAL_ERROR, "写入剪贴板失败")
        self.writes.append(text)
        self.current = text


def _install_spy(monkeypatch: pytest.MonkeyPatch, **kwargs: bool) -> _SendSpy:
    recorder = _SendSpy(**kwargs)
    monkeypatch.setattr(input_mod, "_send", recorder)
    return recorder


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _SendSpy:
    return _install_spy(monkeypatch)


@pytest.fixture
def clip(monkeypatch: pytest.MonkeyPatch) -> _ClipboardSpy:
    """把 `input.clipboard` 的两个函数换成内存替身（硬约束：不碰真实剪贴板）。"""
    recorder = _ClipboardSpy()
    monkeypatch.setattr(input_mod.clipboard, "get_text", recorder.get_text)
    monkeypatch.setattr(input_mod.clipboard, "set_text", recorder.set_text)
    return recorder


def _unicode_events(spy: _SendSpy) -> list[w.INPUT]:
    """字面量字符事件（带 `KEYEVENTF_UNICODE`）。"""
    return [item for item in spy.events if item.ki.dwFlags & UNICODE_FLAG]


def _unicode_downs(spy: _SendSpy) -> list[int]:
    """字面量字符按下事件投递的码点（`wScan` 就是 `ord(ch)`）。"""
    return [item.ki.wScan for item in _unicode_events(spy) if not item.ki.dwFlags & KEYUP_FLAG]


def _vk_events(spy: _SendSpy) -> list[tuple[int, bool]]:
    """合成的按键事件：`(wVk, 是否抬起)`。

    带 `KEYEVENTF_UNICODE` 的事件不属于这里 —— CR 的码点 `0x0D` 与 `VK_RETURN` 相同，
    只看 `wVk` 会把字面量 CR（若真投递了）误判成合成的 Enter。
    """
    return [(item.ki.wVk, bool(item.ki.dwFlags & KEYUP_FLAG)) for item in spy.events
            if not item.ki.dwFlags & UNICODE_FLAG]


def test_win32_key_event_flags_match_the_documented_values() -> None:
    """oracle: specified —— Win32 文档 `KEYBDINPUT.dwFlags`：KEYEVENTF_KEYUP = 0x0002、
    KEYEVENTF_UNICODE = 0x0004。

    「这个事件是字面量字符还是合成的按键」全靠这两位，所以它们本身也要钉住：
    本文件其余断言用的就是这里的字面量。
    """
    assert w.KEYEVENTF_KEYUP == KEYUP_FLAG
    assert w.KEYEVENTF_UNICODE == UNICODE_FLAG


LINE_BREAK_TEXTS = ["a\nb", "a\rb", "a\r\nb", "\n"]


@pytest.mark.parametrize("text", LINE_BREAK_TEXTS, ids=["lf", "cr", "crlf", "lf-only"])
def test_type_text_routes_a_text_with_a_line_break_through_the_clipboard(
    spy: _SendSpy, clip: _ClipboardSpy, text: str
) -> None:
    """oracle: specified —— `type_text` docstring：含换行的文本走剪贴板插入，`detail` 里是
    `via=clipboard` + `reason=newline`，并带 `clipboard_restored`；`_has_line_break` docstring：
    `\n`、`\r\n`、单独的 `\r` 都算换行；`_type_via_clipboard` docstring：写入 → Ctrl+V →
    尽量恢复原剪贴板。

    写进剪贴板的**就是原文本**（含换行原样，不做 CRLF 折叠）与「原剪贴板被还回去」是
    本条路径的核心：契约把内容保真建立在 Win32 API 上，替身收到的参数就是这条保证的落点。
    derived 的部分：`set_text` 的调用顺序与 `get_text` 只被读一次。
    """
    result = input_mod.type_text(text)

    assert result.ok is True
    assert result.detail["via"] == "clipboard"
    assert result.detail["reason"] == "newline"
    assert result.detail["clipboard_restored"] is True
    assert clip.writes[0] == text, "写进剪贴板的必须是原文本，换行不折叠"
    assert clip.writes[1] == ORIGINAL_CLIPBOARD, "之后要把原剪贴板还回去"
    assert clip.reads == 1
    assert _unicode_events(spy) == [], "含换行的路径不投递任何 Unicode 事件"
    assert all(vk != VK_RETURN for vk, _ in _vk_events(spy)), "type 绝不合成 Enter 按键"
    # derived：这条路上唯一的按键事件是粘贴（Ctrl+V）。
    assert sorted(vk for vk, _ in _vk_events(spy)) == [VK_CONTROL, VK_CONTROL, VK_V, VK_V]


NO_LINE_BREAK_CASES = [
    ("", []),
    ("abc", [0x61, 0x62, 0x63]),
    ("中文", [0x4E2D, 0x6587]),
    ("a b", [0x61, 0x20, 0x62]),
    # 非 BMP：一个字符 = 两个码元（DEC-078），期望值是手算的代理对。
    ("\U0001F600", [0xD83D, 0xDE00]),
]


@pytest.mark.parametrize(("text", "expected"), NO_LINE_BREAK_CASES,
                         ids=["empty", "ascii", "cjk", "space", "non-bmp"])
def test_type_text_inserts_each_literal_character_without_any_vk_return(
    spy: _SendSpy, clip: _ClipboardSpy, text: str, expected: list[int]
) -> None:
    """oracle: specified —— `type_text` docstring：不含换行的文本逐字走 `KEYEVENTF_UNICODE`
    （中文无需剪贴板中转）；空文本直接返回 `ok=True, total_ms=0` 且不投递任何事件。

    期望码点是手算的字面量（`a`=0x61、空格=0x20、中=0x4E2D、文=0x6587），不经由被测代码产生。
    「不走剪贴板」与「每个码元一对事件」由契约直接给出 —— 非 BMP 字符是两个码元，
    所以那里的期望长度是 2 而不是字符数。

    注：契约说这条路的 `detail` 为 `None`，而当前实现返回 `{}`（`InputResult.detail` 的
    默认工厂是 `dict`）。这里只断言两种形态共同的可观测语义 —— `detail` 里没有路由信息 ——
    差异本身记在交付报告的缺陷清单里。
    """
    result = input_mod.type_text(text)

    assert result.ok is True
    detail = result.detail or {}
    assert detail.get("via") is None and detail.get("fallback") is None
    assert _unicode_downs(spy) == expected
    expected_flags = [UNICODE_FLAG, UNICODE_FLAG | KEYUP_FLAG] * len(expected)
    assert [item.ki.dwFlags for item in spy.events] == expected_flags
    assert [item.ki.wVk for item in spy.events] == [0] * (2 * len(expected))
    assert _vk_events(spy) == [], "type 绝不合成 Enter 按键，也不投递任何虚拟键"
    # derived：无换行就不该动剪贴板。
    assert clip.writes == []
    assert clip.reads == 0


# --------------------------------------------------------------------------- #
# 非 BMP 字符按 UTF-16 代理对投递（DEC-078）
# --------------------------------------------------------------------------- #


def _units_by_utf16_codec(text: str) -> list[int]:
    """独立口径：交给 Python 的 UTF-16LE 编解码器去拆。

    与 `_utf16_units` 的手算移位互为独立实现 —— 两者都受 UTF-16 规范约束，但不是同一段代码，
    所以能互相证伪。
    """
    raw = text.encode("utf-16-le")
    return [raw[index] | raw[index + 1] << 8 for index in range(0, len(raw), 2)]


NON_BMP_CASES = [
    ("\U0001F600", [0xD83D, 0xDE00]),
    ("\U0001F389", [0xD83C, 0xDF89]),
    ("\U00010000", [0xD800, 0xDC00]),
    ("\U0010FFFF", [0xDBFF, 0xDFFF]),
]


@pytest.mark.parametrize(("text", "expected"), NON_BMP_CASES,
                         ids=["grinning", "tada", "lowest", "highest"])
def test_utf16_units_splits_a_non_bmp_character_into_a_surrogate_pair(
    text: str, expected: list[int]
) -> None:
    """oracle: specified —— `_utf16_units` docstring ＋ UTF-16 规范：BMP 之外的码位是两个 16 位
    码元（高代理 `0xD800..0xDBFF`、低代理 `0xDC00..0xDFFF`）。

    期望值手算：`U+1F600` → `0xD83D` + `0xDE00`（`0x1F600 - 0x10000 = 0xF600`；
    高代理 `0xD800 + (0xF600 >> 10) = 0xD83D`，低代理 `0xDC00 + (0xF600 & 0x3FF) = 0xDE00`）。
    取最小与最大两个边界，是因为移位与掩码的错法在边界上才露出来。
    """
    assert input_mod._utf16_units(text) == expected
    # oracle: derived —— 手算值与编解码器的拆法必须一致（两份独立实现互证）。
    assert input_mod._utf16_units(text) == _units_by_utf16_codec(text)


@pytest.mark.parametrize("text", ["a", "\u4E2D", "\uFFFF"], ids=["ascii", "cjk", "bmp-max"])
def test_utf16_units_leaves_a_bmp_character_as_a_single_unit(text: str) -> None:
    """oracle: specified —— `_utf16_units` docstring：BMP 内（含上界 `U+FFFF`）就是一个码元，
    不做拆分；中文因此完全不受这条改动影响。
    """
    assert input_mod._utf16_units(text) == [ord(text)]


def test_type_text_delivers_a_non_bmp_character_without_truncating_it(
    spy: _SendSpy, clip: _ClipboardSpy
) -> None:
    """oracle: specified —— `type_text` docstring：非 BMP 字符按 UTF-16 拆成代理对投递。

    2026-09-14 真机实测的缺陷就是这条：`wScan` 只有 16 位，`0x1F600` 塞进去只剩低 16 位
    `0xF600`，落下去是**另一个字符**，既不报错也不失败。所以这里既断言两个码元都投出去，
    也断言那个截断值 `0xF600` **没有**出现在事件流里。
    """
    text = "emoji：\U0001F600\U0001F389 中文尾"

    result = input_mod.type_text(text)

    assert result.ok is True
    assert _unicode_downs(spy) == _units_by_utf16_codec(text)
    assert 0xF600 not in _unicode_downs(spy)
    assert [item.ki.dwFlags for item in spy.events] == \
        [UNICODE_FLAG, UNICODE_FLAG | KEYUP_FLAG] * len(_units_by_utf16_codec(text))
    assert _vk_events(spy) == []
    assert clip.reads == 0


def test_type_text_falls_back_to_the_clipboard_when_the_unicode_path_fails(
    monkeypatch: pytest.MonkeyPatch, clip: _ClipboardSpy
) -> None:
    """oracle: specified —— `type_text` docstring：Unicode 路径真失败而降级时 `detail` 是
    `fallback=clipboard` + `unicode_error`，并带 `clipboard_restored`。

    附带 derived：降级只在 Unicode 路径**真失败**时发生，且同样不得合成 Enter。
    """
    record = _install_spy(monkeypatch, fail_on_unicode=True)

    result = input_mod.type_text("abc")

    assert result.ok is True
    assert result.detail["fallback"] == "clipboard"
    assert result.detail["unicode_error"]
    assert result.detail["clipboard_restored"] is True
    assert clip.writes[0] == "abc"
    assert sorted(vk for vk, _ in _vk_events(record)) == [VK_CONTROL, VK_CONTROL, VK_V, VK_V]
    assert _unicode_events(record) == []
    # derived：降级只在 Unicode 路径真失败时才发生 —— 失败那次尝试确实提交过字面量事件。
    assert any(item.ki.dwFlags & UNICODE_FLAG for call in record.calls for item in call)


def test_type_text_still_inserts_when_the_clipboard_had_no_text(
    spy: _SendSpy, clip: _ClipboardSpy
) -> None:
    """oracle: derived —— `clipboard.get_text` docstring：剪贴板里没有文本时返回 `None`；
    `_type_via_clipboard` 只在 `previous is not None` 时回写。所以「本来就没文本」既不能中断
    插入，也不该多出一次回写；`clipboard_restored` 此时为 `False`（没有东西被还回去）。

    契约没有直接规定这个值的字面量，这一条按上面两段描述推出，如有异议以实现方的口径为准。
    """
    clip.current = None

    result = input_mod.type_text("a\nb")

    assert result.ok is True
    assert result.detail["via"] == "clipboard"
    assert clip.writes == ["a\nb"]
    assert result.detail["clipboard_restored"] is False


def test_type_text_reports_restore_failure_without_raising(spy: _SendSpy, clip: _ClipboardSpy) -> None:
    """oracle: specified —— `_type_via_clipboard` docstring：「恢复失败不报错，但如实记进 detail」。

    第 2 次写入（回写原剪贴板）失败时：文本已经插进去了，所以整体仍然 `ok=True`，
    但 `clipboard_restored` 必须是 `False` —— 用户有权知道自己的剪贴板被动过。
    """
    clip.fail_writes = {1}

    result = input_mod.type_text("a\nb")

    assert result.ok is True
    assert clip.writes == ["a\nb"], "失败的那次回写不留下内容"
    assert result.detail["clipboard_restored"] is False


@pytest.mark.parametrize(("text", "fail_unicode"), [("a\nb", False), ("abc", True)],
                         ids=["line-break-path", "unicode-fallback-path"])
def test_type_text_raises_when_the_clipboard_write_fails(
    monkeypatch: pytest.MonkeyPatch, clip: _ClipboardSpy, text: str, fail_unicode: bool
) -> None:
    """oracle: specified —— `clipboard.set_text` docstring：写失败抛 `CUError`；
    `_type_via_clipboard` 的写入失败同样抛（不静默失败，否则调用方以为内容已经粘进去了）。

    无换行路径的降级由同一个函数服务，所以两条路都在这里断一遍。
    """
    _install_spy(monkeypatch, fail_on_unicode=fail_unicode)
    clip.fail_writes = {0}

    with pytest.raises(CUError):
        input_mod.type_text(text)


@pytest.mark.parametrize("combo", ["win+l", "ctrl+alt+del"])
def test_key_blocks_a_blacklisted_combination_with_dangerous_key_blocked(
    spy: _SendSpy, combo: str
) -> None:
    """oracle: specified —— `key` docstring「命中黑名单则拒绝」；SKILL.md 121："A blocked
    combination fails with `dangerous_key_blocked`"。

    附带 derived：拒绝时**一个输入事件都不投递** —— 报错之后仍然把键按下去等于没拦住。
    """
    with pytest.raises(CUError) as excinfo:
        input_mod.key(combo)

    assert excinfo.value.code is ErrorCode.DANGEROUS_KEY_BLOCKED
    assert spy.events == []


def test_key_force_bypasses_the_default_blocklist(spy: _SendSpy) -> None:
    """oracle: specified —— `key` docstring「`force=True` 越过」；VK 值取自 Win32 虚拟键码表。"""
    result = input_mod.key("win+l", force=True)

    assert result.ok is True
    assert sorted(vk for vk, _ in _vk_events(spy)) == [VK_L, VK_L, VK_LWIN, VK_LWIN]
    assert sorted(is_up for _, is_up in _vk_events(spy)) == [False, False, True, True]


def test_key_honours_a_custom_danger_keys_set(spy: _SendSpy) -> None:
    """oracle: specified —— SKILL.md 121：「以及 `danger_keys` 里的任何组合」也在拦截之列。"""
    with pytest.raises(CUError) as excinfo:
        input_mod.key("f13", danger_keys=frozenset({"f13"}))

    assert excinfo.value.code is ErrorCode.DANGEROUS_KEY_BLOCKED
    assert spy.events == []

    # 未列入黑名单的组合在同样的调用形态下必须放行。
    input_mod.key("f13", danger_keys=frozenset({"win+l"}))
    assert _vk_events(spy) == [(VK_F13, False), (VK_F13, True)]


@pytest.mark.parametrize(
    ("combo", "vk"),
    [("enter", VK_RETURN), ("win", VK_LWIN), ("esc", VK_ESCAPE), ("f13", VK_F13)],
)
def test_key_presses_a_single_key_with_the_same_syntax_as_a_combination(
    spy: _SendSpy, combo: str, vk: int
) -> None:
    """oracle: specified —— `_parse_combo` docstring「单键与组合键同一套写法」并点名了
    `enter` / `win` / `esc` / `f13`；SKILL.md 121 "Single keys and combinations share one syntax"。

    「要按 Enter 用 `key enter`」的落点就在这里：单键 `enter` 走这条路，产生 VK_RETURN 的
    按下与抬起各一次。VK 值取自 Win32 虚拟键码表。
    """
    input_mod.key(combo)

    assert _vk_events(spy) == [(vk, False), (vk, True)]


@pytest.mark.parametrize(
    ("combo", "expected"),
    [("ctrl+s", [VK_CONTROL, VK_S]), ("alt+tab", [VK_MENU, VK_TAB])],
)
def test_key_presses_every_vk_of_a_combination_exactly_once_down_before_up(
    spy: _SendSpy, combo: str, expected: list[int]
) -> None:
    """oracle: specified —— 组合键的 VK 序列取自 Win32 虚拟键码表（`ctrl+s` → VK_CONTROL + 'S'，
    `alt+tab` → VK_MENU + VK_TAB）；derived —— 「按下」的语义是同一个 VK 恰好按下一次、
    抬起一次，且按下在抬起之前。

    刻意**不**断言不同键之间按下/抬起的交错顺序：契约未规定，反向抬起与正向抬起都满足契约。
    """
    input_mod.key(combo)

    presses = _vk_events(spy)
    assert sorted(vk for vk, _ in presses) == sorted([*expected, *expected])
    for vk in expected:
        downs = [index for index, (seen, is_up) in enumerate(presses) if seen == vk and not is_up]
        ups = [index for index, (seen, is_up) in enumerate(presses) if seen == vk and is_up]
        assert len(downs) == 1 and len(ups) == 1, f"{vk:#x} 必须恰好按下一次、抬起一次"
        assert downs[0] < ups[0], f"{vk:#x} 必须先按下再抬起"


@pytest.mark.parametrize(
    ("combo", "expected"),
    [
        ("enter", [VK_RETURN]),
        ("esc", [VK_ESCAPE]),
        ("win", [VK_LWIN]),
        ("f13", [VK_F13]),
        ("ctrl+s", [VK_CONTROL, VK_S]),
        ("alt+tab", [VK_MENU, VK_TAB]),
        ("ctrl+shift+f10", [VK_CONTROL, VK_SHIFT, VK_F10]),
    ],
)
def test_parse_combo_maps_the_documented_names_to_vk_codes(combo: str, expected: list[int]) -> None:
    """oracle: specified —— `_parse_combo` docstring 点名的写法（`ctrl+shift+f10` / `ctrl+c` /
    `alt+tab` / 单键 `enter` / `win` / `esc` / `f13`）＋ Win32 虚拟键码表。

    期望值是手算的 VK 字面量，不经由被测代码产生。
    """
    assert input_mod._parse_combo(combo) == expected


@pytest.mark.parametrize("token", ["s", "enter", "f13"])
def test_parse_combo_maps_a_token_the_same_alone_and_inside_a_combination(token: str) -> None:
    """oracle: derived（变形关系）—— 「单键与组合键同一套写法」要求同一个 token 无论单独出现
    还是出现在组合里都映射到同一个 VK：`_parse_combo("ctrl+" + t)` 的末位必须等于
    `_parse_combo(t)` 的首位，且组合以 VK_CONTROL 开头。

    这条关系不依赖任何一个具体 VK 值，因此与上面那张手算表互相独立。
    """
    alone = input_mod._parse_combo(token)
    combined = input_mod._parse_combo(f"ctrl+{token}")

    assert len(alone) == 1
    assert combined[0] == VK_CONTROL
    assert combined[-1] == alone[0]


@pytest.mark.parametrize("combo", ["", "+", "f99", "not-a-key"])
def test_parse_combo_rejects_an_empty_or_unknown_name_with_invalid_params(combo: str) -> None:
    """oracle: specified —— 契约：空串 / 未知按键名抛 `CUError(ErrorCode.INVALID_PARAMS)`，
    **绝不返回空表**（空表会让 `key` 变成一次静默的无操作，调用方却以为键已按下）。
    """
    with pytest.raises(CUError) as excinfo:
        input_mod._parse_combo(combo)

    assert excinfo.value.code is ErrorCode.INVALID_PARAMS
