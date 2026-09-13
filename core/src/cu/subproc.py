"""子进程包装 —— 统一保证子进程**不弹控制台窗口**（发布阻塞项）。

## 为什么需要这个模块

Windows 上「控制台子系统程序」被一个**没有控制台**的父进程拉起时，系统会给它
**新开一个控制台窗口**；只有 `CREATE_NO_WINDOW`（0x08000000）能压住。
`capture_output=True` / `stdout=DEVNULL` **都不影响**这件事 —— 它们管的是流，不是窗口。

本产品的 daemon 是刻意无控制台的（架构 §1.5 第 5 条「崩溃即安全」的一部分），
所以它拉起的每一个 console 子系统子进程（omni worker、`powershell`/`clip`、
uv、git、`python -c` 探针）默认都会弹一个黑窗。发布版里出现黑窗不可接受：
它让「AI 在操作系统」显得可疑，还会污染截图。

**因此本包内禁止裸调 `subprocess.run` / `subprocess.Popen`**，一律走本模块。
`tests/unit/test_no_console_window.py` 有一条守卫盯着这件事。

## 为什么包一层，而不是逐个调用点加参数

逐个加参数是 N 个独立的、将来还会增加的改动点 —— 漏一个就复活一个黑窗。
集中到一处后，「带不带标志」由**唯一的**代码路径决定，加新调用点时也没有选错的机会。

## 与调用方已有的 `creationflags` **合并**，而不是覆盖

`client._spawn_daemon` 要的是 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`：
前两位是语义（不占终端、不随父进程退出），最后一位才是「不弹窗」。
直接覆盖会把语义一起丢掉 —— 那会破坏 daemon 的 detached 契约（DEC-035）。
"""

from __future__ import annotations

import subprocess
from typing import Any

#: Windows 的 `CREATE_NO_WINDOW`。刻意写成常量，而不是取 `subprocess.CREATE_NO_WINDOW`：
#: 后者在非 Windows 上根本不存在，会让本模块**在 import 期**就 AttributeError，
#: 而单测要在任何平台上 import 它来断言行为。本项目只支持 Windows
#: （`package.json` 的 `os: ["win32"]`），值本身就是这个常量。
CREATE_NO_WINDOW = 0x08000000


def creation_flags(existing: int | None = None) -> int:
    """把调用方已有的 `creationflags` 与 `CREATE_NO_WINDOW` 合并（不覆盖已有位）。"""
    return int(existing or 0) | CREATE_NO_WINDOW


def run(args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    """`subprocess.run` 的静默版：永远带上 `CREATE_NO_WINDOW`，其余参数原样透传。"""
    kwargs["creationflags"] = creation_flags(kwargs.get("creationflags"))
    return subprocess.run(args, **kwargs)


def Popen(args: Any, **kwargs: Any) -> subprocess.Popen:
    """`subprocess.Popen` 的静默版：永远带上 `CREATE_NO_WINDOW`，其余参数原样透传。"""
    kwargs["creationflags"] = creation_flags(kwargs.get("creationflags"))
    return subprocess.Popen(args, **kwargs)
