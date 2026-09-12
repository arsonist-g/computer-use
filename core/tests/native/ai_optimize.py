"""`--ai` 路径的真机测试 —— 检测器 + 多模态端点优化。

这条路径此前**一次都没跑过**：`optimize_with_vlm` 的实现与 `validate_optimized`
契约校验都写好了，但没有端到端验证过。而 Q-016 的实测显示描述端质量参差
（`unanswerable` 这类幻觉），恰好说明这条路的重要性比设计时更高。

跑法：
    .venv/Scripts/python.exe core/tests/native/ai_optimize.py [--hwnd 0x...]

需要先在 `~/.computer-use/config.json` 里配好 `vlm.base_url` / `vlm.api_key` /
`vlm.model_name`（用 `computer-use config set vlm.<字段> <值>`）。
**端点必须是视觉模型** —— 纯文本模型收不了 `image_url` 内容块。
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


def parse_md(md_path: Path) -> list[tuple[tuple[int, int, int, int], str, str]]:
    """读产出，返回 [(bbox, type, content)]。"""
    rows = []
    for line in md_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| ") or "---" in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 7 or cells[1] == "#":
            continue
        parts = [p for p in cells[3].split(",") if p.strip()]
        if len(parts) != 4:
            continue
        try:
            x1, y1, x2, y2 = (int(p) for p in parts)
        except ValueError:
            continue
        rows.append(((x1, y1, x2, y2), cells[2], cells[5]))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hwnd", default=None)
    args = parser.parse_args()

    from cu.config import Config
    cfg = Config.load()
    usable = bool(cfg.vlm.base_url and cfg.vlm.model_name)
    record("T1 VLM 已配置", usable,
           f"base_url={cfg.vlm.base_url} · model={cfg.vlm.model_name} · "
           f"api_key={'已配置' if cfg.vlm.api_key else '空'}")
    if not usable:
        print("\n先在 config 里配好 vlm.base_url / vlm.model_name 再来。")
        return 1

    # ---- 会话 + 目标 ----
    code, out, _ = cu("begin", "--agent-hint", "ai-e2e")
    session_id = next((line.split(" ", 1)[1].strip() for line in out.splitlines()
                       if line.startswith("session ")), "")
    if not session_id:
        record("T2 建会话", False, out[:200])
        return 1

    hwnd = args.hwnd
    if not hwnd:
        code, out, err = cu("--json", "windows")
        try:
            rows = json.loads(out)["result"]["windows"]
        except (json.JSONDecodeError, KeyError):
            record("T2 选目标窗口", False, err[:200])
            return 1
        usable_windows = [w for w in rows if not w["is_minimized"] and not w["elevated"]
                          and w["rect"][2] * w["rect"][3] > 50_000]
        if not usable_windows:
            record("T2 选目标窗口", False, "没有可用窗口")
            return 1
        hwnd = max(usable_windows, key=lambda w: w["rect"][2] * w["rect"][3])["hwnd"]
    record("T2 建会话与选目标", True, f"session={session_id} hwnd={hwnd}")

    sessions_root = Path.home() / ".computer-use" / "sessions" / session_id

    # ---- 基线：不带 --ai ----
    t0 = time.monotonic()
    code, out, err = cu("parse", "--hwnd", hwnd, "--session", session_id)
    base_secs = time.monotonic() - t0
    if code != 0:
        record("T3 基线解析（无 --ai）", False, f"exit={code} {err[:300]}")
        return 1
    base_name = re.match(r"^(?P<n>.+?)\s+elements=(?P<c>\d+)\s*$", out).group("n").strip()
    base_rows = parse_md(sessions_root / base_name)
    record("T3 基线解析（无 --ai）", bool(base_rows),
           f"耗时 {base_secs:.0f}s · 元素 {len(base_rows)} 个 · {base_name[:60]}")

    # ---- 带 --ai ----
    t0 = time.monotonic()
    code, out, err = cu("parse", "--hwnd", hwnd, "--ai", "--session", session_id)
    ai_secs = time.monotonic() - t0
    if code != 0:
        detail = err[:500]
        # 端点失败要能一眼看清是哪一类：鉴权 / 模型不存在 / 不支持图片。
        record("T4 --ai 优化", False, f"exit={code} 耗时={ai_secs:.0f}s\n        {detail}")
        cu("session", "end", "--session", session_id)
        cu("daemon", "stop")
        _summary()
        return 1

    ai_name = re.match(r"^(?P<n>.+?)\s+elements=(?P<c>\d+)\s*$", out).group("n").strip()
    ai_rows = parse_md(sessions_root / ai_name)
    record("T4 --ai 优化", bool(ai_rows),
           f"耗时 {ai_secs:.0f}s（比基线多 {ai_secs - base_secs:.0f}s）· "
           f"元素 {len(ai_rows)} 个 · {ai_name[:60]}")

    # ---- 契约：bbox 必须冻结（DEC-011） ----
    base_boxes = sorted(b[0] for b in base_rows)
    ai_boxes = sorted(b[0] for b in ai_rows)
    same_boxes = base_boxes == ai_boxes
    record("T5 bbox 冻结（DEC-011）", same_boxes,
           f"基线 {len(base_boxes)} 个 bbox / --ai {len(ai_boxes)} 个 · "
           f"集合{'完全一致' if same_boxes else '**不一致**（契约被破坏）'}")

    # ---- 描述是否真的被改动 ----
    base_by_box = {b[0]: b[2] for b in base_rows}
    changed = [(b[0], base_by_box.get(b[0], ""), b[2]) for b in ai_rows
               if base_by_box.get(b[0], "") != b[2]]
    record("T6 描述被优化", len(changed) > 0,
           f"{len(changed)}/{len(ai_rows)} 个元素的描述发生了变化")
    if changed:
        print("      优化样例（前 8 个）：", flush=True)
        for box, before, after in changed[:8]:
            print(f"        {box}", flush=True)
            print(f"          优化前: {before[:60]!r}", flush=True)
            print(f"          优化后: {after[:60]!r}", flush=True)

    # ---- 幻觉是否减少（Q-016 的那个问题） ----
    base_halluc = sum(1 for b in base_rows if b[2].strip().lower() in
                      ("unanswerable", "", "n/a"))
    ai_halluc = sum(1 for b in ai_rows if b[2].strip().lower() in
                    ("unanswerable", "", "n/a"))
    record("T7 无效描述减少", ai_halluc <= base_halluc,
           f"基线无效 {base_halluc} 个 · --ai 无效 {ai_halluc} 个 "
           f"（Q-016 记录的 `unanswerable` 幻觉）")

    cu("session", "end", "--session", session_id)
    cu("daemon", "stop")
    return _summary()


def _summary() -> int:
    print("\n===== --ai 路径结果 =====", flush=True)
    failed = [r for r in RESULTS if r[1] == "FAIL"]
    for label, verdict, detail in RESULTS:
        print(f"[{verdict:4}] {label}  —— {detail}")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
