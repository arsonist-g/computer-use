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

#: 取文件名时把标题折叠成 ASCII。
#:
#: **这不是审美选择，是绕开上游库的缺陷。** 实测（2026-09-12）：
#: `windows-capture` 的 `save_as_image(path)` 把路径按本地代码页往返一次，
#: 标题里的 `·`(U+00B7) 会被写成 `路`(U+8DEF) —— 正是 UTF-8 字节 `C2 B7`
#: 被当作 GBK 解释的结果。后果不是「名字难看」，而是**文件写到了另一个路径上**，
#: 我们再 stat 原路径永远为空，截图功能整体失效。
#:
#: 对照实验：Python 自己用同一个名字写文件完全正常，只有经过该库才被改写，
#: 所以这是上游的缺陷而不是本机文件系统的问题，内核层面无法绕开。
#: 契约 DEC-012 说 title_slug「不翻译」—— 那条仍然成立：窗口标题原样保留在
#: `session.json` 的 `window.title` 里（复盘读的是那里），只是**文件名**用 ASCII。
_SLUG_TRANSLITERATE = {
    ord("·"): "-", ord("•"): "-", ord("–"): "-", ord("—"): "-", ord("−"): "-",
    ord("“"): "", ord("”"): "", ord("‘"): "", ord("’"): "", ord("«"): "", ord("»"): "",
    ord("…"): "...", ord("×"): "x", ord("÷"): "-", ord("°"): "deg",
    ord("©"): "(c)", ord("®"): "(r)", ord("™"): "(tm)", ord("€"): "EUR",
    ord("£"): "GBP", ord("¥"): "CNY", ord("→"): "-", ord("←"): "-",
    ord("≤"): "<=", ord("≥"): ">=", ord("≠"): "!=",
}
#: 折叠后可能连续出现多个分隔符，压成一个。
_SLUG_COLLAPSE = re.compile(r"[-_]{2,}")


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
    """窗口标题 → 文件名片段。**始终是纯 ASCII**（理由见 `_SLUG_TRANSLITERATE` 上方）。

    规则来自 DEC-012：替换 `\\/:*?"<>|` 与控制字符 → 折叠连续空白 → 截断 40 字符
    → 空则 `untitled`。在此之上多一步「非 ASCII 字符折叠为 `_`」，把标题里的中文、
    全角符号、emoji 一律折叠 —— 它们原样保留在 `session.json` 的 `window.title` 里。
    """
    cleaned = _ILLEGAL_IN_FILENAME.sub("", title or "")
    cleaned = cleaned.translate(_SLUG_TRANSLITERATE)
    # 非 ASCII 一律折叠，而不是丢弃 —— 丢弃会把「未命名 - 记事本」压成「-」，
    # 折叠成下划线还能看出「这里原本有内容」。
    cleaned = "".join(ch if ch.isascii() and ch.isprintable() else "_" for ch in cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    cleaned = _SLUG_COLLAPSE.sub("-", cleaned).strip("-_ ")
    if len(cleaned) > TITLE_SLUG_MAX:
        cleaned = cleaned[:TITLE_SLUG_MAX].strip("-_ ")
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


#: 结构化数据后缀的拼写。`omni` 与 `omni_ai` 是两种工件（DEC-011）：
#: 同一张图可以解析两次，且必须落在不同文件里，否则后写的覆盖前一个。
PARSED_SUFFIXES = frozenset({"omni", "omni_ai"})


def is_parsed_name(name: str) -> bool:
    """文件名是否是「由图片解析出的结构化数据」。

    **两种落点都要认**，这正是一处曾经漂移过的地方：
      - 解析一张**截图**得到的 md，名字与来源图同一形状（含坐标）：
        `win-0x0001A2B-记事本-omni-100x200-0004.md`
      - 解析一张**外部图片**（DEC-025 的 `--image` 形态，无坐标）：
        `img-photo-omni-0007.md`

    这是 `_omni` / `_omni_ai` 的**唯一**判定入口。配额清理（DEC-017 阶段 1）靠它
    决定「哪些 md 可以随来源图片一起删」—— 判定错了两边都糟：过宽会删掉操作日志，
    过窄会让结构化数据只能等到阶段 2 才消失（等于永远删不掉）。
    """
    if not name.endswith(".md") or name in ("ops.md", "session.json"):
        return False
    stem = name[:-3]
    return any(f"-{suffix}-" in stem for suffix in PARSED_SUFFIXES)


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

    `suffix` 是结构化数据的 `omni` / `omni_ai`（data-model.md §3.2 / DEC-011）。
    后缀永远紧跟 `-` 且落在**序号之前**，两种落点都遵守这一条：

      - 有坐标（窗口 / 全屏）：`win-…-{title}-{suffix}-{x}x{y}-{seq:04d}.{ext}`
      - 无坐标（`img` 外部图片）：`img-{title}-{suffix}-{seq:04d}.{ext}`

    外部图片没有 hwnd 与坐标，硬套 `{x}x{y}` 只能编造无意义的占位值，
    所以走更短的模式 —— 但它仍带零填充序号，同一会话内的字典序依然等于时间序。

    统一成连字符（而不是 data-model.md §3.1/§3.3 示例里的 `_omni`）是为了让
    `is_parsed_name` 只有一条判定规则：**后缀两侧都是连字符**。示例与模式行
    本来就不一致（模式行写 `[-omni]`），取模式行的连字符更自洽。
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
