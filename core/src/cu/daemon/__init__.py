"""cu-daemon —— 常驻进程的包（DEC-032）。

    core.py       主循环、命令路由、空闲退出
    sessions.py   会话生命周期与清单
    writelock.py  全局单写锁
    storage.py    存储配额与分阶段清理
    ops.py        操作日志 ops.md

启动入口是 `python -m cu.daemon`，由 Node 启动器或客户端自动拉起。
"""

from __future__ import annotations

from .core import Daemon, DaemonStatus

__all__ = ["Daemon", "DaemonStatus", "main"]


def main(argv: list[str] | None = None) -> int:
    """`python -m cu.daemon [--pipe NAME]`。前台运行，退出码给启动器用。"""
    import argparse
    import os
    import sys

    from ..config import Config
    from ..errors import CUError
    from ..ipc import PIPE_NAME

    ENV_PIPE = "COMPUTER_USE_PIPE"

    parser = argparse.ArgumentParser(prog="cu-daemon", description="Computer-Use 常驻进程")
    parser.add_argument("--pipe", default=None,
                        help=f"命名管道名（默认读 {ENV_PIPE}，再退回内置名）")
    parser.add_argument("--data-dir", default="", help="数据目录（覆盖配置）")
    args = parser.parse_args(argv)

    # 管道名优先走环境变量：客户端拉起 daemon 时用它传递，
    # 比命令行参数稳 —— 管道名以 `\.\pipe\` 开头，反斜杠在拼接命令行时容易被吃掉，
    # 结果是双方各连一条管道、都连不上（实测踩到过，排查成本很高）。
    pipe = args.pipe or os.environ.get(ENV_PIPE) or PIPE_NAME

    config = Config.load()
    if args.data_dir:
        config.data_dir = args.data_dir
        config.validate()

    daemon = Daemon(config, pipe_name=pipe)
    try:
        summary = daemon.prepare()
    except CUError as exc:
        # 已有 daemon 在跑不是错误 —— 客户端会去连它。用退出码 0 让启动器不要重试。
        print(f"cu-daemon: {exc.message}", file=sys.stderr)
        return 0
    print(f"cu-daemon: 监听 {pipe} · 接管 {len(summary['orphaned'])} 个孤儿会话",
          file=sys.stderr)
    try:
        daemon.serve_forever()
    except CUError as exc:
        print(f"cu-daemon: {exc.message}", file=sys.stderr)
        return 1
    return 0
