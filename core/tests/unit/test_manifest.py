"""``manifest.py`` 的契约测试 —— session.json 的序列化模型。

期望值来源（oracle）逐条标注：

- ``specified`` —— 直接来自权威契约：``data-model.md`` §3.3（session.json 形状与示例 JSON）、
  §3.6（status 封闭枚举、seq 单调性、source_seq 校验），以及 ``DEC-011``（model_name 仅 ai）。
- ``derived`` —— 由契约条款推导出的性质（往返一致、宽容读取、计数器连续）。

绝不从当前实现的输出反推期望值。
"""

from __future__ import annotations

import pytest

from cu.manifest import (
    DisplayContext,
    MonitorInfo,
    ScreenshotRecord,
    SessionManifest,
    StructuredRecord,
    WindowRef,
)

# oracle: specified —— data-model.md §3.3 的示例 JSON，逐字抄录（含中文标题）。
SPEC_SECTION_3_3_EXAMPLE: dict = {
    "schema": 1,
    "session_id": "s-20260912-103000-a7k2",
    "created_at": "2026-09-12T10:30:00.123+08:00",
    "ended_at": None,
    "status": "active",
    "agent_hint": "claude-code",
    "display": {
        "primary_index": 0,
        "monitors": [
            {"index": 0, "rect": [0, 0, 3440, 1440], "primary": True, "dpi": 120, "scale": 1.25}
        ],
    },
    "screenshots": [
        {
            "seq": 3,
            "kind": "window",
            "file": "win-0x0001A2B-未命名-记事本-100x200-0003.png",
            "format": "png",
            "width": 800,
            "height": 600,
            "window": {
                "hwnd": "0x0001A2B",
                "pid": 1234,
                "process": "notepad.exe",
                "title": "未命名 - 记事本",
                "class": "Notepad",
                "rect": [100, 200, 900, 800],
                "monitor": 0,
            },
        }
    ],
    "structured": [
        {
            "seq": 4,
            "kind": "base",
            "file": "win-0x0001A2B-未命名-记事本_omni-100x200-0004.md",
            "source_seq": 3,
            "source_image_path": None,
            "element_count": 137,
        }
    ],
}


# ---------------------------------------------------------------------------
# WindowRef —— 落盘用 "class"，内存用 klass（§3.3 示例）
# ---------------------------------------------------------------------------


def _example_window() -> WindowRef:
    return WindowRef(
        hwnd="0x0001A2B", pid=1234, process="notepad.exe", title="example title",
        klass="Notepad", rect=[100, 200, 900, 800], monitor=0,
    )


def test_window_ref_to_dict_uses_class_key_not_klass() -> None:
    data = _example_window().to_dict()
    # oracle: specified —— §3.3 示例 window 对象用键 "class"
    assert "class" in data
    # oracle: derived —— 内存属性名 klass 不得出现在落盘结构里
    assert "klass" not in data
    # oracle: specified —— 值与内存属性一致
    assert data["class"] == "Notepad"


def test_window_ref_round_trips_class_field() -> None:
    ref = _example_window()
    restored = WindowRef.from_dict(ref.to_dict())
    # oracle: derived —— to_dict / from_dict 往返一致
    assert restored == ref
    # oracle: specified —— 读回后内存属性仍是 klass
    assert restored.klass == "Notepad"


def test_window_ref_of_formats_hwnd_contract_style() -> None:
    ref = WindowRef.of(0x1A2B, 1234, "notepad.exe", "example", "Notepad", (100, 200, 900, 800))
    # oracle: specified —— §3.6：hwnd 经 format_hwnd 统一为 0x%08X
    assert ref.hwnd == "0x00001A2B"


# ---------------------------------------------------------------------------
# ScreenshotRecord —— 全屏不写 window 键（§3.3 / §1.1）
# ---------------------------------------------------------------------------


def test_full_screenshot_omits_window_key_entirely() -> None:
    record = ScreenshotRecord(seq=1, kind="full", file="full-0x00000000-全屏-0x0-0001.png")
    data = record.to_dict()
    # oracle: specified —— 全屏截图无目标窗口：契约要求「不写 window 键」，不是写 null
    assert "window" not in data


def test_full_screenshot_reads_back_without_window() -> None:
    record = ScreenshotRecord.from_dict({"seq": 1, "kind": "full", "file": "f.png"})
    # oracle: specified —— 全屏记录读回后没有目标窗口
    assert record.window is None


def test_window_screenshot_round_trips_window_ref() -> None:
    record = ScreenshotRecord(
        seq=3, kind="window", file="win-0x0001A2B-example-100x200-0003.png",
        width=800, height=600, origin=[100, 200], layer=1, window=_example_window(),
    )
    restored = ScreenshotRecord.from_dict(record.to_dict())
    # oracle: derived —— 带窗口的截图往返一致（含 window.class）
    assert restored == record
    # oracle: specified —— 窗口类经 class 键读回
    assert restored.window is not None
    assert restored.window.klass == "Notepad"


# ---------------------------------------------------------------------------
# StructuredRecord —— model_name 仅 ai（DEC-011 / §3.3）
# ---------------------------------------------------------------------------


def test_base_structured_record_writes_no_model_name() -> None:
    record = StructuredRecord(
        seq=4, kind="base", file="win-0x0001A2B-example_omni-100x200-0004.md",
        source_seq=3, element_count=137,
    )
    data = record.to_dict()
    # oracle: specified —— DEC-011：model_name 只在 ai 形态出现，base 写了就是撒谎
    assert "model_name" not in data


def test_ai_structured_record_carries_model_name_and_round_trips() -> None:
    record = StructuredRecord(
        seq=5, kind="ai", file="win-0x0001A2B-example_omni_ai-100x200-0005.md",
        source_seq=3, element_count=10, model_name="example-vlm",
    )
    data = record.to_dict()
    # oracle: specified —— ai 形态带 model_name
    assert data["model_name"] == "example-vlm"
    restored = StructuredRecord.from_dict(data)
    # oracle: derived —— 往返一致
    assert restored == record


# ---------------------------------------------------------------------------
# SessionManifest 的宽容读取（模块 docstring：字段缺失补默认、未知字段忽略）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "nope", 42, [], ["x"], (), True])
def test_from_dict_non_dict_returns_empty_manifest(raw: object) -> None:
    manifest = SessionManifest.from_dict(raw)
    # oracle: specified —— 非 dict 输入返回空清单，不抛异常
    assert manifest.session_id == ""
    assert manifest.status == "active"
    assert manifest.screenshots == []
    assert manifest.structured == []
    assert manifest.seq_counter == 0


def test_from_dict_ignores_unknown_fields() -> None:
    manifest = SessionManifest.from_dict(
        {
            "session_id": "s-example",
            "future_top_level": {"x": 1},
            "screenshots": [{"seq": 1, "future_field": "y", "unknown": [1, 2]}],
        }
    )
    # oracle: specified —— 未知字段忽略（前向兼容），不影响已知字段
    assert manifest.session_id == "s-example"
    assert len(manifest.screenshots) == 1
    assert manifest.screenshots[0].seq == 1
    data = manifest.to_dict()
    # oracle: derived —— 未知识别字段不会回写
    assert "future_top_level" not in data
    assert "future_field" not in data["screenshots"][0]


@pytest.mark.parametrize("raw_status", ["weird", "", "ACTIVE", "unknown", "0", 0, 42, None])
def test_from_dict_invalid_status_becomes_orphaned(raw_status: object) -> None:
    manifest = SessionManifest.from_dict({"status": raw_status})
    # oracle: specified —— §3.6：读取时非法 status 一律视为 orphaned
    assert manifest.status == "orphaned"


@pytest.mark.parametrize("raw_status", [[], {}, ["active"]])
def test_from_dict_unhashable_status_is_treated_as_orphaned(raw_status: object) -> None:
    manifest = SessionManifest.from_dict({"status": raw_status})
    # oracle: specified —— §3.6：任何非法 status 读取时都视为 orphaned。
    # 宽容读取不得抛异常：list/dict 不可哈希，也不能让整个会话打不开（模块 docstring）。
    assert manifest.status == "orphaned"


@pytest.mark.parametrize("valid_status", ["active", "ended", "orphaned"])
def test_from_dict_valid_status_is_preserved(valid_status: str) -> None:
    manifest = SessionManifest.from_dict({"status": valid_status})
    # oracle: specified —— §3.6 封闭枚举三值原样保留
    assert manifest.status == valid_status


def test_from_dict_drops_dangling_source_seq_but_keeps_existing() -> None:
    manifest = SessionManifest.from_dict(
        {
            "screenshots": [{"seq": 3}],
            "structured": [
                {"seq": 4, "source_seq": 3},     # 来源截图存在 → 保留
                {"seq": 5, "source_seq": 99},    # 悬空 → 置 null
                {"seq": 6, "source_seq": None},  # 本就为空
                {"seq": 7},                       # 字段缺失 → 为空
            ],
        }
    )
    # oracle: specified —— §3.6：source_seq 悬空则置 null，存在的必须保留
    assert [s.source_seq for s in manifest.structured] == [3, None, None, None]


# ---------------------------------------------------------------------------
# schema 版本号与 seq 计数器（§3.2 / §3.3 / §3.6）
# ---------------------------------------------------------------------------


def test_to_dict_carries_schema_version() -> None:
    data = SessionManifest(session_id="s-example").to_dict()
    # oracle: specified —— §3.3 示例首字段 "schema": 1
    assert data["schema"] == 1


def test_next_seq_starts_at_one_and_increments() -> None:
    manifest = SessionManifest()
    # oracle: specified —— §3.6：seq 为正整数；从 1 开始
    assert manifest.next_seq() == 1
    assert manifest.next_seq() == 2
    assert manifest.next_seq() == 3
    # oracle: derived —— 计数器被推进
    assert manifest.seq_counter == 3


def test_next_seq_is_shared_across_screenshot_and_structured() -> None:
    manifest = SessionManifest()
    taken: list[int] = []
    for _ in range(250):
        taken.append(manifest.next_seq())   # 截图取号
        taken.append(manifest.next_seq())   # 结构化数据取号
    # oracle: specified —— §3.2：截图与结构化数据共用同一计数器，500 次调用 500 个不同值
    assert len(set(taken)) == 500
    # oracle: derived —— 无洞、从 1 起的连续序列
    assert sorted(taken) == list(range(1, 501))


def test_seq_counter_resumes_after_round_trip() -> None:
    manifest = SessionManifest(seq_counter=17)
    restored = SessionManifest.from_dict(manifest.to_dict())
    # oracle: specified —— 计数器随清单持久化
    assert restored.seq_counter == 17
    # oracle: derived —— 读回后接着数，不能从头开始
    assert restored.next_seq() == 18
    # oracle: derived —— 字段缺失时默认 0，故首次取号为 1
    assert SessionManifest.from_dict({}).next_seq() == 1


# ---------------------------------------------------------------------------
# DisplayContext / MonitorInfo（§3.3 示例）
# ---------------------------------------------------------------------------


def test_display_context_round_trips() -> None:
    display = DisplayContext(
        primary_index=0,
        monitors=[MonitorInfo(index=0, rect=[0, 0, 3440, 1440], primary=True, dpi=120, scale=1.25)],
    )
    restored = DisplayContext.from_dict(display.to_dict())
    # oracle: derived —— 显示配置往返一致
    assert restored == display


def test_display_context_from_dict_non_dict_is_empty() -> None:
    # oracle: specified —— 宽容读取：非 dict 视为空配置，不抛异常
    assert DisplayContext.from_dict("nope") == DisplayContext()


# ---------------------------------------------------------------------------
# §3.3 示例整段：读入 → 字段核对 → 写回
# ---------------------------------------------------------------------------


def test_spec_section_3_3_example_reads_and_round_trips() -> None:
    manifest = SessionManifest.from_dict(SPEC_SECTION_3_3_EXAMPLE)
    # oracle: specified —— §3.3 示例顶部字段
    assert manifest.session_id == "s-20260912-103000-a7k2"
    assert manifest.created_at == "2026-09-12T10:30:00.123+08:00"
    assert manifest.ended_at is None
    assert manifest.status == "active"
    assert manifest.agent_hint == "claude-code"
    # oracle: specified —— §3.3 示例 display.monitors[0]
    assert manifest.display.monitors[0].rect == [0, 0, 3440, 1440]
    assert manifest.display.monitors[0].scale == 1.25
    # oracle: specified —— §3.3 示例 screenshots[0].window
    window = manifest.screenshots[0].window
    assert window is not None
    assert window.process == "notepad.exe"
    assert window.klass == "Notepad"
    # oracle: specified —— §3.3 示例 structured[0]
    assert manifest.structured[0].element_count == 137
    assert manifest.structured[0].source_seq == 3

    data = manifest.to_dict()
    # oracle: derived —— 写回时 window 落在 "class" 键上
    assert data["screenshots"][0]["window"]["class"] == "Notepad"
    # oracle: specified —— to_dict 顶层带 schema 版本号
    assert data["schema"] == 1
