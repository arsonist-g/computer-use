"""``omni/worker.py`` 的 ``_merge_uia`` / ``_overlap_ratio`` 契约测试。

被测策略（**已反转，别改回去**）：UIA 有元素时 UIA 是基底（框与文本都精确），
检测器元素只在与任何 UIA 元素重叠 < ``_MATCH_THRESHOLD`` 时保留 —— 那块区域
UIA 覆盖不到（自绘部件、画布）。UIA 为空时检测器产出原样返回。

为什么方向是这个：叠加对照图（``tests/native/uia_coord_check.py`` 产出的
calib-*.png，绿=UIA / 红=检测器）显示 UIA 的框精确对齐控件（标签页、菜单栏、
工具栏、状态栏），检测器的框大量错位甚至横跨编辑器中部。最初「以检测器框为基底、
只用 UIA 的文本」的方向是错的，已弃用。阈值与 base 侧 ``desktop/uia.py`` 一致，
``_overlap_ratio`` 用「交集 / 较小者面积」而不是并集 —— 小框落在大框里时判为高重叠。

``worker.py`` 不在 ``cu`` 包里（它跑在独立的 venv-omni 环境，与 base 零代码共享），
所以这里按文件路径加载，不走包导入。

oracle 标注：
- ``specified`` —— 来自 ``_merge_uia`` / ``_overlap_ratio`` 的 docstring 与任务契约。
- ``derived`` —— 由公式与阈值语义推导出的具体数值边界。
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

#: 仓库根下的 omni/worker.py。unit 目录的上溯：unit → tests → core → 仓库根。
WORKER_PATH = Path(__file__).resolve().parents[3] / "omni" / "worker.py"


def _load_worker():
    """按文件路径加载 worker.py（它不是可导入的包）。

    不走包导入是因为 worker 属于独立环境，其模块名不在 ``cu`` 命名空间里。
    """
    spec = importlib.util.spec_from_file_location("cu_omni_worker_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None, f"无法加载 {WORKER_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = _load_worker()


# --------------------------------------------------------------------------- #
# 测试数据构造
# --------------------------------------------------------------------------- #


def _uia(**over) -> dict:
    base = {"type": "tab", "bbox": [0, 0, 100, 40], "interactivity": True,
            "content": "General", "source": "raw-uia"}
    base.update(over)
    return base


def _detector(**over) -> dict:
    base = {"type": "icon", "bbox": [300, 300, 340, 340], "interactivity": False,
            "content": "detected", "source": "omni"}
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# _overlap_ratio：交集 / 较小者面积
# --------------------------------------------------------------------------- #


def test_overlap_ratio_small_box_fully_inside_is_one() -> None:
    # oracle: specified —— docstring「交集面积 / 较小者面积」，分母取较小者而非并集。
    assert worker._overlap_ratio((0, 0, 10, 10), (0, 0, 100, 100)) == 1.0
    # oracle: derived —— 反过来（大框在前）同样是 1.0，分母始终取较小者。
    assert worker._overlap_ratio((0, 0, 100, 100), (0, 0, 10, 10)) == 1.0


def test_overlap_ratio_disjoint_is_zero() -> None:
    # oracle: derived —— 完全不相交：交集宽高都取 0 ⇒ 0.0。
    assert worker._overlap_ratio((0, 0, 10, 10), (20, 20, 10, 10)) == 0.0
    # oracle: derived —— 贴边但不重叠（共享一条边）也算 0.0。
    assert worker._overlap_ratio((0, 0, 10, 10), (10, 0, 10, 10)) == 0.0


def test_overlap_ratio_partial_overlap_is_exact_fraction() -> None:
    # 同尺寸 10x10，偏移 (5,5)：交集 5x5=25，较小者面积 100 ⇒ 0.25。
    # oracle: derived —— 交集 / 较小者面积的具体算术，不是「> 0」这类弱断言。
    assert worker._overlap_ratio((0, 0, 10, 10), (5, 5, 10, 10)) == 0.25
    # 只水平偏移 5：交集 5x10=50 ⇒ 0.5（正好落在阈值上）。
    # oracle: derived —— 阈值边界 0.5 的精确取值。
    assert worker._overlap_ratio((0, 0, 10, 10), (5, 0, 10, 10)) == 0.5
    # 水平偏移 6：交集 4x10=40 ⇒ 0.4（阈值之下）。
    # oracle: derived —— 阈值下方一侧的精确取值。
    assert worker._overlap_ratio((0, 0, 10, 10), (6, 0, 10, 10)) == 0.4


# --------------------------------------------------------------------------- #
# _merge_uia：UIA 为基底
# --------------------------------------------------------------------------- #


def test_uia_elements_become_the_base_with_source_uia() -> None:
    # oracle: specified —— docstring：UIA 有元素时它是基底，框与文本都精确。
    result = worker._merge_uia([], [_uia()])
    assert result == [{"type": "tab", "bbox": [0, 0, 100, 40], "interactivity": True,
                       "content": "General", "source": "uia"}]
    # oracle: specified —— source 一律标 "uia"，覆盖 UIA 通道自带的 source 值。
    assert result[0]["source"] == "uia"
    # oracle: specified —— 框与文本取自 UIA。
    assert result[0]["bbox"] == [0, 0, 100, 40]
    assert result[0]["content"] == "General"


def test_detector_overlapping_uia_is_dropped() -> None:
    uia = [_uia(bbox=[0, 0, 100, 40])]
    # 完全落在 UIA 框内 ⇒ 重叠 1.0。
    inside = _detector(bbox=[0, 0, 50, 40])
    result = worker._merge_uia([inside], uia)
    # oracle: specified —— 与任一 UIA 重叠 ≥ _MATCH_THRESHOLD ⇒ 丢弃（UIA 已覆盖）。
    assert len(result) == 1
    # oracle: specified —— 只剩 UIA 那条。
    assert result[0]["source"] == "uia"

    # 与 (0,0,10,10) 部分重叠 0.64：≥ 0.5 也丢弃（不只是「完全包含」才丢）。
    partial = _detector(bbox=[2, 2, 12, 12])
    result2 = worker._merge_uia([partial], [_uia(bbox=[0, 0, 10, 10])])
    # oracle: derived —— 0.64 ≥ 0.5 的元素被丢弃，输出只留 UIA。
    assert [e["source"] for e in result2] == ["uia"]


def test_detector_below_threshold_is_kept_with_its_source() -> None:
    uia = [_uia(bbox=[0, 0, 100, 40])]
    # 与 UIA 不相交 ⇒ 0.0 < 0.5：UIA 覆盖不到的区域，必须保留。
    free = _detector(bbox=[200, 200, 260, 260])
    result = worker._merge_uia([free], uia)
    # oracle: specified —— 与所有 UIA 都 < 阈值 ⇒ 保留。
    assert len(result) == 2
    # oracle: specified —— 保留时 source 保持原值。
    assert result[1]["source"] == "omni"
    assert result[1]["bbox"] == [200, 200, 260, 260]

    # oracle: derived —— 检测器元素缺少 source 时回落到 "detector"。
    no_source = {"type": "canvas", "bbox": [500, 500, 520, 520], "content": "x"}
    result2 = worker._merge_uia([no_source], uia)
    assert result2[1]["source"] == "detector"


def test_threshold_is_inclusive_at_half() -> None:
    uia = [_uia(bbox=[0, 0, 10, 10])]
    at_half = _detector(bbox=[5, 0, 15, 10])   # 恰好 0.5
    below = _detector(bbox=[6, 0, 16, 10])     # 0.4
    result = worker._merge_uia([at_half, below], uia)
    # oracle: derived —— 判据是 >= 0.5：恰好 0.5 被丢弃，0.4 被保留。
    assert [e["bbox"] for e in result] == [[0, 0, 10, 10], [6, 0, 16, 10]]
    # oracle: derived —— 保留项的 source 是检测器原值。
    assert [e["source"] for e in result] == ["uia", "omni"]


def test_uia_bbox_is_normalized_to_int_pixels() -> None:
    # oracle: derived —— UIA 坐标按 int() 取整（截断），输出 bbox 是整数像素。
    result = worker._merge_uia([], [_uia(bbox=[0.9, 1.9, 10.9, 20.9])])
    assert result[0]["bbox"] == [0, 1, 10, 20]


# --------------------------------------------------------------------------- #
# _merge_uia：空输入与无效输入
# --------------------------------------------------------------------------- #


def test_empty_uia_returns_detector_elements_unchanged() -> None:
    detector = [_detector(), _detector(bbox=[10, 10, 30, 30], source="")]
    result = worker._merge_uia(detector, [])
    # oracle: specified —— UIA 为空时检测器产出原样返回：内容与数量都不变。
    assert result == detector
    # oracle: derived —— 返回的是新列表与浅拷贝，不与输入共享对象。
    assert result is not detector
    assert all(copied is not original for copied, original in zip(result, detector))
    # oracle: derived —— 原输入不被改动。
    assert detector[0]["source"] == "omni"


def test_both_sides_empty_returns_empty_list() -> None:
    # oracle: specified —— 两边都空 ⇒ 空列表。
    assert worker._merge_uia([], []) == []


def test_invalid_uia_elements_are_ignored_and_detector_passes_through() -> None:
    # 非法 UIA：bbox 缺失 / 长度不对 / 非数字 / 零尺寸 / 非 dict。
    uia = [{"bbox": None}, {"bbox": [1, 2, 3]}, {"bbox": ["a", "b", "c", "d"]},
           {"bbox": [5, 5, 5, 5]}, "not-a-dict", 7]
    result = worker._merge_uia([_detector()], uia)
    # oracle: derived —— 无有效 UIA 矩形 ⇒ 不覆盖任何区域，检测器全部保留。
    assert [e["source"] for e in result] == ["omni"]


def test_non_dict_uia_entries_are_ignored() -> None:
    # oracle: derived —— UIA 列表里的非 dict 项（可能来自外部 JSON）被安全忽略，不崩。
    result = worker._merge_uia(
        [_detector(bbox=[300, 300, 340, 340])],
        ["x", 7, None, _uia(bbox=[0, 0, 10, 10])],
    )
    assert [e["source"] for e in result] == ["uia", "omni"]
    assert [e["bbox"] for e in result] == [[0, 0, 10, 10], [300, 300, 340, 340]]


def test_detector_elements_with_invalid_bbox_are_skipped() -> None:
    uia = [_uia()]
    detector = [
        _detector(bbox=None),                # 缺失
        _detector(bbox=[1, 2, 3]),           # 长度不对
        _detector(bbox=["a", "b", "c", "d"]),  # 非数字
        _detector(bbox=[5, 5, 5, 5]),        # 零尺寸
        _detector(bbox=[300, 300, 340, 340]),  # 合法且与 UIA 不相交
    ]
    result = worker._merge_uia(detector, uia)
    # oracle: derived —— 以上无效检测器元素被跳过、不产出垃圾，只留 UIA 与那一条合法项。
    assert [e["bbox"] for e in result] == [[0, 0, 100, 40], [300, 300, 340, 340]]


def test_merge_does_not_mutate_inputs() -> None:
    uia = [_uia()]
    detector = [_detector(bbox=[300, 300, 340, 340], content="keep")]
    uia_snapshot = copy.deepcopy(uia)
    detector_snapshot = copy.deepcopy(detector)
    worker._merge_uia(detector, uia)
    # oracle: derived —— 合并只读输入，不就地改动任一侧。
    assert uia == uia_snapshot
    assert detector == detector_snapshot


# --------------------------------------------------------------------------- #
# 缺陷：检测器侧的「非 dict」输入未被容错（见交付报告）
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    strict=False,
    reason="缺陷：_merge_uia 对检测器侧的非法项按 item.get('bbox') 取框，"
           "非 dict 项会抛 AttributeError（UIA 为空时是 dict(item) 抛 TypeError）。",
)
def test_non_dict_detector_entries_do_not_crash() -> None:
    """契约要求「非 dict 的输入不崩」，但检测器侧目前会崩。

    UIA 侧用 ``isinstance(e, dict)`` 守住了，检测器侧没有。当前内部调用方
    （``handle_parse`` ← ``_to_elements``）保证是 dict，所以这一条在真实链路上
    还触发不到；但它与 UIA 侧的容错不对称，且契约明确要求不崩。以 xfail 钉住。
    """
    # oracle: specified —— 契约：非 dict 输入不崩，且不产出垃圾。
    result_with_uia = worker._merge_uia(["x", 7, _detector(bbox=[300, 300, 340, 340])],
                                        [_uia()])
    assert [e["source"] for e in result_with_uia] == ["uia", "omni"]

    # oracle: specified —— UIA 为空时的同一条要求。
    result_without_uia = worker._merge_uia(["x", 7], [])
    assert isinstance(result_without_uia, list)
