"""命名规则（``ids.artifact_name``）与清理规则（``daemon.storage.is_artifact``）的一致性。

为什么单独写一条跨模块测试：DEC-017 阶段 1 靠 ``is_artifact`` 决定「哪些 md 可以随
来源图片一起删」。命名规则在 ``ids`` 里定义，清理规则在 ``daemon.storage`` 里消费；
两者一旦漂移，结构化数据要么删不掉（只能等阶段 2 整会话删除），要么误删操作日志。
此前正是因为两处各用一套正则/分隔符，窗口来源的结构化数据永远进不了阶段 1。

这条测试的期望值直接取自 ``artifact_name`` 的产出（命名规则的唯一公开面），
断言清理侧必须认得自己产出的文件名 —— 规则以后再漂移，它会立刻红。

契约依据：``data-model.md`` §3.2（命名规则）、``DEC-017``（阶段 1 删除对象）、
``DEC-025``（img 形态）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cu.daemon.storage import is_artifact
from cu.ids import artifact_name

#: 窗口截图：png，有坐标。（标题用 ASCII —— DEC-043 会把非 ASCII 标题折叠掉。）
_WINDOW_PNG = artifact_name("win", 3, hwnd="0x0001A2B", title="notepad", origin=(100, 200))
#: 全屏截图：png，有坐标（原点 0x0）。
_FULL_PNG = artifact_name("full", 1, hwnd="0x00000000", title="fullscreen", origin=(0, 0))
#: 窗口来源的结构化数据：有坐标，后缀在坐标之前、连字符落点。
_WINDOW_OMNI = artifact_name("win", 4, hwnd="0x0001A2B", title="notepad",
                             suffix="omni", origin=(100, 200), ext="md")
_WINDOW_OMNI_AI = artifact_name("win", 5, hwnd="0x0001A2B", title="notepad",
                                suffix="omni_ai", origin=(100, 200), ext="md")
#: img 外部图片的结构化数据：无坐标，_omni 贴标题、seq 紧跟。
_IMG_OMNI = artifact_name("img", 7, title="photo", suffix="omni", ext="md")
_IMG_OMNI_AI = artifact_name("img", 8, title="photo", suffix="omni_ai", ext="md")


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(_WINDOW_PNG, id="window-png"),
        pytest.param(_FULL_PNG, id="fullscreen-png"),
        pytest.param(_IMG_OMNI, id="img-omni-md"),
        pytest.param(_IMG_OMNI_AI, id="img-omni-ai-md"),
        pytest.param(
            _WINDOW_OMNI,
            id="window-omni-md",
                    ),
        pytest.param(
            _WINDOW_OMNI_AI,
            id="window-omni-ai-md",
                    ),
    ],
)
def test_is_artifact_accepts_every_artifact_name_product(name: str) -> None:
    # oracle: derived —— artifact_name 的产出即「阶段 1 可删工件」的定义（DEC-017）
    assert is_artifact(Path(name)) is True


@pytest.mark.parametrize("name", ["ops.md", "session.json"])
def test_is_artifact_rejects_log_and_manifest(name: str) -> None:
    # oracle: specified —— DEC-017 / DEC-006：ops.md 保留到阶段 2；session.json 不是工件
    assert is_artifact(Path(name)) is False
