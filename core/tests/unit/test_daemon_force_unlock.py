"""强夺写锁永远留痕（Q-023）。

oracle: specified —— 契约 §1.4 写着「`unlock --force` 强夺写锁（需显式调用，记入操作日志）」，
而线协议里 `lock.forceUnlock` 的参数**只有 `{reason}`**。所以「记入操作日志」这条承诺
必须在「没有会话身份」时也成立 —— 否则按契约调用时它 100% 落空。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _daemon_shell import daemon_log_text, shell
from cu.errors import CUError, ErrorCode

OTHER_SESSION = "s-20260913-000000-hold"


def test_force_unlock_without_session_identity_still_leaves_a_trace(tmp_path: Path) -> None:
    """按契约调用（参数只有 reason）时**必然没有** session_id —— 必须落进 daemon 日志。

    实测过 §5.2b：那次强夺的 reason 在 sessions/ 与 logs/ 下一个文件里都搜不到。
    """
    daemon = shell(tmp_path)

    result = daemon._lock_force_unlock({"reason": "验收-无会话身份的一次强夺"})

    assert result == {"released": False, "previous_holder": None}
    text = daemon_log_text(daemon)
    assert "强夺写锁" in text
    assert "验收-无会话身份的一次强夺" in text


def test_force_unlock_with_session_identity_also_writes_ops_md(tmp_path: Path) -> None:
    """有会话身份时**额外**写那条会话的 `ops.md` —— 两处都有，不是二选一。"""
    daemon = shell(tmp_path)
    session = daemon.sessions.begin()

    daemon._lock_force_unlock({"reason": "验收-带会话身份", "session_id": session.session_id})

    ops = (session.directory / "ops.md").read_text(encoding="utf-8")
    assert "unlock --force" in ops
    assert "验收-带会话身份" in ops
    assert "验收-带会话身份" in daemon_log_text(daemon)


def test_force_unlock_reports_the_previous_holder(tmp_path: Path) -> None:
    """返回值仍如实报告被夺的持有者 —— 原有行为不许退化。"""
    daemon = shell(tmp_path)
    daemon.lock.acquire(OTHER_SESSION, 4242, 0.0)

    result = daemon._lock_force_unlock({"reason": "夺掉它"})

    assert result == {"released": True, "previous_holder": OTHER_SESSION}


def test_force_unlock_survives_an_unknown_session(tmp_path: Path) -> None:
    """会话身份是错的（不存在 / 已结束）不能让「强夺」本身报失败 —— 锁已经放掉了。"""
    daemon = shell(tmp_path)
    daemon.lock.acquire(OTHER_SESSION, 4242, 0.0)

    result = daemon._lock_force_unlock({"reason": "带着一个不存在的会话", "session_id": "s-nope"})

    assert result["released"] is True
    text = daemon_log_text(daemon)
    assert "写会话 ops.md 失败" in text
    assert "带着一个不存在的会话" in text, "兜底那条日志必须仍然带着原因"


def test_force_unlock_still_requires_a_reason(tmp_path: Path) -> None:
    """没有 reason 仍然拒绝 —— `reason` 是这条日志内容的唯一来源。"""
    daemon = shell(tmp_path)

    with pytest.raises(CUError) as info:
        daemon._lock_force_unlock({"reason": ""})

    assert info.value.code is ErrorCode.INVALID_PARAMS
    assert not daemon.log.path.exists(), "被拒绝的调用不该留下半条日志"
