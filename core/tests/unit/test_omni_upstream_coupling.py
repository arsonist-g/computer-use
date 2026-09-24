"""守卫：worker 对上游的**注入点**仍然存在，且参数仍是它假设的那些。

`omni/worker.py` 不改上游源码（DEC-044 的教训），代价是它假设了几处上游形状：

| 它假设的事 | 上游一变，坏在哪 |
|---|---|
| `util/utils.py` 的 `get_som_labeled_img` 用 `iou_threshold=0.1` 调 `predict_yolo` | 并行预取那份 YOLO 结果成了**另一批框**，没人会报错 |
| `get_som_labeled_img` 仍调用模块全局的 `annotate` | 标注替身失效，那 0.38s 悄悄回来了 |
| `util/omniparser.py` 的 `parse` 仍经模块全局 `check_ocr_box` 进 OCR | 并行那一层不再被调用，退回串行 |
| Florence-2 的取描述仍走 `model.generate(..., max_new_tokens=...)` | 描述长度上限失效，慢回来 |

这些都不是「会不会崩」的问题 —— **它们全都只在速度上表现出来**，而速度没有断言。
所以这里把「上游的形状」钉成断言：装了的机器上跑，上游一变就红。

omni 没装（CI、干净机器）时跳过：这条守卫的对象是那份克隆下来的上游源码。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parents[3] / "omni" / "worker.py"
OMNI_ROOT = Path.home() / ".computer-use" / "OmniParser"
UTILS = OMNI_ROOT / "util" / "utils.py"
OMNIPARSER = OMNI_ROOT / "util" / "omniparser.py"

pytestmark = pytest.mark.skipif(
    not UTILS.is_file() or not OMNIPARSER.is_file(),
    reason="本机未安装 OmniParser（守卫的对象是 ~/.computer-use/OmniParser 那份上游源码）",
)


def _load_worker():
    spec = importlib.util.spec_from_file_location("cu_omni_worker_for_test", WORKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_upstream_predicts_with_the_thresholds_we_prefetch_with() -> None:
    """并行预取必须与上游那次 `predict_yolo` 用**同一组**阈值。"""
    worker = _load_worker()
    src = _flat(UTILS)
    call = re.search(
        r"predict_yolo\(model=model, image=image_source, box_threshold=BOX_TRESHOLD, "
        r"imgsz=imgsz, scale_img=scale_img, iou_threshold=([0-9.]+)\)", src)
    assert call, ("上游 `get_som_labeled_img` 调 `predict_yolo` 的形状变了 —— "
                  "worker 的并行预取要重新对一遍（见 omni/worker.py 的 `_overlap_ocr_and_yolo`）")
    assert float(call.group(1)) == worker.YOLO_IOU_THRESHOLD
    assert "BOX_TRESHOLD" in call.group(0), "阈值名变了，预取用的 conf 也要跟着对"


def test_upstream_still_renders_through_the_module_global_annotate() -> None:
    """标注替身挂在 `util.utils.annotate` 上：上游必须仍按模块全局名找它。"""
    src = _flat(UTILS)
    assert "annotated_frame, label_coordinates = annotate(" in src, \
        "上游不再用 `annotate(...)` 出图 —— 替身（省掉 0.38s）失效了"
    assert "assert w == annotated_frame.shape[1] and h == annotated_frame.shape[0]" in src, \
        "上游对占位图尺寸的校验没了 —— 替身返回的尺寸假设要重看"


def test_upstream_enters_ocr_through_the_module_global_check_ocr_box() -> None:
    """并行那一层挂在 `util.omniparser.check_ocr_box` 上：上游必须仍按模块全局名调它。"""
    src = _flat(OMNIPARSER)
    assert "from util.utils import get_som_labeled_img" in src
    assert "check_ocr_box" in src
    assert "check_ocr_box(image, display_img=False" in src, \
        "上游 `Omniparser.parse` 的 OCR 调用形状变了 —— worker 的并行注入点要重新对"


def test_upstream_caption_generation_still_takes_max_new_tokens() -> None:
    """描述长度上限挂在 `model.generate(max_new_tokens=...)` 上。"""
    assert "max_new_tokens=20" in _flat(UTILS), \
        "上游生成描述调用里不再有 max_new_tokens=20 —— worker 的上限注入失效"
