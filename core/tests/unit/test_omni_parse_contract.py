"""`omni/worker.py` 的 `handle_parse` 返回契约：**元素列表必须跟着回来**。

契约出处：`handle_parse` 的 docstring 与 DEC-094。

为什么这条值得一条用例：base 侧拿到坐标的唯一入口就是这个返回值，而在 DEC-094
之前它只有路径与计数（PM §6.4 的旧约定）。调用方要的是坐标，不是「有多少个元素」——
少了 `elements`，调用方只能再读一次 md，或者干脆凭图估坐标，而后者正是这条链
要解决的问题。返回里少一个键，整条链就退回原样，且**不报错**。

这一层以前没有单测，因为 `handle_parse` 会真的加载检测器（torch + 权重，属于
独立环境，见 DEC-037）。这里把 `detect` 换成替身，只验输出契约这一件事：
输入是假的，输出的形状与坐标透传仍是真的。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WORKER_PATH = REPO / "omni" / "worker.py"


def _load(name: str, path: Path):
    """按文件路径加载 —— worker 属于独立环境，不在 `cu` 命名空间里。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"无法加载 {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = _load("cu_omni_worker_parse_contract_under_test", WORKER_PATH)

#: 两条来源不同的元素：一条 UIA（带 `enabled`），一条检测器（没有这一列）。
#: 坐标是刻意取的不整齐的数 —— 任何一处重新计算或取整都会让断言红。
_DETECTED = {
    "elements": [
        {"type": "button", "bbox": [964, 394, 991, 426], "interactivity": True,
         "enabled": True, "content": "新建", "source": "uia"},
        {"type": "text", "bbox": [10, 20, 120, 40], "interactivity": False,
         "content": "标题栏文字", "source": "detector"},
    ],
    "image": {"path": "fake.png", "width": 100, "height": 100},
}


def _params(tmp_path: Path) -> dict:
    return {"image": "fake.png", "out_dir": str(tmp_path),
            "file_name": "elements-omni-0001.md"}


def test_returns_the_element_list_with_coordinates_intact(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "detect", lambda _path: _DETECTED)
    result = worker.handle_parse(_params(tmp_path))
    assert [element["bbox"] for element in result["elements"]] == [
        [964, 394, 991, 426], [10, 20, 120, 40],
    ]


def test_returned_elements_keep_their_source_and_enabled(tmp_path, monkeypatch):
    """`enabled` 只在 UIA 那一侧存在。带上它，且**不**给检测器那条凭空补一个。"""
    monkeypatch.setattr(worker, "detect", lambda _path: _DETECTED)
    result = worker.handle_parse(_params(tmp_path))
    assert [element["source"] for element in result["elements"]] == ["uia", "detector"]
    assert result["elements"][0]["enabled"] is True
    assert "enabled" not in result["elements"][1]


def test_element_count_and_path_describe_what_was_written(tmp_path, monkeypatch):
    """计数与路径指向真的落盘的那一份 —— 调用方据此回读完整记录。"""
    monkeypatch.setattr(worker, "detect", lambda _path: _DETECTED)
    result = worker.handle_parse(_params(tmp_path))
    assert result["element_count"] == len(result["elements"]) == 2
    assert Path(result["path"]).name == "elements-omni-0001.md"
    assert Path(result["path"]).is_file()
    # 没有配 VLM 端点时 `--ai` 没走，这一栏必须是空而不是一个假模型名。
    assert result["model_name"] is None
