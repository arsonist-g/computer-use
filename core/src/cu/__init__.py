"""Computer-Use —— 面向 AI Agent 的 Windows 桌面控制。

包内分层（依赖方向单向，下层不得 import 上层）：

    errors / protocol / config      契约层：无 Win32、无 IO 副作用
    ipc                             IPC 层：命名管道
    daemon/*                        daemon：会话、锁、存储、路由
    daemon/desktop/*                桌面层：Win32 调用
    client / __main__               CLI 客户端

**base 环境禁止 import 重型库**（torch / transformers / paddleocr / easyocr /
opencv / PIL / numpy）—— 客户端每次命令都是新解释器，import 什么就付什么代价（DEC-039）。
OmniParser 跑在独立的 omni 环境里，与本包零代码共享。
"""

__version__ = "0.1.2"

# 客户端与 daemon 之间的协议版本。不匹配时 daemon 返回 protocol_version_mismatch，
# 客户端自动重启 daemon 后重试一次（api-contract.md §3 约定 4）。
PROTOCOL_VERSION = 1
