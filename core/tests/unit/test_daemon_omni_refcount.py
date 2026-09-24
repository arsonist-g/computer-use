"""守卫：omni 的常驻 worker 由**会话引用计数**决定生死（DEC-014）。

常驻是为了速度：模型只加载一次，之后每次 `parse` 只付识别的代价（本机 2.5s）。
但常驻不能变成常占 —— 一个几 GB 的子进程如果一直挂在 daemon 名下，
DEC-035 的「空闲时没有理由继续占用」就成了空话。

卸载时机只有一个：**最后一个用过解析的会话结束**。这条必须由测试守，
因为「早一步卸载」与「晚一步卸载」在功能上都看不出来：前者让下一次解析慢十几秒，
后者只是白占内存。两种都不会报错。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _daemon_shell import shell
from cu.desktop import omni as omni_bridge


@pytest.fixture
def unloads(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """把「真正停掉 worker」换成一个记账点：本文件量的是**时机**，不是进程本身。"""
    calls: list[bool] = []
    monkeypatch.setattr(omni_bridge, "shutdown_worker",
                        lambda *a, **k: calls.append(True))
    return calls


def test_the_worker_stays_until_the_last_reference_goes(
    tmp_path: Path, unloads: list[bool]
) -> None:
    """两个会话都在用时，先走的那个不该把 worker 从另一个底下停掉。"""
    daemon = shell(tmp_path)
    daemon._omni_acquire("s-1")
    daemon._omni_acquire("s-2")

    assert daemon.omni_refcount == 2

    daemon._omni_release("s-1")

    assert daemon.omni_refcount == 1
    assert unloads == [], "还有会话在用，worker 不该被卸载"


def test_the_worker_is_unloaded_when_the_count_hits_zero(
    tmp_path: Path, unloads: list[bool]
) -> None:
    daemon = shell(tmp_path)
    daemon._omni_acquire("s-1")

    daemon._omni_release("s-1")

    assert daemon.omni_refcount == 0
    assert unloads == [True]

    # 再来一次（会话结束会被再调一遍）不该重复卸载。
    daemon._omni_release("s-1")
    assert unloads == [True]


def test_a_session_that_never_parsed_does_not_touch_the_worker(
    tmp_path: Path, unloads: list[bool]
) -> None:
    """没用过解析的会话结束，与 omni 无关 —— 不该顺手把别人的模型卸掉。"""
    daemon = shell(tmp_path)

    daemon._omni_release("s-never-parsed")

    assert daemon.omni_refcount == 0
    assert unloads == []
