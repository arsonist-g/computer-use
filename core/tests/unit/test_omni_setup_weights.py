"""守卫：omni 权重下载必须在 **venv-omni 环境**里执行，且平铺落盘。

oracle: specified —— T3 的取证（全新 home 里 `setup omni` 报
`ModuleNotFoundError: No module named 'huggingface_hub'`，`detail.method=daemon.setup_omni`）、
DEC-039（base 环境不 import 重依赖）、DEC-044 的第三个坑（权重必须平铺）。

为什么这条约束必须由测试守：`huggingface_hub` 只装进 `venv-omni`（见
`omni_setup.PACKAGES`），base 环境的声明依赖里没有它。在开发机上「base 里 import 它」
可能偶然能跑通（同一个包碰巧也在 base 里），装到干净机器上才炸 —— 正是那种
「本地能跑、装出去就坏」的缺陷。
"""

from __future__ import annotations

import ast
import io
from pathlib import Path
from typing import Any

import pytest

from cu.desktop import omni_setup
from cu.errors import CUError, ErrorCode

MIRROR = "https://hf-mirror.invalid"


class _FakeProc:
    """假子进程：只实现 `_download_weights` 用到的那几样。"""

    def __init__(self, lines: list[str], returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdin = io.StringIO()
        self.stdout = iter(lines)

    def wait(self) -> int:
        return self.returncode


def _capture_popen(monkeypatch: pytest.MonkeyPatch, lines: list[str] | None = None,
                   returncode: int = 0) -> dict[str, Any]:
    """把 `subproc.Popen` 换成记账版，返回 `{"args": ..., "env": ..., "proc": ...}`。"""
    calls: dict[str, Any] = {}

    def fake_popen(args: list[str], **kwargs: Any) -> _FakeProc:
        calls["args"] = args
        calls["env"] = kwargs.get("env")
        calls["proc"] = _FakeProc(lines or [], returncode)
        return calls["proc"]

    monkeypatch.setattr(omni_setup.subproc, "Popen", fake_popen)
    return calls


def _fake_omni_python(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """让 `omni.omni_python()` 指向一个**存在**的假解释器（`_download_weights` 会查存在性）。"""
    fake = tmp_path / "venv-omni" / "Scripts" / "python.exe"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(omni_setup.omni, "omni_python", lambda: fake)
    return fake


# --------------------------------------------------------------------------- #
# 根因守卫：base 环境不 import huggingface_hub
# --------------------------------------------------------------------------- #


def test_base_module_never_imports_huggingface_hub() -> None:
    """base 侧只有一段**文本**，真正的 import 发生在 omni 解释器里。

    扫的是模块代码的 AST：`_DOWNLOAD_SCRIPT` 是字符串常量，它内部的
    `from huggingface_hub import ...` 不会出现在 AST 里 —— 这正是要的形式。
    """
    tree = ast.parse(Path(omni_setup.__file__).read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [f"line {node.lineno}: import {alias.name}"
                          for alias in node.names
                          if alias.name.split(".")[0] == "huggingface_hub"]
        elif (isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
              and node.module.split(".")[0] == "huggingface_hub"):
            offenders.append(f"line {node.lineno}: from {node.module} import ...")

    assert not offenders, (
        "base 环境不得 import huggingface_hub（DEC-039：它只装在 venv-omni 里）：\n  "
        + "\n  ".join(offenders)
        + "\n权重下载必须交给 `omni.omni_python()` 的子进程。"
    )


def test_download_script_is_the_only_place_that_imports_it() -> None:
    """反面确认：那段子进程脚本真的 import 了它（否则「不 import」可以靠删代码通过）。"""
    assert "from huggingface_hub import hf_hub_download" in omni_setup._DOWNLOAD_SCRIPT
    assert "hf_hub_download(" in omni_setup._DOWNLOAD_SCRIPT
    assert "shutil.move" in omni_setup._DOWNLOAD_SCRIPT, "平铺纠正必须在脚本里"


# --------------------------------------------------------------------------- #
# 执行位置：omni 解释器
# --------------------------------------------------------------------------- #


def test_download_runs_the_script_in_the_omni_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    fake = _fake_omni_python(monkeypatch, tmp_path)
    calls = _capture_popen(monkeypatch, ['{"file": "icon_detect_v3/model.pt", "bytes": 1}\n'])

    omni_setup._download_weights(tmp_path / "models")

    args = calls["args"]
    assert args[0] == str(fake), "必须用 omni.omni_python()，不是当前进程的解释器"
    assert args[1] == "-c"
    assert "from huggingface_hub import hf_hub_download" in args[2]


def test_download_reports_each_file_and_ignores_noise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """每个文件的进度日志要保住；非 JSON 的噪音行不该干扰（也不该被打印）。"""
    _fake_omni_python(monkeypatch, tmp_path)
    _capture_popen(monkeypatch, [
        "SomeLibrary: warming up\n",
        '{"file": "icon_detect_v3/model.pt", "bytes": 2097152}\n',
    ])

    omni_setup._download_weights(tmp_path / "models")

    out = capsys.readouterr().out
    assert "icon_detect_v3/model.pt" in out
    assert "2.1 MB" in out
    assert "warming up" not in out


def test_download_forwards_the_mirror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(omni_setup.ENV_HF_MIRROR, MIRROR)
    _fake_omni_python(monkeypatch, tmp_path)
    calls = _capture_popen(monkeypatch)

    omni_setup._download_weights(tmp_path / "models")

    assert calls["env"]["HF_ENDPOINT"] == MIRROR
    assert omni_setup.DEFAULT_HF_MIRROR == "https://hf-mirror.com"


def test_download_surfaces_a_child_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_omni_python(monkeypatch, tmp_path)
    _capture_popen(monkeypatch, ["ConnectionResetError: 连接被重置\n"], returncode=1)

    with pytest.raises(CUError) as info:
        omni_setup._download_weights(tmp_path / "models")

    assert info.value.code is ErrorCode.OMNI_NOT_INSTALLED
    assert "连接被重置" in str(info.value.detail)


def test_download_without_the_omni_environment_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(omni_setup.omni, "omni_python",
                        lambda: tmp_path / "nope" / "python.exe")

    with pytest.raises(CUError) as info:
        omni_setup._download_weights(tmp_path / "models")

    assert info.value.code is ErrorCode.OMNI_NOT_INSTALLED
    assert "setup omni" in info.value.message


# --------------------------------------------------------------------------- #
# 平铺落盘（DEC-044 第三个坑）
# --------------------------------------------------------------------------- #


def test_weight_tasks_are_flat(tmp_path: Path) -> None:
    """远程 `icon_caption/config.json` 必须落到**平铺**的
    `icon_caption_florence/config.json`，而不是 `icon_caption_florence/icon_caption/config.json`。
    """
    models = tmp_path / "models"
    tasks = omni_setup._weight_tasks(models)

    detector = next(t for t in tasks if t["rel"] == "icon_detect_v3/model.pt")
    assert detector["target"] == models / "icon_detect_v3" / "model.pt"
    assert detector["revision"] == omni_setup.DETECTOR_REVISION

    caption = next(t for t in tasks if t["remote"] == "icon_caption/config.json")
    assert caption["target"] == models / "icon_caption_florence" / "config.json"

    assert {t["rel"] for t in tasks} == {
        "icon_detect_v3/model.pt",
        *{f"icon_caption_florence/{name}" for name in omni_setup.CAPTION_FILES},
        *{f"icon_caption_florence/{name}" for name in omni_setup.FLORENCE_REMOTE_FILES},
    }
    for task in tasks:
        # 每个目标都恰好两层：models/<子目录>/<文件>。
        assert task["target"].parent.parent == models


# --------------------------------------------------------------------------- #
# 完整性：`config.json` 在场 ≠ 权重齐（2026-09-24 实测的坑）
# --------------------------------------------------------------------------- #


def _lay_down_every_weight(models: Path) -> None:
    for task in omni_setup._weight_tasks(models):
        task["target"].parent.mkdir(parents=True, exist_ok=True)
        task["target"].write_bytes(b"weights")


def test_weights_are_not_complete_when_the_big_file_is_missing(tmp_path: Path) -> None:
    """缺 `model.safetensors` 必须判「没装好」。

    这是首次安装实测到的缺陷：1.08GB 的 `model.safetensors` 传到 28MB 被重置，
    而 `config.json` 已落盘。旧判据只看 config.json，于是重跑 `setup omni`
    直接跳过下载、一路报到 `ready: True` —— 真正的模型文件没人检查过。
    """
    models = tmp_path / "models"
    _lay_down_every_weight(models)
    assert omni_setup._weights_complete(models) is True

    (models / "icon_caption_florence" / "model.safetensors").unlink()
    assert omni_setup._weights_complete(models) is False


def test_weights_are_not_complete_without_the_florence_remote_code(tmp_path: Path) -> None:
    """远程代码也是「装好」的一部分：缺了就得联网取，离线机器会直接失败。"""
    models = tmp_path / "models"
    _lay_down_every_weight(models)

    (models / "icon_caption_florence" / "modeling_florence2.py").unlink()
    assert omni_setup._weights_complete(models) is False


# --------------------------------------------------------------------------- #
# PyTorch 的来源：有 N 卡才装 CUDA 轮子（2026-09-24 实测的坑）
# --------------------------------------------------------------------------- #


class _ProbeResult:
    def __init__(self, text: str) -> None:
        self.stdout = text
        self.stderr = ""
        self.returncode = 0


def _record_installs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[str, ...], str | None]]:
    calls: list[tuple[tuple[str, ...], str | None]] = []
    monkeypatch.setattr(omni_setup, "_uv_install",
                        lambda _py, packages, *, index=None: calls.append((packages, index)))
    return calls


def test_an_nvidia_machine_installs_torch_from_the_cuda_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """PyPI 的 Windows 轮子是 CPU 版 —— 有 N 卡就必须去 PyTorch 索引拿 `+cu128`。"""
    monkeypatch.setattr(omni_setup.shutil, "which", lambda _n: "nvidia-smi")
    monkeypatch.setattr(omni_setup.subproc, "run",
                        lambda *a, **k: _ProbeResult("2.11.0+cu128 True\n"))
    calls = _record_installs(monkeypatch)

    omni_setup._install_torch(tmp_path / "python.exe")

    assert calls == [(omni_setup.CUDA_PACKAGES, omni_setup.TORCH_INDEX_URL)]
    assert omni_setup.TORCH_INDEX_URL.endswith("/cu128")


def test_a_machine_without_an_nvidia_gpu_gets_the_cpu_wheel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """没有 N 卡就下 124MB 的 CPU 轮子，而不是 2.6GB 的 CUDA 轮子。"""
    monkeypatch.setattr(omni_setup.shutil, "which", lambda _n: None)
    calls = _record_installs(monkeypatch)

    omni_setup._install_torch(tmp_path / "python.exe")

    assert calls == [(omni_setup.CPU_PACKAGES, None)]
