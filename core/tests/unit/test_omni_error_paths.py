"""守卫：omni 的「环境不可用」失败路径必须抛 CUError，hint 走静态文案（不是构造参数）。

oracle: specified —— `errors.py` 的模块文档与 `HINTS`：**每个错误码必须有一条静态 hint**，
而 `CUError` 的构造参数只有 `code` / `message` / `detail` —— `hint` 是 `HINTS[code]` 派生的
只读属性。`api-contract.md` §4 的错误码表里 `omni_not_installed` 的处置列是
「运行 `computer-use setup omni`」。

`test_errors.py` 守的是「错误码 ↔ hint 表」那一层；本文件守的是**调用点**那一层，
两者不能合并：把 `hint=` 当构造参数传的旧写法，代码里看得见 `hint` 这个词、静态检查
也过得去，但 `CUError` 在**构造**的那一刻就 `TypeError`。于是这条契约在真实失败路径上
100% 落空，而那条路径正是 AI 唯一会走到它的时刻（环境没装好、用户第一次 `parse`）。

不联网、不装 omni、不拉权重：两条路径都由「环境不可用」的替身造出来。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cu.desktop import omni, omni_setup
from cu.errors import HINTS, CUError, ErrorCode

#: 契约给 `omni_not_installed` 的处置动作（api-contract.md §4 的错误码表）。
CONTRACT_REMEDY = "computer-use setup omni"


def _assert_omni_not_installed_with_a_static_hint(exc: CUError) -> None:
    assert exc.code is ErrorCode.OMNI_NOT_INSTALLED
    # oracle: derived —— hint 是 HINTS[code] 的派生属性，不是构造参数
    assert exc.hint == HINTS[ErrorCode.OMNI_NOT_INSTALLED]
    # oracle: specified —— 静态 hint 必须点明契约给的处置动作
    assert CONTRACT_REMEDY in exc.hint
    # oracle: derived —— 人读渲染必须带着这条 hint（AI 走 CLI 时看到的就是它）
    assert exc.hint in exc.render()


def _point_omni_at_a_missing_environment(monkeypatch: pytest.MonkeyPatch,
                                         tmp_path: Path) -> None:
    """把 omni 环境指到不存在的路径上 —— 真实的 `available()` 会如实报「不就绪」。

    刻意不替换 `available` 本身：走真实探针，才是「环境不可用」这条判定的实测。
    """
    missing = tmp_path / "no-omni-environment-here"
    monkeypatch.setenv(omni.ENV_OMNI_HOME, str(missing))
    monkeypatch.setenv("COMPUTER_USE_OMNI_PYTHON", str(missing / "python.exe"))


def test_call_parse_without_an_omni_environment_raises_with_a_static_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`parse` 在环境不可用时的用户可见失败：CUError + omni_not_installed + 有 hint。"""
    _point_omni_at_a_missing_environment(monkeypatch, tmp_path)

    with pytest.raises(CUError) as info:
        omni.call_parse(image_path=str(tmp_path / "shot.png"), out_dir=tmp_path,
                        file_name="shot", ai=False)

    _assert_omni_not_installed_with_a_static_hint(info.value)


def test_the_unavailable_path_fails_before_any_worker_is_launched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """未就绪必须**在拉起 worker 之前**失败 —— 这条路径不该付几十秒冷启动的代价。

    oracle: implicit —— `available()` 的 docstring：只做廉价的存在性检查、不拉子进程；
    所以「环境不可用」的结论由它给出，而不是等子进程起不来才知道。
    """
    _point_omni_at_a_missing_environment(monkeypatch, tmp_path)
    launched: list[Any] = []

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        launched.append((args, kwargs))
        raise AssertionError("环境不可用这条路径不该拉起任何子进程")

    # `run` 与 `Popen` 都要盯：parse 这条路现在走**常驻** worker（`Popen`），
    # 只挡住 `run` 的话这条守卫就变成了空转 —— 它会通过，却什么都没守住。
    monkeypatch.setattr(omni.subproc, "run", _forbidden)
    monkeypatch.setattr(omni.subproc, "Popen", _forbidden)

    with pytest.raises(CUError) as info:
        omni.call_parse(image_path=str(tmp_path / "shot.png"), out_dir=tmp_path,
                        file_name="shot", ai=False)

    assert launched == []
    _assert_omni_not_installed_with_a_static_hint(info.value)


def test_uv_missing_from_path_raises_with_a_static_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`setup omni` 在 PATH 里找不到 `uv` 时的失败：同样必须是 CUError + 静态 hint。"""
    monkeypatch.setattr(omni_setup.shutil, "which", lambda _name: None)

    with pytest.raises(CUError) as info:
        omni_setup._uv()

    _assert_omni_not_installed_with_a_static_hint(info.value)
