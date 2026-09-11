"""`python -m cu <命令>` —— CLI 客户端入口（Node 启动器 exec 的就是这个）。

只有这一处 sys.exit：把 `client.main` 的退出码原样交给 shell。
"""

from __future__ import annotations

import sys

from .client import main

if __name__ == "__main__":
    sys.exit(main())
