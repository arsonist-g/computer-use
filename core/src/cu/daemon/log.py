"""daemon 诊断日志 —— 出错时唯一的现场（Q-022 / DEC-038 / DEC-053）。

**它存在的理由**：daemon 是 detached 进程（`DETACHED_PROCESS | CREATE_NO_WINDOW`，
stdout/stderr 都接到 DEVNULL）。它出错时没有任何现场 —— 而 `internal_error` 的 hint
一直写着「详见 daemon 日志（路径见错误详情）」，那个文件却从未被创建过：
`config.daemon_log` / `daemon_log_limit_bytes` 定义了却没有写入者，
`~/.computer-use/logs/` 目录根本不存在。

**滚动方向与工件清理刻意相反**：`storage.cleanup` 删**最旧**的工件（按文件），
这里在**同一个文件内**截掉头部、保留尾部 —— 日志的价值全在最新那几行。出问题时要看的是
「刚刚发生了什么」，不是三小时前的第一行。**别照抄 `storage.cleanup`。**

**上限是软上限，为的是让追加保持廉价**：每次写入后都检查大小，但只有超过
「上限 × (1 + 滞回带)」才裁剪，且一次裁到「上限 × `KEEP_RATIO`」。没有滞回时，
一超过上限每次写入都会重写几乎整个保留区（`start = size - limit`）—— 实测
64KB 上限约 12.3ms/行、16MB 约 23.9ms/行，按默认 500MB 外推约 0.7 秒/行，
daemon 会明显卡住。代价是文件峰值可达「上限 × (1 + 滞回带) + 最长一行」，
因此 `daemon_log_limit_bytes` 是**软上限**（超出量有界，且与行宽同阶）。
**峰值内存有界**这条性质不变：裁剪仍是「从尾部算偏移 → 扫一小段找行首 →
分块拷贝保留段」，不把整个文件读进内存（见 `_enforce_limit`）。

**级别过滤**：`config.daemon_log_level`（默认 `info` = 全写）是级别**下限**，
低于它的记录直接丢掉 —— 噪音期把门槛提到 `warning` 就能只留值得看的那几行。

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
#: 级别名（由低到高）。**必须与 `cu.config.LOG_LEVELS` 一致** —— 两者分居 daemon 层与
#: 契约层（`config` 不能 import 本模块：那会把桌面层拖进 CLI 的冷路径），所以这份清单
#: 写了两处，由 `tests/unit/test_daemon_log.py` 断言它们相同。
LEVELS = ("info", "warning", "error")
#: 级别 → 严重度。未知级别按**最高**严重度处理：宁可多写一行，也不要因为一次拼写错误
#: 把一条消息静默丢掉。
_SEVERITY = {name: index for index, name in enumerate(LEVELS, start=1)}
_MAX_SEVERITY = max(_SEVERITY.values())

#: 找行首时往后扫的窗口；拷贝保留段时的块大小。两者只影响峰值内存，不影响结果。
_SCAN_CHUNK = 8192
_COPY_CHUNK = 1 << 20

#: 裁剪后保留的比例（相对上限）：一次裁到明显低于上限，留出足够长的追加空间，
#: 于是绝大多数写入是**纯追加**。与 `OVERSHOOT_RATIO` 一起决定重写的稀疏程度
#: （两次重写之间约能追加 `(1 - KEEP_RATIO + OVERSHOOT_RATIO) × 上限` 字节）。
KEEP_RATIO = 0.9
#: 滞回带：只有超过「上限 × (1 + OVERSHOOT_RATIO)」才裁剪。上限因此是软上限，
#: 峰值 ≈ 上限 × (1 + 滞回带) + 最长一行。设成 0 就退回「每行都重写」。
OVERSHOOT_RATIO = 0.1


class DaemonLog:
    """追加写 + 按字节上限「截头保尾」+ 级别过滤。

    线程安全：daemon 的 IPC 是一连接一线程，多个 handler 可能同时出错，
    追加与滚动必须在同一把锁里完成 —— 否则滚动会把另一条线程刚写的行切掉。
    """

    def __init__(self, path: Path | str, limit_bytes: int, *, level: str = "info",
                 secrets: Iterable[str] = ()) -> None:
        self.path = Path(path)
        self.limit_bytes = max(0, int(limit_bytes))
        #: 级别下限：低于它的记录直接丢掉。`info` = 全写。
        self.level = level
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
        """写一行。级别低于门槛的直接丢掉；**任何写入失败都不抛** ——
        日志写不进去不该让正在处理的那条命令失败。

        字段以 `key=value` 追加在消息之后；`fields` 为空则只写消息。
        """
        if not self._passes(level):
            return
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

    def _passes(self, level: str) -> bool:
        """这条记录过不过门槛。"""
        # 门槛认不出来时按**最宽**处理：`Config.validate` 会拦住非法配置，能走到这里
        # 说明是代码里拼错了 —— 那时候宁可多写几行，也不要静默丢掉。
        threshold = _SEVERITY.get(self.level, _SEVERITY["info"])
        return _SEVERITY.get(level, _MAX_SEVERITY) >= threshold

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

    def _soft_limit(self) -> int:
        """裁剪的触发点。上限是软的：允许超出一段滞回带，换来重写变得稀疏。"""
        return self.limit_bytes + int(self.limit_bytes * OVERSHOOT_RATIO)

    def _keep_bytes(self) -> int:
        """裁剪后保留多少字节。明显低于上限，于是下次重写要等很久。"""
        return max(1, int(self.limit_bytes * KEEP_RATIO))

    def _enforce_limit(self) -> None:
        """超过**软上限**就**截掉头部、保留尾部**（与 `storage.cleanup` 方向相反）。

        **滞回**：只有超过 `limit × (1 + OVERSHOOT_RATIO)` 才裁剪，且一次裁到
        `limit × KEEP_RATIO`。没有滞回时，每次超过上限的追加都会重写几乎整个保留区
        （`start = size - limit`）—— 实测 16MB 上限 ≈ 23.9ms/行，按默认 500MB 外推
        约 0.7 秒/行。有滞回后，两次重写之间还能再追加约
        `(1 - KEEP_RATIO + OVERSHOOT_RATIO) × 上限` 字节，其余全是纯追加。
        代价是文件峰值可达「上限 × (1 + 滞回带) + 最长一行」—— 软上限。

        **不把整个文件读进内存**：默认上限是 500MB，`read_bytes()` 会让 daemon 在滚动
        那一刻多占 500MB 常驻。这里改成「从尾部往前算偏移 → 扫一小段找到行首 →
        分块拷贝保留段」，峰值内存只与块大小有关，与上限无关。
        """
        if self.limit_bytes <= 0:
            return
        try:
            size = self.path.stat().st_size
            if size <= self._soft_limit():
                return
            start = self._keep_from(size)
            self._rewrite_from(start, size)
        except OSError:
            return

    def _keep_from(self, size: int) -> int:
        """保留段的起始偏移（**落在行首**）。

        起点若落在某一行中间，那一行的残段一并丢掉 —— 半个时间戳比少一行更难读。
        """
        edge = size - self._keep_bytes()
        with self.path.open("rb") as handle:
            handle.seek(edge)
            window = handle.read(_SCAN_CHUNK)
        newline = window.find(b"\n")
        if newline < 0:
            # 整个窗口里都没有换行（保留段的首行比窗口还长，或尾行被写断）。
            # 退回按字节切：宁可留一条残行，也不要把内容清空。
            return edge
        return edge + newline + 1

    def _rewrite_from(self, start: int, size: int) -> None:
        """把 `[start, size)` 这段保留下来，前面加一行轮转标记。

        先写临时文件再 `os.replace`（与 `config.save` / `session.json` 同一套做法）：
        滚动要重写整个文件，中途崩掉会留下一个半截的日志 —— 而日志恰恰是
        「崩溃之后才来看」的东西。
        """
        # 标记写「软上限」：上限之外允许一段滞回带（见 `_enforce_limit`），
        # 读日志的人看到的截断点因此未必正好在上限处。
        marker = (f"--- 超出 {self.limit_bytes} 字节软上限，"
                  f"已截去较旧的部分（{start} 字节） ---\n").encode()
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".daemon-", suffix=".tmp")
        try:
            with self.path.open("rb") as source, os.fdopen(fd, "wb") as target:
                source.seek(start)
                target.write(marker)
                remaining = size - start
                while remaining > 0:
                    block = source.read(min(_COPY_CHUNK, remaining))
                    if not block:
                        break
                    target.write(block)
                    remaining -= len(block)
                target.flush()
                os.fsync(target.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def _one_line(text: str) -> str:
    """把任意文本压成一行。

    滚动是**按行**截头保尾的，一行一条记录才能保证「被截掉的总是整条」。
    异常消息与 traceback 都是多行的，这里把空白折叠成单个空格 ——
    内容保住了，只是不再是原来的排版。
    """
    return " ".join((text or "").split())
