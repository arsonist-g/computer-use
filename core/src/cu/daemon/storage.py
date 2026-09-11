"""存储配额与清理 —— **字节上限 + 最旧优先**（DEC-017 / DEC-038）。

统一规则：任何可增长数据一律用「存储字节上限 + 最旧优先滚动」约束，
**不得**用条数或时长 —— 后两者封不住一个突发噪音期（某个会话反复截 4K 全屏）
几分钟内吃光磁盘的情形。

删除顺序（DEC-017）：**最旧会话优先**，且**始终排除活动会话**。分两阶段：

| 阶段 | 删除对象 | 保留 |
|---|---|---|
| 1 | 图片及其对应的结构化数据 md | **操作日志 md** |
| 2 | 整个会话目录（含操作日志） | — |

只有「所有会话的图片都删光后仍超限」才进入阶段 2。结构化数据随来源图片一起删 ——
图片没了，它失去参照，没有保留意义。操作日志体积远小于图片，应当最后才消失（DEC-006）。

使用量**不落盘**，每次扫描目录算出（data-model.md §1.7）—— 落盘就会有失同步风险。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..ids import is_parsed_name, now_iso

#: 阶段 1 可删除的工件。刻意用白名单而不是「删掉非 ops.md 的一切」——
#: 白名单的失效模式是「没删干净」（可见、可修），黑名单的失效模式是
#: 「删掉了不该删的」（不可逆）。
_IMAGE_SUFFIXES = (".png", ".webp")
#: 永不作为「工件」被阶段 1 删除的文件。
_KEEP_ALWAYS = frozenset({"ops.md", "session.json"})


@dataclass
class CleanupResult:
    freed_bytes: int = 0
    deleted_files: int = 0
    deleted_sessions: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.deleted_sessions is None:
            self.deleted_sessions = []


def dir_size(path: Path) -> int:
    """递归累加 `st_size`。文件在扫描中途消失不算错误（清理与写入可能并发）。"""
    total = 0
    if not path.exists():
        return 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def list_session_dirs(sessions_root: Path) -> list[Path]:
    """按**目录名排序**返回会话目录，即最旧在前。

    不依赖 mtime：文件时间戳在复制、同步、备份还原后都不可靠。
    `session_id` 的格式（`s-YYYYMMDD-HHMMSS-xxxx`）保证字典序 = 创建序。
    """
    if not sessions_root.exists():
        return []
    try:
        with os.scandir(sessions_root) as entries:
            dirs = [Path(e.path) for e in entries if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    return sorted(dirs, key=lambda p: p.name)


def is_artifact(path: Path) -> bool:
    """阶段 1 的可删除对象：图片，或与之配套的结构化数据 md。

    结构化数据的判定委托给 `ids.is_parsed_name` —— 命名规则只有一处定义，
    否则「命名」与「清理」会各自漂移（这正是引入这个委托的原因：
    两者曾经用不同的分隔符，导致结构化数据永远删不掉）。
    """
    if path.name in _KEEP_ALWAYS:
        return False
    if path.suffix.lower() in _IMAGE_SUFFIXES:
        return True
    return is_parsed_name(path.name)


def cleanup(sessions_root: Path, limit_bytes: int,
            protect: set[str] | None = None) -> CleanupResult:
    """把 `sessions_root` 压到 `limit_bytes` 以内。

    `protect` 是活动会话的 session_id 集合 —— 它们**永不**被删（DEC-017）。
    """
    protect = protect or set()
    result = CleanupResult()
    if limit_bytes <= 0:
        return result

    used = dir_size(sessions_root)
    if used <= limit_bytes:
        return result

    candidates = [d for d in list_session_dirs(sessions_root) if d.name not in protect]

    # ---- 阶段 1：删最旧会话的图片 + 结构化数据，操作日志留着 ----
    for session_dir in candidates:
        if used <= limit_bytes:
            break
        files = []
        for root, _dirs, names in os.walk(session_dir):
            files.extend(Path(root) / name for name in names)
        # 同一会话内也按名字排（seq 有序），先删最早的工件。
        for path in sorted(files, key=lambda p: p.name):
            if used <= limit_bytes:
                break
            if not is_artifact(path):
                continue
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                continue
            used -= size
            result.freed_bytes += size
            result.deleted_files += 1

    # ---- 阶段 2：仍超限，整目录删（含 ops.md），最旧优先 ----
    for session_dir in candidates:
        if used <= limit_bytes:
            break
        try:
            size = dir_size(session_dir)
            shutil.rmtree(session_dir)
        except OSError:
            continue
        used -= size
        result.freed_bytes += size
        result.deleted_sessions.append(session_dir.name)

    return result


def session_summary(session_dir: Path) -> dict:
    """会话目录的占用摘要。`daemon status` 与 `session list` 用它，不读文件内容。"""
    return {
        "session_id": session_dir.name,
        "bytes": dir_size(session_dir),
        "modified": now_iso(),
    }
