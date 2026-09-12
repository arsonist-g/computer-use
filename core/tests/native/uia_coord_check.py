"""UIA 与检测器的坐标一致性 —— 决定性诊断。

背景：`_merge_uia` 在进程内用同批数据测是 29/29 全匹配，但走完整链路（daemon）
时只匹配上 4/29。差别只剩「两侧数据来自不同时刻」。前几次诊断都缺一个关键检查：
**窗口在两次读取之间有没有动过**。

这个脚本把它补上：连续读数次窗口矩形 + 一次完整解析，看坐标到底在哪个环节错位。
"""

from __future__ import annotations

import ctypes
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path, utf8_console  # noqa: E402

utf8_console()
add_src_to_path()

from cu.config import Config  # noqa: E402
from cu.desktop import uia, win32  # noqa: E402
from cu.desktop.controller import WriteSequenceController  # noqa: E402
from cu.desktop.dpi import ensure_dpi_awareness  # noqa: E402
from cu.desktop.real import RealDesktop  # noqa: E402


def read_md(path: Path) -> tuple[list[dict], str]:
    detector, diag = [], ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("- merge:"):
            diag = line
        if not line.startswith("| ") or "---" in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 7 or cells[1] == "#":
            continue
        parts = [int(v) for v in cells[3].split(",") if v.strip()]
        detector.append({"type": cells[2], "bbox": parts, "interactivity": cells[4] == "y",
                         "content": cells[5], "source": cells[6]})
    return detector, diag


def overlap(a: tuple, b: tuple) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    return (ix * iy) / (min(aw * ah, bw * bh) or 1)


def main() -> int:
    ensure_dpi_awareness()
    ctypes.windll.user32.FindWindowW.restype = ctypes.c_void_p
    hwnd = ctypes.windll.user32.FindWindowW("Notepad", None)
    if not hwnd:
        print("请先开一个记事本")
        return 1
    print(f"hwnd = 0x{hwnd:08X}\n")

    # ---- 1. 窗口稳不稳 ----
    rects = []
    for _ in range(5):
        rects.append(win32.window_rect(hwnd))
        time.sleep(0.15)
    stable = len(set(rects)) == 1
    print(f"[1] 窗口矩形读数 5 次: {rects[0]}" + ("（稳定）" if stable else f" ⚠️ 变动: {rects}"))

    out = Path(tempfile.mkdtemp(prefix="cu-uia-"))
    desktop = RealDesktop(Config.load(), WriteSequenceController())

    # ---- 2. 截图与 UIA 用同一个矩形吗 ----
    shot = desktop.capture(hwnd=hwnd, monitor=None, image_format="png", out_dir=out, seq=1)
    print(f"[2] 截图 {shot.width}x{shot.height}  origin={shot.origin}"
          + ("" if shot.origin == (rects[0][0], rects[0][1]) else " ⚠️ 与窗口左上不一致"))

    result = uia.read_window(hwnd)
    in_image = 0
    uia_norm = []
    for element in result.elements:
        box = list(element.rect)
        box[0] -= shot.origin[0]
        box[1] -= shot.origin[1]
        if 0 <= box[0] <= shot.width and 0 <= box[1] <= shot.height:
            in_image += 1
        uia_norm.append((box[0], box[1], box[2], box[3]))
    print(f"[3] UIA {len(result.elements)} 个 → 落在图内 {in_image} 个")

    rects_after = [win32.window_rect(hwnd) for _ in range(3)]
    print(f"[4] UIA 之后窗口矩形: {rects_after[0]}"
          + ("（未变）" if set(rects_after) == {rects[0]} else " ⚠️ 变了"))

    # ---- 5. 看检测器产出的坐标空间 ----
    parse_result = desktop.parse(hwnd=hwnd, image_path=None, ai=False, out_dir=out, seq=2)
    md = Path(parse_result.path)
    detector, diag = read_md(md)
    print(f"[5] {md.name}")
    print(f"    {diag}")
    omni_boxes = [d["bbox"] for d in detector if d["source"] == "omni"]
    uia_boxes = [d["bbox"] for d in detector if d["source"] == "uia"]
    for label, boxes in (("检测器(omni)", omni_boxes), ("UIA(uia)", uia_boxes)):
        if not boxes:
            print(f"    {label}: （无）")
            continue
        tgt = [(b[0], b[1], b[2] - b[0], b[3] - b[1]) for b in boxes]
        xs = [b[0] for b in tgt] + [b[0] + b[2] for b in tgt]
        ys = [b[1] for b in tgt] + [b[1] + b[3] for b in tgt]
        print(f"    {label}: {len(boxes)} 个, x {min(xs)}~{max(xs)}, y {min(ys)}~{max(ys)}")

    # ---- 6. 用 UIA 的框去匹配检测器的框，看重叠率分布 ----
    if omni_boxes and uia_boxes:
        ratios = []
        for ub in uia_boxes:
            tgt = (ub[0], ub[1], ub[2] - ub[0], ub[3] - ub[1])
            best = max((overlap(tgt, (b[0], b[1], b[2] - b[0], b[3] - b[1]))
                        for b in omni_boxes), default=0.0)
            ratios.append(round(best, 2))
        print(f"[6] 本产出内 UIA→检测器 重叠率: {sorted(ratios, reverse=True)[:12]}")
        print(f"    ≥0.5 的: {sum(1 for r in ratios if r >= 0.5)}/{len(ratios)}")

    print("\n判读：")
    print("  · [1]/[4] 显示窗口在动 → UIA 与截图不同步，减少两者间隔或先固定窗口")
    print("  · [6] 重叠率普遍低但 [5] 的两侧范围都正常 → 合并函数的问题")
    print("  · [6] 高而 source 分布仍偏 omni → 数据没传到 worker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
