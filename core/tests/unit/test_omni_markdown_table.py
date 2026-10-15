"""`omni/worker.py` 的元素表：列序、`enabled` 的渲染、以及**读回来还对得上**。

契约出处：

- `omni/worker.py` 的 `COLUMNS` / `_flag` / `to_markdown`；
- 消费侧是 `core/tests/native/_element_table.py` 的 `read_element_table` ——
  四个原生诊断脚本都用它。这里把「写」与「读」放在同一条用例里对撞，
  因为**这两侧曾经各自演化**：读的那一侧按列位置取，写的那一侧加了一列，
  于是诊断脚本会安静地读错列（把 `enabled` 当成 `content`），
  而症状是「仪器给出一个看起来正常的错结论」。

为什么这一层以前没有单测：md 的格式只被原生脚本消费，而原生脚本不进 CI。
代价是加一列时没有任何东西会红 —— 这条用例补的就是那个缺口。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
WORKER_PATH = REPO / "omni" / "worker.py"
TABLE_READER_PATH = REPO / "core" / "tests" / "native" / "_element_table.py"


def _load(name: str, path: Path):
    """按文件路径加载 —— worker 属于独立环境，两个文件都不在 `cu` 命名空间里。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"无法加载 {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = _load("cu_omni_worker_table_under_test", WORKER_PATH)
table = _load("cu_element_table_under_test", TABLE_READER_PATH)


def _row(**over) -> dict:
    """一条 UIA 来的行（带 `enabled`）。检测器来的行用 `del row["enabled"]` 造。"""
    base = {"type": "button", "bbox": [964, 394, 991, 426], "interactivity": True,
            "enabled": True, "content": "新建", "source": "uia"}
    base.update(over)
    return base


def _write(rows: list[dict], tmp_path: Path) -> Path:
    target = tmp_path / "elements-omni-0001.md"
    target.write_text(worker.to_markdown({"elements": rows}), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# 列序
# --------------------------------------------------------------------------- #


def test_the_column_order_is_the_contract() -> None:
    # oracle: specified —— COLUMNS 的注释「顺序即列序，改它等于改契约」。
    assert worker.COLUMNS == ("#", "type", "bbox", "interactivity", "enabled",
                              "content", "source")


def test_the_header_row_is_written_from_the_columns(tmp_path: Path) -> None:
    text = _write([_row()], tmp_path).read_text(encoding="utf-8")

    assert "| # | type | bbox | interactivity | enabled | content | source |" in text


# ---------------------------------------------------------------------------
# enabled 的三态
# --------------------------------------------------------------------------- #


def test_enabled_renders_as_y_or_n(tmp_path: Path) -> None:
    text = _write([_row(enabled=True), _row(enabled=False, interactivity=False)],
                  tmp_path).read_text(encoding="utf-8")

    assert "| y |" in text and "| n |" in text


def test_an_absent_enabled_renders_as_empty_not_as_n(tmp_path: Path) -> None:
    """空 = 这条来自检测器、它给不出这个信息；`n` = UIA 明确说此刻禁用。

    两者都印成 `n` 会让读的人以为检测器也做了这个判断 —— 而它做不了。
    """
    row = _row(source="omni")
    del row["enabled"]                       # 检测器产出没有这个字段

    rows = table.read_element_table(_write([row], tmp_path))

    assert rows[0]["enabled"] is None
    assert rows[0]["interactivity"] is True


# ---------------------------------------------------------------------------
# 写出去再读回来
# --------------------------------------------------------------------------- #


def test_the_written_table_round_trips_through_the_shared_reader(tmp_path: Path) -> None:
    rows = [_row(), _row(type="text", bbox=[0, 0, 12, 12], interactivity=False,
                         enabled=False, content="纯文本", source="omni")]
    path = _write(rows, tmp_path)

    read_back = table.read_element_table(path)

    assert [r["bbox"] for r in read_back] == [(964, 394, 991, 426), (0, 0, 12, 12)]
    assert [r["type"] for r in read_back] == ["button", "text"]
    assert [r["content"] for r in read_back] == ["新建", "纯文本"]
    assert [r["source"] for r in read_back] == ["uia", "omni"]
    assert [r["enabled"] for r in read_back] == [True, False]
    assert [r["interactivity"] for r in read_back] == [True, False]
    assert [r["id"] for r in read_back] == [1, 2]


def test_a_cell_containing_a_pipe_does_not_shift_the_columns(tmp_path: Path) -> None:
    """内容里的 `|` 被写成 `\\|`，读回来要还原 —— 不还原的话 `split("|")` 会多切一格，
    整行宽度对不上而被丢掉。窗口标题里带竖线并不罕见。"""
    path = _write([_row(content="a|b")], tmp_path)

    assert "a\\|b" in path.read_text(encoding="utf-8")
    read_back = table.read_element_table(path)

    assert read_back[0]["content"] == "a|b"
    assert read_back[0]["source"] == "uia"


@pytest.mark.parametrize("bad", ["", "1,2,3", "1,2,3,4,5", "a,b,c,d", "10,10,10,10"])
def test_a_row_with_an_unusable_bbox_is_dropped_by_the_reader(bad: str,
                                                              tmp_path: Path) -> None:
    # oracle: derived —— 诊断工具宁可少几行，也不要把错位的值喂给上面。
    path = _write([_row(bbox=[1, 2, 3, 4])], tmp_path)
    text = path.read_text(encoding="utf-8").replace("1,2,3,4", bad, 1)
    path.write_text(text, encoding="utf-8")

    assert table.read_element_table(path) == []


def test_the_worker_self_test_still_passes() -> None:
    """`setup omni` 装完之后会跑这个自检（`omni_setup.py`）。

    它里面断言过 md 的渲染 —— 本轮加 `enabled` 列时它断了一次，而当时的单测一条都没覆盖它，
    破坏是在真机跑自检时才发现的。接进 CI：改了渲染而漏掉这个消费方，这里就红。
    """
    proc = subprocess.run([sys.executable, str(WORKER_PATH), "--selftest"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180)
    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
