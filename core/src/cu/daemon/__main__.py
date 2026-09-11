"""`python -m cu.daemon` —— 常驻进程的入口。

由 CLI 客户端以 detached 方式拉起（DEC-035），也可以手工前台运行来调试。
"""

from __future__ import annotations

import sys

from . import main

if __name__ == "__main__":
    sys.exit(main())
