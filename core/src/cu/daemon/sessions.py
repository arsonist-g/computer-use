"""会话管理 —— 目录、清单、工件登记、崩溃恢复。

会话是锁与全部文件的归属单位（data-model.md §1.1）。它的目录是
`~/.computer-use/sessions/{session_id}/`，里面有 `session.json`、`ops.md`
以及本次会话产出的全部工件。

两个容易搞错的地方，都在这里落实：

1. **`session.json` 只在工件清单变化时重写**，不在每条命令后重写。
   点击、输入、窗口枚举只追加 `ops.md`（data-model.md §3.3）。
2. **启动时把所有 `active` 会话标记为 `orphaned`**：daemon 已经重启过一轮，
   锁和覆盖层都没了，会话无法续用；继续让它们显示 active 会让 AI 以为还能接着操作
   （data-model.md §3.8）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..errors import CUError, ErrorCode
from ..ids import new_session_id, now_iso
from ..manifest import (
    SESSION_ACTIVE,
    SESSION_ENDED,
    SESSION_ORPHANED,
    DisplayContext,
    ScreenshotRecord,
    SessionManifest,
    StructuredRecord,
)
from .ops import OpsEntry, OpsLog
from .storage import cleanup, list_session_dirs

MANIFEST_FILENAME = "session.json"
#: session_id 只允许这些字符。逗号、空格等一律拒绝 —— 它会直接变成目录名。
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

_COUNTED_COMMANDS = frozenset({"windows", "screenshot", "click", "move", "drag",
                               "scroll", "type", "key", "parse"})


def decode_session_id(session_id: str) -> str:
    """会话 id 的命令行解码回调。原样透传 —— 不做大小写折叠。

    会话目录名是大小写不敏感的（Windows），但清单里的 `session_id` 是原样的。
    折叠大小写会让 `S-X` 与 `s-x` 落到同一目录却带两个不同的 id，那才是真麻烦。
    """
    return session_id


@dataclass
class Session:
    """一个活动会话的内存态。manifest 是它的持久形态。"""

    session_id: str
    directory: Path
    manifest: SessionManifest
    ops: OpsLog

    @property
    def display(self) -> DisplayContext:
        return self.manifest.display

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "dir": str(self.directory),
            "status": self.manifest.status,
            "created_at": self.manifest.created_at,
            "ended_at": self.manifest.ended_at,
            "agent_hint": self.manifest.agent_hint,
            "screenshot_count": len(self.manifest.screenshots),
            "structured_count": len(self.manifest.structured),
            "op_count": self.manifest.op_count,
        }


class Sessions:
    """会话的创建、查找、结束与工件登记。"""

    def __init__(self, sessions_root: Path, storage_limit_bytes: int = 0) -> None:
        self.root = sessions_root
        #: `end()` 执行配额清理时用的上限。由 Daemon 注入当前配置值；
        #: 放在实例上是为了让 `end()` 不依赖 Config 的加载路径。
        self.storage_limit_bytes = storage_limit_bytes
        self._active: dict[str, Session] = {}

    # ---- 路径 ----

    def directory_of(self, session_id: str) -> Path:
        """会话目录。**必须**经过合法性校验 —— session_id 来自调用方，
        直接拼进路径就是目录穿越。"""
        if not _SESSION_ID_RE.match(session_id or ""):
            raise CUError(ErrorCode.INVALID_PARAMS,
                          f"session_id 含非法字符：{session_id!r}")
        return self.root / session_id

    # ---- 创建 / 结束 ----

    def begin(self, display: DisplayContext | None = None,
              agent_hint: str | None = None, moment: datetime | None = None) -> Session:
        session_id = new_session_id(moment)
        directory = self.directory_of(session_id)
        directory.mkdir(parents=True, exist_ok=False)

        manifest = SessionManifest(
            session_id=session_id,
            created_at=now_iso(moment),
            status=SESSION_ACTIVE,
            agent_hint=agent_hint,
            display=display or DisplayContext(),
        )
        session = Session(session_id=session_id, directory=directory,
                          manifest=manifest, ops=OpsLog(directory))
        session.ops.write_header(
            session_id=session_id,
            started=manifest.created_at,
            agent_hint=agent_hint,
            display_summary=_display_summary(manifest.display),
        )
        self._write_manifest(session)
        self._active[session_id] = session
        return session

    def end(self, session_id: str, moment: datetime | None = None) -> dict:
        session = self.get(session_id)
        session.manifest.status = SESSION_ENDED
        session.manifest.ended_at = now_iso(moment)
        session.ops.append(OpsEntry(
            entry_no=session.manifest.op_count,
            at=(moment or datetime.now()).strftime("%H:%M:%S"),
            command="session end",
            describe="会话结束",
            result="ok",
        ))
        self._write_manifest(session)
        self._active.pop(session.session_id, None)

        result = cleanup(
            self.root,
            limit_bytes=self.storage_limit_bytes,
            protect=set(self._active),
        )
        return {"freed_bytes": result.freed_bytes,
                "deleted_files": result.deleted_files,
                "deleted_sessions": result.deleted_sessions}

    # ---- 查找 / 列举 ----

    def get(self, session_id: str) -> Session:
        """按 id 取会话。内存里没有就从磁盘加载（daemon 重启后仍能查询历史会话）。"""
        active = self._active.get(session_id)
        if active is not None:
            return active
        directory = self.directory_of(session_id)
        if not (directory / MANIFEST_FILENAME).exists():
            raise CUError(ErrorCode.SESSION_NOT_FOUND,
                          f"会话不存在：{session_id}", {"session_id": session_id})
        manifest = self._read_manifest(directory)
        if manifest.status == SESSION_ENDED:
            raise CUError(ErrorCode.SESSION_ALREADY_ENDED,
                          f"会话已结束：{session_id}", {"session_id": session_id})
        return Session(session_id=session_id, directory=directory,
                       manifest=manifest, ops=OpsLog(directory))

    def list(self) -> list[dict]:
        """按创建时间**倒序**（data-model.md §1.3 的 `session list` 约定）。"""
        rows: list[dict] = []
        for directory in list_session_dirs(self.root):
            manifest = self._read_manifest(directory)
            rows.append({
                "session_id": manifest.session_id or directory.name,
                "status": manifest.status,
                "created_at": manifest.created_at,
                "ended_at": manifest.ended_at,
                "agent_hint": manifest.agent_hint,
                "screenshot_count": len(manifest.screenshots),
                "structured_count": len(manifest.structured),
                "op_count": manifest.op_count,
            })
        rows.sort(key=lambda row: (row["created_at"], row["session_id"]), reverse=True)
        return rows

    # ---- 工件登记 ----

    def next_seq(self, session_id: str) -> int:
        return self.get(session_id).manifest.next_seq()

    def add_screenshot(self, session_id: str, record: ScreenshotRecord) -> None:
        session = self.get(session_id)
        session.manifest.screenshots.append(record)
        session.manifest.seq_counter = max(session.manifest.seq_counter, record.seq)
        self._write_manifest(session)

    def add_structured(self, session_id: str, record: StructuredRecord) -> None:
        session = self.get(session_id)
        session.manifest.structured.append(record)
        session.manifest.seq_counter = max(session.manifest.seq_counter, record.seq)
        self._write_manifest(session)

    def record_op(self, session_id: str, entry: OpsEntry) -> None:
        """追加一条操作日志。**不重写清单** —— 只有工件变化才重写（data-model.md §3.3）。"""
        session = self.get(session_id)
        entry.entry_no = session.manifest.op_count + 1
        session.manifest.op_count = entry.entry_no
        session.ops.append(entry)

    def last_screenshot_of(self, session_id: str, hwnd: str) -> ScreenshotRecord | None:
        """某窗口最近一次截图（DEC-013 的位置漂移检查用）。"""
        session = self.get(session_id)
        for record in reversed(session.manifest.screenshots):
            if record.window is not None and record.window.hwnd == hwnd:
                return record
        return None

    def active_ids(self) -> set[str]:
        return set(self._active)

    # ---- 崩溃恢复 ----

    def mark_orphans(self) -> list[str]:
        """daemon 启动时调用：把所有 `active` 会话标记为 `orphaned`（§3.8）。

        理由不是悲观，是事实：daemon 是唯一持有锁与覆盖层的进程，
        它重启过就意味着那些会话的运行时状态已经不存在了。
        """
        marked: list[str] = []
        for directory in list_session_dirs(self.root):
            manifest = self._read_manifest(directory)
            if manifest.status != SESSION_ACTIVE:
                continue
            manifest.status = SESSION_ORPHANED
            manifest.session_id = manifest.session_id or directory.name
            tmp = _atomic_write_json(directory / MANIFEST_FILENAME, manifest.to_dict())
            if tmp:
                marked.append(manifest.session_id)
        return marked

    # ---- 清单读写 ----

    def _write_manifest(self, session: Session) -> None:
        _atomic_write_json(session.directory / MANIFEST_FILENAME, session.manifest.to_dict())

    @staticmethod
    def _read_manifest(directory: Path) -> SessionManifest:
        path = directory / MANIFEST_FILENAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 清单损坏不是致命错误：它只是缓存，真相在 ops.md 与文件系统里。
            # 返回一个最小清单，让 `session list` 仍能列出这个目录。
            manifest = SessionManifest(session_id=directory.name, status=SESSION_ORPHANED)
            return manifest
        manifest = SessionManifest.from_dict(raw)
        manifest.session_id = manifest.session_id or directory.name
        return manifest


def _atomic_write_json(path: Path, payload: dict) -> bool:
    """先写临时文件再 `os.replace`（data-model.md §3.3）。

    同卷上 `os.replace` 是原子的，崩溃只会留下旧版或新版，不会留下半个文件。
    失败不抛错 —— 清单是可重建的缓存，写不进去不该让会话本身失败。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".session-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError:
        return False
    return True


def _display_summary(display: DisplayContext) -> str:
    if not display.monitors:
        return "(未知)"
    parts = []
    for monitor in display.monitors:
        rect = monitor.rect
        size = f"{rect[2] - rect[0]}x{rect[3] - rect[1]}" if len(rect) >= 4 else "?"
        scale = f"{monitor.scale:.2f}".rstrip("0").rstrip(".")
        parts.append(f"{size} @{int(monitor.dpi)}dpi x{scale}" + (" (primary)" if monitor.primary else ""))
    return " | ".join(parts)


def purge_session_dir(path: Path) -> bool:
    """整目录删除。只给测试与显式清理用 —— 配额清理走 storage.cleanup。"""
    try:
        shutil.rmtree(path)
    except OSError:
        return False
    return True
