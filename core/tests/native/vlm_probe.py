"""VLM 端点最小探针 —— 分清「鉴权 / 模型 / 视觉能力 / 路径」四类失败。

产品侧的报错只说「远端关闭连接」，那对应好几种原因。这个脚本逐项排除：

  T1 `/models` 可达性与模型列表        → 直接区分「鉴权失败」与「网络/路径失败」
  T2 纯文本 chat 调用能通              → 端点本体可用、模型名有效
  T3 同样的调用换成**带图**内容块        → 模型是否接受 image_url

T3 是最关键的一条：本项目 `--ai` 是视觉任务，纯文本模型会在这一项上失败。
失败的方式不会是一句「我不支持图片」，而更可能是服务端直接断开 ——
那正是产品侧看到的现象。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "native"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from _bootstrap import utf8_console  # noqa: E402

utf8_console()

from cu.config import Config  # noqa: E402

#: 1x1 的白色 PNG —— 够小，排除「请求体太大」这个变量。
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def call(url: str, key: str, body: dict | None, timeout: int = 60) -> tuple[str, str]:
    """返回 (结论, 详情)。结论是 OK / HTTP n / 异常类型。"""
    headers = {"Authorization": f"Bearer {key}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        request = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return "OK", response.read().decode("utf-8", "replace")[:300]
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}", exc.read().decode("utf-8", "replace")[:300]
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__, str(exc)[:200]


def main() -> int:
    cfg = Config.load()
    base = cfg.vlm.base_url.rstrip("/")
    key = cfg.vlm.api_key
    model = cfg.vlm.model_name
    if not (base and model):
        print("config 里 vlm.base_url / vlm.model_name 未配好")
        return 1
    print(f"base_url={base}  model={model}  key={'已配置' if key else '空'}\n")

    # ---- T1 /models ----
    for suffix in ("", "/v1"):
        verdict, detail = call(f"{base}{suffix}/models", key, None)
        print(f"[T1] {base}{suffix}/models -> {verdict}")
        if verdict == "OK":
            try:
                ids = sorted(m.get("id", "") for m in (json.loads(detail).get("data") or []))
                print(f"     模型 {len(ids)} 个：{', '.join(ids[:15])}{' ...' if len(ids) > 15 else ''}")
                if model not in ids:
                    print(f"     ⚠️ 配置里的 `{model}` 不在列表里")
            except json.JSONDecodeError:
                print(f"     响应非 JSON：{detail[:120]}")
        else:
            print(f"     {detail[:200]}")
    print()

    # ---- T2 / T3：纯文本 vs 带图 ----
    chat_url = f"{base}/chat/completions"
    text_body = {"model": model, "temperature": 0,
                 "messages": [{"role": "user", "content": "reply with the single word: ok"}]}
    image_body = {"model": model, "temperature": 0, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "What color is this image? One word."},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{TINY_PNG_B64}"}},
    ]}]}

    for label, body in (("纯文本", text_body), ("带图 image_url", image_body)):
        verdict, detail = call(chat_url, key, body, timeout=90)
        print(f"[T2/T3] {label:16} -> {verdict}")
        print(f"        {detail[:260]}")
        print()

    print("判读：")
    print("  · 纯文本通、带图不通  -> 模型不支持视觉。`--ai` 需要换成视觉模型。")
    print("  · 两个都 RemoteDisconnected -> 端点/网络层的问题，与视觉无关。")
    print("  · /models 401/403     -> 鉴权；若同时 chat 能通，说明该端点未开放 /models。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
