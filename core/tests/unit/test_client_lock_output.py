"""守卫：`lock` 的人读输出必须能区分「确实夺了锁」与「锁本就空闲」。

oracle: specified —— 契约要求 `lock unlock --force` 的人读输出点明原持有者（原持有者的
session id 要出现在输出里）；锁本来就空闲时不得声称夺了锁；`lock status` 的既有三形态
（持有者行 /「锁空闲」/ 空闲但有等待者）不得改变。

为什么必须由测试守：`lock.forceUnlock` 的返回形状是 `{released, previous_holder}`
（`daemon/core.py::_lock_force_unlock`），而人读分支只认 `holder`（`client.py` 里
`command == "lock"` 那一段）—— 两条 key 不重合。真机症状：刚夺掉一个**活着**的持有者，
人读输出却印「锁空闲」，于是 AI 读到「桌面没人占用」并照常操作，而真相是它刚打断了
另一个会话 —— 这条输出把「夺锁成功」伪装成了「什么都没发生」。

断言钉的是**契约性质**（是否出现原持有者 / 两条输出是否可区分 / status 三形态是否仍可区分），
不钉措辞 —— 具体文案是实现方的自由。输入形态照抄 daemon 的真实返回
（`LockStatus.to_dict` 与 `_lock_force_unlock`）。
"""

from __future__ import annotations

from cu.client import render_text

#: 被夺掉的持有者。占位符，不是任何真实会话。
PREVIOUS_HOLDER = "s-20260101-000000-lock1"
#: 空闲但有等待者时，等待者也是 session id（status 的返回里带着它）。
WAITER = "s-20260101-000000-wait1"


def _force_unlock_released() -> dict:
    """`lock.forceUnlock` 确实夺到锁时 daemon 的返回（daemon/core.py）。"""
    return {"released": True, "previous_holder": PREVIOUS_HOLDER}


def _force_unlock_free() -> dict:
    """`lock.forceUnlock` 面对一把空闲锁时的返回 —— 没有夺到任何持有者。"""
    return {"released": False, "previous_holder": None}


def _status_held() -> dict:
    """`lock.status` 有持有者（LockStatus.to_dict）。"""
    return {"waiting": False, "waiting_sessions": [],
            "holder": {"session_id": PREVIOUS_HOLDER, "pid": 4242,
                       "held_for_s": 3.0, "idle_for_s": 1.2}}


def _status_idle() -> dict:
    """`lock.status` 空闲、无人等待。"""
    return {"waiting": False, "waiting_sessions": []}


def _status_idle_with_waiters() -> dict:
    """`lock.status` 空闲但有人在等。"""
    return {"waiting": True, "waiting_sessions": [WAITER]}


# --------------------------------------------------------------------------- #
# `lock unlock --force` 的两条结果
# --------------------------------------------------------------------------- #


def test_a_real_takeover_names_the_previous_holder() -> None:
    """released=true → 原持有者的 session id 必须出现在输出里。"""
    # oracle: specified —— 夺锁成功必须点明原持有者
    assert PREVIOUS_HOLDER in render_text("lock", _force_unlock_released())


def test_takeover_and_already_free_are_told_apart() -> None:
    """两条结果不许被印成同一句话 —— 否则 AI 分不出「夺了」与「本来就空闲」。"""
    # oracle: specified —— released=false 时不得声称夺了锁（两种结果必须可区分）
    assert render_text("lock", _force_unlock_released()) != \
        render_text("lock", _force_unlock_free())


def test_the_already_free_result_never_names_a_holder() -> None:
    """released=false 的返回里没有任何 session id，输出自然也不许凭空点名一个。"""
    # oracle: implicit —— 返回里 previous_holder 是 None；输出不得出现任何 session id
    out = render_text("lock", _force_unlock_free())
    assert PREVIOUS_HOLDER not in out
    assert WAITER not in out
    assert "none" not in out.lower(), "把 None 原样印出来，等于说「有个持有者叫 None」"


# --------------------------------------------------------------------------- #
# `lock status` 的既有三形态：不许被这次改动带偏
# --------------------------------------------------------------------------- #


def test_status_with_a_holder_still_names_it() -> None:
    """既有形态之一（持有者行）：session id 必须在。"""
    # oracle: existing —— status 的持有者行不得改变
    assert PREVIOUS_HOLDER in render_text("lock", _status_held())


def test_status_idle_still_says_nobody_holds_it() -> None:
    """既有形态之二（锁空闲）：不点名任何人，且与持有者行仍可区分。"""
    # oracle: existing —— status 的空闲形态不得改变
    idle = render_text("lock", _status_idle())
    assert PREVIOUS_HOLDER not in idle
    assert idle != render_text("lock", _status_held()), "空闲与持有必须仍可区分"


def test_status_free_with_waiters_still_differs_from_plain_free() -> None:
    """既有形态之三（空闲但有等待者）：与纯空闲仍可区分，且仍不点名持有者。"""
    # oracle: existing —— status 的「有等待者」形态不得改变
    waiting = render_text("lock", _status_idle_with_waiters())
    assert waiting != render_text("lock", _status_idle())
    assert PREVIOUS_HOLDER not in waiting, "没有持有者就不许点名持有者"
    assert WAITER not in waiting, "既有形态不点名等待者 —— 打印等待者 id 是另一处改动"
