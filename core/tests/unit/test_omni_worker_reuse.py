"""守卫：omni worker **常驻复用**，不是每次解析起一个新进程（DEC-014）。

为什么这条必须由测试守：它是「3440x1440 全屏截图 5s 内出结果」的唯一来源。
worker 的冷启动要读 1.3GB 权重（本机 2026-09-24 实测 12.6s），每次重来一遍的话，
识别本身再快，稳态也过不了 5s —— 真正的识别只占 2.5s。

而「每次新起一个」与「常驻复用」在功能上**看不出差别**：两者返回同一份 markdown。
只有数启动次数才分辨得出，所以这条只能由测试盯着。

反面同样要守：常驻必须能被卸下（引用归零、daemon 退出），否则它不是常驻而是常占。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cu.desktop import omni
from cu.errors import CUError, ErrorCode


class _FakeStdin:
    """接收请求：立刻把响应（外加一段上游噪音）压进 stdout。"""

    def __init__(self, proc: _FakeProc) -> None:
        self._proc = proc
        self.closed = False

    def write(self, text: str) -> int:
        if self.closed:
            raise ValueError("写入已关闭的 stdin")
        request = json.loads(text.strip())
        self._proc.requests.append(request)
        respond = self._proc.respond
        if respond is not None:
            # 上游库真的会往 stdout 打这些：worker 的响应不是第一行。
            self._proc.stdout.lines.append("Omniparser initialized!!!")
            self._proc.stdout.lines.append("image size: (3440, 1440)")
            self._proc.stdout.lines.append(json.dumps(respond(request)))
        return len(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeStdout:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.closed = False

    def __iter__(self) -> _FakeStdout:
        return self

    def __next__(self) -> str:
        if not self.lines:
            raise StopIteration
        return self.lines.pop(0) + "\n"

    def close(self) -> None:
        self.closed = True


class _FakeStderr:
    def __iter__(self) -> _FakeStderr:
        return self

    def __next__(self) -> str:
        raise StopIteration

    def close(self) -> None:
        pass


class _FakeProc:
    """假 worker 进程：只实现 `omni._Worker` 用到的那几样。"""

    def __init__(self, args: list[str], **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.requests: list[dict] = []
        self.respond: Callable[[dict], dict] | None = None
        self.stdin = _FakeStdin(self)
        self.stdout = _FakeStdout()
        self.stderr = _FakeStderr()
        self.killed = False
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        self._returncode = 0
        return 0

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9


def _ok(request: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request["id"],
            "result": {"path": "out.md", "element_count": 224, "model_name": None}}


class _Launcher:
    """把 `subproc.Popen` 换成一个记账替身：起了几次、每次收到什么，都在这里。"""

    def __init__(self) -> None:
        self.made: list[_FakeProc] = []
        self.respond: Callable[[dict], dict] = _ok

    def popen(self, args: list[str], **kwargs: Any) -> _FakeProc:
        proc = _FakeProc(args, **kwargs)
        proc.respond = self.respond
        self.made.append(proc)
        return proc


@pytest.fixture(autouse=True)
def _clean_worker() -> Any:
    """每个用例前后都把模块级单例清干净 —— 它是进程级的，会串味。"""
    omni.shutdown_worker()
    yield
    omni.shutdown_worker()


@pytest.fixture
def launcher(monkeypatch: pytest.MonkeyPatch) -> _Launcher:
    """换掉进程的启动点；`available()` 直接放行（环境细节不是本文件的主题）。"""
    made = _Launcher()
    monkeypatch.setattr(omni, "available", lambda: (True, ""))
    monkeypatch.setattr(omni.subproc, "Popen", made.popen)
    return made


def _parse(file_name: str = "img-omni-0001.md") -> dict:
    return omni.call_parse(image_path="shot.png", out_dir=Path("out"), file_name=file_name,
                           ai=False)


def test_two_parses_reuse_one_worker_process(launcher: _Launcher) -> None:
    """两次解析只起一个进程，且两次请求都真的发给了它。"""
    first = _parse()
    second = _parse("img-omni-0002.md")

    assert first["element_count"] == second["element_count"] == 224
    assert len(launcher.made) == 1, f"worker 被起了 {len(launcher.made)} 次，常驻复用没生效"
    assert [r["id"] for r in launcher.made[0].requests] == [1, 2]


def test_worker_is_not_started_until_the_first_parse(launcher: _Launcher) -> None:
    """只拿句柄不起进程：`available()` 这类廉价检查不该顺手拉起 1.3GB 的模型。"""
    assert omni._worker().running() is False
    assert launcher.made == []

    _parse()
    assert len(launcher.made) == 1


def test_upstream_stdout_noise_is_skipped(launcher: _Launcher) -> None:
    """响应不是 stdout 的第一行（上游会先打 `Omniparser initialized!!!` 之类）。"""
    assert _parse()["element_count"] == 224
    assert launcher.made[0].stdout.lines == []      # 噪音与响应都被读掉了


def test_shutdown_unloads_and_the_next_parse_restarts(launcher: _Launcher) -> None:
    """常驻不等于常占：卸下之后进程真的没了，下一次解析才会重新起。"""
    _parse()
    proc = launcher.made[0]

    omni.shutdown_worker()

    assert proc.stdin.closed is True, "卸载必须先关 stdin（worker 读到 EOF 自己退）"
    assert omni._worker().running() is False

    _parse()
    assert len(launcher.made) == 2, "卸载后应当重新拉起，而不是复用一个已经关掉的进程"


def test_worker_error_keeps_its_own_error_code(launcher: _Launcher) -> None:
    """worker 的错误码按码透传，不塌缩成 omni_failed（api-contract.md §4）。"""
    def respond(request: dict) -> dict:
        return {"jsonrpc": "2.0", "id": request["id"], "error": {
            "code": -32000, "message": "端点不通",
            "data": {"code": "vlm_failed", "hint": "检查 base_url", "detail": {"endpoint": "x"}}}}

    launcher.respond = respond

    with pytest.raises(CUError) as info:
        _parse()

    assert info.value.code is ErrorCode.VLM_FAILED
    assert info.value.detail["hint"] == "检查 base_url"
