"""全局单写锁 —— 桌面输入的独占权（DEC-004 / DEC-022 / CONSTRAINT-001）。

桌面输入必须串行：同一时刻只有一个会话能发出点击与按键，否则两个 Agent 的输入会交错。
读操作（枚举 / 截图 / 解析）**不取锁** —— 它们天然幂等，加锁只会让「只想截图」的
会话阻塞别人。

锁的持有者是**会话**而非进程：Agent 崩溃不调 `session end` 是常态，
所以必须有回收路径（心跳 + `force-unlock`），否则整个工具死锁。

三条不变式：
  - 锁只在 daemon 内 —— 「释放锁」与「解除输入封锁」因此是一次本地操作（DEC-032）。
  - 竞争是**有限等待**：最多 N 秒，超时报错并带上持有者信息，由 AI 决定怎么办。
  - 强夺必须显式调用，且记入操作日志 —— 强夺会让另一个会话的操作半途而废。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from ..errors import CUError, ErrorCode

#: 心跳超过这个时长即认为持有者已死。
#: 取 120s 是因为它必须大于「一次写操作 + AI 思考间隔」的合理上限：
#: 误判活着的会话为陈旧会强夺锁，比多等一会儿糟糕得多。
STALE_AFTER_SECONDS = 120.0


@dataclass
class LockHolder:
    session_id: str
    pid: int
    acquired_at: float
    last_heartbeat: float

    def held_for(self, now: float | None = None) -> float:
        return max(0.0, (now or time.time()) - self.acquired_at)

    def idle_for(self, now: float | None = None) -> float:
        return max(0.0, (now or time.time()) - self.last_heartbeat)


@dataclass
class LockStatus:
    holder: LockHolder | None = None
    waiting: bool = False
    waiting_sessions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out: dict = {"waiting": self.waiting, "waiting_sessions": list(self.waiting_sessions)}
        if self.holder is not None:
            out["holder"] = {
                "session_id": self.holder.session_id,
                "pid": self.holder.pid,
                "held_for_s": round(self.holder.held_for(), 1),
                "idle_for_s": round(self.holder.idle_for(), 1),
            }
        return out


class WriteLock:
    """可重入于同一会话、互斥于不同会话的写锁。"""

    def __init__(self, stale_after: float = STALE_AFTER_SECONDS) -> None:
        self._cond = threading.Condition()
        self._holder: LockHolder | None = None
        self._waiters: dict[str, float] = {}   # session_id → 开始等待的时刻
        self.stale_after = stale_after

    # ---- 取锁 ----

    def acquire(self, session_id: str, pid: int, timeout: float,
                stop_event: threading.Event | None = None) -> LockHolder:
        """取锁。同一会话重复取锁是幂等的（自己持的锁自己当然能拿）。

        超时抛 `lock_timeout`，`detail` 带持有者 session / pid / 已持有时长 ——
        这是让 AI 能做决策而不是干等的关键（DEC-022）。
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            self._waiters[session_id] = time.monotonic()
            try:
                while True:
                    if self._holder is None:
                        holder = LockHolder(session_id=session_id, pid=pid,
                                            acquired_at=time.time(), last_heartbeat=time.time())
                        self._holder = holder
                        return holder
                    if self._holder.session_id == session_id:
                        self._holder.last_heartbeat = time.time()
                        return self._holder

                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or (stop_event is not None and stop_event.is_set()):
                        raise self._timeout_error(session_id)
                    # 每 100ms 醒一次，让 stop_event 与心跳请求有机会插进来。
                    self._cond.wait(min(0.1, remaining))
            finally:
                self._waiters.pop(session_id, None)

    def _timeout_error(self, session_id: str) -> CUError:
        holder = self._holder
        detail: dict = {"waited_session": session_id}
        if holder is not None:
            detail.update({
                "holder_session": holder.session_id,
                "holder_pid": holder.pid,
                "held_for_s": round(holder.held_for(), 1),
            })
        return CUError(
            ErrorCode.LOCK_TIMEOUT,
            "等待写锁超时：桌面正被另一个会话占用",
            detail,
        )

    # ---- 释放 ----

    def release(self, session_id: str) -> bool:
        """按会话释放。非持有者调用是空操作（返回 False），不报错 ——
        会话结束时的清理路径不该因为「锁本来就不在自己手上」而失败。"""
        with self._cond:
            if self._holder is None or self._holder.session_id != session_id:
                return False
            self._holder = None
            self._cond.notify_all()
            return True

    def force_unlock(self, reason: str) -> LockHolder | None:
        """强夺。只有显式调用才会走到这里 —— 调用方负责把这次强夺记入日志。"""
        with self._cond:
            previous = self._holder
            self._holder = None
            self._cond.notify_all()
            return previous

    # ---- 心跳与陈旧回收 ----

    def heartbeat(self, session_id: str) -> None:
        with self._cond:
            if self._holder is not None and self._holder.session_id == session_id:
                self._holder.last_heartbeat = time.time()

    def reclaim_if_stale(self) -> LockHolder | None:
        """持有者心跳过期则回收，返回被回收的持有者（没有则 None）。

        DEC-004 的「下一次 begin 时回收陈旧锁」—— 触发点是会话创建，
        不是后台定时器：没有新会话要进来时，一个陈旧锁并不妨碍任何人。
        """
        with self._cond:
            holder = self._holder
            if holder is None or holder.idle_for() < self.stale_after:
                return None
            self._holder = None
            self._cond.notify_all()
            return holder

    # ---- 查询 ----

    def status(self) -> LockStatus:
        with self._cond:
            return LockStatus(
                holder=self._holder,
                waiting=bool(self._waiters),
                waiting_sessions=sorted(self._waiters),
            )

    @property
    def held(self) -> bool:
        with self._cond:
            return self._holder is not None

    def holder_session(self) -> str | None:
        with self._cond:
            return self._holder.session_id if self._holder else None
