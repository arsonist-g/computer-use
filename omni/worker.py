"""cu-omni worker —— 把一张图片解析成结构化数据，可选再用多模态端点优化描述。

**独立环境，与 base 零代码共享**（架构 §1.5 第 3 条 / DEC-037）。
本目录下的代码**不得 import `cu` 包里的任何东西** —— 它跑在 `venv-omni` 里，
那边的解释器里没有 `cu`。两边的关系只有管道与文件，且**不传图像字节流**：
入参是图片路径，出参是 markdown 路径。

协议与 daemon↔客户端同形（NDJSON over 管道），换传输不换格式（架构 §1.4）。
方法表见 `spec/backend-design/computer-use/api-contract.md` §2.1 的 `omni.parse`。

为什么单独一个进程、单独一个环境：OmniParser 要拖进 torch / transformers /
paddleocr，三到六 GB。把它挡在 base 环境之外，是为了让 CLI 的每一次冷启动
不必付这份 import 代价（DEC-039）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

# 同环境的兄弟模块。**不能**改成 `from cu.xxx` —— 那边没有这个名字。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from markdown import ContractViolation, merge_descriptions, to_markdown, validate_optimized  # noqa: E402

OMNI_NOT_INSTALLED = "omni_not_installed"
OMNI_FAILED = "omni_failed"
VLM_FAILED = "vlm_failed"


class WorkerError(Exception):
    def __init__(self, code: str, message: str, hint: str = "", detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.detail = detail or {}

    def as_rpc(self) -> dict:
        return {"code": self.code, "message": self.message,
                "hint": self.hint, "detail": self.detail}


# ---------------------------------------------------------------------------
# 检测器
# ---------------------------------------------------------------------------


def _load_detector():
    """加载 OmniParser。未安装时**显式报错**，不静默降级（DEC-002）。"""
    try:
        from ultralytics import YOLO  # noqa: F401
    except ImportError as exc:
        raise WorkerError(
            OMNI_NOT_INSTALLED,
            f"OmniParser 未安装（缺少 {exc.name}）",
            hint="运行 `computer-use setup omni`",
        ) from exc
    raise WorkerError(
        OMNI_NOT_INSTALLED,
        "OmniParser 尚未接入本进程",
        hint="检测器接线未完成；在它完成前，`parse` 一律显式报错而不是返回空结果",
    )


def detect(image_path: str) -> dict:
    """对一张图跑检测，返回 `{"elements": [...]}`。

    这里刻意抛错而不是返回空结果：**「没有元素」与「检测器不可用」必须是两件事**。
    返回空结果会让 AI 认为界面上什么都没有，然后据此做决策。
    """
    if not Path(image_path).exists():
        raise WorkerError(OMNI_FAILED, f"图片不存在：{image_path}")
    detector = _load_detector()
    return detector(image_path)


# ---------------------------------------------------------------------------
# 多模态优化（DEC-011）
# ---------------------------------------------------------------------------

#: 优化请求的提示词。**只改描述，不动位置** —— 这一条必须在提示词里说死，
#: 并且在返回后由 `validate_optimized` 强制校验。提示词是请求，校验是保证。
OPTIMIZE_PROMPT = """You are given a screenshot and a list of UI elements detected in it.

Your job is to correct the description of each element. The detection model locates
elements well but describes them poorly. You understand what things are, but you do
not know precisely where they are.

Hard rules:
1. Do NOT change any bbox. Copy every bbox exactly as given.
2. Do NOT add elements. You may drop an element you judge to be noise, but you may
   not invent one.
3. Return the same JSON shape: {"elements": [{"bbox": [...], "content": "..."}]}

Element list:
"""


def optimize_with_vlm(original: dict, image_path: str, config: dict) -> dict:
    """调多模态端点纠正描述。失败时抛 `vlm_failed`，**不重试**。

    不重试的理由（架构 §3）：重试会放大 token 成本，而用户可以自己重跑一次；
    更重要的是，失败时原始结构化数据仍然完好，重试的收益小于它的代价。
    """
    import base64
    import urllib.error
    import urllib.request

    base_url = (config.get("base_url") or "").rstrip("/")
    api_key = config.get("api_key") or ""
    model = config.get("model_name") or ""
    if not base_url or not model:
        raise WorkerError(VLM_FAILED, "多模态端点未配置",
                          hint="`computer-use config set vlm.base_url <url>` 与 `vlm.model_name`")

    try:
        image_b64 = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
    except OSError as exc:
        raise WorkerError(VLM_FAILED, f"图片读取失败：{exc}") from exc

    body = json.dumps({
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": OPTIMIZE_PROMPT + json.dumps(original, ensure_ascii=False)},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            ],
        }],
        "temperature": 0,
    }).encode("utf-8")

    request = urllib.request.Request(
        f"{base_url}/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise WorkerError(VLM_FAILED, f"端点返回 {exc.code}",
                          hint="检查 base_url / api_key；原始结构化数据仍可用",
                          detail={"status": exc.code, "body": detail}) from exc
    except Exception as exc:  # noqa: BLE001
        raise WorkerError(VLM_FAILED, f"端点调用失败：{type(exc).__name__}: {exc}",
                          hint="检查 base_url / api_key；原始结构化数据仍可用") from exc

    try:
        content = payload["choices"][0]["message"]["content"]
        optimized = json.loads(_strip_code_fence(content))
        validate_optimized(original, optimized)
    except ContractViolation as exc:
        raise WorkerError(VLM_FAILED, f"优化结果违约：{exc}",
                          hint="原始结构化数据仍可用（DEC-011 的契约已挡住这次改动）") from exc
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise WorkerError(VLM_FAILED, f"端点响应无法解析：{type(exc).__name__}") from exc
    return optimized


def _strip_code_fence(text: str) -> str:
    """模型常把 JSON 包在 ```json 围栏里。剥掉，别让它变成解析失败。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines)
    return stripped


# ---------------------------------------------------------------------------
# 命令面
# ---------------------------------------------------------------------------


def handle_parse(params: dict) -> dict:
    """`omni.parse`：图片 → markdown 文件。返回文件路径与元素数，**不返回内容**。

    不返回内容是有意的（PM §6.4）：一个内容多的界面会产生几百个元素，
    把它们塞进调用方的上下文，比让它按需读文件贵得多。
    """
    image_path = params.get("image")
    out_dir = params.get("out_dir")
    file_name = params.get("file_name")
    if not image_path or not out_dir or not file_name:
        raise WorkerError(OMNI_FAILED, "omni.parse 需要 image / out_dir / file_name")

    detected = detect(image_path)

    model_name = None
    payload = detected
    ai = bool(params.get("ai"))
    if ai:
        optimized = optimize_with_vlm(detected, image_path, params.get("vlm") or {})
        payload = merge_descriptions(detected, optimized)
        model_name = (params.get("vlm") or {}).get("model_name")

    target = Path(out_dir) / file_name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        to_markdown(payload, source=image_path, model=model_name), encoding="utf-8"
    )
    return {"path": str(target), "element_count": len(payload.get("elements") or []),
            "model_name": model_name}


ROUTES = {"omni.parse": handle_parse, "system.ping": lambda _p: {"ok": True}}


def dispatch(request: dict) -> dict:
    method = request.get("method")
    handler = ROUTES.get(method)
    if handler is None:
        raise WorkerError(OMNI_FAILED, f"未知方法：{method}")
    return handler(request.get("params") or {})


def _dump_line(message: dict) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"


def serve_stdio() -> int:
    """stdio 传输。daemon 也可以用管道接它，换传输不换格式（架构 §1.4）。"""
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        request_id = None
        try:
            request = json.loads(raw)
            request_id = request.get("id")
            result = dispatch(request)
            sys.stdout.write(_dump_line({"jsonrpc": "2.0", "id": request_id, "result": result}))
        except WorkerError as exc:
            sys.stdout.write(_dump_line({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32000, "message": exc.message, "data": exc.as_rpc()}}))
        except Exception as exc:  # noqa: BLE001 —— 边界：任何异常都要变成响应，不能断连
            traceback.print_exc(file=sys.stderr)
            sys.stdout.write(_dump_line({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32000, "message": f"{type(exc).__name__}: {exc}",
                "data": {"code": OMNI_FAILED, "hint": "看 worker 的 stderr"}}}))
        sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cu-omni", description="Computer-Use 解析 worker")
    parser.add_argument("--stdio", action="store_true", help="走 stdio NDJSON（默认）")
    parser.add_argument("--selftest", action="store_true", help="不依赖检测器自检")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    return serve_stdio()


def _selftest() -> int:
    """不依赖 torch 的自检：验证 markdown 渲染与契约校验这两块纯逻辑。"""
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sample = {"elements": [
        {"type": "icon", "bbox": [10, 20, 30, 40], "interactivity": True, "content": "设置"},
        {"type": "text", "bbox": [50, 60, 150, 80], "interactivity": False, "content": "标题|含竖线\n含换行"},
    ]}
    text = to_markdown(sample, source="shot.png", model="example-vlm")
    assert "| # | type | bbox | interactivity | content |" in text, text
    assert "10,20,30,40" in text, text
    assert "\\|" in text, "竖线必须被转义，否则表格结构被破坏"
    assert "\n含换行" not in text, "换行必须被消掉"

    # 契约校验：新增元素必须被拒
    try:
        validate_optimized(sample, {"elements": sample["elements"] + [
            {"type": "icon", "bbox": [1, 1, 2, 2], "content": "凭空造的"}]})
    except ContractViolation:
        pass
    else:
        raise AssertionError("新增元素未被契约拦下")

    # 契约校验：不动 bbox 的优化必须通过
    optimized = {"elements": [
        {"bbox": [50, 60, 150, 80], "content": "标题"},
        {"bbox": [10, 20, 30, 40], "content": "偏好设置"},
    ]}
    validate_optimized(sample, optimized)
    merged = merge_descriptions(sample, optimized)
    assert merged["elements"][0]["content"] == "偏好设置"
    assert merged["elements"][0]["bbox"] == [10, 20, 30, 40], "bbox 必须保留检测器的值"

    print("cu-omni 自检通过：markdown 渲染 + 契约校验")
    return 0


if __name__ == "__main__":
    sys.exit(main())
