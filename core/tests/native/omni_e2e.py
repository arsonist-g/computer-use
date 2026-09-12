"""omni 通道的真机端到端测试 —— 从 CLI 一路到 OmniParser。

前面几层已经各自验过：
  - `ipc_smoke.py` / `cli_e2e.py`：CLI ↔ daemon
  - `desktop_smoke.py`：桌面层
  - worker 的自检：markdown 渲染 + 契约 + 传输层（**不依赖 torch**）

这个脚本补的是最后一段、也是最容易断的一段：**base 环境 → omni 环境**。
它跨了两个解释器，所以不可能用单元测试覆盖 —— 只能真跑。

跑法：
    .venv/Scripts/python.exe core/tests/native/omni_e2e.py [--hwnd 0x...] [--image PATH]

不指定目标时，自动挑一个面积最大的可见窗口截图再解析。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path, local_pipe, utf8_console  # noqa: E402

utf8_console()
CORE = add_src_to_path()

#: 独立管道，不动用户真实的 daemon。
# 用项目自己的常量，避免手写反斜杠被 shell/heredoc 吃掉（踩过两次）。
PIPE = local_pipe()
RESULTS: list[tuple[str, str, str]] = []


def record(label: str, ok: bool, detail: str) -> None:
    RESULTS.append((label, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}\n        {detail}", flush=True)


def cu(*args: str, timeout: float = 1200.0) -> tuple[int, str, str]:
    env = {**os.environ, "COMPUTER_USE_PIPE": PIPE, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-m", "cu", *args], cwd=str(CORE), env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def pick_window() -> str | None:
    """挑一个面积最大的可见窗口当目标。"""
    code, out, err = cu("--json", "windows")
    if code != 0:
        print("   windows 失败:", err[:200], flush=True)
        return None
    try:
        rows = json.loads(out)["result"]["windows"]
    except (json.JSONDecodeError, KeyError):
        return None
    usable = [r for r in rows if not r["is_minimized"] and r["rect"][2] * r["rect"][3] > 100_000
              and not r["elevated"]]
    if not usable:
        return None
    best = max(usable, key=lambda r: r["rect"][2] * r["rect"][3])
    print(f"   目标: {best['hwnd']} {best['process']} {best['rect']}", flush=True)
    return best["hwnd"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hwnd", default=None)
    parser.add_argument("--image", default=None)
    parser.add_argument("--ai", action="store_true", help="同时试 AI 优化（需配好端点）")
    args = parser.parse_args()

    # ---- 1. 通道就绪检查（廉价，不拉起子进程）----
    from cu.desktop import omni
    ready, reason = omni.available()
    record("T1 omni 环境就绪", ready,
           f"python={omni.omni_python()} · worker={omni.worker_script()}"
           + ("" if ready else f" · 原因: {reason}"))
    if not ready:
        print("\nomni 未就绪，后续用例无法进行。", flush=True)
        return 1

    # ---- 2. 会话 ----
    code, out, _ = cu("begin", "--agent-hint", "omni-e2e")
    session_id = ""
    for line in out.splitlines():
        if line.startswith("session "):
            session_id = line.split(" ", 1)[1].strip()
    if code != 0 or not session_id:
        record("T2 建会话", False, f"exit={code} out={out[:200]}")
        return 1
    record("T2 建会话", True, f"session={session_id}")

    target_hwnd = args.hwnd or (None if args.image else pick_window())
    if args.image is None and target_hwnd is None:
        record("T3 选取目标", False, "没有可用的窗口，也没有给 --image")
        return 1

    # ---- 3. 真实解析（这一步是整条链路的验收点）----
    if args.image:
        cmd = ["parse", "--image", args.image, "--session", session_id]
    else:
        cmd = ["parse", "--hwnd", target_hwnd, "--session", session_id]

    started = time.monotonic()
    code, out, err = cu(*cmd)
    elapsed = time.monotonic() - started
    if code != 0:
        record("T3 解析（CLI→daemon→worker→OmniParser）", False,
               f"exit={code} 耗时={elapsed:.0f}s · {err[:400]}")
        return 1

    # 输出形如：`<文件名>  elements=<n>`。
    # **文件名里可能含空格**（窗口标题原样进名字），所以不能用 `split()[0]` ——
    # 那条路只在标题不含空格时对，是典型的「在开发机上刚好能跑」。
    # 用正则从行尾锚定 `elements=` 反推文件名。
    match = re.match(r"^(?P<name>.+?)\s+elements=(?P<count>\d+)\s*$", out.splitlines()[0]) \
        if out else None
    md_name = match.group("name").strip() if match else ""
    element_count = int(match.group("count")) if match else 0
    record("T3 解析（CLI→daemon→worker→OmniParser）",
           element_count > 0 and bool(md_name),
           f"耗时 {elapsed:.0f}s · 元素 {element_count} 个 · {out[:160]}")

    # ---- 4. markdown 真的落盘且内容可读 ----
    sessions_root = Path.home() / ".computer-use" / "sessions" / session_id
    md_path = sessions_root / md_name if md_name else None
    if md_path and md_path.is_file():
        text = md_path.read_text(encoding="utf-8")
        header_ok = "| # | type | bbox | interactivity | content |" in text
        sample = [ln for ln in text.splitlines() if ln.startswith("| ") and "---" not in ln]
        data_rows = max(0, len(sample) - 1)      # 减掉表头行
        # 抽几行看坐标：必须是**像素**而不是 0~1 的比例。
        # 判据是「最大值明显大于 2」—— 比例坐标恒 ≤ 1，像素坐标在 4K 上到几千。
        boxes = []
        for line in sample[1:]:
            cells = [c.strip() for c in line.split("|")]
            if len(cells) > 3:
                parts = [p for p in cells[3].split(",") if p.strip()]
                if len(parts) == 4:
                    try:
                        boxes.append([int(p) for p in parts])
                    except ValueError:
                        pass
        # 比例坐标会被 `_to_elements` 乘上图像尺寸；若那条换算丢了，
        # 所有框会挤在左上角（max < 2 或极少数值远小于图像维度）。
        pixel_like = bool(boxes) and max(max(b) for b in boxes) > 100
        record("T4 markdown 落盘且坐标为像素",
               header_ok and data_rows >= 1 and pixel_like,
               f"{md_name[:60]} · {len(text)} 字符 · 数据行 {data_rows} · "
               f"表头={'有' if header_ok else '无'} · 坐标像像素={pixel_like} · "
               f"框数={len(boxes)}")
        print("      前 4 行数据：", flush=True)
        for line in sample[:5]:
            print("        " + line[:110], flush=True)
    else:
        record("T4 markdown 落盘且坐标为像素", False, f"文件不存在：{md_path}")

    cu("session", "end", "--session", session_id)
    cu("daemon", "stop")

    print("\n===== omni 端到端结果 =====", flush=True)
    failed = [r for r in RESULTS if r[1] == "FAIL"]
    for label, verdict, detail in RESULTS:
        print(f"[{verdict:4}] {label}  —— {detail}")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
