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
