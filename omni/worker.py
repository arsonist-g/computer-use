"""cu-omni worker —— 把一张图片解析成结构化数据，可选再用多模态端点优化描述。

**独立环境，与 base 零代码共享**（架构 §1.5 第 3 条 / DEC-037）。
本目录下的代码**不得 import `cu` 包里的任何东西** —— 它跑在 `venv-omni` 里，
那边的解释器里没有 `cu`。两边的关系只有管道与文件，且**不传图像字节流**：
入参是图片路径，出参是 markdown 路径。

协议与 daemon↔客户端同形（NDJSON over 管道），换传输不换格式（架构 §1.4）。
方法表见 `spec/backend-design/computer-use/api-contract.md` §2.1 的 `omni.parse`。

为什么单独一个进程、单独一个环境：OmniParser 要拖进 torch / transformers /
paddleocr，三到六 GB。把它挡在 base 环境之外，是为了让 CLI 的每一次冷启动
不必付这份 import 代价（DEC-039）。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

OMNI_NOT_INSTALLED = "omni_not_installed"
OMNI_FAILED = "omni_failed"
VLM_FAILED = "vlm_failed"


class WorkerError(Exception):
    def __init__(self, code: str, message: str, hint: str = "", detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.detail = detail or {}

    def as_rpc(self) -> dict:
        return {"code": self.code, "message": self.message,
                "hint": self.hint, "detail": self.detail}


# ===========================================================================
# markdown 渲染与 AI 优化契约（DEC-011 / DEC-015）
#
# 这三段为什么和 worker 放在**同一个文件**里，而不是拆成 `markdown.py`：
#
# 本 worker 的全部意义是「独立环境、零代码共享」（架构 §1.5 第 3 条）。
# 拆成两个文件后，导入只能靠 `sys.path` 把一个裸模块名塞进来，而那个名字
# （`markdown`）与标准库冲突，编辑器解析到的是标准库那个，四个符号全部报未知。
# 更根本的是：「能单独拷进 omni 环境跑」是本模块的契约，一个文件才是这条契约
# 最直白的形式。拆出去省下的行数，换来的是一个每次 import 都要重新判断的边界。
# ===========================================================================

#: 元素表头。顺序即列序，改它等于改契约。
#: `source` 标出这条来自哪个通道 —— UIA 的精确文本与检测器的框混在一张表里，
#: 不标来源的话读的人无从判断哪条该信（实测两者质量差一个数量级）。
COLUMNS = ("#", "type", "bbox", "interactivity", "content", "source")


class ContractViolation(Exception):
    """优化结果违反了冻结契约。"""


def _bbox_text(raw: Any) -> str:
    """`[x1, y1, x2, y2]` → `x1,y1,x2,y2`。

    逗号而不是空格：markdown 表格用 `|` 分隔，bbox 里再用空格会让「一个 bbox」
    在读表的人眼里变成四个单元格。
    """
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        return ",".join(str(int(v)) for v in raw)
    return ""


def _cell(text: Any) -> str:
    """把任意文本塞进一个表格单元格而不破坏表结构。"""
    out = str(text if text is not None else "")
    # `|` 会提前结束单元格，换行会结束整行，两者都必须转义。
    return out.replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def _interactivity(raw: Any) -> str:
    """可交互性是布尔，渲染成 `y` / `n` 而不是 `true` / `false`。

    这是 token 预算最直接的体现：一张表里出现上百次 `false`，换成 `n` 省掉一半，
    语义不受影响，因为表头已经交代了这一列的含义。
    """
    if raw is None:
        return ""
    return "y" if raw else "n"


def to_markdown(payload: dict, *, source: str = "", model: str | None = None,
                diag: str = "") -> str:
    """把一次解析的完整结果渲染成 markdown。

    **只换格式，不裁信息**（DEC-015）：元素一个不删，坐标一位不少。
    省下的是重复的键名，不是内容。AI 需要精确坐标去点击，裁掉坐标等于把功能裁掉。
    """
    elements = payload.get("elements") or []
    lines: list[str] = ["# 界面元素解析", ""]
    if source:
        lines.append(f"- source: {source}")
    if model:
        # 记下是哪个模型改写的，复盘时才知道该信任到什么程度（DEC-011）。
        lines.append(f"- description model: {model}")
    lines.append(f"- elements: {len(elements)}")
    if diag:
        lines.append(f"- merge: {diag}")
    lines.append("")
    lines.append("> 坐标为图像像素，原点在图像左上角。"
                 "换算到屏幕坐标需加上截图返回的 `origin`（DEC-001）。")
    lines.append("")

    if not elements:
        lines.append("（未检测到元素）")
        return "\n".join(lines) + "\n"

    lines.append("| " + " | ".join(COLUMNS) + " |")
    lines.append("|" + "|".join(["---"] * len(COLUMNS)) + "|")
    for index, element in enumerate(elements, start=1):
        if not isinstance(element, dict):
            continue
        lines.append("| " + " | ".join([
            str(index),
            _cell(element.get("type")),
            _bbox_text(element.get("bbox")),
            _cell(_interactivity(element.get("interactivity"))),
            _cell(element.get("content")),
            _cell(element.get("source")),
        ]) + " |")

    return "\n".join(lines) + "\n"


#: 判定「两个框是同一个元素」的重叠阈值。与 base 侧 `desktop/uia.py` 的取值一致 ——
#: 两边算的是同一件事，阈值不同会让同一份数据在两侧合并出不同结果。
_MATCH_THRESHOLD = 0.5


def _overlap_ratio(a: tuple, b: tuple) -> float:
    """交集面积 / 较小者面积。用较小者：小框落在大框里时应判为高重叠。"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    smaller = min(aw * ah, bw * bh) or 1
    return (ix * iy) / smaller


def _merge_uia(detector: list, uia: list) -> list:
    """把 UIA 元素并入检测器产出。

    **策略在实测后反转了一次，这里是反转后的版本。** 最初写的是「检测器的 bbox 保留、
    UIA 只覆盖文本」，理由是「检测器对『哪里有可点的东西』覆盖更全」。那个假设是错的 ——
    它来自一次读到别的会话文件的误判。叠加对照图（`tests/native/uia_coord_check.py`
    产出的 calib-*.png，绿=UIA / 红=检测器）显示：

      - **UIA 的框精确对齐控件**（标签页、菜单栏、工具栏、状态栏，一个不差）；
      - **检测器的框大量错位**，甚至有一个横跨编辑器中部的大框。

    所以现在反过来：**UIA 是基底**（框与文本都精确），检测器只用来补 UIA 覆盖不到的区域。

    这样也**不再依赖「两侧框能否匹配」** —— 那一步本来就脆弱（检测器坐标在两次调用间
    会变），而现在变成「UIA 有就用它，没有就退回检测器」，逻辑单一且不会因匹配失败
    而静默退化。

    检测器元素若与**任一** UIA 元素重叠超过阈值，就认为 UIA 已经覆盖了这块区域，
    丢弃它；否则保留 —— 那正是 UIA 拿不到的东西（自绘部件、画布内容）。
    `uia` 为空时是**纯退回**：检测器产出逐项原样返回，连 `source` 都不改 ——
    那是 Electron/自绘界面的正常路径，什么都不该被动过。
    `uia` 非空时才是**合并**：这时留下的检测器元素标 `source="detector"`，
    好与 `uia` 区分开（一张表里两个来源，不标就无从判断哪条该信）。

    两侧的输入都要容错：非 dict、bbox 缺失或形状不对的条目一律跳过。
    这是纯函数，调用方不止一处，一条坏数据不该让整次解析失败。
    """
    if not uia:
        # 纯退回：原样复制，不校验也不补字段（校验留给真正的合并路径）。
        return [dict(item) for item in detector if isinstance(item, dict)]

    def rect_of(box) -> tuple | None:
        if not (isinstance(box, (list, tuple)) and len(box) == 4):
            return None
        try:
            x1, y1, x2, y2 = (int(v) for v in box)
        except (TypeError, ValueError):
            return None
        if x2 <= x1 or y2 <= y1:
            return None
        return (x1, y1, x2 - x1, y2 - y1)

    uia_rects = [r for r in (rect_of(e.get("bbox")) for e in uia if isinstance(e, dict)) if r]

    merged = [{
        "type": str(e.get("type") or ""),
        "bbox": [int(v) for v in e["bbox"]],
        "interactivity": bool(e.get("interactivity")),
        "content": str(e.get("content") or ""),
        "source": "uia",
    } for e in uia if isinstance(e, dict) and rect_of(e.get("bbox"))]

    for item in detector:
        # 检测器侧的输入同样可能不是 dict（这个函数是纯函数，调用方不止一处）。
        # 委派契约要求「非 dict 不崩」，而且**不要**在这里抛 —— 一条坏数据不该
        # 让整次解析失败。
        if not isinstance(item, dict):
            continue
        rect = rect_of(item.get("bbox"))
        if rect is None:
            continue
        if any(_overlap_ratio(rect, other) >= _MATCH_THRESHOLD for other in uia_rects):
            continue          # UIA 已覆盖这块区域，它的文本与框都更可信
        # 这是合并路径：标出来源，否则读的人分不清哪条来自哪一侧。
        merged.append({**item, "source": item.get("source") or "detector"})

    return merged


def _bbox_multiset(elements: list) -> list[tuple[int, ...]]:
    return sorted(
        tuple(int(v) for v in element.get("bbox", []))
        for element in elements
        if isinstance(element, dict) and len(element.get("bbox", [])) == 4
    )


def validate_optimized(original: dict, optimized: dict) -> dict:
    """校验多模态端点返回的结果是否守约，守约则返回它，违约则抛错。

    契约（DEC-011）只有两条，但两条都是硬要求：

    1. **bbox 冻结**：优化只改描述，不改位置。模型的理解力强于定位力，
       让它碰坐标等于用它的弱项覆盖检测器的强项。
    2. **不得新增元素**：模型不能凭空造出检测器没找到的元素，否则 AI 会去点
       一个不存在的东西。**允许减少**，因为模型可能识别出某个元素是噪声。

    违约时抛错而不是「尽力修正」：静默接受一份被改过坐标的结果，会让 AI
    点到错误的位置，而它没有任何办法察觉。
    """
    original_elements = original.get("elements") or []
    optimized_elements = optimized.get("elements")
    if not isinstance(optimized_elements, list):
        raise ContractViolation("优化结果的 elements 不是数组")
    if len(optimized_elements) > len(original_elements):
        raise ContractViolation(
            f"优化结果新增了元素：原 {len(original_elements)} 个，"
            f"优化后 {len(optimized_elements)} 个（契约禁止新增）"
        )

    # 用多重集合比对而不是按下标：模型可能重排元素，但**集合**必须一致。
    # 按下标比对会把「顺序变了」误判成「坐标被改」。
    remaining = _bbox_multiset(original_elements)
    for box in _bbox_multiset(optimized_elements):
        if box not in remaining:
            raise ContractViolation(
                f"优化结果出现原始结果中没有的 bbox：{box}（契约冻结 bbox）")
        remaining.remove(box)
    return optimized


def merge_descriptions(original: dict, optimized: dict) -> dict:
    """把优化后的描述并回原始元素，**保留原始 bbox**。

    按 bbox 配对而不是按顺序：位置是检测器给出的事实，描述是模型给的判断，
    两者靠坐标对齐最可靠。
    """
    descriptions: dict[tuple[int, ...], str] = {}
    for element in optimized.get("elements") or []:
        if not isinstance(element, dict):
            continue
        box = element.get("bbox")
        if isinstance(box, list) and len(box) == 4:
            descriptions[tuple(int(v) for v in box)] = str(element.get("content") or "")

    merged: list[dict] = []
    for element in original.get("elements") or []:
        if not isinstance(element, dict):
            continue
        item = dict(element)
        box = item.get("bbox")
        if isinstance(box, list) and len(box) == 4:
            better = descriptions.get(tuple(int(v) for v in box))
            if better:
                item["content"] = better
        merged.append(item)
    return {**original, "elements": merged}


# ---------------------------------------------------------------------------
# 检测器
#
# OmniParser 由 `computer-use setup omni` 装在本环境里，源码在
# `~/.computer-use/OmniParser`，权重在 `~/.computer-use/models/icon_detect_v3`。
# 这里**只 import 本环境的东西**，绝不碰 `cu` 包（架构 §1.5 第 3 条）。
# ---------------------------------------------------------------------------

#: 模型与源码的位置。环境变量可覆盖，便于换机器与测试。
ENV_OMNI_HOME = "COMPUTER_USE_OMNI_HOME"
ENV_OMNI_WEIGHTS = "COMPUTER_USE_OMNI_WEIGHTS"

#: 检测框阈值。取 OmniParser 自带的默认值 0.01 —— 它偏召回，
#: 宁可多报几个再让 AI 忽略，也不要漏掉可点元素。
BOX_THRESHOLD = 0.01
#: 标题模型。florence2 是 V2 的默认，比 blip2 小且在这里够用。
CAPTION_MODEL = "florence2"

#: easyocr 识别阶段的批大小。上游 `check_ocr_box` 不传这个参数，而 easyocr 的默认值是
#: **1** —— 每个文本框单独跑一次识别。本机 3440x1440 全屏截图（150 个文本框）实测：
#: batch_size=1 → 3.4s，32 → 1.3s，两者检出的框与文本逐条一致。
EASYOCR_BATCH_SIZE = 32

#: Florence-2 单条描述的 token 上限。上游**写死** 20，而批生成要等这一批里最长的那条
#: 跑完才算完 —— 于是几个啰嗦的框会把整批拖住。实测 94 个图标（3440x1440 全屏）：
#: 20 → 0.93s，14 → 0.33s，输出 87/94 逐字相同（其余 7 条是长描述被截掉尾部，多为结尾句号）。
CAPTION_MAX_NEW_TOKENS = 14

#: `get_som_labeled_img` 调 `predict_yolo` 时用的两个阈值。并行预取那份 YOLO 结果时
#: 必须与它们**逐字一致**，否则拿到的是另一批框、而且没有任何地方会报错。
#: 这层耦合由 `core/tests/unit/test_omni_upstream_coupling.py` 盯着。
YOLO_IOU_THRESHOLD = 0.1

_detector_cache: Any = None


def _omni_root() -> Path:
    """OmniParser 源码根目录。"""
    import os

    override = os.environ.get(ENV_OMNI_HOME)
    if override:
        return Path(override)
    return Path.home() / ".computer-use" / "OmniParser"


def _weights_root() -> Path:
    import os

    override = os.environ.get(ENV_OMNI_WEIGHTS)
    if override:
        return Path(override)
    return Path.home() / ".computer-use" / "models"


def _missing_piece() -> str | None:
    """返回第一处缺失的安装件，全都就位则返回 None。

    逐项检查而不是「import 一下就完事」：这样报错能说清**缺哪一样**，
    而不是笼统的「OmniParser 未安装」。安装失败最常见的原因是权重没下全，
    那和依赖没装是两回事，用户要采取的补救也不同。
    """
    # 用 `find_spec` 而不是「import 完再不用它」：这里要的只是「在不在」，
    # 而真正 import torch 要几秒到几十秒。检查不该付加载的代价。
    import importlib.util

    for module in ("torch", "transformers", "ultralytics"):
        if importlib.util.find_spec(module) is None:
            return f"缺少 Python 依赖 {module}"

    root = _omni_root()
    if not (root / "util" / "omniparser.py").is_file():
        return f"缺少 OmniParser 源码（{root}）"

    detector = _weights_root() / "icon_detect_v3" / "model.pt"
    if not detector.is_file():
        return f"缺少检测权重（{detector}）"

    caption = _weights_root() / "icon_caption_florence"
    if not (caption / "config.json").is_file():
        return f"缺少描述模型权重（{caption}）"
    # `config.json` 是这套权重里最小的一件，光看它会把「传到一半断掉」当成装好了
    # （2026-09-24 实测：1.08GB 的 model.safetensors 没下完，config.json 已落盘）。
    if not (caption / "model.safetensors").is_file():
        return f"描述模型权重不完整，缺 model.safetensors（{caption}）"
    return None


#: 远程代码里声明了、但**运行时可选**的依赖。
#:
#: Florence-2 的 `modeling_florence2.py` 把 flash_attn 的 import 写在
#: `if is_flash_attn_2_available():` 守卫里 —— 在 CPU 上那段永远不会执行。
#: 但 `transformers.dynamic_module_utils.check_imports` 做的是**文本扫描**，
#: 不认运行时守卫，于是直接抛
#: 「requires the following packages that were not found: flash_attn」。
#:
#: 这是 transformers 的已知行为，不是 Florence-2 的错。正确做法是告诉它
#: 这个依赖是可选的，而不是去装一个 CPU 上装了也没用的 CUDA 内核库。
_OPTIONAL_REMOTE_IMPORTS = frozenset({"flash_attn"})


def _allow_optional_remote_imports() -> None:
    """让 `transformers` 的远程代码导入检查放行运行时可选的依赖。

    包一层 `check_imports` 而不是往 `sys.modules` 塞假模块：后者会让
    `is_flash_attn_2_available()` 有可能返回真，那才是真的撒谎。
    这里只是把「文本扫描发现的依赖」与「运行时真正需要的依赖」区分开。
    """
    try:
        from transformers import dynamic_module_utils
    except ImportError:
        return

    original = dynamic_module_utils.check_imports

    def check_imports(filename: str | bytes):
        try:
            return original(filename)
        except ImportError as exc:
            if any(name in str(exc) for name in _OPTIONAL_REMOTE_IMPORTS):
                return []
            raise

    dynamic_module_utils.check_imports = check_imports  # type: ignore[assignment]


def _stub_unused_paddle() -> None:
    """把 `paddleocr` 换成一个空壳，绕开它的模块级导入。

    OmniParser 的 `util/utils.py` 在**模块顶层**就 `from paddleocr import PaddleOCR`
    并立刻实例化一个 `paddle_ocr`，所以它是硬导入。但我们走的是 easyocr 分支
    （调用 `check_ocr_box(..., use_paddleocr=False)` —— 上游自己就有这个开关），
    那个对象**永远不会被调用**。

    不装 paddleocr + paddlepaddle 的理由：那是几百 MB，为一段永远不执行的路径付
    磁盘与 import 代价，与「独立环境、零共享」的初衷（DEC-037 / DEC-039）正好相反。

    这是**依赖注入而非打补丁**：不修改上游源码（它是 `setup omni` 克隆下来的，
    改了就与上游脱钩），只在导入前把那个用不到的模块占位。

    **只占位 `paddleocr` 这一个名字，绝不碰 `paddle`。** 这一条踩过坑：
    最初把 `paddle` 也塞进了 `sys.modules`，而 einops 探测张量后端的条件正是
    `framework_name in sys.modules` —— 于是它以为 paddle 在场，去访问
    `paddle.Tensor` 与 `paddle.static.Variable`，在真正的推理里炸掉。
    伪装成一个库是件很容易伤到第三方探测逻辑的事，能不做就不做。
    """
    import sys
    import types

    if "paddleocr" in sys.modules:
        return
    try:
        import importlib.util

        if importlib.util.find_spec("paddleocr") is not None:
            return                # 真装了就别动它
    except (ImportError, ValueError):
        pass

    module = types.ModuleType("paddleocr")

    class _UnusedPaddleOCR:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def ocr(self, *args: Any, **kwargs: Any) -> Any:
            raise WorkerError(
                OMNI_FAILED,
                "本 worker 走 easyocr 分支，paddleocr 未安装且不应被调用",
            )

    module.PaddleOCR = _UnusedPaddleOCR  # type: ignore[attr-defined]
    sys.modules["paddleocr"] = module


def _tune_easyocr_batch() -> None:
    """给上游的 easyocr 调用补上 `batch_size`（默认 1 太慢，见 `EASYOCR_BATCH_SIZE`）。

    **必须在 `util.omniparser` 被 import 之前调用。** 那个模块用
    `from util.utils import check_ocr_box` 把函数**按值**绑进自己的命名空间，
    之后再改 `util.utils.check_ocr_box` 对它无效 —— 这是本节最容易踩空的地方，
    顺序错了不会报错，只会静默地退回 batch_size=1。

    做法与 `_stub_unused_paddle` 同源：**依赖注入而非打补丁**。不改上游源码
    （那是 `setup omni` 克隆下来的，改了就与上游脱钩），只替换那一个要调的入口。
    """
    try:
        from util import utils as upstream
    except ImportError:
        return

    original = upstream.check_ocr_box

    def check_ocr_box(image_source: Any, *args: Any, **kwargs: Any) -> Any:
        easyocr_args = kwargs.get("easyocr_args")
        merged = dict(easyocr_args) if isinstance(easyocr_args, dict) else {}
        # 调用方显式给的 batch_size 优先 —— 这层只补默认值，不改别人的选择。
        merged.setdefault("batch_size", EASYOCR_BATCH_SIZE)
        kwargs["easyocr_args"] = merged
        return original(image_source, *args, **kwargs)

    upstream.check_ocr_box = check_ocr_box  # type: ignore[assignment]


def _skip_unused_annotation() -> None:
    """把「画标注框 + 编码 PNG + base64」换成一次几乎不花时间的占位。

    那张画好框的图**产品侧从来不用**：我们只取结构化数据，AI 看的是原始截图。
    而上游 `get_som_labeled_img` 无条件画框、PNG 编码、base64 编码一张 3440x1440 的图
    —— 实测 0.38s，纯白花（换成空图后 0.02s）。

    做法与 `_stub_unused_paddle` 同源：**依赖注入而非打补丁**。不改上游源码，
    只把一个用不到的渲染器换成返回空图的替身；上游的编排（过滤、合并、描述、
    坐标换算）一个字都没动，只有那张没人看的图变成了黑的。

    返回空 dict 而不是假的坐标：`label_coordinates` 我们同样不用（元素坐标来自
    `filtered_boxes_elem`），编一份假的反而是多余的谎。
    """
    from util import utils as upstream

    def annotate(image_source: Any, boxes: Any, logits: Any, phrases: Any,
                 **kwargs: Any) -> Any:
        import numpy as np

        height, width = image_source.shape[:2]
        # 尺寸必须与真实图像一致：上游在 `output_coord_in_ratio` 分支里有 assert 校验它。
        return np.zeros((height, width, 3), dtype=np.uint8), {}

    upstream.annotate = annotate  # type: ignore[assignment]


def _cap_caption_length(detector: Any) -> None:
    """给 Florence-2 的生成补一个 `max_new_tokens` 上限（见 `CAPTION_MAX_NEW_TOKENS`）。

    包 `generate` 而不是包上游的取描述函数：`max_new_tokens=20` 是在
    `get_parsed_content_icon` 里**写在调用处**的，而包住 `generate` 只动这一个数，
    其余参数（输入、beam、采样策略）原样透传。
    """
    model = detector.caption_model_processor.get("model")
    if model is None:
        return
    original = model.generate

    def generate(*args: Any, **kwargs: Any) -> Any:
        limit = kwargs.get("max_new_tokens")
        if isinstance(limit, int) and limit > CAPTION_MAX_NEW_TOKENS:
            kwargs["max_new_tokens"] = CAPTION_MAX_NEW_TOKENS
        return original(*args, **kwargs)

    model.generate = generate  # type: ignore[assignment]


def _overlap_ocr_and_yolo(detector: Any) -> None:
    """让 OCR 与 YOLO **同时**跑（两者互不依赖，串行却要 1.7s，并行实测 0.75s）。

    为什么不去重写编排：`Omniparser.parse` 的顺序是「OCR → get_som_labeled_img
    （内部先 YOLO 再描述）」，而描述那一步**需要 OCR 的结果**做过滤与排序。
    所以能并行的只有最前面这两段，而它们恰好在两个不同的模块全局名下被调用：

    - `util.omniparser.check_ocr_box` —— OCR 入口（`_tune_easyocr_batch` 已经包过一层）
    - `util.utils.predict_yolo`       —— YOLO 入口（`get_som_labeled_img` 里调用）

    于是：OCR 丢到后台线程，前台把 YOLO 先算完存进 `pending`；稍后
    `get_som_labeled_img` 来调 `predict_yolo` 时，直接把这份结果还给它。
    上游的编排一行不改，只有「谁先跑」变了。

    `pending` 只留一个槽位、且每次 `check_ocr_box` 都是**覆盖**而不是追加：
    worker 的请求循环是串行的，不会有两次解析交叠；万一 `predict_yolo` 没被调到，
    残留的那份也会在下一次解析开始时被覆盖掉，不会被错用。
    """
    from util import omniparser as upstream_parser
    from util import utils as upstream

    original_check = upstream_parser.check_ocr_box
    original_predict = upstream.predict_yolo
    pending: list[Any] = []

    def check_ocr_box(image_source: Any, *args: Any, **kwargs: Any) -> Any:
        import threading

        slot: dict = {}
        # `Image.open(...)` 交回来的对象是**懒加载**的：谁先 `.load()` 谁读那个流。
        # 两个线程各自去读同一个流会把它读坏（实测报 `OSError: broken data stream`）。
        # 所以先把像素读进内存，再各拿一份独立的图，之后两边都只读不碰文件。
        if hasattr(image_source, "load"):
            image_source.load()
        ocr_source = image_source.copy() if hasattr(image_source, "copy") else image_source

        def run_ocr() -> None:
            try:
                slot["value"] = original_check(ocr_source, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 —— 后台线程里必须接住再回主线程抛
                slot["error"] = exc

        thread = threading.Thread(target=run_ocr, name="cu-omni-ocr", daemon=True)
        thread.start()
        # 前台算 YOLO。阈值必须与 `get_som_labeled_img` 传给 `predict_yolo` 的一致。
        try:
            result = detector.som_model.predict(source=image_source,
                                                conf=BOX_THRESHOLD, iou=YOLO_IOU_THRESHOLD)
        except Exception:
            # 前台先炸：也要把后台那条收干净再抛，否则 OCR 线程会一次次攒在进程里。
            thread.join()
            raise
        pending[:] = [result]
        thread.join()
        if "error" in slot:
            raise slot["error"]
        return slot["value"]

    def predict_yolo(model: Any, image: Any, box_threshold: float, imgsz: Any, scale_img: bool,
                     iou_threshold: float = 0.7) -> Any:
        if pending and not scale_img:
            result = pending.pop()
            boxes = result[0].boxes.xyxy
            conf = result[0].boxes.conf
            return boxes, conf, [str(i) for i in range(len(boxes))]
        return original_predict(model=model, image=image, box_threshold=box_threshold,
                                imgsz=imgsz, scale_img=scale_img, iou_threshold=iou_threshold)

    upstream_parser.check_ocr_box = check_ocr_box  # type: ignore[assignment]
    upstream.predict_yolo = predict_yolo  # type: ignore[assignment]


def _load_detector():
    """构造 OmniParser。未安装时**显式报错**，不静默降级（DEC-002）。

    结果缓存：加载要读几百 MB 权重，而 worker 是常驻的，没理由每条命令重来一次。
    """
    global _detector_cache
    if _detector_cache is not None:
        return _detector_cache

    missing = _missing_piece()
    if missing is not None:
        raise WorkerError(
            OMNI_NOT_INSTALLED,
            f"OmniParser 不可用：{missing}",
            hint="运行 `computer-use setup omni` 完成安装",
            detail={"omni_home": str(_omni_root()), "weights": str(_weights_root())},
        )

    import sys

    # OmniParser 的 `util` 是包内相对导入（`from util.utils import ...`），
    # 所以必须把**源码根目录**放进 sys.path，而不是 util 目录。
    root = str(_omni_root())
    if root not in sys.path:
        sys.path.insert(0, root)

    _stub_unused_paddle()               # 必须在 import util.utils 之前
    _tune_easyocr_batch()               # 必须在 import util.omniparser 之前（按值绑定）
    _allow_optional_remote_imports()    # 必须在 Florence-2 模型加载之前

    try:
        from util.omniparser import Omniparser
    except ImportError as exc:
        raise WorkerError(
            OMNI_NOT_INSTALLED,
            f"OmniParser 依赖不全（{exc.name}）",
            hint="运行 `computer-use setup omni` 重装",
        ) from exc

    detector = Omniparser({
        "som_model_path": str(_weights_root() / "icon_detect_v3" / "model.pt"),
        "caption_model_name": CAPTION_MODEL,
        "caption_model_path": str(_weights_root() / "icon_caption_florence"),
        "BOX_TRESHOLD": BOX_THRESHOLD,
    })
    # 下面三步都必须在**第一次 parse 之前**装好；它们只改「用哪份结果 / 跑多长」，
    # 不改上游的编排。顺序无关，但都放在这里，让「装了什么」集中在一处可查。
    _skip_unused_annotation()
    _cap_caption_length(detector)
    _overlap_ocr_and_yolo(detector)
    _detector_cache = detector
    return detector


def _to_elements(parsed: list, width: int, height: int) -> list[dict]:
    """把 OmniParser 的产出转成我们的元素形状，**并把比例坐标换成像素**。

    OmniParser 用 `output_coord_in_ratio=True` 调用，bbox 是 0~1 的比例。
    而本项目的坐标契约一律是**物理像素**（DEC-001），所以必须乘回图像尺寸。

    这一步做错的后果很隐蔽：所有框都挤在左上角一小块里，看着像检测失败，
    实际是坐标没换算。所以它在自检里有一条专门的守卫。
    """
    elements: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        bbox = item.get("bbox")
        if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            continue
        try:
            x1, y1, x2, y2 = (float(v) for v in bbox)
        except (TypeError, ValueError):
            continue
        # 比例 → 像素。取整到像素，AI 拿到的就是能直接点的整数。
        pixel_box = [int(round(x1 * width)), int(round(y1 * height)),
                     int(round(x2 * width)), int(round(y2 * height))]
        elements.append({
            "type": str(item.get("type") or ""),
            "bbox": pixel_box,
            "interactivity": bool(item.get("interactivity")),
            "content": str(item.get("content") or ""),
            "source": "omni",
        })
    return elements


def detect(image_path: str) -> dict:
    """对一张图跑检测，返回 `{"elements": [...]}`。

    这里刻意抛错而不是返回空结果：**「没有元素」与「检测器不可用」必须是两件事**。
    返回空结果会让 AI 认为界面上什么都没有，然后据此做决策。
    """
    import base64
    import io

    source = Path(image_path)
    if not source.is_file():
        raise WorkerError(OMNI_FAILED, f"图片不存在：{image_path}",
                          detail={"image": image_path})

    detector = _load_detector()

    try:
        from PIL import Image

        with Image.open(source) as image:
            width, height = image.size
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="PNG")
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        raise WorkerError(OMNI_FAILED, f"图片无法读取：{exc}",
                          detail={"image": image_path}) from exc

    try:
        # 第一个返回值是画好标注框的图。我们不需要它 —— 产品只取结构化数据，
        # AI 看的是原始截图。刻意不返回它，省掉一次多余的图片编码。
        _labelled_image, parsed = detector.parse(encoded)
    except Exception as exc:  # noqa: BLE001
        # 推理失败与「没检测到元素」是两回事，必须分开报。
        raise WorkerError(OMNI_FAILED, f"检测失败：{type(exc).__name__}: {exc}",
                          detail={"image": image_path}) from exc

    return {
        "elements": _to_elements(parsed or [], width, height),
        "image": {"path": str(source), "width": width, "height": height},
    }


# ---------------------------------------------------------------------------
# 多模态优化（DEC-011）
# ---------------------------------------------------------------------------

#: 优化请求的提示词。**只改描述，不动位置** —— 这一条必须在提示词里说死，
#: 并且在返回后由 `validate_optimized` 强制校验。提示词是请求，校验是保证。
OPTIMIZE_PROMPT = """You are given a screenshot and a list of UI elements detected in it.

Your job is to correct the description of each element. The detection model locates
elements well but describes them poorly. You understand what things are, but you do
not know precisely where they are.

Hard rules:
1. Do NOT change any bbox. Copy every bbox exactly as given.
2. Do NOT add elements. You may drop an element you judge to be noise, but you may
   not invent one.
3. Return the same JSON shape: {"elements": [{"bbox": [...], "content": "..."}]}

Element list:
"""


def optimize_with_vlm(original: dict, image_path: str, config: dict) -> dict:
    """调多模态端点纠正描述。失败时抛 `vlm_failed`，**不重试**。

    不重试的理由（架构 §3）：重试会放大 token 成本，而用户可以自己重跑一次；
    更重要的是，失败时原始结构化数据仍然完好，重试的收益小于它的代价。
    """
    import base64
    import urllib.error
    import urllib.request

    base_url = (config.get("base_url") or "").rstrip("/")
    api_key = config.get("api_key") or ""
    model = config.get("model_name") or ""
    if not base_url or not model:
        raise WorkerError(VLM_FAILED, "多模态端点未配置",
                          hint="`computer-use config set vlm.base_url <url>` 与 `vlm.model_name`")

    try:
        image_b64 = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
    except OSError as exc:
        raise WorkerError(VLM_FAILED, f"图片读取失败：{exc}") from exc

    body = json.dumps({
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": OPTIMIZE_PROMPT + json.dumps(original, ensure_ascii=False)},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            ],
        }],
        "temperature": 0,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        # User-Agent 必须显式给：默认的 `Python-urllib/3.x` 会被不少 WAF
        # 直接 403（实测 Cloudflare error code 1010）。
        "User-Agent": config.get("user_agent") or "computer-use/0.1",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # **端点路径要试两次。** 配置里的 `base_url` 可能是网关根
    # （`https://host`），也可能是完整前缀（`https://host/v1`）。少了 `/v1`
    # 时请求会打到网关的**网页首页**，拿回 200 但 Content-Type 是 text/html，
    # 而报错是 JSON 解析失败 —— 看起来像端点坏了，实际只是路径不对。
    # 这是**端点发现**的一次尝试，不是对模型调用的重试（DEC-011 的「不重试」针对后者）。
    payload = None
    failures: list[str] = []
    for url in (f"{base_url}/chat/completions", f"{base_url}/v1/chat/completions"):
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 405):
                failures.append(f"{url} -> {exc.code}")
                continue
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise WorkerError(VLM_FAILED, f"端点返回 {exc.code}",
                              hint="检查 base_url / api_key；原始结构化数据仍可用",
                              detail={"status": exc.code, "url": url, "body": detail}) from exc
        except Exception as exc:  # noqa: BLE001
            raise WorkerError(VLM_FAILED, f"端点调用失败：{type(exc).__name__}: {exc}",
                              hint="检查 base_url / api_key；原始结构化数据仍可用") from exc

        # 拿到 JSON 才算成功；text/html 说明这是网关的首页而不是 API。
        if "json" not in content_type.lower():
            failures.append(f"{url} -> 200 但 Content-Type={content_type!r}（不是 API）")
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            failures.append(f"{url} -> 响应不是合法 JSON")
            continue
        break

    if payload is None:
        raise WorkerError(
            VLM_FAILED,
            "端点未返回可用的 JSON 响应",
            hint="检查 base_url —— 有些网关需要在 base_url 里带 `/v1`；"
                 "或该地址其实是个网页而不是 API 入口。原始结构化数据仍可用。",
            detail={"tried": failures},
        )

    try:
        content = payload["choices"][0]["message"]["content"]
        optimized = json.loads(_strip_code_fence(content))
        validate_optimized(original, optimized)
    except ContractViolation as exc:
        raise WorkerError(VLM_FAILED, f"优化结果违约：{exc}",
                          hint="原始结构化数据仍可用（DEC-011 的契约已挡住这次改动）") from exc
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise WorkerError(VLM_FAILED, f"端点响应无法解析：{type(exc).__name__}") from exc
    return optimized


def _strip_code_fence(text: str) -> str:
    """模型常把 JSON 包在 ```json 围栏里。剥掉，别让它变成解析失败。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines)
    return stripped


# ---------------------------------------------------------------------------
# 命令面
# ---------------------------------------------------------------------------


def handle_parse(params: dict) -> dict:
    """`omni.parse`：图片 → markdown 文件。返回文件路径与元素数，**不返回内容**。

    不返回内容是有意的（PM §6.4）：一个内容多的界面会产生几百个元素，
    把它们塞进调用方的上下文，比让它按需读文件贵得多。
    """
    image_path = params.get("image")
    out_dir = params.get("out_dir")
    file_name = params.get("file_name")
    if not (isinstance(image_path, str) and isinstance(out_dir, str)
            and isinstance(file_name, str)):
        raise WorkerError(OMNI_FAILED, "omni.parse 需要 image / out_dir / file_name（均为字符串）")

    detected = detect(image_path)

    # UIA 文本通道（可选）：调用方在 base 环境里读好 UIA，把元素一起送进来合并。
    # 为什么由 base 读而不是 worker：UIA 是 Win32 调用，而 base 才是桌面层所在；
    # worker 的职责是「图片 → 结构化数据」，不该管窗口。
    extra = params.get("extra_elements")
    _diag = ""
    if isinstance(extra, list) and extra:
        _before = detected.get("elements") or []
        _after = _merge_uia(_before, extra)
        _uia_n = sum(1 for _e in _after if _e.get("source") == "uia")
        _diag = (f"uia={_uia_n} detector={len(_after) - _uia_n} "
                 f"(detector input {len(_before)}, uia input {len(extra)})")
        detected = {"elements": _after, "image": detected.get("image")}
    else:
        _diag = f"uia_in=0 det_in={len(detected.get('elements') or [])}"

    model_name: str | None = None
    payload = detected
    raw_vlm = params.get("vlm")
    vlm_params: dict = raw_vlm if isinstance(raw_vlm, dict) else {}
    if params.get("ai"):
        try:
            optimized = optimize_with_vlm(detected, image_path, vlm_params)
        except WorkerError as exc:
            # VLM 失败时**先把已经算出来的检测结果落盘**，再报错。
            #
            # 检测是这条链上最贵的一环（CPU 上数十秒），而 VLM 那一步只负责「把描述
            # 改写得更准」。让后者失败把前者一起废掉，等于一次网络抖动就白跑一次解析。
            # 而且 `vlm_failed` 的提示语里写着「原始结构化数据仍可用」—— 不落盘，
            # 那句提示就是假的。
            #
            # 落盘用的是**基础名**（`-omni-`），不是调用方给的 `-omni_ai-`：
            # 这份文件里没有经过 AI 优化，用 ai 的名字会让下一次读取的人以为优化成功了。
            base_name = file_name.replace("-omni_ai-", "-omni-")
            base_path = Path(out_dir) / base_name
            base_path.parent.mkdir(parents=True, exist_ok=True)
            base_path.write_text(
                to_markdown(detected, source=image_path, diag=_diag), encoding="utf-8")
            exc.detail = {**(exc.detail or {}), "base_markdown": str(base_path)}
            raise
        payload = merge_descriptions(detected, optimized)
        raw_model = vlm_params.get("model_name")
        model_name = raw_model if isinstance(raw_model, str) else None

    target = Path(out_dir) / file_name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        to_markdown(payload, source=image_path, model=model_name, diag=_diag),
        encoding="utf-8"
    )
    return {"path": str(target), "element_count": len(payload.get("elements") or []),
            "model_name": model_name}


ROUTES = {"omni.parse": handle_parse, "system.ping": lambda _p: {"ok": True}}


def dispatch(request: dict) -> dict:
    """按方法名路由。`method` 来自管道，可能是任何 JSON 值，因此逐层校验。"""
    method = request.get("method")
    if not isinstance(method, str):
        raise WorkerError(OMNI_FAILED, f"请求缺少 method 字段：{method!r}")
    handler = ROUTES.get(method)
    if handler is None:
        raise WorkerError(OMNI_FAILED, f"未知方法：{method}")
    params = request.get("params")
    return handler(params if isinstance(params, dict) else {})


def _force_utf8_streams() -> None:
    """把 stdio 切成 UTF-8。

    **这是传输层的正确性问题，不是显示问题。** Windows 上 Python 的 stdout
    默认跟随本地代码页（本机 cp936/GBK），而协议那头按 UTF-8 解码 ——
    实测：写出的 `未知方法：bogus` 在 UTF-8 侧读出乱码，daemon 解析响应直接失败。

    `_dump_line` 已经保证了 `ensure_ascii=False`（中文不被转义成 \\uXXXX），
    但那只决定**字符如何序列化**，编码由这个流决定。两件事都要对。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _dump_line(message: dict) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"


def serve_stdio() -> int:
    """stdio 传输。daemon 也可以用管道接它，换传输不换格式（架构 §1.4）。"""
    _force_utf8_streams()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        request_id = None
        try:
            request = json.loads(raw)
            request_id = request.get("id")
            result = dispatch(request)
            sys.stdout.write(_dump_line({"jsonrpc": "2.0", "id": request_id, "result": result}))
        except WorkerError as exc:
            sys.stdout.write(_dump_line({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32000, "message": exc.message, "data": exc.as_rpc()}}))
        except Exception as exc:  # noqa: BLE001 —— 边界：任何异常都要变成响应，不能断连
            traceback.print_exc(file=sys.stderr)
            sys.stdout.write(_dump_line({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32000, "message": f"{type(exc).__name__}: {exc}",
                "data": {"code": OMNI_FAILED, "hint": "看 worker 的 stderr"}}}))
        sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cu-omni", description="Computer-Use 解析 worker")
    parser.add_argument("--stdio", action="store_true", help="走 stdio NDJSON（默认）")
    parser.add_argument("--selftest", action="store_true", help="不依赖检测器自检")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    return serve_stdio()


def _selftest() -> int:
    """不依赖 torch 的自检：markdown 渲染、契约校验，以及 **JSON-RPC 传输层**。

    transport 那一段**必须起子进程**测。同进程调用 `dispatch()` 永远不会暴露编码问题 ——
    缺陷恰恰在「把字符串写进 stdout」这一步，而测试进程的 stdout 编码与被测进程的
    可能不同。本轮真实踩到过：worker 用 GBK 写中文，daemon 按 UTF-8 读，响应直接损坏。

    也**不得**在这里给子进程预设 `PYTHONIOENCODING=utf-8`。那等于给被测对象戴上安全帽，
    把这个自检要抓的缺陷正好遮住（实测：加了那一行之后，撤掉修复自检依然全绿）。
    子进程必须在「没有任何环境兜底」的条件下跑。
    """
    _force_utf8_streams()

    sample = {"elements": [
        {"type": "icon", "bbox": [10, 20, 30, 40], "interactivity": True, "content": "设置"},
        {"type": "text", "bbox": [50, 60, 150, 80], "interactivity": False,
         "content": "标题|含竖线\n含换行"},
    ]}
    text = to_markdown(sample, source="shot.png", model="example-vlm")
    assert "| # | type | bbox | interactivity | content |" in text, text
    assert "10,20,30,40" in text, text
    assert "\\|" in text, "竖线必须被转义，否则表格结构被破坏"
    assert "\n含换行" not in text, "换行必须被消掉"

    # ---- 契约校验（DEC-011）----
    try:
        validate_optimized(sample, {"elements": sample["elements"] + [
            {"type": "icon", "bbox": [1, 1, 2, 2], "content": "凭空造的"}]})
    except ContractViolation:
        pass
    else:
        raise AssertionError("新增元素未被契约拦下")

    optimized = {"elements": [
        {"bbox": [50, 60, 150, 80], "content": "标题"},
        {"bbox": [10, 20, 30, 40], "content": "偏好设置"},
    ]}
    validate_optimized(sample, optimized)
    merged = merge_descriptions(sample, optimized)
    assert merged["elements"][0]["content"] == "偏好设置"
    assert merged["elements"][0]["bbox"] == [10, 20, 30, 40], "bbox 必须保留检测器的值"

    _selftest_transport()

    print("cu-omni 自检通过：markdown 渲染 + 契约校验 + JSON-RPC 传输层")
    return 0


def _selftest_transport() -> None:
    """起一个真实的 worker 子进程，走一遍 stdio 协议。

    测三件事，每一件都是「只在真实进程边界上才会错」的：
      1. 响应能按 UTF-8 解出来（编码）；
      2. 中文原样可读，不是被转义成 \\uXXXX，也不是乱码；
      3. 错误响应带封闭错误码，且**不泄露栈**。
    """
    import os
    import subprocess

    requests = "".join(_dump_line(message) for message in [
        {"jsonrpc": "2.0", "id": 1, "method": "system.ping", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "no.such.method", "params": {}},
    ])
    # 显式摘掉环境里的编码兜底：子进程必须在「裸」条件下跑，
    # 这样它只能靠自己切换 stdout 编码，而这正是被测的行为。
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--stdio"],
        input=requests.encode("utf-8"), capture_output=True, timeout=60, env=env,
    )
    assert proc.returncode == 0, f"worker 退出码 {proc.returncode}"

    # 关键：不解码就交给 json.loads 会掩盖编码错误，所以显式按 UTF-8 解。
    try:
        lines = proc.stdout.decode("utf-8").strip().splitlines()
    except UnicodeDecodeError as exc:
        raise AssertionError(
            f"worker 的 stdout 不是 UTF-8：{exc}。"
            "协议那头按 UTF-8 解码，这会让响应直接损坏。"
        ) from exc
    assert len(lines) == 2, f"应当有两个响应，实际 {len(lines)} 个：{lines}"

    first = json.loads(lines[0])
    assert first["id"] == 1 and first.get("result") == {"ok": True}, first

    second = json.loads(lines[1])
    error = second.get("error")
    assert error is not None, second
    data = error["data"]
    assert data["code"] == OMNI_FAILED, data
    # 中文必须原样可读：既是编码正确，也验证了 ensure_ascii=False 没有被改回去。
    assert "未知方法" in error["message"], error["message"]
    assert "\\u" not in error["message"], "中文被转义了，`ensure_ascii=False` 被改回去了"
    assert "Traceback" not in json.dumps(second), "错误响应里不得出现栈"


if __name__ == "__main__":
    sys.exit(main())
