"""UIA ↔ 检测器的对齐诊断 —— 为什么只匹配上少数元素。

终链路跑出来只有 4/29 匹配，而单独测两边都正常。这类问题靠读代码看不出来，
必须把两侧的框铺开来对。这个脚本不修任何东西，只打印：

  1. 两侧的坐标范围（先确认坐标系转换没做错）；
  2. 每个 UIA 元素与「最接近的检测器框」的重叠率，按重叠率排序；
  3. 阈值上下的分布 —— 判断是「阈值太高」还是「框根本没对上」。
"""

from __future__ import annotations

import ctypes
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path, utf8_console  # noqa: E402

utf8_console()
add_src_to_path()

from cu.desktop import uia, win32  # noqa: E402
from cu.desktop.dpi import ensure_dpi_awareness  # noqa: E402


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
    rect = win32.window_rect(hwnd)
    print(f"窗口矩形 = {rect}")

    sessions = sorted((Path.home() / ".computer-use" / "sessions").iterdir(),
                      key=lambda p: p.name)
    latest = None
    for session in reversed(sessions):
        found = list(session.glob("*omni*.md"))
        if found:
            latest = found[0]
            break
    if latest is None:
        print("没有找到解析产出，先跑一次 `computer-use parse --hwnd ...`")
        return 1
    print(f"产出 = {latest.name}")

    detector: list[tuple[tuple[int, int, int, int], str]] = []
    for line in latest.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| ") or "---" in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 7 or cells[1] == "#":
            continue
        parts = [p for p in cells[3].split(",") if p.strip()]
        if len(parts) != 4:
            continue
        x1, y1, x2, y2 = (int(p) for p in parts)
        detector.append(((x1, y1, x2 - x1, y2 - y1), cells[5]))

    result = uia.read_window(hwnd)
    print(f"UIA {len(result.elements)} 个元素 · 检测器 {len(detector)} 个框")
    if not result.elements or not detector:
        return 0

    origin_match = re.search(r"-(-?\d+)x(-?\d+)-\d+\.md$", latest.name)
    origin = (int(origin_match.group(1)), int(origin_match.group(2))) if origin_match else (0, 0)
    print(f"从文件名读出的 origin = {origin}（应与窗口矩形左上一致）")

    print("\n两侧坐标范围（**归一化到图像坐标后**）：")
    uia_boxes = [(e.rect[0] - origin[0], e.rect[1] - origin[1], e.rect[2], e.rect[3])
                 for e in result.elements]
    for label, boxes in (("UIA", uia_boxes), ("检测器", [b[0] for b in detector])):
        xs = [b[0] for b in boxes] + [b[0] + b[2] for b in boxes]
        ys = [b[1] for b in boxes] + [b[1] + b[3] for b in boxes]
        print(f"  {label:6} x {min(xs):>5} ~ {max(xs):>5}   y {min(ys):>5} ~ {max(ys):>5}")

    print("\n每个 UIA 元素的最佳匹配（按重叠率排序）：")
    pairs = []
    for element, box in zip(result.elements, uia_boxes, strict=False):
        best = max(((overlap(box, b[0]), b[0], b[1]) for b in detector), default=(0, None, ""))
        pairs.append((best[0], element, best[1], best[2]))
    pairs.sort(key=lambda x: -x[0])
    for ratio, element, box, text in pairs[:28]:
        print(f"  {ratio:5.2f}  {str(element.rect):<26} {element.control_type:<10} "
              f"{element.name[:22]!r:<24} -> {str(box):<26} {text[:18]!r}")

    above = sum(1 for r, *_ in pairs if r >= 0.5)
    print(f"\n重叠率 ≥0.5 的: {above}/{len(pairs)}；0.2~0.5 的: "
          f"{sum(1 for r, *_ in pairs if 0.2 <= r < 0.5)}")
    print("判读：若大多数 <0.2，是框根本没对上（坐标系或检测质量）；"
          "若集中在 0.3~0.5，是阈值偏高。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
