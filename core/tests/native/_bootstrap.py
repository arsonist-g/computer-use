"""真机测试脚本的共用小工具。

只做一件在 Windows 上必须做的事：把 stdout/stderr 切成 UTF-8。
产品代码会打印中文与 emoji（窗口标题、文件路径、错误信息），而 Windows 控制台
默认是 GBK（cp936），不显式切换就会在**打印结果的那一刻**抛 UnicodeEncodeError ——
测试跑完了却看不到结果。ipc_smoke 首跑就撞上了这个。
"""
from __future__ import annotations

import sys
from pathlib import Path


def utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def add_src_to_path() -> Path:
    """让 native 脚本能直接 `python core/tests/native/xxx.py` 跑，无需先安装。"""
    core = Path(__file__).resolve().parents[2]
    src = core / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    return core


#: 默认管道名。**逐字符构造而不是写字符串字面量**：这个值里有连续反斜杠，
#: 写在测试脚本里会被 shell / heredoc / 各层转义反复吃掉 —— 本轮为此踩了两次，
#: 每次的表现都是「err=3 管道不存在」，排查成本很高。构造一次，全项目共用。
#:
#: 与 `cu.ipc.PIPE_NAME` 必须一致；这里刻意**不 import 它** —— 测试脚本可能在
#: import cu 之前就要用这个值，而 import 顺序是每个脚本自己的事。
DEFAULT_PIPE = chr(92) * 2 + ".\\pipe\\computer-use-daemon"


def local_pipe() -> str:
    """native 脚本用的管道名。"""
    return DEFAULT_PIPE
