"""守卫：子进程一律走 `cu.subproc`，包装确实带上 `CREATE_NO_WINDOW`，启动器不弹窗。

## 这条守卫防的是什么

发布阻塞项（2026-09-13）：Windows 上「**没有控制台**的创建者」拉起控制台子系统
程序时，系统会给它新开一个**控制台窗口**；只有 `CREATE_NO_WINDOW`（Python）
/ `windowsHide: true`（Node）能压住 —— `capture_output` / `stdio` 设置都不影响。
本产品的 daemon 刻意无控制台（架构 §1.5 第 5 条「崩溃即安全」的一半），
所以它拉起的每一个子进程都默认弹窗；用户看到的是跑 `computer-use …` 时的 python 黑窗。

修法是把 spawn 集中到 `cu/subproc.py`（详见该模块）。集中之后，**回退的风险点只剩
「以后有人绕过包装」** —— 本文件就是钉住这一点的守卫，共三层：

  ① 除 `cu/subproc.py` 外，`core/src/cu/**` 不得出现裸的 `subprocess.run/Popen`。
     用 AST 而不是正则：注释/字符串里提到 `subprocess.run` 不该被误判。
  ② 包装本身必须带 `CREATE_NO_WINDOW`，且**不覆盖**调用方已有的 `creationflags`
     （daemon 的 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` 是语义，不能丢）。
  ③ Node 启动器的每个 `spawnSync` 都必须带 `windowsHide`。

守卫自己也要能被验证：`test_scanner_flags_a_bare_call` 用一段人造源码证明判据有效
（否则「扫描没报错」可能只是判据写瞎了 —— 见 test_guards.py 的同款思路）。
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

from cu import client, subproc

#: `core/tests/unit/test_no_console_window.py` → 仓库根。
REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "core" / "src" / "cu"
#: 包装自身的实现 —— **只有它**被允许直呼 `subprocess.run/Popen`。
WRAPPER = (SRC / "subproc.py").resolve()
LAUNCHER = REPO / "bin" / "computer-use.mjs"

#: 会真正**创建进程**的 subprocess 入口。不止 run/Popen：call / check_output /
#: check_call 同样会拉起进程，同样会在无控制台的 daemon 下弹窗。
SPAWN_NAMES = frozenset({"run", "Popen", "call", "check_call", "check_output"})


def _offending_lines(tree: ast.AST) -> list[int]:
    """源码里绕过包装的裸 spawn 调用所在行号。"""
    lines: list[int] = []
    for node in ast.walk(tree):
        # `subprocess.run(...)` / `subprocess.Popen(...)` / ...
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func.value
            if (isinstance(target, ast.Name) and target.id == "subprocess"
                    and node.func.attr in SPAWN_NAMES):
                lines.append(node.lineno)
        # `from subprocess import run` —— 另一种绕过方式：之后 `run(...)` 就是裸的。
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            lines.extend(node.lineno for alias in node.names if alias.name in SPAWN_NAMES)
    return sorted(lines)


def test_no_bare_subprocess_spawn_outside_wrapper() -> None:
    """守卫①：`core/src/cu/**`（除 `subproc.py`）不得有裸的 spawn。"""
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.resolve() == WRAPPER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(f"{path.relative_to(REPO)}:{line}"
                         for line in _offending_lines(tree))
    assert offenders == [], (
        "本包内出现裸的 subprocess 调用 —— 在无控制台的 daemon 下它们会弹控制台窗口。"
        "改用 `cu.subproc.run` / `cu.subproc.Popen`（见 cu/subproc.py）：\n  "
        + "\n  ".join(offenders)
    )


def test_scanner_flags_a_bare_call() -> None:
    """守卫①自证：判据真的能抓到裸调用，而不是「写瞎了所以永远绿」。"""
    sample = ast.parse(
        "import subprocess\n"
        "subprocess.run(['x'])\n"
        "subprocess.Popen(['x'])\n"
        "from subprocess import check_output\n"
        "def f():\n"
        "    # 注释里提到 subprocess.run( 不算\n"
        "    '字符串里 subprocess.run( 也不算'\n"
        "    return 1\n"
    )
    assert _offending_lines(sample) == [2, 3, 4]


def test_wrapper_always_adds_create_no_window(monkeypatch) -> None:
    """守卫②：`run` / `Popen` 都必须把 `CREATE_NO_WINDOW` 带进 `creationflags`。"""
    assert subproc.CREATE_NO_WINDOW == 0x08000000
    captured: dict = {}

    def fake_run(args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return "sentinel"

    def fake_popen(args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return "popen-sentinel"

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    # 裸调用：只有 CREATE_NO_WINDOW 这一位
    assert subproc.run(["uv", "--version"], capture_output=True) == "sentinel"
    assert captured["args"] == ["uv", "--version"]
    assert captured["kwargs"]["creationflags"] == subproc.CREATE_NO_WINDOW
    # 其余参数原样透传
    assert captured["kwargs"]["capture_output"] is True

    assert subproc.Popen(["x"], stdout=subprocess.DEVNULL) == "popen-sentinel"
    assert captured["kwargs"]["creationflags"] == subproc.CREATE_NO_WINDOW


def test_wrapper_merges_caller_creation_flags_instead_of_overwriting(monkeypatch) -> None:
    """守卫②续：调用方已有的 `creationflags` 必须被**合并**（daemon 的语义位不能丢）。"""
    captured: dict = {}

    def fake_run(args, **kwargs):
        captured.update(args=args, kwargs=kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    detached = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subproc.run(["x"], creationflags=detached)
    flags = captured["kwargs"]["creationflags"]
    assert flags & subproc.CREATE_NO_WINDOW, "包装把 CREATE_NO_WINDOW 弄丢了"
    assert flags & detached == detached, "包装覆盖掉了调用方原有的创建标志（detached 语义被破坏）"


def test_daemon_is_spawned_with_the_base_interpreter() -> None:
    """守卫④：#7 的根因修复不得回退 —— daemon 必须绕过 venv 启动桩。

    uv 建的 venv 里 `Scripts\\python.exe` 是启动桩，它再拉基础解释器且**不传递**
    `CREATE_NO_WINDOW` —— 于是 daemon 自己新开一个控制台（实测 AttachConsole=True）。
    绕过桩的办法是直接跑 `sys._base_executable`，再用 `__PYVENV_LAUNCHER__` 把
    venv 的路径解析补回来。
    """
    base = getattr(sys, "_base_executable", "") or sys.executable
    pipe = "\\\\.\\pipe\\cu-guard"
    assert client._daemon_command(pipe)[0] == base
    env = client._daemon_env(pipe)
    assert env[client.ENV_PIPE] == pipe
    if base != sys.executable:
        # 在 venv 里：必须把 venv 的启动桩路径告知基础解释器，否则它解析不到 venv。
        assert env["__PYVENV_LAUNCHER__"] == sys.executable
    else:
        assert "__PYVENV_LAUNCHER__" not in env


def _spawn_call_spans(text: str) -> list[tuple[int, str]]:
    """每个 `spawnSync(` 调用点的 `(行号, 调用文本)`。

    用括号配平从 `(` 扫到匹配的 `)`。启动器里 spawnSync 的实参不含括在字符串里的
    括号（只有 `"uv"` / `["-m","cu",...]` 这类），所以朴素配平足够；真出现字符串
    里的 `)` 时这里会先扫多、再由「span 里必须有 windowsHide」把问题暴露出来。
    """
    spans: list[tuple[int, str]] = []
    for match in re.finditer(r"\bspawnSync\(", text):
        depth = 0
        index = match.end() - 1
        while index < len(text):
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        spans.append((text.count("\n", 0, match.start()) + 1, text[match.end():index]))
    return spans


def test_launcher_hides_every_spawned_window() -> None:
    """守卫③：`bin/computer-use.mjs` 的每个 spawnSync 都要带 `windowsHide`。

    少了它，启动器拉起的控制台子系统程序（uv / 客户端 python）会弹窗 ——
    用户报告的正是「跑 `computer-use …` 弹一个 python 黑窗」。
    """
    text = LAUNCHER.read_text(encoding="utf-8")
    spans = _spawn_call_spans(text)
    assert len(spans) >= 3, f"启动器的 spawnSync 调用点变少了（{len(spans)}），守卫可能失准"
    missing = [line for line, span in spans if "windowsHide" not in span]
    assert missing == [], (
        f"bin/computer-use.mjs 里这些 spawnSync 缺 windowsHide: true → {missing}"
    )
