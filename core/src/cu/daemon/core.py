"""daemon —— 系统的唯一状态持有者（DEC-032）。

会话、写锁、配置、存储配额，以及**全部**桌面操作都在这里。Node 侧只是极薄启动器，
不存在运行时协议。

结构约束（架构 §1.5，实现时不可违反）：

1. 必须在任何坐标读取之前声明 DPI 感知 —— 放在 `Daemon.__init__` 的第一句。
2. 写锁、覆盖层、输入封锁三者同属 daemon，「释放锁」与「解除封锁」是一次本地操作。
3. 崩溃即安全：daemon 进程死亡 ⟹ 其安装的钩子被 OS 摘除 + 覆盖层窗口销毁
   ⟹ 用户输入立即恢复。**不得**用任何使钩子脱离 daemon 生命周期的做法破坏它。
"""

from __future__ import annotations

import ctypes
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import PROTOCOL_VERSION, __version__
from ..config import Config, apply_set
from ..desktop import build_desktop
from ..desktop.base import Desktop, InputResult, WindowIdentity
from ..desktop.controller import WriteSequenceController
from ..errors import CUError, ErrorCode
from ..ids import format_hwnd, now_iso, parse_hwnd
from ..ipc import PIPE_NAME, DaemonLock, IpcServer, read_lock_pid
from ..manifest import ScreenshotRecord, StructuredRecord
from .log import DaemonLog
from .ops import OpsEntry
from .sessions import Sessions
from .writelock import WriteLock

#: 目标框的半径（像素）。写操作的落点用一个 24x24 的小方框标出来 ——
#: 我们只知道 AI 给的屏幕坐标，不知道那上面是什么元素，所以不硬凑成大框。
_TARGET_MARK_HALF = 12

#: 一次写命令在 ops.md 里的命令名，以及它对应的桌面层方法。
_WRITE_METHODS = frozenset({
    "input.click", "input.move", "input.drag", "input.scroll", "input.type", "input.key",
})


@dataclass
class DaemonStatus:
    pid: int
    started_at: str
    pipe_name: str
    active_sessions: int
    omni_refcount: int
    idle_for_s: float
    resident_bytes: int | None
    protocol_version: int
    version: str
    overlay_state: str
    input_blocked: bool
    omni_ready: bool = False
    omni_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid, "started_at": self.started_at, "pipe_name": self.pipe_name,
            "active_sessions": self.active_sessions, "omni_refcount": self.omni_refcount,
            "idle_for_s": round(self.idle_for_s, 1), "resident_bytes": self.resident_bytes,
            "protocol_version": self.protocol_version, "version": self.version,
            "overlay_state": self.overlay_state, "input_blocked": self.input_blocked,
            "omni_ready": self.omni_ready, "omni_reason": self.omni_reason,
        }


class Daemon:
    """常驻进程。构造即完成全部单实例准备，`serve_forever()` 阻塞到该退出为止。"""

    def __init__(self, config: Config, desktop: Desktop | None = None,
                 pipe_name: str = PIPE_NAME) -> None:
        # 第 1 句就必须是 DPI 声明（架构 §1.5 第 6 条 / CONSTRAINT-002）。
        # 实测：不声明时 SM_CXSCREEN 返回 2752，声明后 3440 —— 差 25%，
        # 代价是每一次点击都偏 1/4 屏。这条没有折中余地。
        from ..desktop.dpi import ensure_dpi_awareness

        self.dpi_result = ensure_dpi_awareness()

        self.config = config
        self.pipe_name = pipe_name
        # 诊断日志要在控制器之前建好：覆盖层的渲染线程要用它兜住「一直渲染失败」
        # 这个现场 —— detached 进程的 stderr 是 DEVNULL，打到那里等于没打（Q-022）。
        # `secrets` 登记已知密钥：整行落盘之前会被抹掉（硬约束：绝不写 api_key）。
        self.log = DaemonLog(config.daemon_log, config.daemon_log_limit_bytes,
                             level=config.daemon_log_level,
                             secrets=[config.vlm.api_key])
        self.controller = WriteSequenceController(on_error=self._log_component_error)
        self.desktop: Desktop = desktop or build_desktop(config, self.controller)
        self.sessions = Sessions(config.sessions_dir, storage_limit_bytes=config.storage_limit_bytes)
        self.lock = WriteLock()
        self.started_at = now_iso()
        self._booted_at = time.monotonic()
        self.omni_refcount = 0
        #: 写序列的锁：写路径与空闲清理都会碰覆盖层，必须串行。
        self._write_lock = threading.Lock()

        self._stop = threading.Event()
        self._stop_event = self._stop
        self._last_activity = time.time()
        self._daemon_lock: DaemonLock | None = None
        self._server: IpcServer | None = None
        self._idle_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def acquire_singleton(self) -> None:
        """单实例：拿不到锁说明已有 daemon 在跑。

        `daemon.lock` 用的是文件锁而非「写个 PID 进去看看进程还在不在」——
        文件锁在进程死亡时由内核释放，因此不存在 PID 复用导致的误判。
        """
        self.config.root.mkdir(parents=True, exist_ok=True)
        lock = DaemonLock(self.config.daemon_lock_path)
        if not lock.acquire():
            holder = read_lock_pid(self.config.daemon_lock_path)
            raise CUError(ErrorCode.INTERNAL_ERROR,
                          "已有 daemon 在运行" + (f"（pid={holder}）" if holder else ""),
                          {"pid": holder})
        self._daemon_lock = lock

    def prepare(self) -> dict[str, Any]:
        """启动前的准备工作，返回一份摘要供启动器使用。"""
        try:
            self.acquire_singleton()
        except CUError as exc:
            # 「第二个 daemon 起不来」不是错误（客户端会去连第一个），但它是
            # 「daemon 明明在跑、命令却连不上」这类排查的开场白，值得留痕。
            self.log.warning("daemon 未启动：单实例锁已被占用", detail=exc.message)
            raise
        self.log.info("daemon 启动", pid=os_getpid(), pipe=self.pipe_name,
                      dpi=str(self.dpi_result))
        orphaned = self.sessions.mark_orphans()
        # 钩子与覆盖层随 daemon 一起起来 —— 它们**必须**同生命周期
        # （架构 §1.5 第 5 条：进程死亡 ⟹ OS 摘除钩子 ⟹ 用户输入立即恢复）。
        try:
            self.controller.start()
        except CUError as exc:
            # 钩子装不上 / 覆盖层起不来：daemon 最该留下现场的一类失败 ——
            # 它起来之后不会再报任何东西，而症状只是「输入没被封锁」，看不出原因。
            self.log.error("控制器启动失败（输入钩子或覆盖层）",
                           code=exc.code.value, detail=exc.message)
            raise
        self.log.info("控制器就绪", orphaned=len(orphaned), overlay=self.controller.state)
        return {"orphaned": orphaned, "overlay": self.controller.state}

    def serve_forever(self) -> None:
        self._server = IpcServer(self.handle_request, pipe_name=self.pipe_name)
        self._server.on_activity = self.touch
        self._server.start()
        self._idle_thread = threading.Thread(target=self._idle_watch, name="cu-idle", daemon=True)
        self._idle_thread.start()
        try:
            while not self._stop.wait(0.2):
                # 写序列的保持窗口在这里推进：写操作之间隔一次 LLM 思考时，
                # 覆盖层与输入封锁由这个 tick 决定何时退场。
                was_visible = self.controller.overlay.visible
                self.controller.tick(self.config.overlay_exit_hold_ms)
                if was_visible and not self.controller.overlay.visible:
                    # 退场时清掉上一轮的标记，否则下次武装会先闪一下旧位置。
                    self.controller.overlay.set_target(None)
                    self.controller.overlay.set_cursor(None)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """停止服务并释放一切：钩子/覆盖层随进程消失，锁与管道在这里显式收尾。

        `controller.shutdown()` 的**第一步就是解除输入封锁** —— 即便后续清理出错，
        用户的键鼠也已经拿回来了。
        """
        self._stop.set()
        try:
            self.controller.shutdown()
        except Exception as exc:  # noqa: BLE001 —— 清理路径不该因覆盖层故障而中断
            self.log.error("控制器关闭异常", detail=f"{type(exc).__name__}: {exc}")
        if self._server is not None:
            self._server.stop()
            self._server = None
        if self._daemon_lock is not None:
            self._daemon_lock.release()
            self._daemon_lock = None
        self.log.info("daemon 退出", uptime_s=round(time.monotonic() - self._booted_at, 1),
                      idle_for_s=round(self.idle_for(), 1))

    def request_stop(self) -> None:
        self._stop.set()

    def touch(self) -> None:
        self._last_activity = time.time()

    def _idle_watch(self) -> None:
        """空闲退出（DEC-035）。

        **硬不变式**：有活跃会话、或写锁被持有、或覆盖层在场时**绝不退出**。
        检查放在这里而不是只在收到命令时，是因为「AI 崩了没调 session end」
        也必须被这个不变式挡住。
        """
        while not self._stop.wait(2.0):
            if self.sessions.active_ids() or self.lock.held or self._overlay_present():
                self.touch()
                continue
            if self.idle_for() >= self.config.daemon_idle_exit_seconds:
                self.request_stop()
                return

    def idle_for(self) -> float:
        return max(0.0, time.time() - self._last_activity)

    def _overlay_present(self) -> bool:
        """覆盖层是否在场，或输入是否仍被封锁。

        **这一条是空闲退出的安全闸门**（DEC-035 的硬不变式）：覆盖层在场
        ⟹ 输入可能正被封锁 ⟹ 此刻退出就等于把用户锁在电脑外。
        判断放在这里而不是挂在桌面层，「输入封锁」这个状态属于 daemon 自身。
        """
        return self.controller.overlay.visible or self.controller.blocker.blocking

    def _log_component_error(self, message: str) -> None:
        """桌面层组件（覆盖层的渲染线程）报上来的失败。

        覆盖层原来只把渲染失败打一行到 stderr —— 而 detached 的 daemon 上 stderr 是
        DEVNULL，于是症状只剩「屏幕上看不见」，代码上看不出任何问题。
        这是接入点里唯一一个**不在本类主动调用链上**的：它从渲染线程回调进来，
        所以走 `on_error` 注入，而不是让桌面层反向依赖 daemon。
        """
        self.log.error(message)

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    def handle_request(self, request: dict) -> Any:
        method = request["method"]
        params = request["params"]
        handler = self._routes().get(method)
        if handler is None:
            raise CUError(ErrorCode.INVALID_PARAMS, f"未知方法：{method}", {"method": method})
        self.touch()
        try:
            return handler(params)
        except CUError:
            # 预期内的错误：调用方拿到的是错误码与建议动作，它不是「现场」——
            # 记进去只会把真出事的那些行冲掉。
            raise
        except Exception as exc:  # noqa: BLE001 —— 边界：这里就是「未捕获」的落点
            # daemon 是 detached 进程，这个异常若不留痕，出过什么事就永远查不到。
            self.log.error("未捕获异常", method=method,
                           detail=f"{type(exc).__name__}: {exc}",
                           traceback=traceback.format_exc())
            raise CUError(
                ErrorCode.INTERNAL_ERROR,
                f"{type(exc).__name__}: {exc}",
                # hint 承诺「路径见错误详情」，这里把那个承诺兑现。
                {"method": method, "log": str(self.log.path)},
            ) from exc

    def _routes(self) -> dict[str, Any]:
        return {
            "system.handshake": self._handshake,
            "session.begin": self._session_begin,
            "session.end": self._session_end,
            "session.list": self._session_list,
            "desktop.windows": self._desktop_windows,
            "desktop.capture": self._desktop_capture,
            "input.click": self._input_click,
            "input.move": self._input_move,
            "input.drag": self._input_drag,
            "input.scroll": self._input_scroll,
            "input.type": self._input_type,
            "input.key": self._input_key,
            "omni.parse": self._omni_parse,
            "lock.status": self._lock_status,
            "lock.forceUnlock": self._lock_force_unlock,
            "config.get": self._config_get,
            "config.set": self._config_set,
            "daemon.setup_omni": self._setup_omni,
            "daemon.status": self._daemon_status,
            "daemon.stop": self._daemon_stop,
        }

    # ---- system ----

    def _handshake(self, params: dict) -> dict:
        client_protocol = params.get("protocol_version")
        if client_protocol != PROTOCOL_VERSION:
            raise CUError(
                ErrorCode.PROTOCOL_VERSION_MISMATCH,
                f"协议版本不一致：客户端 {client_protocol}，daemon {PROTOCOL_VERSION}",
                {"client": client_protocol, "daemon": PROTOCOL_VERSION},
            )
        return {"daemon_version": __version__, "protocol_version": PROTOCOL_VERSION,
                "started_at": self.started_at}

    # ---- session ----

    def _session_begin(self, params: dict) -> dict:
        # DEC-004：陈旧锁在**下一次 begin** 时回收，不靠后台定时器 ——
        # 没有新会话要进来时，一个陈旧锁并不妨碍任何人。
        reclaimed = self.lock.reclaim_if_stale()
        session = self.sessions.begin(
            display=self.desktop.display_context(),
            agent_hint=params.get("agent_hint"),
        )
        self.touch()
        result = {"session_id": session.session_id, "dir": str(session.directory)}
        if reclaimed is not None:
            result["reclaimed_stale_lock"] = {
                "session_id": reclaimed.session_id,
                "idle_for_s": round(reclaimed.idle_for(), 1),
            }
        return result

    def _session_end(self, params: dict) -> dict:
        session_id = self._require_session_id(params)
        # 释放锁与结束会话是一次本地操作：锁在本进程，不存在「锁已释放但会话还在」的窗口
        # （架构 §1.5 第 2 条）。
        self.lock.release(session_id)
        result = self.sessions.end(session_id)
        self.touch()
        return result

    def _session_list(self, _params: dict) -> dict:
        return {"sessions": self.sessions.list()}

    # ---- desktop ----

    def _desktop_windows(self, params: dict) -> dict:
        verbose = bool(params.get("verbose"))
        windows = self.desktop.list_windows(all_windows=bool(params.get("all")))
        return {"windows": [w.to_dict(verbose=verbose) for w in windows]}

    def _desktop_capture(self, params: dict) -> dict:
        session_id = self._require_session_id(params)
        hwnd = parse_hwnd(params["hwnd"]) if params.get("hwnd") else None
        monitor = params.get("monitor")
        session = self.sessions.get(session_id)
        seq = self.sessions.next_seq(session_id)

        result = self.desktop.capture(
            hwnd=hwnd, monitor=monitor,
            image_format=params.get("format") or self.config.image_format,
            out_dir=session.directory, seq=seq,
            draw_cursor=bool(params.get("cursor")),
        )
        self.sessions.add_screenshot(session_id, ScreenshotRecord(
            seq=seq,
            kind=result.kind,
            file=Path(result.path).name,
            format=(params.get("format") or self.config.image_format),
            width=result.width,
            height=result.height,
            origin=list(result.origin),
            layer=_layer_index(result.layer),
            window=_window_ref(result.window),
        ))
        payload = result.to_dict()
        payload["seq"] = seq
        self.sessions.record_op(session_id, OpsEntry(
            entry_no=0,
            at=datetime.now().strftime("%H:%M:%S"),
            command=self._command_label("screenshot", params),
            describe=params.get("describe", ""),
            result="ok",
            artifact=(f"{Path(result.path).name} · origin={result.origin[0]},{result.origin[1]}"
                      f" · {result.width}x{result.height} · layer={result.layer}"),
            target=_target_summary(result.window),
        ))
        return payload

    # ---- input（写命令：取锁 + describe 必填）----
    #
    # 每个 action 都收一个 `expect`：这次写操作要比对的「期望身份」。它由 `_write`
    # 统一解析 —— 只有 daemon 持有 `Sessions`，那张 pid/class 的底稿在会话记录里。

    def _input_click(self, params: dict) -> dict:
        return self._write(params, "click", lambda expect: self.desktop.click(
            int(params["x"]), int(params["y"]),
            button=params.get("button", "left"), count=int(params.get("count", 1)),
            hwnd=self._optional_hwnd(params), expect=expect,
        ))

    def _input_move(self, params: dict) -> dict:
        return self._write(params, "move", lambda expect: self.desktop.move(
            int(params["x"]), int(params["y"]), hwnd=self._optional_hwnd(params),
            expect=expect,
        ))

    def _input_drag(self, params: dict) -> dict:
        return self._write(params, "drag", lambda expect: self.desktop.drag(
            int(params["x1"]), int(params["y1"]), int(params["x2"]), int(params["y2"]),
            button=params.get("button", "left"), hwnd=self._optional_hwnd(params),
            expect=expect,
        ))

    def _input_scroll(self, params: dict) -> dict:
        at = params.get("at")
        point = (int(at[0]), int(at[1])) if isinstance(at, (list, tuple)) and len(at) == 2 else None
        # 滚动没有 hwnd（契约只有 `--at`），所以没有可比对的身份：收下 expect，
        # 但不用它 —— `_write` 对每条写命令都是同一条通路，逐条分叉更糟。
        return self._write(params, "scroll", lambda _expect: self.desktop.scroll(
            int(params["dx"]), int(params["dy"]), at=point,
        ))

    def _input_type(self, params: dict) -> dict:
        return self._write(params, "type", lambda expect: self.desktop.type_text(
            str(params["text"]), hwnd=self._optional_hwnd(params), expect=expect,
        ))

    def _input_key(self, params: dict) -> dict:
        return self._write(params, "key", lambda expect: self.desktop.key(
            str(params["combo"]), hwnd=self._optional_hwnd(params),
            force=bool(params.get("force")), expect=expect,
        ))

    # ---- omni ----

    def _omni_parse(self, params: dict) -> dict:
        session_id = self._require_session_id(params)
        hwnd = parse_hwnd(params["hwnd"]) if params.get("hwnd") else None
        image_path = params.get("image")
        if hwnd is None and not image_path:
            raise CUError(ErrorCode.INVALID_PARAMS, "必须提供 hwnd 或 image 之一")
        session = self.sessions.get(session_id)
        seq = self.sessions.next_seq(session_id)
        result = self.desktop.parse(
            hwnd=hwnd, image_path=image_path, ai=bool(params.get("ai")),
            out_dir=session.directory, seq=seq,
        )
        self.sessions.add_structured(session_id, StructuredRecord(
            seq=seq,
            kind="ai" if params.get("ai") else "base",
            file=Path(result.path).name,
            source_seq=self._source_seq_for(session_id, hwnd),
            source_image_path=image_path,
            element_count=result.element_count,
            model_name=result.model_name,
        ))
        payload = result.to_dict()
        payload["seq"] = seq
        return payload

    # ---- lock ----

    def _lock_status(self, _params: dict) -> dict:
        return self.lock.status().to_dict()

    def _lock_force_unlock(self, params: dict) -> dict:
        reason = params.get("reason") or ""
        if not reason:
            raise CUError(ErrorCode.INVALID_PARAMS,
                          "强行解锁必须给出 reason —— 它会记入操作日志")
        session_id = params.get("session_id")
        previous = self.lock.force_unlock(reason)
        # **永远**写 daemon 日志。契约里 `lock.forceUnlock` 的参数只有 `{reason}`，
        # 所以按契约调用时**必然没有** session_id；而「只在有会话身份时才记」的写法
        # 让这次强夺在 sessions/ 与 logs/ 下**一个文件里都搜不到**（实测 §5.2b），
        # 而契约写着「记入操作日志」—— 主路径上那条承诺 100% 不成立。
        self.log.warning("强夺写锁", reason=reason, released=previous is not None,
                         previous_holder=(previous.session_id if previous else ""),
                         session=session_id or "")
        if session_id:
            self._record_force_unlock(session_id, reason, previous)
        return {"released": previous is not None,
                "previous_holder": previous.session_id if previous else None}

    def _record_force_unlock(self, session_id: str, reason: str, previous) -> None:
        """有会话身份时**额外**把这次强夺写进那条会话的 `ops.md`。

        写不进去不让命令失败：锁已经放掉了，不能因为日志写不进就报「强夺失败」——
        而且上面那行 daemon 日志已经兜住了这次强夺的记录。
        """
        try:
            self.sessions.record_op(session_id, OpsEntry(
                entry_no=0, at=datetime.now().strftime("%H:%M:%S"),
                command="unlock --force", describe=reason, result="ok",
                detail=(f"被强夺的持有者：{previous.session_id}" if previous else "锁当时无人持有"),
            ))
        except CUError as exc:
            self.log.warning("强夺写锁：写会话 ops.md 失败", session=session_id,
                             code=exc.code.value, detail=exc.message)

    # ---- config ----

    def _config_get(self, _params: dict) -> dict:
        return self.config.to_dict()

    def _config_set(self, params: dict) -> dict:
        key = params.get("key")
        if not isinstance(key, str) or not key:
            raise CUError(ErrorCode.INVALID_PARAMS, "config set 需要 key")
        updated = apply_set(self.config, key, str(params.get("value", "")))
        updated.save()
        self.config = updated
        self.sessions.storage_limit_bytes = updated.storage_limit_bytes
        # 日志的上限与「要抹掉的密钥」也要跟着换 —— 否则 `config set
        # daemon_log_limit_bytes` / `vlm.api_key` 就成了定义了却不生效的项，
        # 那正是 Q-022 的成因（配置在，没有生效的写入者）。
        self.log.limit_bytes = updated.daemon_log_limit_bytes
        self.log.level = updated.daemon_log_level
        self.log.register_secrets([updated.vlm.api_key])
        # **桌面层也要换**：它持有的是构造时那一份 Config，只换 daemon 自己那份的话，
        # 桌面层读到的仍是旧值。实测踩到过：`config set vlm.base_url` 指向一个死端点后，
        # `parse --ai` 照样打到了原来的模型（跑了 101 秒并成功返回），
        # 于是「改配置」这个动作在 vlm.* 上等于没发生。
        if hasattr(self.desktop, "config"):
            self.desktop.config = updated
        return updated.to_dict()

    # ---- daemon ----

    def _daemon_status(self, _params: dict) -> dict:
        # omni 就绪状态只做**廉价的文件检查**，不拉起子进程 —— `status` 是诊断命令，
        # 不该为了报一个布尔值付几十秒的模型加载代价。真要验能不能跑，用 `parse`。
        from ..desktop import omni as omni_bridge

        omni_ready, omni_reason = omni_bridge.available()
        return DaemonStatus(
            pid=os_getpid(),
            started_at=self.started_at,
            pipe_name=self.pipe_name,
            active_sessions=len(self.sessions.active_ids()),
            omni_refcount=self.omni_refcount,
            idle_for_s=self.idle_for(),
            resident_bytes=_resident_bytes(),
            protocol_version=PROTOCOL_VERSION,
            version=__version__,
            overlay_state=self.controller.state,
            input_blocked=self.controller.blocker.blocking,
            omni_ready=omni_ready,
            omni_reason=omni_reason,
        ).to_dict()

    def _setup_omni(self, params: dict) -> dict:
        """装配 omni 环境。**耗时很长**（下 1.4GB 权重），因此 CLI 侧超时要放宽。

        跑在 daemon 里而不是客户端里：环境与 daemon 同属一个部署单元，
        让客户端进程去装会让「谁负责这个环境」变得含糊。
        """
        from ..desktop import omni_setup

        return omni_setup.run_setup(
            force=bool(params.get("force")),
            skip_weights=bool(params.get("skip_weights")),
        )

    def _daemon_stop(self, _params: dict) -> dict:
        self.request_stop()
        return {"stopping": True, "pid": os_getpid()}

    # ------------------------------------------------------------------
    # 共用路径
    # ------------------------------------------------------------------

    def _write(self, params: dict, label: str,
               action: Callable[[WindowIdentity | None], InputResult]) -> dict:
        """所有写命令的唯一通路：describe 校验 → 取锁 → 解析期望身份 → 执行 → 记日志。

        写命令**不做幂等、不自动重试**（DEC-041）：一次超时的 click 可能已经点下去了，
        重试就是点两下。日志 `ops.md` 记录的是「实际执行了什么」，那才是判断依据。

        无论成功失败都必须记一条日志 —— 失败的那次尤其重要（DEC-006）。

        `expect` 在**这里**解析而不是在桌面层：那张 pid/class 的底稿在会话记录里，
        只有 daemon 持有 `Sessions`（见 `_expected_identity`）。
        """
        describe = params.get("describe")
        if not isinstance(describe, str) or not describe.strip():
            raise CUError(ErrorCode.DESCRIBE_REQUIRED,
                          f"写操作 {label} 必须带 describe", {"command": label})

        session_id = self._require_session_id(params)
        self.sessions.get(session_id)          # 会话不存在时在这里就失败，不去动桌面
        expect = self._expected_identity(session_id, self._optional_hwnd(params))
        entry = OpsEntry(
            entry_no=0,
            at=datetime.now().strftime("%H:%M:%S"),
            command=self._command_label(label, params),
            describe=describe.strip(),
        )

        self.lock.acquire(session_id, os_getpid(), float(self.config.lock_wait_seconds),
                          stop_event=self._stop_event)
        try:
            # 覆盖层与输入端封锁：写锁、覆盖层、输入封锁三者同属 daemon，
            # 「释放锁」与「解除封锁」因此是一次本地操作（架构 §1.5 第 2 条）。
            # begin_write 只在**写序列开始时**武装；保持窗口内的后续命令直接继续，
            # 不再等前摇 —— 否则一次 20 步的任务要多等 30 秒（DEC-030）。
            # 带 `--continue` 才有保持窗口；不带 = 这条结束，序列就结束（DEC-075）。
            with self._write_lock:
                self.controller.begin_write(
                    self.config.overlay_arm_ms, self.config.overlay_continue_seconds,
                    keep_alive=bool(params.get("continue")),
                )
            try:
                self.controller.wait_for_arm(self.config.overlay_arm_ms)
                result = action(expect)
            finally:
                # 保护期从武装一直盖到这条命令**跑完**：`--end` 也在这里才生效。
                # 覆盖层与封锁在派发输入期间必须在场 —— 那是它们唯一的意义。
                self.controller.end_write(end=bool(params.get("end")))
            self.lock.heartbeat(session_id)
            entry.result = "ok" if result.ok else "error"
            entry.detail = _input_detail(result)
            if result.warning:
                entry.detail = (entry.detail + " · " if entry.detail else "") + f"warning: {result.warning}"
            if result.detail.get("target"):
                entry.target = str(result.detail["target"])
            # 写完之后把「AI 点到了哪」画给用户看（目标框 + 光标光晕，仅 Active 态）。
            self._show_write_feedback(label, params)
            return result.to_dict()
        except CUError as exc:
            entry.result = "error"
            entry.error_code = exc.code.value
            entry.detail = exc.message
            # 写操作失败**不改覆盖层状态**（DEC-080）：失败由返回值与退出码表达，
            # 不把屏幕变成一块要按 Esc 才能清掉的红。用户中止也一样 ——
            # 那条路径上 controller 自己切到 Stopping（DEC-068）。
            raise
        finally:
            # 锁**不在这里释放**：锁由会话持有，跨多条写命令保持（DEC-004）。
            # 覆盖层也不在这里退场 —— 它由保持阈值决定（controller.tick）。
            # 这里只落日志 —— 失败的那次也要留痕。
            self.sessions.record_op(session_id, entry)

    def _show_write_feedback(self, label: str, params: dict) -> None:
        """把「AI 刚才点/移到哪」画到覆盖层上（目标框 + 光标光晕）。

        没有这一步，overlay.md §2.1 状态表里 Active 态的目标框与光标两列就是空的 ——
        用户只看得到边缘光晕和胶囊，看不到 AI 具体在动哪里。

        **点状目标**（click/move/drag/scroll --at）用一个围绕落点的小方框表示。
        刻意不按「元素」画：AI 给的是屏幕坐标，我们并不知道那个坐标上是什么元素，
        硬凑一个大框反而会指错地方。小方框诚实地表达「就在这里」。

        `type` / `key` 没有落点（目标是当前焦点窗口），因此只更新光标、不画框。
        """
        overlay = self.controller.overlay
        point: tuple[int, int] | None = None
        if label in ("click", "move"):
            if params.get("x") is not None and params.get("y") is not None:
                point = (int(params["x"]), int(params["y"]))
        elif label == "drag":
            if params.get("x2") is not None and params.get("y2") is not None:
                point = (int(params["x2"]), int(params["y2"]))
        elif label == "scroll":
            at = params.get("at")
            if isinstance(at, (list, tuple)) and len(at) == 2:
                point = (int(at[0]), int(at[1]))
        elif label in ("type", "key"):
            # 没有坐标可画：目标是当前焦点窗口。保留系统光标位置，
            # 不画目标框 —— 画一个假位置比不画更糟。
            return

        if point is None:
            return
        overlay.set_cursor(point)
        half = _TARGET_MARK_HALF
        overlay.set_target((point[0] - half, point[1] - half, half * 2, half * 2))

    def _require_session_id(self, params: dict) -> str:
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise CUError(ErrorCode.INVALID_PARAMS,
                          "缺少 session_id；先执行 begin，或用 --session / COMPUTER_USE_SESSION 指定")
        return session_id

    @staticmethod
    def _optional_hwnd(params: dict) -> int | None:
        value = params.get("hwnd")
        return parse_hwnd(value) if value else None

    @staticmethod
    def _command_label(label: str, params: dict) -> str:
        """`ops.md` 锚行里的命令文本。参数按命令面写法还原，便于人读复盘。"""
        if label == "click":
            return f"click {params.get('x')} {params.get('y')}"
        if label == "move":
            return f"move {params.get('x')} {params.get('y')}"
        if label == "drag":
            return (f"drag {params.get('x1')} {params.get('y1')} "
                    f"{params.get('x2')} {params.get('y2')}")
        if label == "scroll":
            return f"scroll {params.get('dx')} {params.get('dy')}"
        if label == "type":
            return "type <文本>"
        if label == "key":
            return f"key {params.get('combo')}"
        if label == "screenshot":
            target = params.get("hwnd") or (f"monitor {params.get('monitor')}"
                                            if params.get("monitor") is not None else "full")
            return f"screenshot --hwnd {target}"
        return label

    def _source_seq_for(self, session_id: str, hwnd: int | None) -> int | None:
        if hwnd is None:
            return None
        record = self.sessions.last_screenshot_of(session_id, format_hwnd(hwnd))
        return record.seq if record is not None else None

    def _expected_identity(self, session_id: str, hwnd: int | None) -> WindowIdentity | None:
        """写操作前置要比对的「期望身份」（DEC-013 第 1 层的落地 / Q-024）。

        数据早就在了：`windows.check_hwnd()` 的 `expect_pid` / `expect_class` 全仓
        没有任何调用点传过，于是「hwnd 被系统复用给了别的进程」这条检查从未生效，
        `window_stale` 这个错误码也就一直发不出来。而该窗口**最近一次截图**已经把
        pid 与窗口类记在会话清单里了 —— 这里把它取出来当前置的比对基准。

        **没有历史记录就不校验**（返回 None）：无从比对，保守放行。宁可不拦，
        也不要因为「这个会话没截过图」拒绝一次合法点击 —— 这条挡的是
        「hwnd 复用后点到**另一个**窗口」，不是「调用方没按标准流程先截图」。
        """
        if hwnd is None:
            return None
        record = self.sessions.last_screenshot_of(session_id, format_hwnd(hwnd))
        if record is None or record.window is None:
            return None
        return WindowIdentity(pid=record.window.pid, klass=record.window.klass)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

_LAYERS = {"wgc": 1, "printwindow": 2, "dxgi": 3}


def _layer_index(layer: str) -> int:
    """截图降级层号，进清单供诊断用（DEC-007 的四层链）。"""
    return _LAYERS.get((layer or "").lower(), 0)


def _window_ref(window) -> Any:
    if window is None:
        return None
    from ..manifest import WindowRef

    ref = window
    return WindowRef.of(ref.hwnd, ref.pid, ref.process, ref.title, ref.klass, ref.rect,
                        monitor=ref.monitor)


def _target_summary(window) -> str | None:
    if window is None:
        return None
    x, y, w, h = window.rect
    return (f"{window.hwnd:08X} · {window.process} · "
            f"rect={x},{y},{x + w},{y + h}")


def _input_detail(result) -> str:
    parts = [f"moved={result.moved_ms}ms", f"total={result.total_ms}ms"]
    return " · ".join(parts)


def os_getpid() -> int:
    import os

    return os.getpid()


def _resident_bytes() -> int | None:
    """常驻内存。查不到就返回 None —— 这是诊断信息，不该让 status 命令失败。"""
    try:
        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p,
                                              ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                                              ctypes.c_ulong]
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(),
                                          ctypes.byref(counters), counters.cb):
            return None
        return int(counters.WorkingSetSize)
    except (OSError, AttributeError):
        return None
