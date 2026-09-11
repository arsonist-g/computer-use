"""``ids.py`` 的契约测试 —— session_id / title_slug / 工件文件名 / 解析产物判定。

期望值来源（oracle）逐条标注：

- ``specified`` —— 直接来自权威契约：``data-model.md`` §3.2（命名规则与逐字示例）、
  §3.3（session.json 示例里的时间格式）、§3.6（hwnd / seq 约束），
  以及 ``DEC-012``（标题 slug 规则、字典序=时间序）。
- ``derived`` —— 由契约条款推导出的性质（往返一致、单调、字典序等），
  以及本文件末尾对命名形态的解歧结论。
- ``implicit`` —— 契约未规定、只钉住当前实现细节的行为（如后缀大小写敏感）。

绝不从当前实现的输出反推期望值。

**命名形态的契约冲突与解歧**：``data-model.md`` §3.2 的模式行写 ``[-omni]``（连字符、
后缀在坐标之前），而 §3.1 / §3.3 与 ``DEC-012`` / ``DEC-025`` / ``decisions.md`` D8 的
示例写 ``_omni``（下划线）。实现采用的形态（本文件按此钉住）：

- 有坐标（窗口 / 全屏）：后缀在坐标之前、**连字符**，``win-…-omni-100x200-0004.md``；
- 无坐标（img 外部图片）：``_omni`` 贴标题、seq 紧跟，``img-photo_omni-0007.md``。

这两条因此标 ``derived``，不是逐字 ``specified``。

**DEC-043（文件名一律纯 ASCII）**：``slug_title`` 在 DEC-012 的四条规则之上，先把常见
非 ASCII 标点转写为 ASCII、再把其余非 ASCII 字符折叠为 ``_``，故纯中文标题落为
``untitled``。DEC-012「不翻译」仍成立：窗口标题**原样**保存在 ``session.json`` 的
``window.title`` 字段里（见 ``test_slug_title_folds_non_ascii_but_window_title_keeps_original``）。

**本轮核实到的缺口（见交付报告）**：上面的**分隔符**解歧并未落盘 —— ``DEC-043`` 记的是
ASCII 文件名而非分隔符（``ids.py`` 里「这条冲突已记入 DEC-043」的注释系误挂）；被钉住的
分隔符形态与已记录的 §3.1/§3.3/DEC-012/DEC-025/D8 示例不一致；且 ``is_parsed_name``
两种落点（有坐标 / 无坐标）都必须认——这条跨模块不变量由 test_storage_ids_consistency.py 守卫。
"""

from __future__ import annotations

import os
import random
import re
from datetime import datetime, timedelta, timezone

import pytest

from cu.errors import CUError, ErrorCode
from cu.ids import (
    artifact_name,
    format_hwnd,
    is_parsed_name,
    new_session_id,
    now_iso,
    parse_hwnd,
    slug_title,
)

# 契约常量（来自 data-model.md §3.2：标题截断 40 字符）。
TITLE_SLUG_MAX_SPEC = 40

# session_id 的随机后缀字符集（DEC-012 的示例用 4 位小写字母数字）。
_SESSION_ID_RE = re.compile(r"^s-\d{8}-\d{6}-[0-9a-z]{4}$")


# ---------------------------------------------------------------------------
# now_iso —— ISO-8601 带时区偏移、毫秒精度（§3.6 / §3.3 示例）
# ---------------------------------------------------------------------------


def test_now_iso_formats_explicit_moment_like_spec_example() -> None:
    moment = datetime(2026, 9, 12, 10, 30, 0, 123000, tzinfo=timezone(timedelta(hours=8)))
    # oracle: specified —— data-model.md §3.3 示例 created_at 字面量
    assert now_iso(moment) == "2026-09-12T10:30:00.123+08:00"


def test_now_iso_default_has_offset_and_millisecond_precision() -> None:
    value = now_iso()
    # oracle: specified —— §3.6「ISO-8601 带时区偏移，毫秒精度」
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}", value)
    # oracle: derived —— 允许被 datetime.fromisoformat 无损解析（消费方是 AI 与人）
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# new_session_id —— s-YYYYMMDD-HHMMSS-xxxx（§3.1 / §3.3 示例）
# ---------------------------------------------------------------------------


def test_new_session_id_shape_matches_spec() -> None:
    moment = datetime(2026, 9, 12, 10, 30, 0)
    # oracle: specified —— DEC-012 / §3.1：s-YYYYMMDD-HHMMSS-<4 位随机后缀>
    assert _SESSION_ID_RE.match(new_session_id(moment))
    # oracle: specified —— 时间部分取自入参 moment（仅后 4 位是随机）
    assert new_session_id(moment).startswith("s-20260912-103000-")


def test_new_session_id_lexicographic_order_follows_time() -> None:
    earlier = new_session_id(datetime(2026, 9, 12, 10, 30, 0))
    later = new_session_id(datetime(2026, 9, 12, 10, 30, 1))
    # oracle: derived —— 清理按目录名字典序排（§3.1「字典序 = 创建时间序」）
    assert earlier < later


def test_new_session_id_unique_within_same_second() -> None:
    # 随机后缀是「同一秒内不撞名」的唯一保证；固定种子使其确定可复现。
    random.seed(20260912)
    moment = datetime(2026, 9, 12, 10, 30, 0)
    ids = [new_session_id(moment) for _ in range(300)]
    # oracle: specified —— DEC-012「永不撞名」；同秒多次调用必须互不相同
    assert len(set(ids)) == 300


# ---------------------------------------------------------------------------
# slug_title —— DEC-012：替换非法字符 → 折叠空白 → 截断 40 → 空则 untitled
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "",              # 空串
        "   ",           # 全空白
        "\t\r\n",        # 控制字符（\x00-\x1f）
        "\x00\x01\x1f",  # 纯控制字符
        "???",           # 全是非法字符
        "///",
        '\\:*?"<>|',     # 全是文件名保留字符
    ],
)
def test_slug_title_falls_back_to_untitled(title: str) -> None:
    # oracle: specified —— DEC-012：清理后为空则用 untitled
    assert slug_title(title) == "untitled"


def test_slug_title_removes_filename_reserved_characters() -> None:
    # oracle: specified —— DEC-012：替换 \ / : * ? " < > | 与控制字符
    assert slug_title('a\\b/c:d*e?f"g<h>i|j') == "abcdefghij"


def test_slug_title_removes_control_characters() -> None:
    # oracle: specified —— 控制字符（\x00-\x1f）逐字符过滤
    assert slug_title("a\x00b\x1fc") == "abc"


def test_slug_title_collapses_whitespace_runs() -> None:
    # oracle: specified —— DEC-012「折叠连续空白」
    assert slug_title("a    b") == "a b"
    # oracle: derived —— 首尾空白同样被折叠后的 strip 清掉
    assert slug_title("  lead trail  ") == "lead trail"


def test_slug_title_truncates_to_forty_characters() -> None:
    # oracle: specified —— DEC-012：截断 40 字符
    assert slug_title("a" * 60) == "a" * TITLE_SLUG_MAX_SPEC
    # oracle: derived —— 边界：恰好 40 与 39 不截断
    assert slug_title("a" * 40) == "a" * 40
    assert slug_title("a" * 39) == "a" * 39


def test_slug_title_folds_non_ascii_to_ascii() -> None:
    # oracle: specified —— DEC-043：在 DEC-012 四条之上，非 ASCII 字符折叠为 "_"，
    #   折叠前先对常见标点做 ASCII 转写；连续 [-_] 再压成一个 "-"。
    # 先转写再折叠：· → "-"（而不是下划线）
    assert slug_title("Computer-Use · Control Overlay (/d%3A/De") == "Computer-Use - Control Overlay (d%3ADe"
    # 全角空格是非 ASCII，折叠为 "_" 后与相邻 "_" 压成 "-"
    assert slug_title("a　　b") == "a-b"
    # 纯中文 / emoji 全部折叠、strip 后为空 → untitled
    assert slug_title("未命名 - 记事本") == "untitled"
    assert slug_title("记事本 🎯 完成") == "untitled"
    # 全角字母同样非 ASCII
    assert slug_title("ａｂｃ") == "untitled"


def test_slug_title_folds_non_ascii_but_window_title_keeps_original() -> None:
    # oracle: specified —— DEC-043：「不翻译」仍成立，被 ASCII 化的只是**文件名**；
    #   窗口标题原样保存在 session.json 的 window.title（复盘读的是那里）。
    from cu.manifest import WindowRef

    assert slug_title("记事本 - 未命名") == "untitled"
    ref = WindowRef(title="记事本 - 未命名")
    # oracle: derived —— 落盘的 window.title 与读回的内存字段都保留原文（不经过 slug）
    assert ref.to_dict()["title"] == "记事本 - 未命名"
    assert WindowRef.from_dict(ref.to_dict()).title == "记事本 - 未命名"
    assert WindowRef(title="📁 文件夹").to_dict()["title"] == "📁 文件夹"


def test_slug_title_neutralizes_path_traversal() -> None:
    slug = slug_title("../../etc/passwd")
    # oracle: specified —— 分隔符是保留字符，必须被清掉
    assert slug == "....etcpasswd"
    # oracle: derived —— 清掉后不再含任何路径分隔符，无法逃出会话目录
    assert "/" not in slug
    assert "\\" not in slug


# ---------------------------------------------------------------------------
# format_hwnd / parse_hwnd —— §3.6：hwnd 统一 0x%08X
# ---------------------------------------------------------------------------


def test_format_hwnd_uses_fixed_eight_hex_digits() -> None:
    # oracle: specified —— §3.6：hwnd 一律 `0x%08X`
    assert format_hwnd(0x1A2B) == "0x00001A2B"
    assert format_hwnd(0) == "0x00000000"
    assert format_hwnd(1) == "0x00000001"
    assert format_hwnd(0xFFFFFFFF) == "0xFFFFFFFF"


def test_format_hwnd_passes_string_through_unchanged() -> None:
    # oracle: derived —— 已是字符串则不再格式化（防 int/str 混用，见 ids.py docstring）
    assert format_hwnd("0x1A2B") == "0x1A2B"


def test_parse_hwnd_accepts_hex_decimal_and_int() -> None:
    # oracle: specified —— 接受 0x 十六进制与十进制两种写法
    assert parse_hwnd("0x1A2B") == 0x1A2B
    assert parse_hwnd("0X1a2b") == 0x1A2B
    assert parse_hwnd("6699") == 6699
    assert parse_hwnd(0x1A2B) == 0x1A2B
    # oracle: derived —— 首尾空白被容忍
    assert parse_hwnd(" 0x1A2B ") == 0x1A2B


@pytest.mark.parametrize("bad", ["", "   ", "zzz", "0xZZZ", "0x", "12.5", "1e3", "abc"])
def test_parse_hwnd_rejects_invalid_input_with_invalid_params(bad: str) -> None:
    with pytest.raises(CUError) as excinfo:
        parse_hwnd(bad)
    # oracle: specified —— 非法输入抛 CUError(INVALID_PARAMS)
    assert excinfo.value.code is ErrorCode.INVALID_PARAMS


def test_parse_hwnd_round_trips_with_format_hwnd() -> None:
    for value in (0, 1, 0x1A2B, 0xFFFFFFFF, 123456):
        # oracle: derived —— format 与 parse 互为逆运算（§3.6 统一格式）
        assert parse_hwnd(format_hwnd(value)) == value


# ---------------------------------------------------------------------------
# artifact_name —— data-model.md §3.2 命名规则
# ---------------------------------------------------------------------------


def test_artifact_name_screenshot_forms_match_spec_shape_with_ascii_titles() -> None:
    # oracle: specified —— §3.2 表格的骨架（{kind}-{hwnd}-{title}-{x}x{y}-{seq}）仍然成立；
    #   title 经 DEC-043 的 ASCII 折叠后，原示例里的中文标题落为 untitled。
    assert (
        artifact_name("full", 1, hwnd="0x00000000", title="全屏", origin=(0, 0))
        == "full-0x00000000-untitled-0x0-0001.png"
    )
    assert (
        artifact_name("win", 3, hwnd="0x0001A2B", title="未命名-记事本", origin=(100, 200))
        == "win-0x0001A2B-untitled-100x200-0003.png"
    )
    # oracle: derived —— 换成 ASCII 标题即与 §3.2 表格骨架逐字一致
    assert (
        artifact_name("win", 3, hwnd="0x0001A2B", title="notepad", origin=(100, 200))
        == "win-0x0001A2B-notepad-100x200-0003.png"
    )


def test_artifact_name_zero_pads_seq_to_four_digits() -> None:
    def name_for(seq: int) -> str:
        return artifact_name("full", seq, hwnd="0x00000000", title="全屏", origin=(0, 0))

    # oracle: specified —— DEC-012：序号零填充 4 位
    assert name_for(1).endswith("-0001.png")
    assert name_for(42).endswith("-0042.png")
    assert name_for(1234).endswith("-1234.png")
    # oracle: derived —— 超过 4 位不截断（仍是完整整数）
    assert name_for(9999).endswith("-9999.png")
    assert name_for(10000).endswith("-10000.png")


def test_artifact_name_seq_orders_lexicographically() -> None:
    seqs = [1, 2, 9, 10, 11, 99, 100]
    names = [
        artifact_name("win", s, hwnd="0x0001A2B", title="t", origin=(0, 0)) for s in seqs
    ]
    # oracle: derived —— 零填充使字典序 = 时间序（DEC-012；清理据此排序）
    assert names == sorted(names)


def test_artifact_name_omni_and_omni_ai_do_not_collide() -> None:
    base = artifact_name("win", 4, hwnd="0x0001A2B", title="t", suffix="omni",
                         origin=(100, 200), ext="md")
    optimized = artifact_name("win", 4, hwnd="0x0001A2B", title="t", suffix="omni_ai",
                              origin=(100, 200), ext="md")
    # oracle: specified —— DEC-011：base 与 ai 是两种工件，同一 seq 下必须不同名，
    #                     否则后写的会覆盖前一个
    assert base != optimized
    # oracle: derived —— 两者都可通过后缀识别，且都带零填充 seq
    assert "omni" in base
    assert "omni_ai" in optimized
    assert "0004" in base and "0004" in optimized


def test_artifact_name_img_short_mode_omits_hwnd_and_coordinates() -> None:
    name = artifact_name("img", 7, title="photo", suffix="omni", ext="md")
    # oracle: specified —— §3.2：img 走短模式，无 hwnd、无坐标
    assert name.startswith("img-")
    assert name.endswith(".md")
    assert "photo" in name
    assert "omni" in name
    # oracle: derived —— 短模式下不应出现 hwnd（0x…）与坐标（NxM）
    assert "0x" not in name
    assert re.search(r"\d+x\d+", name) is None
    # oracle: specified —— 仍带零填充序号，保证同会话字典序 = 时间序
    assert "0007" in name


def test_artifact_name_neutralizes_malicious_title() -> None:
    name = artifact_name("win", 1, hwnd="0x00000001", title="../../etc/passwd", origin=(0, 0))
    # oracle: specified —— 标题先过 slug_title，非法字符被清掉
    assert "/" not in name
    assert "\\" not in name
    # oracle: derived —— 结果必须是一个纯文件名，basename 即全名，不能逃出会话目录
    assert os.path.basename(name) == name


# ---------------------------------------------------------------------------
# 命名形态的解歧 —— §3.2 模式行（连字符）vs 示例（下划线）。
# 下面钉住 artifact_name 的实际产出，再单测 is_parsed_name 的判定。
# 注意：这处**分隔符**解歧并未落盘到 project-memory（DEC-043 记的是 ASCII 文件名）。
# ---------------------------------------------------------------------------


def test_artifact_name_img_form_follows_disambiguation() -> None:
    # oracle: derived —— 后缀两侧统一用连字符、且落在 seq 之前；无坐标时省掉坐标段。
    #   契约的表格/示例（data-model.md §3.2 / decisions.md D8 / DEC-025）把这些写成
    #   「seq 在前、_omni 收尾」或下划线形态，三处互不一致；取模式行的连字符最自洽，
    #   也让 is_parsed_name 只需一条判定规则。
    assert (
        artifact_name("img", 7, title="photo", suffix="omni", ext="md")
        == "img-photo-omni-0007.md"
    )
    # oracle: derived —— AI 优化形态同理
    assert (
        artifact_name("img", 8, title="photo", suffix="omni_ai", ext="md")
        == "img-photo-omni_ai-0008.md"
    )


def test_artifact_name_window_omni_form_follows_disambiguation() -> None:
    # oracle: derived —— §3.2 模式行把后缀写在坐标之前（`[-omni]`，连字符）；
    #   §3.1/§3.3/DEC-012 的示例写 `_omni`（下划线）。实现采用连字符落点。
    #   中文标题经 DEC-043 折叠为 untitled（改用 ASCII 标题即得可读名字）。
    assert (
        artifact_name("win", 4, hwnd="0x0001A2B", title="未命名-记事本", suffix="omni",
                      origin=(100, 200), ext="md")
        == "win-0x0001A2B-untitled-omni-100x200-0004.md"
    )
    assert (
        artifact_name("win", 4, hwnd="0x0001A2B", title="notepad", suffix="omni",
                      origin=(100, 200), ext="md")
        == "win-0x0001A2B-notepad-omni-100x200-0004.md"
    )
    # oracle: derived —— AI 优化形态同理，同一 seq 下与 base 不同名（DEC-011）
    assert (
        artifact_name("win", 5, hwnd="0x0001A2B", title="notepad", suffix="omni_ai",
                      origin=(100, 200), ext="md")
        == "win-0x0001A2B-notepad-omni_ai-100x200-0005.md"
    )


# ---------------------------------------------------------------------------
# DEC-043 守卫 —— artifact_name 的**任意**产出都必须是纯 ASCII。
# 交给 windows-capture 的文件名一旦含非 ASCII，库会按本地代码页往返改写路径，
# 文件被写到别处、stat 落空、截图整体失效。这条守卫在此拦死：以后谁放开 slug
# 规则，它立刻红，而不是等用户发现截图文件不见了。标题原文另存 session.json。
# ---------------------------------------------------------------------------

_ASCII_GUARD_TITLES = [
    "未命名 - 记事本",            # 纯中文
    "全屏",
    "记事本 🎯 完成",              # emoji
    "Καλημέρα",                   # 希腊文
    "Привет",                     # 西里尔
    "日本語タイトル",              # 日文
    "ａｂｃ",                      # 全角字母
    "·—–−“”‘’…×©®™€£¥→←≤≥≠",     # 常见非 ASCII 标点（转写表覆盖）
    "a\tb\nc",                    # 空白 / 控制字符
    "a\x00b\x1fc",                # 控制字符
    'a\\b/c:d*e?f"g<h>i|j',       # 路径分隔符与文件系统保留字符
    "../../etc/passwd",           # 路径穿越
    "a" * 300,                    # 超长（截断后仍须 ASCII）
    "",
    "   ",
    "???",
]

_ASCII_GUARD_BUILDERS = [
    pytest.param(
        lambda t: artifact_name("win", 1, hwnd="0x00000001", title=t, origin=(0, 0)),
        id="window-png",
    ),
    pytest.param(
        lambda t: artifact_name("full", 1, hwnd="0x00000000", title=t, origin=(0, 0)),
        id="fullscreen-png",
    ),
    pytest.param(
        lambda t: artifact_name("win", 1, hwnd="0x00000001", title=t, suffix="omni",
                                origin=(0, 0), ext="md"),
        id="window-omni-md",
    ),
    pytest.param(
        lambda t: artifact_name("win", 1, hwnd="0x00000001", title=t, suffix="omni_ai",
                                origin=(0, 0), ext="md"),
        id="window-omni-ai-md",
    ),
    pytest.param(
        lambda t: artifact_name("img", 1, title=t, suffix="omni", ext="md"),
        id="img-omni-md",
    ),
]


@pytest.mark.parametrize("build", _ASCII_GUARD_BUILDERS)
@pytest.mark.parametrize("title", _ASCII_GUARD_TITLES)
def test_artifact_name_output_is_always_pure_ascii(build, title: str) -> None:
    name = build(title)
    # oracle: derived —— DEC-043：文件名一律纯 ASCII（交给 windows-capture 的前提）
    assert name.isascii()
    # oracle: derived —— 结果仍是单个文件名，不含路径分隔符，无法逃出会话目录
    assert os.path.basename(name) == name
    assert "/" not in name and "\\" not in name


# ---------------------------------------------------------------------------
# is_parsed_name —— 「这个 md 是不是图片解析出的结构化数据」的唯一判定入口。
# 配额清理（DEC-017 阶段 1）靠它决定哪些 md 可随来源图片一起删：
# 判定过宽会删掉操作日志，过窄会让结构化数据只能等到阶段 2 才消失。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "img-photo-omni-0007.md",     # artifact_name 的 img 产出（无坐标，省掉坐标段）
        "img-photo-omni_ai-0008.md",  # 同上，AI 优化形态
    ],
)
def test_is_parsed_name_accepts_no_coord_forms(name: str) -> None:
    # oracle: derived —— 无坐标形态（外部图片）的产出集合
    assert is_parsed_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "win-0x0001A2B-notepad-omni-100x200-0004.md",
        "win-0x0001A2B-notepad-omni_ai-100x200-0005.md",
    ],
)
def test_is_parsed_name_accepts_hyphen_window_forms(name: str) -> None:
    # oracle: derived —— 有坐标时后缀在坐标之前、用连字符（artifact_name 的实际产出）
    assert is_parsed_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "ops.md",        # 操作日志：DEC-006 / DEC-017 要求保留到阶段 2，绝不能误判
        "session.json",  # 清单，非解析产物
        "photo.png",     # 普通图片走图片后缀分支，不靠本函数
        "notes.md",      # 不含 omni 后缀的普通 md
        "",              # 空串
    ],
)
def test_is_parsed_name_rejects_non_artifact_names(name: str) -> None:
    # oracle: specified / derived —— DEC-017 阶段 1 只删图片与其结构化数据 md，ops.md 必须留
    assert is_parsed_name(name) is False


@pytest.mark.parametrize("name", ["img-photo_OMNI.md", "img-photo_OMNI-0007.md"])
def test_is_parsed_name_suffix_match_is_case_sensitive(name: str) -> None:
    # oracle: implicit —— 契约未规定后缀大小写；此处只钉住当前实现细节：比较大小写敏感。
    # 这是实现细节而非契约要求（Windows 文件系统大小写不敏感，_OMNI.md 与 _omni.md 会同名）。
    assert is_parsed_name(name) is False
