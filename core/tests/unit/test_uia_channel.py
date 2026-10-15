"""``uia.py`` 的对外形状：`bbox` 语义、`IsEnabled`、以及「现在能不能点」。

契约出处（本文件唯一的行为权威）：

- ``core/src/cu/desktop/uia.py`` 中 ``UiaElement`` / ``to_dict`` / ``_is_actionable``
  的 docstring 与 ``_SLOT_CURRENT_IS_ENABLED`` 的注释；
- ``omni/worker.py`` 的 ``COLUMNS`` / ``_merge_uia``（消费侧，只在 ``test_omni_merge_uia.py``
  里测，本文件只保证**产出侧**给出它期待的形状）。

为什么这几条必须由测试钉住：

1. **`bbox` 的语义曾经是错的，而且错了很久没人发现。** `UiaElement.rect` 是
   `(left, top, width, height)`，早先 `to_dict()` 把它原样输出成 `bbox`，而合并侧按
   `(x1, y1, x2, y2)` 解包 —— 于是「左边距大于自身宽度」的元素被静默丢掉。实测一个
   资源管理器窗口 UIA 读到 162 个、**只有 1 个过关**；侥幸过关的那些框在 md 里又是
   另一套语义。下面第一条用例就是照这个形状写的。
2. **`IsEnabled` 是检测器与多模态端点都给不出的信息。** 一个灰掉的按钮在像素上和亮的
   按钮一样，只有 UIA 知道此刻能不能点。它和「类型上是不是控件」是两件事，必须分开问。
"""

from __future__ import annotations

import pytest

from cu.desktop import uia


def _element(**over) -> uia.UiaElement:
    base = {"name": "下载", "control_type": "treeitem", "rect": (964, 394, 27, 32),
            "interactive": True, "enabled": True}
    base.update(over)
    return uia.UiaElement(**base)


# ---------------------------------------------------------------------------
# bbox 语义：角对角，不是左上角加宽高
# ---------------------------------------------------------------------------


def test_bbox_is_a_pair_of_corners() -> None:
    # oracle: specified —— to_dict 的 docstring：`bbox` 一律 [x1, y1, x2, y2]。
    assert _element(rect=(10, 20, 30, 40)).to_dict()["bbox"] == [10, 20, 40, 60]


def test_an_element_further_right_than_its_own_width_is_still_a_valid_box() -> None:
    """**这条就是那个 bug 的形状。**

    `rect=(964, 394, 27, 32)` 是一个左侧栏里的树项：左边距 964、宽 27。
    旧实现原样输出 `[964, 394, 27, 32]`，而合并侧要求 `x2 > x1`（即 `27 > 964`），
    于是它被当成「非法框」丢掉 —— 而这个窗口里 162 个元素有 161 个是这个形状。
    """
    # oracle: derived —— 转换后 x2 = left + width = 991 > x1 = 964，任何按角对角的
    #   合法性判据都收下它。
    box = _element(rect=(964, 394, 27, 32)).to_dict()["bbox"]

    assert box == [964, 394, 991, 426]
    assert box[2] > box[0] and box[3] > box[1]


def test_a_box_at_the_origin_still_reports_a_positive_size() -> None:
    # oracle: derived —— (0,0) 处的元素宽高为正，转换后不能塌成一个点。
    box = _element(rect=(0, 0, 12, 12)).to_dict()["bbox"]

    assert box == [0, 0, 12, 12]


# ---------------------------------------------------------------------------
# 能不能点：类型 × 启用
# ---------------------------------------------------------------------------


def test_a_disabled_button_is_not_actionable() -> None:
    """实测场景：资源管理器没有历史时，「后退」是灰的。

    旧实现只看控件类型，于是它照样报可交互 —— 调用方据此点下去，什么都不会发生。
    """
    # oracle: specified —— _is_actionable 的 docstring。
    assert uia._is_actionable(50000, enabled=False) is False      # Button
    assert uia._is_actionable(50000, enabled=True) is True


@pytest.mark.parametrize("control_id", [50020, 50033, 50008])   # Text / Pane / List
def test_a_plain_container_is_never_actionable(control_id: int) -> None:
    # oracle: specified —— 不在 _INTERACTIVE_TYPES 里的类型，启用与否都不是「能点」。
    assert uia._is_actionable(control_id, enabled=True) is False


def test_the_serialised_row_carries_both_answers() -> None:
    """`interactivity` 回答「现在能不能点」，`enabled` 回答「为什么不能」。

    两者都留着：禁用的按钮和纯文本在 `interactivity` 上都是假，
    但前者是「等条件满足就能点」，后者是「别管它」。
    """
    disabled = _element(interactive=False, enabled=False).to_dict()

    assert disabled["interactivity"] is False
    assert disabled["enabled"] is False


def test_enabled_defaults_to_true_when_the_reader_did_not_ask() -> None:
    # oracle: implicit —— 构造时不传 enabled 的调用方（测试替身、将来的别的读法）
    #   拿到的是「已启用」，而不是把 `None` 当成假值静默降级。
    assert uia.UiaElement(name="x", control_type="button", rect=(0, 0, 1, 1),
                          interactive=True).to_dict()["enabled"] is True


# ---------------------------------------------------------------------------
# 只有一套合并
# ---------------------------------------------------------------------------


def test_the_module_does_not_export_a_second_merge() -> None:
    """`uia` 侧**不得**再长出第二套元素合并。

    它曾经有一套（`merge` / `overlap_ratio`），用的是另一套框语义；被反转掉的策略
    留在原地没人调用，而真正在跑的那套（`omni/worker.py` 的 `_merge_uia`）单位是错的。
    两套实现只有一套在跑，正好掩盖了分歧 —— 这是那个 bug 活了这么久的直接原因。
    """
    assert not hasattr(uia, "merge")
    assert not hasattr(uia, "overlap_ratio")
    assert set(uia.__all__) == {"MAX_ELEMENTS", "MAX_SCAN", "UiaElement", "UiaResult",
                                "available", "read_window"}


# ---------------------------------------------------------------------------
# 扫描预算：与「返回几个」是两件事
# ---------------------------------------------------------------------------


def test_the_scan_budget_is_looser_than_the_return_budget() -> None:
    """两个界限必须分开。

    早先它们是同一个数：`range(min(count, MAX_ELEMENTS))` —— 「树里前 400 个下标」
    被当成「前 400 个可用元素」。而可用元素在树里的分布不均匀，Electron 窗口前面
    一大块无名子树会把后面的名字全顶出去（实测 550 个元素里 217 个可用，只返回了 114 个）。
    """
    assert uia.MAX_SCAN > uia.MAX_ELEMENTS


@pytest.mark.parametrize(("total", "expected"), [
    (0, 0),
    (550, 550),                     # 树比上限小：全都翻
    (uia.MAX_SCAN, uia.MAX_SCAN),
    (uia.MAX_SCAN + 1, uia.MAX_SCAN),
    (999_999, uia.MAX_SCAN),
])
def test_the_scan_budget_clamps_to_the_scan_limit(total: int, expected: int) -> None:
    # oracle: specified —— _scan_budget 的 docstring。
    assert uia._scan_budget(total) == expected


def test_a_tree_under_the_return_budget_is_never_reported_as_truncated() -> None:
    # oracle: derived —— 没截断就不许说明，否则读的人会去找一个不存在的问题。
    assert uia._truncation_note(total=550, scanned=550, returned=217) == ""


def test_the_two_kinds_of_truncation_are_reported_apart() -> None:
    """翻不完和取满了要分开说：前者要调 `MAX_SCAN`，后者才要调 `MAX_ELEMENTS`。"""
    scanned_out = uia._truncation_note(total=uia.MAX_SCAN + 500,
                                       scanned=uia.MAX_SCAN, returned=10)
    filled_up = uia._truncation_note(total=900, scanned=900, returned=uia.MAX_ELEMENTS)

    assert "没翻" in scanned_out and "取满" not in scanned_out
    assert "取满" in filled_up and "没翻" not in filled_up
    assert "已截断" in scanned_out and "已截断" in filled_up
