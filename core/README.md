# cu

Computer-Use 的 Python 核心。**不发布到 PyPI** —— 它随 npm 包分发（DEC-037）。

## 结构

```
src/cu/
  __init__.py        包说明与协议版本
  errors.py          19 个错误码的封闭枚举 + hint + 退出码映射
  protocol.py        JSON-RPC 2.0 信封 + NDJSON 分帧
  config.py          配置读写、默认值、校验
  ipc.py             命名管道（ctypes 手写 DACL，零 pywin32）
  ids.py             session_id / title_slug / 工件文件名
  manifest.py        session.json 的序列化模型
  client.py          CLI 客户端
  __main__.py        python -m cu
  daemon/            常驻进程：主循环、路由、会话、写锁、存储、操作日志
  desktop/           Win32 层：DPI、窗口、截图、输入、覆盖层、钩子
```

依赖方向单向：`desktop/` 与 `daemon/` 依赖上层的契约模块，反之不成立。

## 两条硬约束

**base 环境不得 import 重型库**（torch / transformers / paddleocr / easyocr /
opencv / PIL / numpy）。CLI 每次命令都是新解释器，import 什么就付什么代价。
`omni` 是独立环境，与本包**零代码共享**，只通过管道与文件交互。
由 `tests/unit/test_guards.py` 守卫。

**所有返回句柄的 Win32 API 必须声明 `restype`。** 不声明时 ctypes 按 32 位截断，
`GetCurrentProcess()` 的伪句柄 `-1` 会变成 `0xFFFFFFFF`，随后报 `ERROR_INVALID_HANDLE`。

## 本地开发

```powershell
uv venv                                   # 建环境
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
.venv/Scripts/python.exe -m pytest -q                      # 单元测试（CI 跑这个）
.venv/Scripts/python.exe -m ruff check src tests           # lint
.venv/Scripts/python.exe -m cu daemon status               # 跑一条命令
```

## 测试三层

| 层 | 位置 | 在哪跑 |
|---|---|---|
| 单元测试 | `tests/unit/` | **CI**，全部 mock 掉 Win32 |
| 真机集成 | `tests/native/` | 本机，需要桌面会话与真实窗口 |
| 手工验收 | `tests/acceptance.md` | 人工，发布前逐条过 |

`tests/native/red_verification.py` 是变异测试：临时改写 `src/` 下的源码，
确认测试**真的会红**，再逐字节还原。它默认不跑（会动源码），加 `--ci` 显式启用。

## 设计文档

设计不在本仓库（源码公开、设计记录不公开）。需要时向维护者索取。
