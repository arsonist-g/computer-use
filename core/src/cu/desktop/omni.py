"""omni 通道 —— base 环境里唯一一处「跨环境」的代码。

它**不 import omni 的任何东西**（DEC-037 的零共享）：omni worker 跑在
`~/.computer-use/venv-omni` 里，base 这边只是把它当子进程拉起、按 NDJSON 对话。
入参是图片路径，出参是 markdown 路径 —— **不传图像字节流**（架构 §1.5 第 3 条）。

为什么用子进程而不是常驻：worker 的冷启动要读 1.3GB 权重（torch + Florence），
让它常驻要占一两个 GB 内存，而 `parse` 是低频命令。子进程的代价是每次几十秒冷启动，
收益是**空闲时零占用** —— 与 DEC-035「空闲时没有理由继续占用」同一条思路。

（等实测确认真实耗时后再评估是否需要改成常驻；这是个有数据的取舍，不是拍脑袋。）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ..errors import CUError, ErrorCode

ENV_OMNI_HOME = "COMPUTER_USE_OMNI_HOME"

#: worker 的冷启动上限。加载 1.3GB 权重 + 首帧推理，机器慢时要几分钟。
DEFAULT_TIMEOUT = 900.0


def omni_python() -> Path:
    """omni 环境的解释器。"""
    override = os.environ.get("COMPUTER_USE_OMNI_PYTHON")
    if override:
        return Path(override)
    return Path.home() / ".computer-use" / "venv-omni" / "Scripts" / "python.exe"


def worker_script() -> Path:
    """worker 脚本的路径。

    base 包与 `omni/` 在源码树里是兄弟目录；装成 npm 包之后同样如此。
    """
    override = os.environ.get(ENV_OMNI_HOME)
    if override:
        return Path(override) / "worker.py"
    # core/src/cu/desktop/omni.py -> 上溯到仓库根 -> omni/worker.py
    return Path(__file__).resolve().parents[4] / "omni" / "worker.py"


def available() -> tuple[bool, str]:
    """omni 是否就绪。返回 `(就绪?, 不就绪的原因)`。

    只做**廉价的文件存在性检查**，不拉起子进程 —— 这个函数会被 `parse` 与
    `daemon status` 调用，不该为此付一次几十秒的冷启动。
    """
    python = omni_python()
    if not python.is_file():
        return False, f"omni 环境不存在（{python}）"
    script = worker_script()
    if not script.is_file():
        return False, f"worker 脚本不存在（{script}）"
    return True, ""


def call_parse(*, image_path: str, out_dir: Path, file_name: str, ai: bool,
               vlm: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """调 worker 解析一张图。返回 worker 的 `result` 字典。

    子进程通信走 stdio NDJSON，与 daemon↔客户端同形 —— 换传输不换格式（架构 §1.4）。
    """
    ready, reason = available()
    if not ready:
        raise CUError(
            ErrorCode.OMNI_NOT_INSTALLED,
            f"OmniParser 未安装：{reason}",
            hint="运行 `computer-use setup omni` 完成安装",
        )

    python = omni_python()
    script = worker_script()
    request = {"jsonrpc": "2.0", "id": 1, "method": "omni.parse", "params": {
        "image": str(image_path),
        "out_dir": str(out_dir),
        "file_name": file_name,
        "ai": bool(ai),
        "vlm": vlm or {},
    }}

    env = dict(os.environ)
    # worker 的 stdout 是协议通道，任何库往 stdout 打日志都会污染它。
    # 让它把缓冲关掉，报错也走 stderr。
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        proc = subprocess.run(
            [str(python), str(script), "--stdio"],
            input=json.dumps(request, ensure_ascii=False) + "\n",
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env, cwd=str(script.parent),
        )
    except subprocess.TimeoutExpired as exc:
        raise CUError(
            ErrorCode.OMNI_FAILED,
            f"解析超时（{timeout:.0f}s）。首次运行要加载模型权重，慢是正常的；"
            "若反复超时，检查 omni 环境是否完整。",
            detail={"image": str(image_path)},
        ) from exc
    except OSError as exc:
        raise CUError(ErrorCode.OMNI_FAILED,
                      f"无法拉起 omni worker：{exc}",
                      detail={"python": str(python)}) from exc

    response = _first_response(proc.stdout)
    if response is None:
        # 子进程没吐出可解析的响应 —— 把 stderr 尾部带出来，否则无从排查。
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        raise CUError(ErrorCode.OMNI_FAILED,
                      "omni worker 未返回可解析的响应",
                      detail={"returncode": proc.returncode, "stderr": " | ".join(tail)})

    if "error" in response:
        error = response["error"] or {}
        data = error.get("data") or {}
        code = str(data.get("code") or "omni_failed")
        mapped = ErrorCode.OMNI_NOT_INSTALLED if code == "omni_not_installed" else ErrorCode.OMNI_FAILED
        raise CUError(mapped, str(error.get("message") or "解析失败"),
                      detail={"hint": data.get("hint"), **(data.get("detail") or {})})
    return response.get("result") or {}


def _first_response(stdout: str) -> dict | None:
    """从 worker 的输出里找出第一条**合法的 JSON-RPC 响应**。

    刻意逐行尝试而不是只看第一行：上游库会往 stdout 打进度与警告
    （`OmniParser initialized!!!`、`image size:` 之类），第一行未必是响应。
    """
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and ("result" in message or "error" in message):
            return message
    return None


def worker_argv() -> list[str]:
    """给诊断用：worker 的确切启动命令。"""
    return [str(omni_python()), str(worker_script()), "--stdio"]


__all__ = ["available", "call_parse", "omni_python", "worker_argv", "worker_script",
           "DEFAULT_TIMEOUT", "sys"]
