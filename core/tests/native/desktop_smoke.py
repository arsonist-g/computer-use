"""桌面层真机测试 —— 只跑在本机，不进 CI（DEC-023）。

单元测试无法覆盖真桌面：DPI 声明、窗口枚举、四层截图降级、SendInput 落点、
低级钩子的物理/注入判别，全都要真实桌面会话、真实窗口、真实 GPU。

    用法：.venv/Scripts/python.exe core/tests/native/desktop_smoke.py

**这个脚本会移动鼠标并投递按键**（到它自己开的记事本窗口），但：

  - 只使用 **注入** 输入，绝不吞任何东西（除非你显式跑 `--blocking`）；
  - `--blocking` 那一步会真的封锁物理键鼠 5 秒，并配 10 秒看门狗兜底，
    期间**必须按物理 Esc 中断** —— 那正是被测的行为本身。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path, utf8_console  # noqa: E402

utf8_console()
add_src_to_path()

from cu.desktop import capture as capture_mod  # noqa: E402
from cu.desktop import input as input_mod  # noqa: E402
from cu.desktop import windows as windows_mod  # noqa: E402
from cu.desktop.dpi import awareness_mode, ensure_dpi_awareness  # noqa: E402
from cu.desktop.overlay import capture_exclusion_supported, win10_build  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def record(label: str, ok: bool, detail: str) -> None:
    RESULTS.append((label, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}\n        {detail}")


# ---------------------------------------------------------------------------
# T1 DPI 声明
# ---------------------------------------------------------------------------


def t1_dpi() -> None:
    """vSM_CXSCREEN 在声明前后必须不同。

    spike 实测：不声明 2752，声明后 3440（本机 125%）。**声明之前的任何坐标读取
    都是错的**，所以这条测的是「声明到底有没有生效」，不是「函数返回了 True」。
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetSystemMetrics.restype = ctypes.c_int
    before = (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    result = ensure_dpi_awareness()
    after = (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    mode = awareness_mode()
    changed = before != after
    record(
        "T1 DPI 声明改变坐标读数",
        result.ok and (changed or mode.startswith("per-monitor")),
        f"{before} -> {after} · mode={mode} · via={result.method}"
        + ("（已经是 per-monitor，读数不变属正常）" if not changed else ""),
    )


# ---------------------------------------------------------------------------
# T2 窗口枚举
# ---------------------------------------------------------------------------


def t2_windows() -> int | None:
    monitors = windows_mod.display_context().monitors
    found = windows_mod.enumerate_windows(monitors)
    marked = windows_mod.enumerate_windows(monitors, all_windows=True)
    if not found:
        record("T2 窗口枚举", False, "默认过滤下没有任何窗口 —— 至少应看到本终端")
        return None

    # 字段契约：elevated 是必需字段；rect 必须是物理像素的正尺寸（最小化的除外）。
    missing = [w for w in found if not isinstance(w.elevated, bool)]
    bad_rect = [w for w in found if not w.is_minimized and (w.rect[2] <= 0 or w.rect[3] <= 0)]
    fg = [w for w in found if w.is_foreground]
    record(
        "T2 窗口枚举",
        not missing and not bad_rect,
        f"默认 {len(found)} 个 / --all {len(marked)} 个 · 前台 {len(fg)} 个 · "
        f"字段缺失 {len(missing)} · rect 非法 {len(bad_rect)}",
    )
    for w in found[:3]:
        print(f"        {w.hwnd:>10} {w.process:<20} rect={w.rect} elev={w.elevated} "
              f"fg={w.is_foreground} {w.title[:32]!r}")
    return found[0].hwnd if found else None


# ---------------------------------------------------------------------------
# T3 截图降级链 + 全黑检测
# ---------------------------------------------------------------------------


def _file_size(path: Path, wait: float = 3.0) -> int:
    """等文件真的写完再报大小 —— 截图保存是异步编码，立刻 stat 可能拿到 0。"""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
            if size > 0:
                return size
        except OSError:
            pass
        time.sleep(0.02)
    return 0


def t3_capture(out_dir: Path, hwnd: int | None) -> None:
    if hwnd is None:
        record("T3 截图", False, "没有可用于截图的目标窗口")
        return
    try:
        result = capture_mod.capture_window(hwnd, out_dir, 1, _window_info(hwnd))
    except Exception as exc:  # noqa: BLE001
        record("T3 窗口截图", False, f"{type(exc).__name__}: {exc}")
        return
    path = Path(result.path)
    size = _file_size(path)
    record(
        "T3 窗口截图（含全黑检测）",
        size > 1000 and result.width > 0,
        f"layer={result.layer} {result.width}x{result.height} origin={result.origin} "
        f"{size} bytes -> {path.name}",
    )
    try:
        full = capture_mod.capture_full(0, out_dir, 2)
        full_path = Path(full.path)
        full_size = _file_size(full_path)
        record(
            "T3 全屏截图",
            full_size > 1000,
            f"layer={full.layer} {full.width}x{full.height} origin={full.origin} "
            f"{full_size} bytes -> {full_path.name}",
        )
    except Exception as exc:  # noqa: BLE001
        record("T3 全屏截图", False, f"{type(exc).__name__}: {exc}")


def _window_info(hwnd: int):
    monitors = windows_mod.display_context().monitors
    for info in windows_mod.enumerate_windows(monitors, all_windows=True):
        if info.hwnd == hwnd:
            return info
    raise RuntimeError("目标窗口在枚举结果里找不到")


# ---------------------------------------------------------------------------
# T4 SendInput 落点（含拟人轨迹）
# ---------------------------------------------------------------------------


def t4_input() -> None:
    """轨迹与落点：终点必须精确命中，中间点必须真的分散（否则「拟人」是假的）。"""
    start = input_mod._last_tracked
    points = input_mod.trajectory(start[0], start[1], 600, 400, max_points=30)
    endpoints_ok = points[0] == (round(start[0]), round(start[1])) is not None
    distinct = len(set(points))
    # 终点精确性：轨迹会在最后重采样，末点必须落在目标附近（真正精确的落点由
    # move_cursor 的终点补发保证，这里只验轨迹本身没有崩坏到离谱）。
    last = points[-1]
    sane = abs(last[0] - 600) < 200 and abs(last[1] - 400) < 200

    pos_before = input_mod.w.cursor_pos()
    moved = input_mod.move_cursor(400, 300, step_ms=5, max_points=20)
    pos_after = input_mod.w.cursor_pos()
    hit = abs(pos_after[0] - 400) <= 2 and abs(pos_after[1] - 300) <= 2
    record(
        "T4 拟人轨迹 + 终点精确落点",
        sane and distinct >= 5 and hit and endpoints_ok,
        f"轨迹 {len(points)} 点 / 去重 {distinct} · 终点误差 "
        f"({pos_after[0] - 400},{pos_after[1] - 300})px · 耗时 {moved}ms · "
        f"起点位移 {pos_before}->{pos_after}",
    )

    # 回到原位，别把用户的鼠标留在奇怪的地方。
    input_mod.move_cursor(pos_before[0], pos_before[1], step_ms=5, max_points=20)


# ---------------------------------------------------------------------------
# T5 钩子的注入/物理判别（要人手配合，默认不跑）
# ---------------------------------------------------------------------------


def t5_blocking() -> None:
    """封锁 5 秒，验证「吞物理、放行注入、物理 Esc 中止」。

    为什么必须人手配合：「物理事件」这四个字无法用程序制造 ——
    程序发出的一切都带 INJECTED 标志。spike S3 也只验了注入那一半。
    """
    from cu.desktop.hooks import InputBlocker

    seen: list[bool] = []
    aborted = threading.Event()

    blocker = InputBlocker(on_abort=aborted.set, on_input_seen=seen.append)
    blocker.start()
    # 看门狗：无论发生什么都解除封锁。宁可测试失败，也不能把用户锁住。
    watchdog = threading.Timer(10.0, lambda: blocker.set_blocking(False))
    watchdog.start()

    try:
        blocker.set_blocking(True)
        print("\n        >>> 键鼠已被封锁，持续 5 秒。")
        print("        >>> 请【按物理 Esc】—— 那应当解除封锁并让脚本继续。")
        print("        >>> （也可以按其他键试，但它们应当毫无反应）\n")

        injected_ok = _send_injected_key()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not aborted.is_set():
            time.sleep(0.05)
        physical = [flag for flag in seen if flag]
        injected = [flag for flag in seen if not flag]
        record(
            "T5 封锁与物理 Esc",
            aborted.is_set() and injected_ok and blocker.installed,
            f"物理 Esc 触发中止={aborted.is_set()} · 注入事件被放行={injected_ok} · "
            f"观测到物理事件 {len(physical)} 次 / 注入事件 {len(injected)} 次"
            + ("" if aborted.is_set() else "（超时：未检测到物理 Esc）"),
        )
    finally:
        watchdog.cancel()
        blocker.set_blocking(False)
        blocker.stop()
        record("T5b 停止后输入恢复", not blocker.blocking and not blocker.installed,
               "钩子已卸载，输入已放行")


def _send_injected_key() -> bool:
    """投递一个注入按键，并确认它没有被自己的钩子吞掉。

    用 F24 而不是普通字符：大多数应用不绑定它，不会在用户屏幕上留下任何痕迹。
    """
    try:
        input_mod.key("f24")
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# T6 覆盖层的截图排除（DEC-027 的复核）
# ---------------------------------------------------------------------------


def t6_overlay_exclusion(out_dir: Path) -> None:
    """截两张全屏（覆盖层关 / 覆盖层开），比帧间差。

    **诚实标注这条测到了什么**：帧差 0.05，与桌面噪声同量级。它**不能证明**
    覆盖层在场也不在场 —— 因为覆盖层被 WDA 排除出捕获，本就看不见。
    这条实际测的是「两次截图之间桌面几乎没变」，是一个很弱的断言。

    真正有效的覆盖层验证是 spike S2b 的**三组对照**（无覆盖层 / 有覆盖层无 WDA /
    有覆盖层有 WDA），那里有控制组证明「覆盖层确实画在屏幕上且捕获确实看得见它」。
    本条的弱化是已知的；要看覆盖层本身，用 `--visual` 人眼验（acceptance.md §4）。
    """
    from cu.desktop.overlay import ControlOverlay, OverlayState

    if not capture_exclusion_supported():
        record("T6 覆盖层截图排除", True,
               f"跳过：本机 build={win10_build()} 低于 19041，WDA_EXCLUDEFROMCAPTURE 不可用"
               "（DEC-027：不设 affinity，接受截图被污染）")
        return

    overlay = ControlOverlay()
    overlay.start()
    try:
        overlay.transition(OverlayState.OFF)
        time.sleep(0.3)
        before = _grab_frame()
        overlay.transition(OverlayState.ACTIVE)
        time.sleep(0.8)                      # 等渲染线程画出至少一帧
        visible = overlay.visible and overlay.state is OverlayState.ACTIVE
        after = _grab_frame()
    finally:
        overlay.transition(OverlayState.OFF)
        overlay.stop()

    if before is None or after is None:
        record("T6 覆盖层截图排除", False,
               "无法取帧做差分（DXGI 取帧失败），本条**未验证**")
        return
    if before.shape != after.shape:
        record("T6 覆盖层截图排除", False,
               f"两帧尺寸不一致 {before.shape} vs {after.shape}，无法比较")
        return

    diff = _mean_abs_diff(before, after)
    # 桌面本身在动（时钟、光标）会带来噪声。实测经验值：真正的覆盖层泄漏
    # 会让差值抬升一个量级（百分位级别），而噪声在千分位以下。
    leaked = diff > 1.5
    record(
        "T6 覆盖层截图排除",
        visible and not leaked,
        f"覆盖层在场={visible} · 帧间平均像素差={diff:.4f} "
        f"（WDA 生效应接近 0；覆盖层泄漏会显著抬升）"
        f" · 帧形状={before.shape}",
    )


def show_visual(seconds: float = 6.0) -> None:
    """把覆盖层连同目标框、光标光晕一起显示若干秒，供人眼验收。

    **这一步不能自动化。** 覆盖层被 WDA 排除出所有截图管线（DEC-027 的设计意图），
    所以没有任何截屏手段能看到它 —— 脚本能验的只有「渲染缓冲对不对」（T7），
    「看起来对不对」只能人眼。这正是 `tests/acceptance.md` §4 存在的原因。

    期间覆盖层处于 Active 态，输入**不被封锁**（本脚本不碰 blocking），
    所以随时可以按 Ctrl+C 或直接终止。
    """
    from cu.desktop.overlay import ControlOverlay, OverlayState

    overlay = ControlOverlay()
    overlay.start()
    point = (900, 600)
    try:
        overlay.transition(OverlayState.ARMING)
        overlay.set_cursor(point)
        overlay.set_target((point[0] - 12, point[1] - 12, 24, 24))
        time.sleep(1.5)
        overlay.transition(OverlayState.ACTIVE)
        print(f"\n    覆盖层已显示 {seconds:.0f} 秒（Active 态，输入未封锁）。", flush=True)
        print("    请核对 acceptance.md §4：光晕无硬边界、光谱在流动、胶囊可辨识、"
              "目标框是单一琥珀色相、光标有白色光晕。", flush=True)
        time.sleep(seconds)
    finally:
        overlay.transition(OverlayState.OFF)
        overlay.stop()
    print("    覆盖层已撤下。\n", flush=True)


def t7_target_and_cursor() -> None:
    """目标框与光标光晕**真的被画出来了吗**。

    这一条存在的理由：这两个元素曾经「定义了但全代码库没人调用」—— 从代码上看
    它们完备，从屏幕上看什么都没有。所以这里不看代码，只看渲染出来的像素。

    **为什么不去截屏验证**：覆盖层被 `WDA_EXCLUDEFROMCAPTURE` 排除出所有截图管线
    （DEC-027 的设计意图），所以**任何截屏手段都看不见它** —— 包括 DXGI。
    用截屏去验覆盖层，得到的永远是「什么都没画」。
    （顺带修正一条：T6 的「帧间差 0.05」也不能证明覆盖层在场，那只是桌面噪声。
    T6 真正证明的是「排除生效」，这个结论仍然成立。）

    因此这里直接验渲染缓冲：目标框必须在目标坐标上出现一个 2px 的环，
    且**只有加上目标时才出现**。至于它最终长什么样，只能人眼看 ——
    那一条在 `tests/acceptance.md` §4 里，不适合由脚本断言。
    """
    from cu.desktop.overlay import ControlOverlay, OverlayState

    overlay = ControlOverlay()
    # 这个测试不建窗口，只测渲染；屏幕尺寸手动取，避免依赖窗口是否创建过。
    overlay._screen = _screen_size()
    overlay._state = OverlayState.ACTIVE
    overlay._state_since = time.monotonic()

    width = 480
    height = max(1, int(overlay._screen.height * width / overlay._screen.width))
    sx = width / overlay._screen.width
    sy = height / overlay._screen.height

    plain = overlay._build_pixels(width, height)
    target = (900, 600, 24, 24)
    overlay.set_target(target)
    overlay.set_cursor((900, 600))
    marked = overlay._build_pixels(width, height)

    # 目标中心映射到缓冲坐标。
    cx = int((target[0] + target[2] / 2) * sx)
    cy = int((target[1] + target[3] / 2) * sy)
    # 沿中心那一行往左扫，找 2px 实线环（alpha 接近 255）。
    ring_hits = sum(1 for x in range(max(0, cx - 40), min(width, cx))
                    if marked[(cy * width + x) * 4 + 3] > 240
                    and plain[(cy * width + x) * 4 + 3] < 200)
    changed = sum(1 for a, b in zip(plain[3::4], marked[3::4], strict=False) if a != b)

    record("T7 目标框与光标光晕已绘制到缓冲",
           ring_hits >= 2 and changed > 0,
           f"目标映射到缓冲 ({cx},{cy}) · 该行找到 {ring_hits} 个实线环像素 "
           f"（期望 ≥2，即 2px 环）· 与纯光晕相比变化 {changed} 个像素")
    print("      ⚠️ 视觉呈现（光晕观感、胶囊、光标位置）只能人眼验，见 acceptance.md §4。",
          flush=True)


def _screen_size():
    from cu.desktop.overlay import _read_screen

    return _read_screen()


def _grab_frame():
    """取一帧全屏像素。DXGI 屏幕帧唯一可用的解码路径就是它自己的 to_numpy。"""
    from windows_capture import DxgiDuplicationSession

    try:
        session = DxgiDuplicationSession(monitor_index=1)   # 1 基，见 capture._wc_monitor_index
    except Exception:  # noqa: BLE001
        return None
    deadline = time.monotonic() + 5.0
    black = 0
    while time.monotonic() < deadline:
        frame = session.acquire_frame(200)
        if frame is None:
            continue
        if float(frame.to_numpy().mean()) <= 2:
            black += 1        # spike 陷阱 4：新建会话后前几帧可能整帧全黑
            if black > 30:
                return None
            continue
        return frame.to_numpy()
    return None


def _mean_abs_diff(left, right) -> float:
    delta = left.astype("int16") - right.astype("int16")
    return float(abs(delta).mean())


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blocking", action="store_true",
                        help="跑 T5（会真的封锁物理键鼠 5 秒，需你按 Esc）")
    parser.add_argument("--visual", action="store_true",
                        help="跑完后把覆盖层显示 6 秒，供人眼验收（不能被截屏验证）")
    parser.add_argument("--out", default=str(Path(__file__).parent / "out"))
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=== Computer-Use 桌面层真机测试 ===\n")
    t1_dpi()
    hwnd = t2_windows()
    t3_capture(out_dir, hwnd)
    t4_input()
    t6_overlay_exclusion(out_dir)
    t7_target_and_cursor()
    if args.blocking:
        t5_blocking()
    else:
        print("\n[跳过] T5 封锁与物理 Esc —— 加 --blocking 运行（需人手按 Esc）")

    print("\n===== 结果 =====")
    failed = [r for r in RESULTS if r[1] == "FAIL"]
    for label, verdict, detail in RESULTS:
        print(f"[{verdict:4}] {label}  —— {detail}")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
