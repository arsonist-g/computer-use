"""检测器产出的 `content` 里，哪些根本不是「标签」（`looks_like_a_label`）。

契约出处：`omni/worker.py` 的 `looks_like_a_label` / `blank_unreadable_content`
与它们的 docstring。

判据是**「它是不是一个名字」，不是「它对不对」** —— 这条边界是本文件的主线：

- 清掉：路径形状（`M0,0L9,0 4.5,`，实测 801 次）、模型自称不知道（`unanswerable`）、
  没有两个连续字母的碎片（`j147` / `21` / `W`）。
- 留着：Florence 的句子描述（`Microsoft Edge browser.` / `a folder.`），
  因为它们在 UIA 覆盖不到的图标上是**唯一的**信号；以及版本号（`v0.16.1` 实测被误伤过）。

语料来自本机 `~/.computer-use/sessions` 里 9357 条检测器元素，命中 1720 条（18.4%）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
WORKER_PATH = REPO / "omni" / "worker.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"无法加载 {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = _load("cu_omni_worker_filter_under_test", WORKER_PATH)


# ---------------------------------------------------------------------------
# 不是名字的
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "M0,0L9,0 4.5,",          # 实测出现 801 次的那个
    "M0,0L9,0 4.5,5z",
    "S13m01433",
    "tz1t2a7",
])
def test_path_shaped_content_is_not_a_label(text: str) -> None:
    # oracle: specified —— _PATH_ONLY + 至少两组「命令字母+数字」。
    assert worker.looks_like_a_label(text) is False


@pytest.mark.parametrize("text", ["unanswerable", "Unanswerable", "unanswerable.", "N/A", "unknown"])
def test_the_models_own_abstention_is_not_a_label(text: str) -> None:
    # oracle: specified —— _ABSTENTIONS：模型在说自己不知道，那不是名字。
    assert worker.looks_like_a_label(text) is False


@pytest.mark.parametrize("text", [
    "j147", "j143", "1147", "6143", "12328", "2A2", "2i026", "173", "21", "45", "3", "0", "W",
])
def test_a_fragment_without_two_consecutive_letters_is_not_a_label(text: str) -> None:
    # oracle: specified —— 实测语料里这一类全是 OCR 从图形边缘抠出来的碎片。
    assert worker.looks_like_a_label(text) is False


@pytest.mark.parametrize("text", ["", "   ", "\t", "..."])
def test_empty_content_is_not_a_label(text: str) -> None:
    # oracle: specified —— 空的不可能当名字用（`...` 剥掉点号后也是空）。
    assert worker.looks_like_a_label(text) is False


# ---------------------------------------------------------------------------
# 是名字的
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Microsoft Edge browser.",     # Florence 的句子描述
    "a folder.",
    "a battery level indicator.",  # 这条是幻觉，但它**是**一个名字
    "View as full screen",
    "More options",
    "System",
    "Settings",
])
def test_sentence_descriptions_are_kept(text: str) -> None:
    """**不判断描述对不对。** 在 UIA 覆盖不到的图标上，这是唯一的信号 ——
    过滤掉它等于把非原生应用的那条路彻底关掉。"""
    # oracle: specified —— docstring「Florence 的句子描述一律保留」。
    assert worker.looks_like_a_label(text) is True


@pytest.mark.parametrize("text", ["v0.16.1", "V0.16.1", "3.0", "0.1.14", "3.23", "1.12"])
def test_version_strings_survive(text: str) -> None:
    # oracle: specified —— _VERSION 是一条**实测踩出来的**例外：这些是真标签。
    assert worker.looks_like_a_label(text) is True


@pytest.mark.parametrize("text", ["QQ", "py", "HE", "WB", "Sn", "FA", "Pi"])
def test_two_letter_labels_survive(text: str) -> None:
    """判据取「两个连续字母」是有意偏保守：`QQ` 是真的应用名，`py` 是文件扩展名。"""
    # oracle: derived —— _longest_letter_run >= 2 的边界。
    assert worker.looks_like_a_label(text) is True


@pytest.mark.parametrize("text", ["设置", "保存", "文件资源管理器", "在 cu-probe-test 中搜索", "刷新"])
def test_chinese_labels_survive(text: str) -> None:
    """`isalpha()` 认中文，所以中文天然有长度 ≥2 的字母段 —— 不需要单列一条 CJK 规则。"""
    # oracle: derived —— 判据用 isalpha() 而不是 ASCII 字母表。
    assert worker.looks_like_a_label(text) is True


# ---------------------------------------------------------------------------
# blank_unreadable_content：清内容，不删元素
# ---------------------------------------------------------------------------


def _payload(*contents: str) -> dict:
    return {"elements": [
        {"type": "icon", "bbox": [i, i, i + 10, i + 10], "interactivity": True, "content": c}
        for i, c in enumerate(contents)
    ]}


def test_unreadable_content_is_blanked_but_the_element_survives() -> None:
    # oracle: specified —— docstring：框可能是真的控件，删掉等于丢一个可点的东西。
    payload = worker.blank_unreadable_content(_payload("M0,0L9,0 4.5,", "保存"))

    assert payload["elements"][0]["content"] == ""
    assert payload["elements"][0]["bbox"] == [0, 0, 10, 10]
    assert payload["elements"][0]["interactivity"] is True
    assert payload["elements"][1]["content"] == "保存"


def test_blanking_is_idempotent() -> None:
    # oracle: derived —— 清过的再清一次结果相同（空串不是标签，仍然被清）。
    once = worker.blank_unreadable_content(_payload("j147"))
    twice = worker.blank_unreadable_content(once)

    assert once == twice


@pytest.mark.parametrize("payload", [
    {},
    {"elements": None},
    {"elements": "不是列表"},
    {"elements": ["不是字典", None, 7]},
    {"elements": [{"bbox": [0, 0, 1, 1]}]},        # 连 content 都没有
])
def test_a_malformed_payload_does_not_raise(payload: dict) -> None:
    # oracle: specified —— 与 `_merge_uia` 同一条约定：纯函数，一条坏数据不该让整次解析失败。
    worker.blank_unreadable_content(payload)


def test_the_self_test_covers_this_filter() -> None:
    """过滤器接在 `handle_parse` 上，而 `setup omni` 会跑 worker 的自检 ——
    自检里必须也走一遍它，否则改了规则而自检照样绿。"""
    assert "looks_like_a_label" in WORKER_PATH.read_text(encoding="utf-8")
    selftest = WORKER_PATH.read_text(encoding="utf-8").split("def _selftest()", 1)[1]
    assert "blank_unreadable_content" in selftest, "自检没有覆盖这条过滤器"
