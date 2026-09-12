"""daemon 诊断日志 —— 出错时唯一的现场（Q-022 / DEC-038）。

**它存在的理由**：daemon 是 detached 进程（`DETACHED_PROCESS | CREATE_NO_WINDOW`，
stdout/stderr 都接到 DEVNULL）。它出错时没有任何现场 —— 而 `internal_error` 的 hint
一直写着「详见 daemon 日志（路径见错误详情）」，那个文件却从未被创建过：
`config.daemon_log` / `daemon_log_limit_bytes` 定义了却没有写入者，
`~/.computer-use/logs/` 目录根本不存在。

**滚动方向与工件清理刻意相反**：`storage.cleanup` 删**最旧**的工件（最旧优先），
这里**截掉文件头部、保留尾部** —— 日志的价值全在最新那几行。出问题时要看的是
「刚刚发生了什么」，不是三小时前的第一行。别照抄 `storage.cleanup`。

**绝不写凭据**：`vlm.api_key` 进日志一次就等于把密钥落盘。构造时把已知的密钥值登记进
`secrets`，写入前逐条抹成 `***` —— 靠机制，而不是靠「记得别写」。
`config set vlm.api_key` 之后必须调 `register_secrets`，否则旧值被抹、新值不抹。

**这不是会话日志**。`ops.md` 记的是「AI 做了什么」（DEC-006 的复盘证据链，
一个会话一个文件）；这里是 daemon 自身的现场，全局一个文件，与会话无关。
"""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

#: 敏感值被抹掉后的占位符。
_REDACTED = "***"
#: 级别名。刻意只有三个 —— 这是给人看的现场，不是一套分级框架。
LEVELS = ("info", "warning", "error")


class DaemonLog:
    """追加写 + 按字节上限「截头保尾」。

    线程安全：daemon 的 IPC 是一连接一线程，多个 handler 可能同时出错，
    追加与滚动必须在同一把锁里完成 —— 否则滚动会把另一条线程刚写的行切掉。
    """

    def __init__(self, path: Path | str, limit_bytes: int, *,
                 secrets: Iterable[str] = ()) -> None:
        self.path = Path(path)
        self.limit_bytes = max(0, int(limit_bytes))
        # 空串必须剔除：`str.replace("")` 会在每个字符间插占位符，把整行毁掉。
        self._secrets: list[str] = [s for s in secrets if s]
        self._lock = threading.Lock()

    # ---- 写 ----

    def info(self, message: str, **fields: object) -> None:
        self.write("info", message, **fields)

    def warning(self, message: str, **fields: object) -> None:
        self.write("warning", message, **fields)

    def error(self, message: str, **fields: object) -> None:
        self.write("error", message, **fields)

    def write(self, level: str, message: str, **fields: object) -> None:
        """写一行。**任何失败都不抛** —— 日志写不进去不该让正在处理的那条命令失败。

        字段以 `key=value` 追加在消息之后；`fields` 为空则只写消息。
        """
        line = self._format(level, message, fields)
        try:
            with self._lock:
                self._append(line)
                self._enforce_limit()
        except OSError:
            return

    def register_secrets(self, secrets: Iterable[str]) -> None:
        """登记要抹掉的值。可重复调用（幂等）。
        """
        with self._lock:
            for value in secrets:
                if value and value not in self._secrets:
                    self._secrets.append(value)

    # ---- 内部 ----

    def _format(self, level: str, message: str, fields: dict[str, object]) -> str:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        parts = [f"{stamp} {level:<7} {_one_line(message)}"]
        for key, value in fields.items():
            text = _one_line(str(value))
            if text:
                parts.append(f"{key}={text}")
        # 抹密钥放在最后一步：整行一起过，fields 里溜进来的也跑不掉。
        return self._redact(" · ".join(parts)) + "\n"

    def _redact(self, line: str) -> str:
        for secret in self._secrets:
            if secret in line:
                line = line.replace(secret, _REDACTED)
        return line

    def _append(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()

    def _enforce_limit(self) -> None:
        """超限就**截掉头部、保留尾部**（与 `storage.cleanup` 方向相反，见模块文档）。

        保留段的起点若落在某一行中间，把那一行的残段一并丢掉 ——
        半个时间戳比少一行更难读。
        """
        if self.limit_bytes <= 0:
            return
        try:
            if self.path.stat().st_size <= self.limit_bytes:
                return
            data = self.path.read_bytes()
        except OSError:
            return
        keep = data[-self.limit_bytes:]
        cut = keep.find(b"\n")
        if cut >= 0:
            keep = keep[cut + 1:]
        dropped = len(data) - len(keep)
        marker = f"--- 超出 {self.limit_bytes} 字节上限，已截去较旧的部分（{dropped} 字节） ---\n"
        _write_atomically(self.path, marker.encode("utf-8") + keep)


def _one_line(text: str) -> str:
    """把任意文本压成一行。

    滚动是**按行**截头保尾的，一行一条记录才能保证「被截掉的总是整条」。
    异常消息与 traceback 都是多行的，这里把空白折叠成单个空格 ——
    内容保住了，只是不再是原来的排版。
    """
    return " ".join((text or "").split())


def _write_atomically(path: Path, payload: bytes) -> None:
    """先写临时文件再 `os.replace`（与 `config.save` / `session.json` 同一套做法）。

    滚动是要**重写整个文件**的，中途崩掉会留下一个半截的日志 ——
    而日志恰恰是「崩溃之后才来看」的东西。
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".daemon-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
