"""`setup omni` —— 装配 OmniParser 环境（DEC-037 / DEC-044）。

**为什么需要这个命令，而不是照抄官方说明**：上游 `requirements.txt` 对
`transformers` 不锁版本，而它的 Florence-2 远程代码在新版上跑不起来；另有
两个「声明了但运行时用不到」的依赖会硬失败。照官方说明装，得到的是一串
**看起来像模型损坏**的报错。这三个坑是本机实测出来的（DEC-044），
把它们固化成代码，下一个人就不必再踩一遍。

装什么（`~/.computer-use/` 下，全部可一步删除）：

    venv-omni/              独立环境，与 base 零共享（DEC-037）
    OmniParser/             上游源码（setup 会 clone，用户不接触）
    models/icon_detect_v3/  检测权重（YOLOv9-E，PR #37）
    models/icon_caption_florence/  描述权重 + Florence-2 远程代码

不装 paddleocr / paddlepaddle / flash_attn —— 理由见 DEC-044。

**第四条坑（2026-09-24 实测）**：Windows 上 PyPI 的 `torch` 轮子是 **CPU 版**，
而「装错了」的表现不是报错，是慢两个数量级 —— 本机 3440x1440 全屏截图
CPU 163s / GPU 2.5s。有 N 卡的机器必须从 PyTorch 自己的索引装 `+cu128` 轮子。
见 `TORCH_INDEX_URL`。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .. import subproc
from ..errors import CUError, ErrorCode
from . import omni

#: 与 DEC-044 实测一致的版本。**不要跟着上游 requirements.txt 走** ——
#: 那份文件对 transformers 不锁版本，装最新必炸。
TRANSFORMERS_PIN = "transformers==4.44.2"

#: 上游仓库与权重来源。
OMNI_REPO = "https://github.com/microsoft/OmniParser.git"
WEIGHTS_REPO = "microsoft/OmniParser-v2.0"
#: PR #37 的检测器（README 明确推荐它，且 v2.0 仓库里那个是旧版）。
DETECTOR_REVISION = "refs/pr/37"
DETECTOR_FILE = "icon_detect_v3/model.pt"
CAPTION_FILES = ("config.json", "generation_config.json", "model.safetensors")
#: Florence-2 的远程代码。`config.json` 的 `auto_map` 指向它们，
#: 本地加载必须有这些文件在场，否则 transformers 会去网上找。
FLORENCE_REMOTE_FILES = (
    "configuration_florence2.py", "modeling_florence2.py", "processing_florence2.py",
    "preprocessor_config.json", "tokenizer.json", "tokenizer_config.json", "vocab.json",
)
FLORENCE_REPO = "microsoft/Florence-2-base-ft"

#: 依赖最小集。刻意**不含** paddleocr / paddlepaddle / flash_attn / gradio /
#: streamlit / boto3 / groq 等 —— 它们要么用不到，要么会硬失败（DEC-044）。
PACKAGES = (
    "numpy==1.26.4",
    "ultralytics==8.3.70",
    TRANSFORMERS_PIN,
    "timm", "einops==0.8.0", "accelerate",
    "huggingface_hub", "easyocr", "supervision==0.18.0",
    "opencv-python-headless", "Pillow",
    # `util/utils.py` 顶层就 `from openai import AzureOpenAI` —— 产品不走它，
    # 但它是模块级导入，不装就 import 不了。它很轻，装比绕开简单。
    "openai",
)

#: PyTorch 单独装：装哪一个轮子取决于机器有没有 N 卡，而两者**不是同一个来源**。
#:
#: 2026-09-24 实测：PyPI（含国内各镜像）上的 `torch-2.14.0-cp312-cp312-win_amd64.whl`
#: 只有 124MB —— 那是 CPU 版，`torch.cuda.is_available()` 为 False。CUDA 轮子带
#: `+cu128` 本地版本号、2.6GB，只发布在 PyTorch 自己的索引上。**装错不报错**，
#: 代价是 CPU 推理：本机 3440x1440 全屏截图 163s（GPU 2.5s）。
TORCH_INDEX_URL = "https://download.pytorch.org/whl/cu128"
CPU_PACKAGES = ("torch", "torchvision")
#: 与 cu128 索引配套的一对（torchvision 0.26 ↔ torch 2.11）。
CUDA_PACKAGES = ("torch==2.11.0", "torchvision==0.26.0")

ENV_PYPI_MIRROR = "COMPUTER_USE_OMNI_INDEX_URL"
ENV_HF_MIRROR = "COMPUTER_USE_HF_ENDPOINT"
DEFAULT_HF_MIRROR = "https://hf-mirror.com"


def _log(message: str) -> None:
    print(f"  {message}", flush=True)


def _run(argv: list[str], *, env: dict | None = None, cwd: Path | None = None) -> int:
    proc = subproc.run(argv, env=env, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        _log("失败：" + " | ".join(tail))
    return proc.returncode


def _uv() -> str:
    found = shutil.which("uv")
    if not found:
        raise CUError(
            ErrorCode.OMNI_NOT_INSTALLED,
            "uv 未安装，无法创建隔离环境（DEC-018 明确不退回系统 pip）",
        )
    return found


def _uv_install(python: Path, packages: tuple[str, ...], *, index: str | None = None) -> None:
    """装依赖。镜像失败时单次回退官方源，并把两次的原委都透出来（DEC-042）。

    `index` 显式给出时**只用它**：CUDA 版 torch 只发布在 PyTorch 自己的索引上，
    PyPI 的镜像和官方源都没有它 —— 那两个源能装上的只有 CPU 版。
    """
    uv = _uv()
    base = [uv, "pip", "install", "--python", str(python), *packages]
    if index is not None:
        if _run(base + ["--index-url", index]) != 0:
            raise CUError(
                ErrorCode.OMNI_NOT_INSTALLED,
                f"从 {index} 安装 PyTorch 失败。该索引在部分网络下需要走代理，"
                "可设置 HTTPS_PROXY 后重试。",
            )
        return
    mirror = os.environ.get(ENV_PYPI_MIRROR)
    attempts = []
    if mirror:
        attempts.append(base + ["--index-url", mirror])
    attempts.append(base + ["--index-url", "https://pypi.org/simple"])
    for attempt, argv in enumerate(attempts):
        if _run(argv) == 0:
            return
        if attempt + 1 < len(attempts):
            _log("索引不可用，回退 https://pypi.org/simple 重试一次（DEC-042）")
    raise CUError(ErrorCode.OMNI_NOT_INSTALLED,
                  "omni 依赖安装失败。若报 403/404，通常是镜像源不含某个包；"
                  f"可用 {ENV_PYPI_MIRROR} 指定索引后重试。")


def _gpu_present() -> bool:
    """机器上有没有 NVIDIA 显卡。

    这一步决定下 2.6GB 的 CUDA 轮子还是 124MB 的 CPU 轮子。用 `nvidia-smi` 判：
    它随驱动装、在 PATH 上，比任何 WMI/注册表查询都直接。
    """
    return shutil.which("nvidia-smi") is not None


def _install_torch(python: Path) -> None:
    """按有没有 N 卡选 torch 的来源，并**如实报出装完是哪个**。

    「装错不报错」是这一条最坏的部分：CPU 轮子功能完全正常，只是慢两个数量级。
    所以装完立刻回读一次 `torch.cuda.is_available()` —— 那是唯一能分辨两者的判据。
    """
    if not _gpu_present():
        _log("未检测到 NVIDIA 显卡，安装 CPU 版 PyTorch")
        _uv_install(python, CPU_PACKAGES)
        return

    _log(f"检测到 NVIDIA 显卡，从 {TORCH_INDEX_URL} 安装 CUDA 版 PyTorch（约 2.6GB）")
    _uv_install(python, CUDA_PACKAGES, index=TORCH_INDEX_URL)
    probe = subproc.run(
        [str(python), "-c",
         "import torch;print(torch.__version__, torch.cuda.is_available())"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    report = (probe.stdout or probe.stderr or "").strip().splitlines()[-1:] or [""]
    _log(f"PyTorch 就位：{report[0]}")
    if "True" not in report[0]:
        _log("警告：torch.cuda.is_available() 不是 True —— 识别会退到 CPU，慢两个数量级")


#: 在 **omni 环境**里执行的下载脚本（`python -c` 的源码）。
#:
#: **为什么不 in-process 调 `hf_hub_download`**：`huggingface_hub` 只装在
#: `venv-omni` 里（见 `PACKAGES`），base 环境的声明依赖里没有它
#: （`core/pyproject.toml` 只有 `windows-capture`）。在 base 环境里 import 它必然
#: `ModuleNotFoundError`，而且那本身就违反 DEC-039（base 环境不 import 重依赖）。
#: 所以这段逻辑必须由 `omni.omni_python()` 跑起来。
#:
#: 契约：stdin 收一份任务数组（JSON），每下完一个文件往 stdout 打一行
#: `{"file": <相对 models 的路径>, "bytes": <字节数>}`；出错则非零退出并把原因
#: 打到 stderr。**平铺落盘**的纠正（DEC-044 第三个坑）就在这段里 ——
#: `hf_hub_download(local_dir=X)` 会保留 `filename` 里的目录，得靠 `shutil.move`
#: 把文件搬到显式目标位置，否则权重落在嵌套层、远程代码落在平铺层，两边对不上。
_DOWNLOAD_SCRIPT = r'''
import json
import shutil
import sys
import traceback
from pathlib import Path


def main() -> int:
    tasks = json.loads(sys.stdin.read() or "[]")
    from huggingface_hub import hf_hub_download

    for task in tasks:
        target = Path(task["target"])
        target.parent.mkdir(parents=True, exist_ok=True)
        got = Path(hf_hub_download(
            repo_id=task["repo"],
            filename=task["remote"],
            revision=task.get("revision") or None,
            local_dir=str(target.parent),
        ))
        if got.resolve() != target.resolve():
            shutil.move(str(got), str(target))
        print(json.dumps({"file": task["rel"], "bytes": target.stat().st_size},
                         ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
'''


def _weight_tasks(models: Path) -> list[dict]:
    """要下载的文件清单。每个任务的 `target` 是**平铺**后的最终位置。

    拆成独立函数：单测与隔离验证可以只换一份小清单，走完全相同的下载路径
    （同一个子进程脚本、同一套平铺纠正），不必真的下 1.4GB。
    """
    caption_dir = models / "icon_caption_florence"
    tasks = [{"repo": WEIGHTS_REPO, "remote": DETECTOR_FILE, "revision": DETECTOR_REVISION,
              "target": models / "icon_detect_v3" / "model.pt",
              "rel": "icon_detect_v3/model.pt"}]
    for name in CAPTION_FILES:
        tasks.append({"repo": WEIGHTS_REPO, "remote": f"icon_caption/{name}",
                      "target": caption_dir / name,
                      "rel": f"icon_caption_florence/{name}"})
    # 远程代码：config.json 的 auto_map 指向它们，缺了就要联网找，离线/内网会失败。
    for name in FLORENCE_REMOTE_FILES:
        tasks.append({"repo": FLORENCE_REPO, "remote": name,
                      "target": caption_dir / name,
                      "rel": f"icon_caption_florence/{name}"})
    return tasks


def _weights_complete(models: Path) -> bool:
    """权重是否**齐**。判据就是下载清单本身 —— 少一件都不算装好。

    为什么不能只看 `icon_caption_florence/config.json`：那是清单里**最小**的一件。
    2026-09-24 首次安装正好撞上这个 —— 1.08GB 的 `model.safetensors` 传到 28MB 断了，
    而 config.json 已经落盘，于是重跑被判定为「权重已就位」直接跳过，
    `run_setup` 一路报到 `ready: True`。**真正的模型文件从头到尾没人检查过**，
    直到 `parse` 才炸，而那时报的是 transformers 的加载错，指着的是「模型坏了」。
    """
    return all(task["target"].is_file() for task in _weight_tasks(models))


def _parse_progress(line: str) -> dict | None:
    """子进程 stdout 的一行是不是「一个文件下完了」的进度事件。"""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(event, dict) and "file" in event and "bytes" in event:
        return event
    return None


def _download_weights(models: Path) -> None:
    """下权重。**在 omni 环境里执行**（见 `_DOWNLOAD_SCRIPT`），平铺落盘。

    镜像：`HF_ENDPOINT` 由 `ENV_HF_MIRROR` 给出，透传给子进程。
    """
    python = omni.omni_python()
    if not python.is_file():
        raise CUError(
            ErrorCode.OMNI_NOT_INSTALLED,
            f"omni 环境不存在（{python}），无法下载权重；"
            "先运行 `computer-use setup omni` 创建 venv-omni 并装依赖",
        )

    endpoint = os.environ.get(ENV_HF_MIRROR) or ""
    env = dict(os.environ)
    if endpoint:
        env["HF_ENDPOINT"] = endpoint
        _log(f"使用 HuggingFace 镜像 {endpoint}")
    # 进度条关掉：子进程的 stderr 并进了 stdout 一起读，tqdm 的 `\r` 进度会变成
    # 上千行噪音，而这里每个文件只需要一条结论。
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    manifest = json.dumps(
        [{"repo": t["repo"], "remote": t["remote"], "rel": t["rel"],
          "target": str(t["target"]), "revision": t.get("revision")}
         for t in _weight_tasks(models)],
        ensure_ascii=False,
    )
    try:
        proc = subproc.Popen(
            [str(python), "-c", _DOWNLOAD_SCRIPT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env,
        )
    except OSError as exc:
        raise CUError(ErrorCode.OMNI_NOT_INSTALLED,
                      f"无法在 omni 环境里启动下载进程：{exc}",
                      detail={"python": str(python)}) from exc

    tail: list[str] = []
    assert proc.stdin is not None and proc.stdout is not None  # 上面就是 PIPE
    proc.stdin.write(manifest)
    proc.stdin.close()
    for line in proc.stdout:
        text = line.strip()
        if not text:
            continue
        tail.append(text)          # 出错时要用最后几行解释原因
        del tail[:-20]
        event = _parse_progress(text)
        if event is not None:
            _log(f"{event['file']}  {event['bytes'] / 1e6:.1f} MB")
    code = proc.wait()
    if code != 0:
        raise CUError(
            ErrorCode.OMNI_NOT_INSTALLED,
            "权重下载失败（在 venv-omni 环境里执行的下载脚本报错）",
            detail={"returncode": code, "tail": " | ".join(tail[-6:])},
        )


def run_setup(*, force: bool = False, skip_weights: bool = False) -> dict:
    """装配 omni 环境。返回一份摘要，供 CLI 打印。

    幂等：已就位的部分跳过。`force=True` 时重装依赖（用于修损坏的环境）。
    """
    root = Path.home() / ".computer-use"
    venv = root / "venv-omni"
    python = omni.omni_python()
    source = omni.omni_source()
    models = omni.omni_weights()
    steps: list[str] = []

    # ---- 1. 独立环境 ----
    root.mkdir(parents=True, exist_ok=True)
    if not python.is_file():
        _log(f"创建独立环境 {venv}")
        if _run([_uv(), "venv", str(venv), "--python", "3.12"]) != 0:
            raise CUError(ErrorCode.OMNI_NOT_INSTALLED, "创建 venv-omni 失败")
        steps.append("创建 venv-omni")
    else:
        _log(f"环境已存在 {venv}")

    # ---- 2. 上游源码 ----
    if not (source / "util" / "omniparser.py").is_file():
        _log(f"克隆 OmniParser 到 {source}")
        if source.exists():
            shutil.rmtree(source, ignore_errors=True)
        if _run(["git", "clone", "--depth", "1", OMNI_REPO, str(source)]) != 0:
            raise CUError(
                ErrorCode.OMNI_NOT_INSTALLED,
                "克隆 OmniParser 失败。国内网络直连 GitHub 常被重置，"
                "可先设置 HTTPS_PROXY 再重试。",
            )
        steps.append("克隆 OmniParser")
    else:
        _log(f"源码已存在 {source}")

    # ---- 3. 依赖（最小集 + 锁定的 transformers + 按显卡选的 torch）----
    if force or not _venv_has(python, ("torch", "transformers", "easyocr")):
        _log("安装依赖（最小集，transformers 锁定 4.44.2 —— 见 DEC-044）")
        _uv_install(python, PACKAGES)
        _install_torch(python)
        steps.append("安装依赖")
    else:
        _log("依赖已就位")

    # ---- 4. 权重 ----
    if skip_weights:
        _log("按参数跳过权重下载")
    elif not _weights_complete(models):
        _log(f"下载权重到 {models}（约 1.4 GB，首次较慢）")
        _download_weights(models)
        steps.append("下载权重")
    else:
        _log("权重已就位")

    # ---- 5. 验收：真的能加载吗 ----
    _log("校验安装件")
    ready, reason = omni.available()
    if ready:
        # 用 worker 自己的检查逻辑，避免「检查代码」与「加载代码」漂移。
        check = subproc.run(
            [str(python), str(omni.worker_script()), "--selftest"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        if check.returncode != 0:
            _log((check.stderr or check.stdout or "").strip().splitlines()[-3:][-1]
                 if (check.stderr or check.stdout) else "自检失败")
            raise CUError(ErrorCode.OMNI_NOT_INSTALLED,
                          "环境装好了但自检没通过，详见上面的输出")
        steps.append("自检通过")

    return {"steps": steps, "venv": str(venv), "source": str(source),
            "models": str(models), "ready": ready, "reason": reason}


def _venv_has(python: Path, modules: tuple[str, ...]) -> bool:
    """环境里有没有这几个模块。用 find_spec，不真正 import（省几秒）。"""
    script = ("import importlib.util as u,json,sys;"
              f"print(json.dumps([m for m in {list(modules)!r} if u.find_spec(m) is None]))")
    proc = subproc.run([str(python), "-c", script], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)
    if proc.returncode != 0:
        return False
    try:
        return not json.loads(proc.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return False


def status() -> dict:
    """omni 的当前状态，给 `daemon status` 与诊断用。"""
    ready, reason = omni.available()
    python = omni.omni_python()
    models = omni.omni_weights()
    info = {
        "ready": ready,
        "reason": reason,
        "venv": str(python.parent.parent),
        "source": str(omni.omni_source()),
        "models": str(models),
    }
    if python.is_file():
        missing = [m for m in ("torch", "transformers", "easyocr", "ultralytics")
                   if not _venv_has(python, (m,))]
        if missing:
            info["ready"] = False
            info["reason"] = f"缺少依赖：{', '.join(missing)}"
    if not _weights_complete(models):
        info["ready"] = False
        info["reason"] = "权重不完整（缺检测器 / 描述模型 / Florence-2 远程代码之一）"
    return info


def main(argv: list[str] | None = None) -> int:
    """`computer-use setup omni [--force] [--skip-weights]`。"""
    import argparse

    parser = argparse.ArgumentParser(prog="computer-use setup omni")
    parser.add_argument("--force", action="store_true", help="重装依赖（修损坏的环境）")
    parser.add_argument("--skip-weights", action="store_true", help="只装环境，不下权重")
    args = parser.parse_args(argv)

    try:
        result = run_setup(force=args.force, skip_weights=args.skip_weights)
    except CUError as exc:
        print(f"error: {exc.code.value}\n  {exc.message}\n  hint: {exc.hint}", file=sys.stderr)
        return 1

    print("\nomni 环境就绪：")
    for key in ("venv", "source", "models"):
        print(f"  {key}: {result[key]}")
    if result["steps"]:
        print(f"  本次执行: {', '.join(result['steps'])}")
    print("\n用 `computer-use parse --hwnd <h> --session <s>` 验证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
