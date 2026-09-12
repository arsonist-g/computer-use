"""错误码的封闭枚举 —— AI 的分支依据（api-contract.md §4）。

这个枚举是**契约的一部分**。AI 靠 `error_code` 决定下一步，而不是去猜散文：
「窗口不存在」与「窗口被系统复用」需要不同的处理，「提权窗口」则应当直接放弃。

因此：
  - 码名是封闭集合，新增是**加性变更**，不得改动既有码的语义；
  - 每个码必须同时有静态 `hint`（下一步动作）与退出码分类，缺一不可
    （由 tests/unit/test_errors.py 守卫）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """封闭枚举。值是字符串，直接进 JSON-RPC 的 `error.data.code`。"""

    INVALID_PARAMS = "invalid_params"
    PROTOCOL_VERSION_MISMATCH = "protocol_version_mismatch"

    SESSION_NOT_FOUND = "session_not_found"
    SESSION_ALREADY_ENDED = "session_already_ended"
    DESCRIBE_REQUIRED = "describe_required"
    LOCK_TIMEOUT = "lock_timeout"

    WINDOW_NOT_FOUND = "window_not_found"
    WINDOW_STALE = "window_stale"
    WINDOW_MINIMIZED = "window_minimized"
    ELEVATED_WINDOW = "elevated_window"
    FOREGROUND_FAILED = "foreground_failed"

    CAPTURE_FAILED = "capture_failed"
    CAPTURE_BLACK = "capture_black"

    OMNI_NOT_INSTALLED = "omni_not_installed"
    OMNI_FAILED = "omni_failed"
    VLM_FAILED = "vlm_failed"

    DANGEROUS_KEY_BLOCKED = "dangerous_key_blocked"
    ABORTED_BY_USER = "aborted_by_user"
    INTERNAL_ERROR = "internal_error"


class ExitCode(int, Enum):
    """粗分类，给 shell 与人。细分类给 AI，走 ErrorCode —— 两者并存不互相替代。"""

    OK = 0
    INTERNAL = 1
    PARAMS = 2
    SESSION = 3
    TARGET = 4
    CAPTURE = 5
    ABORTED = 6


#: 每个错误码的静态建议动作。`detail` 承载运行时数据，`hint` 只讲「下一步该干什么」。
HINTS: dict[ErrorCode, str] = {
    ErrorCode.INVALID_PARAMS: "检查参数名与取值；`computer-use --help` 列出全部可用参数。",
    ErrorCode.PROTOCOL_VERSION_MISMATCH: "客户端会自动重启 daemon 后重试一次；若仍失败，`computer-use daemon stop` 后重试。",
    ErrorCode.SESSION_NOT_FOUND: "先执行 `begin` 建会话，并把返回的 session_id 传给后续命令。",
    ErrorCode.SESSION_ALREADY_ENDED: "该会话已结束；为当前任务新建一个会话。",
    ErrorCode.DESCRIBE_REQUIRED: "写操作必须带 `--describe \"这次操作要做什么、为什么\"`（这是复盘依据）。",
    ErrorCode.LOCK_TIMEOUT: "桌面正被另一个会话占用；可稍后重试，或 `unlock --force` 强夺（会记入日志）。",
    ErrorCode.WINDOW_NOT_FOUND: "窗口已不存在；重新执行 `windows` 枚举。",
    ErrorCode.WINDOW_STALE: "hwnd 被系统复用给了别的进程；重新执行 `windows` 枚举。",
    ErrorCode.WINDOW_MINIMIZED: "目标窗口已最小化；先让用户还原它，或改用其他窗口。",
    ErrorCode.ELEVATED_WINDOW: "该窗口属提权进程，UIPI 会拦截输入，本工具无法操作；请让用户手动完成这一步。",
    ErrorCode.FOREGROUND_FAILED: "无法把目标窗口提到前台；重试一次，仍失败则该窗口当前不可激活。",
    ErrorCode.CAPTURE_FAILED: "四层截图降级全部失败；确认窗口仍存在，独占全屏请改为无边框全屏。",
    ErrorCode.CAPTURE_BLACK: "截到全黑帧；该窗口可能受保护或仍在渲染，稍后重试。",
    ErrorCode.OMNI_NOT_INSTALLED: "OmniParser 未安装；执行 `computer-use setup omni`。",
    ErrorCode.OMNI_FAILED: "解析进程异常；重试一次，持续失败请查看 daemon 日志。",
    ErrorCode.VLM_FAILED: "多模态端点调用失败；检查 base_url / api_key。原始结构化数据仍可用。",
    ErrorCode.DANGEROUS_KEY_BLOCKED: "该按键序列在黑名单中；确需执行请加 `--force`。",
    ErrorCode.ABORTED_BY_USER: "用户主动中止；不要自动重来，先与用户确认。",
    ErrorCode.INTERNAL_ERROR: "未预期错误；详见 daemon 日志（路径在 detail.log，"
                             "默认 `~/.computer-use/logs/daemon.log`）。",
}

#: 错误码 → 退出码分类。不逐码一对一，按大类划分。
EXIT_CODES: dict[ErrorCode, ExitCode] = {
    ErrorCode.INVALID_PARAMS: ExitCode.PARAMS,
    ErrorCode.PROTOCOL_VERSION_MISMATCH: ExitCode.INTERNAL,
    ErrorCode.SESSION_NOT_FOUND: ExitCode.SESSION,
    ErrorCode.SESSION_ALREADY_ENDED: ExitCode.SESSION,
    ErrorCode.DESCRIBE_REQUIRED: ExitCode.PARAMS,
    ErrorCode.LOCK_TIMEOUT: ExitCode.SESSION,
    ErrorCode.WINDOW_NOT_FOUND: ExitCode.TARGET,
    ErrorCode.WINDOW_STALE: ExitCode.TARGET,
    ErrorCode.WINDOW_MINIMIZED: ExitCode.TARGET,
    ErrorCode.ELEVATED_WINDOW: ExitCode.TARGET,
    ErrorCode.FOREGROUND_FAILED: ExitCode.TARGET,
    ErrorCode.CAPTURE_FAILED: ExitCode.CAPTURE,
    ErrorCode.CAPTURE_BLACK: ExitCode.CAPTURE,
    ErrorCode.OMNI_NOT_INSTALLED: ExitCode.CAPTURE,
    ErrorCode.OMNI_FAILED: ExitCode.CAPTURE,
    ErrorCode.VLM_FAILED: ExitCode.CAPTURE,
    ErrorCode.DANGEROUS_KEY_BLOCKED: ExitCode.PARAMS,
    ErrorCode.ABORTED_BY_USER: ExitCode.ABORTED,
    ErrorCode.INTERNAL_ERROR: ExitCode.INTERNAL,
}

#: 写操作必须携带 describe。缺了直接报错，不静默放行（DEC-019）。
WRITE_COMMANDS: frozenset[str] = frozenset(
    {"click", "move", "drag", "scroll", "type", "key"}
)


@dataclass(slots=True)
class CUError(Exception):
    """贯穿客户端与 daemon 的错误载体。

    `message` 给人读，`hint` 给下一步动作，`detail` 给运行时数据
    （例如 lock_timeout 的持有者 session/pid/时长）。
    """

    code: ErrorCode
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 显式写基类而不用零参 super()：`@dataclass(slots=True)` 会重建类对象，
        # 零参 super() 依赖的 `__class__` 单元仍指向重建前的旧类，调用即
        # `TypeError: super(type, obj): obj must be an instance or subtype of type`。
        Exception.__init__(self, self.message)

    @property
    def hint(self) -> str:
        return HINTS[self.code]

    @property
    def exit_code(self) -> ExitCode:
        return EXIT_CODES[self.code]

    def to_rpc_data(self) -> dict[str, Any]:
        """JSON-RPC `error.data` 的形状（api-contract.md §3 约定 1）。"""
        return {"code": self.code.value, "hint": self.hint, "detail": self.detail}

    def render(self) -> str:
        """CLI 的人读渲染（默认输出形态）。"""
        lines = [f"error: {self.code.value}", f"  {self.message}"]
        if self.detail:
            lines.append("  detail: " + ", ".join(f"{k}={v}" for k, v in self.detail.items()))
        lines.append(f"  hint: {self.hint}")
        return "\n".join(lines)
