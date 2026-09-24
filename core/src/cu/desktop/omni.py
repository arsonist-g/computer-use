"""omni 通道 —— base 环境里唯一一处「跨环境」的代码。

它**不 import omni 的任何东西**（DEC-037 的零共享）：omni worker 跑在
`~/.computer-use/venv-omni` 里，base 这边只是把它当子进程拉起、按 NDJSON 对话。
入参是图片路径，出参是 markdown 路径 —— **不传图像字节流**（架构 §1.5 第 3 条）。

**worker 是常驻的**（DEC-014）：daemon 持唯一一个实例，跨请求复用已加载的模型。
这条以前是反的（每次 `parse` 起一个新进程），依据是「`parse` 低频、空闲时零占用」。
真机实测把那个依据推翻了（2026-09-24，3440x1440 全屏截图）：

| 形态 | 一次 `parse` |
|---|---|
| 每次新起 worker（旧） | 冷启动 12.6s + 加载 easyocr/YOLO/Florence 若干秒 |
| 常驻 worker（现在） | 稳态 **2.5s**；只有本次 daemon 的第一次要付加载代价 |

也就是说逐次冷启动时，**八成时间花在反复读同一份权重上**，真正的识别只有 2.5s。
空闲占用由引用计数管住：用过解析的会话全部结束（`omni_refcount` 归零）或 daemon
退出时就停掉 worker —— DEC-035 的「空闲时没有理由继续占用」仍然成立，只是不再
拿它换每一次调用的十几秒。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any

from .. import subproc
from ..errors import CUError, ErrorCode

ENV_OMNI_HOME = "COMPUTER_USE_OMNI_HOME"
ENV_OMNI_WEIGHTS = "COMPUTER_USE_OMNI_WEIGHTS"

#: 一次 `parse` 的上限。首次调用含加载 1.3GB 权重 + 首帧推理，机器慢时要几分钟。
DEFAULT_TIMEOUT = 900.0

#: 常驻 worker 的 stderr 留痕行数。出错时贴进 `detail`，平时只是把管道抽干。
_STDERR_KEEP = 20


def omni_python() -> Path:
    """omni 环境的解释器。"""
    override = os.environ.get("COMPUTER_USE_OMNI_PYTHON")
    if override:
        return Path(override)
    return Path.home() / ".computer-use" / "venv-omni" / "Scripts" / "python.exe"


def omni_source() -> Path:
    """OmniParser 源码根目录（`setup omni` 克隆下来的）。

    **这三个路径函数放在这里而不是 worker 里**：base 侧要能回答「环境装在哪」，
    而 worker 跑在另一个解释器里 —— base 拿不到它的函数。两边各有一份的话
    迟早漂移，所以以这里为准，worker 用环境变量接。
    """
    override = os.environ.get(ENV_OMNI_HOME)
    if override:
        return Path(override)
    return Path.home() / ".computer-use" / "OmniParser"


def omni_weights() -> Path:
    """权重根目录。布局见 DEC-044（描述模型必须平铺）。"""
    override = os.environ.get(ENV_OMNI_WEIGHTS)
    if override:
        return Path(override)
    return Path.home() / ".computer-use" / "models"


def worker_script() -> Path:
    """worker 脚本的路径。

    base 包与 `omni/` 在源码树里是兄弟目录；装成 npm 包之后同样如此。
    """
    override = os.environ.get(ENV_OMNI_HOME)
    if override:
        candidate = Path(override) / "worker.py"
        if candidate.is_file():
            return candidate
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


#: worker 报回来的错误码 → 契约里的错误码。两者是同一套字符串
#: （`omni/worker.py` 的 `OMNI_NOT_INSTALLED` / `OMNI_FAILED` / `VLM_FAILED`）。
_WORKER_ERROR_CODES = {
    "omni_not_installed": ErrorCode.OMNI_NOT_INSTALLED,
    "omni_failed": ErrorCode.OMNI_FAILED,
    "vlm_failed": ErrorCode.VLM_FAILED,
}


class _Worker:
    """常驻的 omni worker 进程（DEC-014）。

    一个 daemon 一个实例，所有会话共用：模型只加载一次，之后每次 `parse` 只付识别的代价。
    `shutdown_worker()` 在「用过解析的会话全部结束」或 daemon 退出时调用，空闲时不留常驻内存。

    防孤儿：worker 的 stdin 是本进程给它的管道。daemon 无论怎么死，管道都会关上，
    worker 读到 EOF 自己退出 —— 这条不依赖任何清理代码跑成功（架构 §1.5 第 3 条）。
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._stderr: deque[str] = deque(maxlen=_STDERR_KEEP)

    # ---- 进程 ----

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _start(self) -> None:
        if self._proc is not None:
            _teardown(self._proc)
            self._proc = None
        python = omni_python()
        script = worker_script()
        env = dict(os.environ)
        # worker 的 stdout 是协议通道：任何一行延迟到达都等于调用方多等一次。
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self._stderr.clear()
        proc = subproc.Popen(
            [str(python), str(script), "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=env,
            cwd=str(script.parent),
        )
        self._proc = proc
        threading.Thread(target=self._drain_stderr, args=(proc,),
                         name="cu-omni-stderr", daemon=True).start()

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        """把 worker 的 stderr 抽干。

        必须抽干，不能放着不管：stderr 是管道，写满 64KB 之后 worker 会**阻塞在写
        错误输出上**，表现是解析卡死。留下的最后几行进 `detail` —— 那是子进程侧
        唯一的现场（它崩的时候 stdout 上什么都不会有）。
        """
        stream = proc.stderr
        if stream is None:
            return
        try:
            for line in stream:
                self._stderr.append(line.rstrip())
        except (OSError, ValueError):
            pass      # 进程被停掉时底层句柄先关，这不是错误

    def stop(self, timeout: float = 5.0) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            _teardown(proc, timeout)

    # ---- 协议 ----

    def parse(self, params: dict, timeout: float) -> dict:
        """发一条 `omni.parse` 并等它的响应。**串行**：一次只有一条请求在飞。

        串行是刻意的：只有一个模型实例，多线程同时喂它只会互相排队，却让「谁让谁等」
        变得看不出来。daemon 侧多个会话并发解析时，等的是这把锁。
        """
        with self._lock:
            if not self.running():
                self._start()
            proc = self._proc
            assert proc is not None and proc.stdin is not None   # `_start` 刚建好
            self._next_id += 1
            request_id = self._next_id
            payload = {"jsonrpc": "2.0", "id": request_id,
                       "method": "omni.parse", "params": params}
            try:
                proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise self._failure(f"无法向 omni worker 写入请求：{exc}") from exc

            # 超时不靠「取消读」—— 阻塞中的读没有可移植的取消手段 —— 而是到点就杀掉
            # worker：管道一断，那次读立刻拿到 EOF。杀的是自己的子进程，没有副作用，
            # 下一次 `parse` 会重新拉起它（代价是再付一次模型加载）。
            watchdog = threading.Timer(timeout, proc.kill)
            watchdog.start()
            try:
                response = self._read_response(proc, request_id)
            finally:
                watchdog.cancel()
            if response is None:
                raise self._failure(f"解析未完成：worker 超过 {timeout:.0f}s 没有响应或已退出")
            return self._unpack(response)

    def _read_response(self, proc: subprocess.Popen, request_id: int) -> dict | None:
        """读响应。刻意逐行尝试而不是只看第一行：上游库会往 stdout 打进度与警告
        （`Omniparser initialized!!!`、`image size:` 之类），第一行未必是响应。
        """
        stream = proc.stdout
        if stream is None:
            return None
        for line in stream:
            text = line.strip()
            if not text.startswith("{"):
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if "result" in message or "error" in message:
                return message
        return None

    def _unpack(self, response: dict) -> dict:
        if "error" not in response:
            return response.get("result") or {}
        error = response["error"] or {}
        data = error.get("data") or {}
        code = str(data.get("code") or "omni_failed")
        # worker 的错误码与契约里的错误码**是同一套字符串**（`omni/worker.py` 顶部那三个
        # 常量），所以这里要按码透传，而不是一律塌缩成 omni_failed。
        #
        # 塌缩的代价是 AI 拿不到正确的分支依据：端点坏了会被报成「解析进程异常，重试一次」，
        # 而正确的处置是「检查 base_url / api_key，且原始结构化数据仍然可用」——
        # 两件事的下一步动作完全不同。实测：配错端点时收到的就是 omni_failed。
        mapped = _WORKER_ERROR_CODES.get(code, ErrorCode.OMNI_FAILED)
        raise CUError(mapped, str(error.get("message") or "解析失败"),
                      detail={"hint": data.get("hint"), **(data.get("detail") or {})})

    def _failure(self, message: str) -> CUError:
        """子进程侧故障。带上退出码与 stderr 尾部 —— 否则现场只剩一句「失败了」。"""
        proc = self._proc
        detail: dict = {"stderr": " | ".join(list(self._stderr)[-6:])}
        if proc is not None:
            detail["returncode"] = proc.poll()
        return CUError(ErrorCode.OMNI_FAILED, message, detail=detail)


def _teardown(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    """停掉一个 worker 进程：先关 stdin（它读到 EOF 自己退），超时才强杀。"""
    _close_quietly(proc.stdin)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
    _close_quietly(proc.stdout)
    _close_quietly(proc.stderr)


def _close_quietly(stream: Any) -> None:
    try:
        if stream is not None:
            stream.close()
    except (OSError, ValueError):
        pass


def call_parse(*, image_path: str, out_dir: Path, file_name: str, ai: bool,
               vlm: dict | None = None, extra_elements: list[dict] | None = None,
               timeout: float = DEFAULT_TIMEOUT) -> dict:
    """调常驻 worker 解析一张图。返回 worker 的 `result` 字典。

    与 worker 的通信是 stdio NDJSON，与 daemon↔客户端同形 —— 换传输不换格式（架构 §1.4）。
    入参是图片路径，出参是 markdown 路径：**图像字节流不在这条通道上**。
    """
    ready, reason = available()
    if not ready:
        raise CUError(
            ErrorCode.OMNI_NOT_INSTALLED,
            f"OmniParser 未安装：{reason}",
        )

    return _worker().parse({
        "image": str(image_path),
        "out_dir": str(out_dir),
        "file_name": file_name,
        "ai": bool(ai),
        "vlm": vlm or {},
        # UIA 元素（可选）：由 base 侧读好，交给 worker 与检测器产出合并。
        # 图像字节流不在这条通道上（架构 §1.5 第 3 条），元素列表不是图像。
        "extra_elements": extra_elements or [],
    }, timeout=timeout)


_worker_lock = threading.Lock()
_worker_instance: _Worker | None = None


def _worker() -> _Worker:
    """进程内唯一的 worker 句柄。**只建句柄，不起进程** —— 进程在第一次解析时才起。"""
    global _worker_instance
    with _worker_lock:
        if _worker_instance is None:
            _worker_instance = _Worker()
        return _worker_instance


def shutdown_worker(timeout: float = 10.0) -> None:
    """停掉常驻 worker，释放它占的内存与显存。

    调用点都在 daemon 侧：用过解析的会话全部结束（`omni_refcount` 归零）、daemon 退出。
    「最后一个引用消失」是唯一的正常卸载时机（DEC-014）—— 也正因为有这一步，
    **常驻不等于常占**：没人用的时候它和以前一样不在。
    """
    global _worker_instance
    with _worker_lock:
        instance, _worker_instance = _worker_instance, None
    if instance is not None:
        instance.stop(timeout=timeout)


def worker_argv() -> list[str]:
    """给诊断用：worker 的确切启动命令。"""
    return [str(omni_python()), str(worker_script()), "--stdio"]


__all__ = ["DEFAULT_TIMEOUT", "available", "call_parse", "omni_python", "omni_source",
           "omni_weights", "shutdown_worker", "worker_argv", "worker_script"]
