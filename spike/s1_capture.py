"""S1 —— 验证 windows-capture 的截图能力。

回答三个问题：
  Q1a 能否按 HWND 截到单个窗口？
  Q1b 目标窗口被完全遮挡后，还能不能截到它自己的内容？（关键：主路径的成立前提）
  Q1c DXGI Desktop Duplication 是否可用？（独占全屏的兜底路径）

方法：建两个纯色 tkinter 窗口，绿窗完全盖住品红窗。
  截到品红  -> 截的是窗口自身内容，遮挡无关       => 通过
  截到绿色  -> 截的是屏幕区域，遮挡会污染结果     => 不通过
  近全黑    -> 黑帧，该路径不可用                 => 不通过
"""
import ctypes
import threading
from pathlib import Path

import tkinter as tk

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

TARGET_RGB = (255, 0, 255)   # 品红
COVER_RGB = (0, 200, 0)      # 绿


def root_hwnd(widget: tk.Misc) -> int:
    """Tk 的 winfo_id() 给的是客户区窗口，取其顶层祖先才是真正的 HWND。"""
    hwnd = widget.winfo_id()
    return ctypes.windll.user32.GetAncestor(hwnd, 2)  # GA_ROOT


def grab(window_hwnd=None, monitor_index=None, out_path=None, timeout=8.0):
    """截一帧存盘，返回 (bgr_ndarray | None, note)。

    注意 API 差异（实测）：WGC 的 Frame 只有 save_as_image / convert_to_bgr / frame_buffer，
    没有 to_bgr —— to_bgr 属于 DXGI 的 frame。
    """
    import cv2
    from windows_capture import WindowsCapture

    box = {}

    cap = WindowsCapture(
        cursor_capture=False,
        draw_border=False,          # 关掉 WGC 的黄框
        window_hwnd=window_hwnd,
        monitor_index=monitor_index,
    )

    @cap.event
    def on_frame_arrived(frame, control):
        try:
            box["size"] = (frame.width, frame.height)
            frame.save_as_image(str(out_path))
        except Exception as exc:                      # noqa: BLE001
            box["err"] = repr(exc)
        control.stop()

    @cap.event
    def on_closed():
        box.setdefault("closed", True)

    t = threading.Thread(target=cap.start, daemon=True)
    t.start()
    t.join(timeout)
    if "err" in box:
        return None, f"frame error: {box['err']}"
    if out_path is None or not Path(out_path).exists():
        return None, "no frame within timeout"
    img = cv2.imread(str(out_path))
    if img is None:
        return None, "saved file unreadable"
    return img, f"ok {box.get('size')}"


def classify(img, expect_rgb):
    """判断图像主体是哪种颜色。返回 (label, 说明)。"""
    import numpy as np

    h, w = img.shape[:2]
    # 取中心 20% 区域，避开窗口边框
    cy0, cy1 = int(h * 0.4), int(h * 0.6)
    cx0, cx1 = int(w * 0.4), int(w * 0.6)
    roi = img[cy0:cy1, cx0:cx1].reshape(-1, 3).astype(float)
    b, g, r = roi.mean(axis=0)          # OpenCV 是 BGR
    mean = (r, g, b)

    if max(mean) < 25:
        return "BLACK", f"黑帧 mean_rgb=({r:.0f},{g:.0f},{b:.0f})"

    want_r, want_g, want_b = expect_rgb
    d_want = ((r - want_r) ** 2 + (g - want_g) ** 2 + (b - want_b) ** 2) ** 0.5
    # 与另一个已知颜色比距离，判断更接近谁
    other = COVER_RGB if expect_rgb == TARGET_RGB else TARGET_RGB
    d_other = ((r - other[0]) ** 2 + (g - other[1]) ** 2 + (b - other[2]) ** 2) ** 0.5

    who = "期望色" if d_want < d_other else "干扰色"
    return ("MATCH" if d_want < d_other else "WRONG"), (
        f"mean_rgb=({r:.0f},{g:.0f},{b:.0f}) 距离: 期望={d_want:.0f} 干扰={d_other:.0f} -> {who}"
    )


def main():
    root = tk.Tk()
    root.title("S1-coordinator")
    root.geometry("300x80+20+20")

    target = tk.Toplevel(root)
    target.title("S1-TARGET")
    target.geometry("400x300+200+200")
    target.configure(bg="#ff00ff")
    tk.Label(target, text="TARGET", bg="#ff00ff", fg="#ffffff",
             font=("Segoe UI", 24)).pack(expand=True)

    cover = tk.Toplevel(root)
    cover.title("S1-COVER")
    cover.geometry("400x300+200+200")     # 与 target 完全重合
    cover.configure(bg="#00c800")
    tk.Label(cover, text="COVER", bg="#00c800", fg="#ffffff",
             font=("Segoe UI", 24)).pack(expand=True)
    cover.lift()
    cover.focus_force()

    results = []

    def step_capture_window():
        hwnd = root_hwnd(target)
        path = OUT / "s1_window.png"
        img, note = grab(window_hwnd=hwnd, out_path=path)
        if img is None:
            results.append(("Q1a/Q1b 按 HWND 截被遮挡窗口", "FAIL", note))
        else:
            label, detail = classify(img, TARGET_RGB)
            verdict = "PASS" if label == "MATCH" else "FAIL"
            results.append(
                ("Q1a/Q1b 按 HWND 截被遮挡窗口", verdict,
                 f"hwnd=0x{hwnd:X} {detail} -> {path.name}")
            )
        root.after(150, step_capture_monitor)

    def step_capture_monitor():
        path = OUT / "s1_monitor.png"
        img, note = grab(monitor_index=1, out_path=path)
        if img is None:
            results.append(("Q1a' 按显示器截全屏", "FAIL", note))
        else:
            h, w = img.shape[:2]
            # 盖窗位于屏幕 (200,200)-(600,500)，取其中一点；scaling 由 WGC 处理
            sx, sy = 400, 350
            px = img[min(sy, h - 1), min(sx, w - 1)]
            b, g, r = [int(v) for v in px]
            seen_cover = abs(r - 0) < 60 and abs(g - 200) < 60 and abs(b - 0) < 60
            results.append(
                ("Q1a' 按显示器截全屏", "PASS" if seen_cover else "WARN",
                 f"{w}x{h} 屏幕点({sx},{sy}) rgb=({r},{g},{b}) 见盖窗={seen_cover} -> {path.name}")
            )
        root.after(150, step_dxgi)

    def step_dxgi():
        try:
            from windows_capture import DxgiDuplicationSession
            s = DxgiDuplicationSession()
            f = s.acquire_frame(timeout_ms=1000)
            if f is None:
                results.append(("Q1c DXGI Desktop Duplication", "FAIL", "1000ms 内无帧"))
            else:
                import cv2
                arr = f.to_bgr()
                path = OUT / "s1_dxgi.png"
                cv2.imwrite(str(path), arr)
                results.append(("Q1c DXGI Desktop Duplication", "PASS",
                                f"{arr.shape[1]}x{arr.shape[0]} -> {path.name}"))
        except Exception as exc:                          # noqa: BLE001
            results.append(("Q1c DXGI Desktop Duplication", "FAIL", repr(exc)))
        root.after(150, finish)

    def finish():
        print("\n===== S1 结果 =====")
        for name, verdict, detail in results:
            print(f"[{verdict:4}] {name}\n         {detail}")
        root.destroy()

    root.after(1200, step_capture_window)   # 等窗口画完
    root.mainloop()


if __name__ == "__main__":
    main()
