"""UIA vs OmniParser 对照实验 —— 同一窗口，两个通道各拿一次，比覆盖率与重叠度。

回答一个具体问题：**把 UIA 作为「准确文本通道」补进 OmniParser 的产出，
值不值得做？**

判据是三条数字，不是印象：
  1. UIA 能给出多少个带文本的元素（覆盖多少界面文字）；
  2. 这些元素的坐标与检测器的框有多少重叠（能否对齐到同一个元素上）；
  3. UIA 拿到的文本，检测器（经 OCR）拿到的是不是错的。

用法：
    .venv/Scripts/python.exe core/tests/native/uia_compare.py --process Notepad
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path, utf8_console  # noqa: E402

utf8_console()
CORE = add_src_to_path()

#: 用 PowerShell 自带的 UIAutomationClient 取 UIA 树。
#: 刻意不引入 pywinauto/frozen 之类：这是评估，不是实现 ——
#: 先确认「值不值得做」，再决定用什么库。
_UIA_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = [System.Windows.Automation.Condition]::TrueCondition
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
foreach ($w in $wins) {
  $pn = (Get-Process -Id $w.Current.ProcessId -ErrorAction SilentlyContinue).ProcessName
  if ($pn -ne $env:UIA_TARGET) { continue }
  if (-not $w.Current.Name) { continue }
  $all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
  for ($i = 0; $i -lt $all.Count; $i++) {
    $e = $all.Item($i)
    try {
      $n = $e.Current.Name
      if (-not $n -or $n.Trim().Length -eq 0) { continue }
      $r = $e.Current.BoundingRectangle
      if ($r.Width -le 0 -or $r.Height -le 0) { continue }
      $act = ''
      foreach ($p in $e.GetSupportedPatterns()) {
        $s = $p.ProgrammaticName -replace 'PatternIdentifiers\.','' -replace 'Pattern$',''
        if ($s -in @('Invoke','Value','Toggle','SelectionItem','ExpandCollapse','RangeValue','Scroll','Text')) { $act = $s; break }
      }
      $obj = [ordered]@{
        type = ($e.Current.ControlType.ProgrammaticName -replace 'ControlType\.','')
        name = $n
        rect = @([int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height)
        action = $act
        hwnd = $w.Current.NativeWindowHandle
      }
      Write-Output ($obj | ConvertTo-Json -Compress)
    } catch {}
  }
}
"""

RESULTS: list[tuple[str, str, str]] = []


def record(label: str, ok: bool, detail: str) -> None:
    RESULTS.append((label, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}\n        {detail}")


def uia_elements(process: str) -> tuple[list[dict], int]:
    """取一个进程的 UIA 元素。返回 (元素列表, 该进程的顶层窗口数)。"""
    env = {**os.environ, "UIA_TARGET": process}
    ts = Path(os.environ["TEMP"]) / "cu-uia-probe.ps1"
    ts.write_text(_UIA_SCRIPT, encoding="utf-8")
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ts)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, env=env,
    )
    elements = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            elements.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return elements, 1 if elements else 0


def overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """交集面积 / 较小者面积。用于判断两个框是否指同一个元素。"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    smaller = min(aw * ah, bw * bh) or 1
    return inter / smaller


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--process", default="Notepad", help="目标进程名（不含 .exe）")
    args = parser.parse_args()

    # ---- 1. UIA 侧 ----
    uia, _ = uia_elements(args.process)
    record("T1 UIA 元素数", bool(uia), f"{args.process}: 带名字且可见的元素 {len(uia)} 个")
    if not uia:
        print("\nUIA 在该进程上拿不到元素 —— 这本身就是结论（自绘/Electron 界面）。")
        return 0
    print("      UIA 样例：", flush=True)
    for e in uia[:8]:
        print(f"        {e['type']:<14} {e['rect']}  act={e.get('action',''):<14} {e['name'][:34]!r}",
              flush=True)

    hwnd = next((e["hwnd"] for e in uia if e.get("hwnd")), None)
    if not hwnd:
        record("T2 定位窗口", False, "UIA 没给出 NativeWindowHandle")
        return 1
    hwnd_str = f"0x{int(hwnd):08X}"

    # ---- 2. 检测器侧（走产品完整链路）----
    pipe = chr(92) * 2 + ".\\pipe\\cu-uia-compare"
    env = {**os.environ, "COMPUTER_USE_PIPE": pipe, "PYTHONIOENCODING": "utf-8"}

    def cu(*a, timeout=1200.0):
        p = subprocess.run([sys.executable, "-m", "cu", *a], cwd=str(CORE), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    code, out, _ = cu("begin", "--agent-hint", "uia-compare")
    session_id = next((line.split(" ", 1)[1].strip() for line in out.splitlines()
                       if line.startswith("session ")), "")
    if not session_id:
        record("T2 建会话", False, out[:200])
        return 1

    t0 = time.monotonic()
    code, out, err = cu("parse", "--hwnd", hwnd_str, "--session", session_id)
    if code != 0:
        record("T3 检测器解析", False, f"exit={code} {err[:300]}")
        return 1
    md_name = out.rsplit("elements=", 1)[0].strip()
    md_path = Path.home() / ".computer-use" / "sessions" / session_id / md_name
    record("T3 检测器解析", md_path.is_file(),
           f"耗时 {time.monotonic()-t0:.0f}s · {out[:90]}")

    # ---- 3. 读检测器产出，算重叠 ----
    # markdown 行形如 `| 1 | text | 16,60,58,84 | n | YIt |`，
    # split('|') 后首尾各有一个空串，因此：c[1]=# c[2]=type c[3]=bbox c[4]=inter c[5]=content。
    boxes: list[tuple[tuple[int, int, int, int], str]] = []
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
        boxes.append(((x1, y1, x2 - x1, y2 - y1), cells[5]))

    matched = 0
    exact = 0
    better = 0
    samples = []
    for e in uia:
        ex, ey, ew, eh = e["rect"]
        best = max((overlap((ex, ey, ew, eh), b[0]), b[1]) for b in boxes) if boxes \
            else (0.0, "")
        if best[0] >= 0.5:
            matched += 1
            uia_text = e["name"]
            ocr_text = best[1]
            # 「UIA 更准」的粗判据：OCR 文本里不含 UIA 文本的主要片段。
            key = "".join(ch for ch in uia_text if ch.isalnum() or ord(ch) > 127)[:6]
            if key and key not in ocr_text:
                better += 1
                if len(samples) < 8:
                    samples.append((uia_text[:30], ocr_text[:30]))
            if uia_text.strip() == ocr_text.strip():
                exact += 1

    print(f"\n      UIA {len(uia)} 个元素 · 检测器 {len(boxes)} 个框", flush=True)
    record("T4 两通道能对齐上（重叠 ≥50%）", matched > 0,
           f"{matched}/{len(uia)} 个 UIA 元素在检测器产出里找到了对应框")
    record("T5 UIA 文本比 OCR 更准", better > 0,
           f"{better}/{matched} 个对齐上的元素，UIA 文本不在 OCR 文本里")
    record("T6 文本完全相同", True, f"{exact}/{matched} 个逐字相同")
    if samples:
        print("      UIA 文本 vs 检测器的 OCR 文本：", flush=True)
        for u, o in samples:
            print(f"        UIA={u!r}", flush=True)
            print(f"        OCR={o!r}", flush=True)

    cu("session", "end", "--session", session_id)
    cu("daemon", "stop")

    print("\n===== 对照结论 =====", flush=True)
    for label, verdict, detail in RESULTS:
        print(f"[{verdict:4}] {label}  —— {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
