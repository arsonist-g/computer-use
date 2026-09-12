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
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

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
    "torch", "torchvision",
    "ultralytics==8.3.70",
    TRANSFORMERS_PIN,
    "timm", "einops==0.8.0", "accelerate",
    "huggingface_hub", "easyocr", "supervision==0.18.0",
    "opencv-python-headless", "Pillow",
    # `util/utils.py` 顶层就 `from openai import AzureOpenAI` —— 产品不走它，
    # 但它是模块级导入，不装就 import 不了。它很轻，装比绕开简单。
    "openai",
)

ENV_PYPI_MIRROR = "COMPUTER_USE_OMNI_INDEX_URL"
ENV_HF_MIRROR = "COMPUTER_USE_HF_ENDPOINT"
DEFAULT_HF_MIRROR = "https://hf-mirror.com"


def _log(message: str) -> None:
    print(f"  {message}", flush=True)


def _run(argv: list[str], *, env: dict | None = None, cwd: Path | None = None) -> int:
    proc = subprocess.run(argv, env=env, cwd=str(cwd) if cwd else None,
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
            hint="安装：irm https://astral.sh/uv/install.ps1 | iex",
        )
    return found


def _uv_install(python: Path, packages: tuple[str, ...]) -> None:
    """装依赖。镜像失败时单次回退官方源，并把两次的原委都透出来（DEC-042）。"""
    mirror = os.environ.get(ENV_PYPI_MIRROR)
    base = [_uv(), "pip", "install", "--python", str(python), *packages]
    attempts = []
    if mirror:
        attempts.append(base + ["--index-url", mirror])
    attempts.append(base + ["--index-url", "https://pypi.org/simple"])
    for index, argv in enumerate(attempts):
        if _run(argv) == 0:
            return
        if index + 1 < len(attempts):
            _log("索引不可用，回退 https://pypi.org/simple 重试一次（DEC-042）")
    raise CUError(ErrorCode.OMNI_NOT_INSTALLED,
                  "omni 依赖安装失败。若报 403/404，通常是镜像源不含某个包；"
                  f"可用 {ENV_PYPI_MIRROR} 指定索引后重试。")


def _download_weights(models: Path) -> None:
    """下权重。**必须平铺**到目标目录 —— 这是 DEC-044 记的第三个坑。

    `hf_hub_download(filename="icon_caption/config.json", local_dir=X)` 会把文件
    放到 `X/icon_caption/config.json`（filename 里的目录会被保留），于是权重落在
    嵌套层、远程代码落在平铺层，两边对不上，模型加载失败。
    正确做法是每个文件都显式指定它在目标目录里的**平铺**位置。
    """
    endpoint = os.environ.get(ENV_HF_MIRROR) or ""
    env = dict(os.environ)
    if endpoint:
        env["HF_ENDPOINT"] = endpoint
        _log(f"使用 HuggingFace 镜像 {endpoint}")

    from huggingface_hub import hf_hub_download

    def fetch(repo: str, remote: str, target: Path, revision: str | None = None) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        path = hf_hub_download(repo_id=repo, filename=remote, revision=revision,
                               local_dir=str(target.parent))
        got = Path(path)
        if got.resolve() != target.resolve():
            shutil.move(str(got), str(target))
        _log(f"{target.relative_to(models)}  {target.stat().st_size / 1e6:.1f} MB")

    _log("检测器（YOLOv9-E，来自 PR #37）")
    fetch(WEIGHTS_REPO, DETECTOR_FILE, models / "icon_detect_v3" / "model.pt",
          revision=DETECTOR_REVISION)

    _log("描述模型（Florence-2）")
    caption_dir = models / "icon_caption_florence"
    for name in CAPTION_FILES:
        fetch(WEIGHTS_REPO, f"icon_caption/{name}", caption_dir / name)
    # 远程代码：config.json 的 auto_map 指向它们，缺了就要联网找，离线/内网会失败。
    for name in FLORENCE_REMOTE_FILES:
        fetch(FLORENCE_REPO, name, caption_dir / name)


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

    # ---- 3. 依赖（最小集 + 锁定的 transformers）----
    if force or not _venv_has(python, ("torch", "transformers", "easyocr")):
        _log("安装依赖（最小集，transformers 锁定 4.44.2 —— 见 DEC-044）")
        _uv_install(python, PACKAGES)
        steps.append("安装依赖")
    else:
        _log("依赖已就位")

    # ---- 4. 权重 ----
    if skip_weights:
        _log("按参数跳过权重下载")
    elif not (models / "icon_detect_v3" / "model.pt").is_file() \
            or not (models / "icon_caption_florence" / "config.json").is_file():
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
        check = subprocess.run(
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
    proc = subprocess.run([str(python), "-c", script], capture_output=True,
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
    detector = models / "icon_detect_v3" / "model.pt"
    caption = models / "icon_caption_florence" / "config.json"
    if not detector.is_file() or not caption.is_file():
        info["ready"] = False
        info["reason"] = "权重不完整（缺检测器或描述模型）"
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
