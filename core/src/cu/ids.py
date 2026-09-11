"""标识与命名 —— session_id、title_slug、工件文件名（data-model.md §3.2 / DEC-012）。

这些都设计成**字典序即时间序**，因为清理的 LRU 顺序直接按目录名排，
不依赖文件 mtime（复制、同步、备份还原后时间戳都不可靠）。
"""

from __future__ import annotations

import random
import re
import secrets
from datetime import datetime

#: 文件名里不允许出现的字符（Windows 保留字符集）+ 控制字符。
#: 标题是运行时字符串，可能来自任何窗口，必须逐字符过滤而不是信任输入。
_ILLEGAL_IN_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")

TITLE_SLUG_MAX = 40
#: 随机后缀的字符集：去掉易混的 0/o/1/l/i。
_ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
_ID_SUFFIX_LEN = 4


def now_iso(moment: datetime | None = None) -> str:
    """ISO-8601 带本地时区偏移、毫秒精度（data-model.md §3.6）。

    刻意不用 epoch 整数：这条数据的消费者是读文本的 AI 与人，
    没有任何排序依赖它（排序一律走 session_id / seq）。
    """
    moment = moment or datetime.now().astimezone()
    return moment.isoformat(timespec="milliseconds")


def new_session_id(moment: datetime | None = None) -> str:
    """`s-YYYYMMDD-HHMMSS-xxxx`。同级目录内字典序 = 创建时间序。"""
    moment = moment or datetime.now()
    suffix = "".join(random.choice(_ID_ALPHABET) for _ in range(_ID_SUFFIX_LEN))
    return f"s-{moment:%Y%m%d-%H%M%S}-{suffix}"


def slug_title(title: str) -> str:
    """窗口标题 → 文件名片段。**不翻译**（DEC-012）：标题是运行时字符串，无法可靠英文化。"""
    cleaned = _ILLEGAL_IN_FILENAME.sub("", title or "")
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    if len(cleaned) > TITLE_SLUG_MAX:
        cleaned = cleaned[:TITLE_SLUG_MAX].strip()
    return cleaned or "untitled"


def format_hwnd(hwnd: int | str) -> str:
    """hwnd 一律 `0x%08X` 字符串（data-model.md §3.6）—— 防 int/str 混用。"""
    if isinstance(hwnd, str):
        return hwnd
    return f"0x{hwnd:08X}"


def parse_hwnd(value: str | int) -> int:
    """解析用户/调用方给的 hwnd。接受 `0x1A2B` 与十进制两种写法。"""
    if isinstance(value, int):
        return value
    text = value.strip()
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text, 10)
    except ValueError as exc:
        from .errors import CUError, ErrorCode

        raise CUError(ErrorCode.INVALID_PARAMS, f"hwnd 不是合法数字：{value!r}") from exc


def unix_now() -> float:
    """心跳等内部计时用。对外的时间戳一律走 `now_iso`。"""
    import time

    return time.time()


def random_token(length: int = 8) -> str:
    return secrets.token_hex(length // 2)


def artifact_name(
    kind: str,
    seq: int,
    *,
    hwnd: str | None = None,
    title: str | None = None,
    origin: tuple[int, int] | None = None,
    suffix: str = "",
    ext: str = "png",
) -> str:
    """`{kind}-{hwnd}-{title_slug}[-{suffix}]-{x}x{y}-{seq:04d}.{ext}`。

    `suffix` 是结构化数据的 `omni` / `omni_ai`（data-model.md §3.2）。
    外部图片（`kind="img"`）没有 hwnd 与坐标，走短模式 —— 硬套 `{x}x{y}`
    只能编造无意义的占位值。
    """
    parts = [kind]
    if hwnd is not None:
        parts.append(hwnd)
    if title is not None:
        parts.append(slug_title(title))
    if suffix:
        parts.append(suffix)
    if origin is not None:
        parts.append(f"{origin[0]}x{origin[1]}")
    parts.append(f"{seq:04d}")
    return "-".join(parts) + f".{ext}"
