"""守卫：启动器把「调用方的工作目录」从模块搜索路径里摘出去，脚本顶不掉标准库。

## 这条守卫防的是什么

2026-09-25 的真实事故（用户报告）：`cd <工作目录> && computer-use begin …` 崩在 import 阶段 ——

    File "<工作目录>\\inspect.py", line 3, in <module>
    FileNotFoundError: [Errno 2] No such file or directory: 'begin'

根因：`python -m cu` 会把**当前目录**排在 `sys.path` 最前，而那个工作目录里躺着调用方
自己写的 `inspect.py` —— 标准库 `inspect` 被顶掉，`dataclasses` 一 import 就炸
（本机复现同一根因：`AttributeError: module 'inspect' has no attribute 'get_annotations'`）。
Agent 侧的约定恰恰是「临时脚本都放当前工作目录」，所以这不是偶发：脚本名撞上任何标准库
模块名（inspect / json / types / code / copy / parser…）都会复现。

修法：启动器给客户端设 `PYTHONSAFEPATH=1`（Python 3.11+，等价于 `python -P`；
`requires-python >= 3.11`）。**用环境变量而不是加 `-P` 参数**，是因为 daemon 由客户端以
`-m cu.daemon` 拉起，环境变量经 `os.environ` 一并传下去 —— 一处修复护住两个进程；只给
客户端加 `-P` 的话，症状会从「CLI 崩在 import」变成「daemon 起不来（管道不可用）」。

两层守卫，对应两处会各自回退的地方：
  ① 启动器里拉起客户端的那个 spawnSync 必须显式带 `PYTHONSAFEPATH`。
  ② daemon 的环境必须真的能把该变量带下去（`_daemon_env` 是自己造的 env，不是纯继承）。
"""

from __future__ import annotations

from test_no_console_window import LAUNCHER, _spawn_call_spans

from cu import client


def _client_spawn_span(text: str) -> tuple[int, str]:
    """拉起 Python 客户端的那个 spawnSync 的 `(行号, 调用文本)`。"""
    spans = [(line, span) for line, span in _spawn_call_spans(text) if '"cu"' in span]
    assert len(spans) == 1, (
        f"启动器里「跑 cu 客户端」的 spawnSync 应恰好一处，实为 {len(spans)} —— 判据已失准"
    )
    return spans[0]


def test_matcher_flags_a_launcher_without_safe_path() -> None:
    """判据自证：少了 `PYTHONSAFEPATH` 的启动器文本必须被认出来（而不是判据写瞎了）。"""
    sample = 'const child = spawnSync(VENV_PYTHON, ["-m", "cu"], { stdio: "inherit" });'
    assert _client_spawn_span(sample)[1].count("PYTHONSAFEPATH") == 0


def test_launcher_isolates_cwd_from_sys_path() -> None:
    """守卫①：拉客户端的 spawnSync 必须带 `PYTHONSAFEPATH`。"""
    text = LAUNCHER.read_text(encoding="utf-8")
    line, span = _client_spawn_span(text)
    assert "PYTHONSAFEPATH" in span, (
        f"bin/computer-use.mjs:{line} 拉起客户端时没设 PYTHONSAFEPATH —— "
        "调用方工作目录里的同名脚本会顶掉标准库，CLI 崩在 import 阶段（见本文件 docstring）"
    )


def test_daemon_env_carries_python_safe_path(monkeypatch) -> None:
    """守卫②：环境变量必须真的传给 daemon（否则 CLI 活着、daemon 起不来）。"""
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    env = client._daemon_env("\\\\.\\pipe\\cu-guard")
    assert env["PYTHONSAFEPATH"] == "1"
