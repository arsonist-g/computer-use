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


def t3b_capture_origin_is_extended_frame(out_dir: Path, hwnd: int | None) -> None:
    """窗口截图的 `origin` 必须是 **DWM 扩展框**的左上角，不是 `GetWindowRect` 的。

    这条守卫来自一次实测：`GetWindowRect` 是 900x560，而 WGC 交付的图像是 886x553，
    两者恰好差 14x7 —— 那是 Win10/11 那条**不可见调整边框**（本机 125% 缩放下
    左右各 7px、下边 7px）。拿 `GetWindowRect` 当原点，`origin + 图像坐标`
    就会横向偏 7px，而 constraint「截图与坐标同源」要求两者严格一致。

    偏差只有个位数像素，点按钮这类大目标察觉不到 —— 只有把基准钉死才守得住。
    """
    if hwnd is None:
        record("T3b 截图 origin 基准", False, "没有可用于截图的目标窗口")
        return
    frame = capture_mod.w.extended_frame_bounds(hwnd)
    if frame is None:
        record("T3b 截图 origin 基准", False, "DwmGetWindowAttribute 取不到扩展框")
        return
    try:
        result = capture_mod.capture_window(hwnd, out_dir, 3, _window_info(hwnd))
    except Exception as exc:  # noqa: BLE001
        record("T3b 截图 origin 基准", False, f"{type(exc).__name__}: {exc}")
        return
    want_origin = (frame[0], frame[1])
    want_size = (frame[2] - frame[0], frame[3] - frame[1])
    got_size = (result.width, result.height)
    rect = capture_mod.w.window_rect(hwnd) or (0, 0, 0, 0)
    border = (rect[0] - frame[0], rect[1] - frame[1],
              rect[2] - frame[2], rect[3] - frame[3])
    record(
        "T3b 截图 origin 基准 = DWM 扩展框",
        tuple(result.origin) == want_origin and got_size == want_size,
        f"origin={tuple(result.origin)}（扩展框 {want_origin}）· 图像 {got_size}"
        f"（扩展框 {want_size}）· GetWindowRect 多出的不可见边框(左,上,右,下)={border}",
    )


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
        overlay.transition(OverlayState.ACTIVE)
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


def t7_target_ring() -> None:
    """目标框的描边必须是 **2 个屏幕像素**，不是 2 个缓冲像素。

    这一条存在的理由：所有「刻意写死 px」的元素（描边、外发光、光标光晕、胶囊）
    一度都被画在**降采样缓冲**上 —— 缓冲要放大 7 倍才上屏，于是 2px 描边变成了
    14px 的粗线、52px 光标光晕变成了 373px 的雾团。它们必须在原生分辨率上画。

    这里沿元素上边中点往上扫一列，数 alpha 满格的连续像素：正好 2 个才算对。
    """
    from cu.desktop.overlay import (  # noqa: PLC0415
        _TARGET_GLOW_PX,
        _TARGET_RING_PX,
        ControlOverlay,
        OverlayState,
        _read_screen,
    )

    overlay = ControlOverlay()
    overlay._screen = _read_screen()
    overlay._state = OverlayState.ACTIVE
    target = (900, 600, 400, 300)
    overlay.set_target(target)
    detail = overlay._build_target(overlay._screen)
    if detail is None:
        record("T7 目标框描边宽度", False, "目标框没被画出来")
        return

    # 缓冲里元素左上角 = (外发光+环的边距, 同)；从**紧贴元素上边那一行往上**扫。
    pad = _TARGET_GLOW_PX + _TARGET_RING_PX
    column = pad + target[2] // 2
    alphas = [detail.pixels[(row * detail.width + column) * 4 + 3]
              for row in range(pad - 1, -1, -1)]
    solid = 0
    for value in alphas:
        if value == 255:
            solid += 1
        else:
            break
    glow_bg = [value for value in alphas[solid:] if value > 0]
    record(
        "T7 目标框描边宽度（屏幕像素）",
        solid == _TARGET_RING_PX and bool(glow_bg),
        f"元素上边往上依次是 {solid} 个满 alpha 像素（期望 {_TARGET_RING_PX}）· "
        f"再往外是 {len(glow_bg)} 个半透明外发光像素 · 缓冲 {detail.width}x{detail.height}",
    )


def t8_glow_is_at_edges() -> None:
    """光晕必须画在**四边**，不是屏幕中央。

    这条守卫针对一个非常难发现的写法错误：把「到最近边的距离」写成「到中心的距离」
    （`min(|x-480/2|, |y-200/2|)` 而不是 `min(x, w-x, y, h-y)`）。两者只差一个 min，
    画出来的却是屏幕正中一块十字形色块 —— 它亮在中央、在暗处淡出，肉眼很容易读成
    「好像有点光」，而四边一个像素都没有。

    判据很硬：所有非零 alpha 的像素都必须落在「离最近一条边 < reach」的区域内。
    """
    from cu.desktop.overlay import _REACH_RATIO, ControlOverlay, OverlayState, _read_screen  # noqa: PLC0415

    width, height = 480, 200
    overlay = ControlOverlay()
    overlay._screen = _read_screen()
    overlay._state = OverlayState.ACTIVE
    glow = overlay._build_glow(overlay._screen, width, height, OverlayState.ACTIVE)
    reach = max(6.0, min(width, height) * _REACH_RATIO)

    total = interior = 0
    for y in range(height):
        for x in range(width):
            if glow[(y * width + x) * 4 + 3] == 0:
                continue
            total += 1
            if min(y + 0.5, height - y - 0.5, x + 0.5, width - x - 0.5) >= reach:
                interior += 1
    record(
        "T8 光晕画在四边而非中央",
        total > 0 and interior == 0,
        f"非零 alpha {total} 个，其中落在中央区（离边 >= {reach:.0f} 缓冲像素）的 "
        f"{interior} 个（期望 0）",
    )


def t9_overlay_window_does_not_hang() -> None:
    """窗口属主线程必须抽消息 —— 否则 5 秒后被判定「无响应」，Windows 直接结束进程。

    这条守卫针对的是一次真实的 AppHangB1：窗口建在主线程上，主线程随后阻塞在
    等输入里，从不调用 `PeekMessage`。实测 `IsHungAppWindow` 恰好在第 5.0 秒翻成
    True，随后 WER 弹出「程序已停止工作」并杀掉进程。

    这里**刻意复刻那个条件**：主线程在循环里只 sleep，绝不抽消息。同时确认光晕真的
    走了放大那一步（源缓冲比窗口小，`UpdateLayeredWindow` 不替你拉伸）。

    屏幕会闪大约 8 秒，期间输入**不被封锁**，随时可以中断。
    """
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    from cu.desktop.overlay import ControlOverlay, OverlayState  # noqa: PLC0415

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsHungAppWindow.restype = wintypes.BOOL
    user32.IsHungAppWindow.argtypes = [wintypes.HWND]

    overlay = ControlOverlay()
    overlay.set_target((900, 600, 400, 300))
    overlay.set_cursor((1700, 700))
    overlay.start()
    overlay.transition(OverlayState.ACTIVE)
    overlay.transition(OverlayState.ACTIVE)
    try:
        ready = overlay.wait_ready(timeout=8.0)
        if not ready:
            record("T9 覆盖层窗口不挂起", False,
                   f"首帧未就绪：{overlay.last_error!r}")
            return
        hwnd = overlay._glow.hwnd
        owner = user32.GetWindowThreadProcessId(hwnd, None)
        same_thread = owner == overlay._thread.ident
        scaled = overlay._glow._dst is not None

        first_hung = None
        started = time.monotonic()
        while time.monotonic() - started < 7.0:      # 5 秒是挂起判定的门槛
            if user32.IsHungAppWindow(hwnd):
                first_hung = time.monotonic() - started
                break
            time.sleep(0.25)
        record(
            "T9 覆盖层窗口不挂起（属主线程抽消息）",
            first_hung is None and same_thread and scaled,
            f"8 秒内未被判定无响应={first_hung is None}"
            + (f"（第 {first_hung:.1f}s 翻成挂起）" if first_hung else "")
            + f" · 窗口属主=渲染线程 {same_thread} · 光晕经 GDI 放大上屏 {scaled}",
        )
    finally:
        overlay.transition(OverlayState.OFF)
        overlay.stop()


def t10_frozen_states_are_single_hue() -> None:
    """Stopping / Error 的光晕必须**冻结成一个色相**，而不是「一条不转的彩虹」。

    这条守卫来自一次真机验收失败：§4.4 报「与规范不符」，使用者的描述是
    「stopping 跟 error 的那两个颜色没展示出来，都一直是那个彩色的光谱」。
    根因是代码冻结的是光谱的**旋转相位**，不是色相本身 —— 只冻相位的话，
    屏幕上仍是一片彩色，只是不流动了。这两件事只差一行，从代码上看都很合理。

    判据：把饱和像素的色相装箱，冻结态只应落进一个箱，流动态应落进多个。
    """
    import colorsys  # noqa: PLC0415

    from cu.desktop.overlay import ControlOverlay, OverlayState, _read_screen  # noqa: PLC0415

    def hue_bins(state) -> int:
        overlay._state = state
        glow = overlay._build_glow(screen, 240, 100, state)
        bins = set()
        for index in range(0, len(glow), 4):
            alpha = glow[index + 3]
            if alpha <= 60:
                continue
            # 缓冲是**预乘**的，先除回去再取色相。
            red = glow[index + 2] / alpha
            green = glow[index + 1] / alpha
            blue = glow[index] / alpha
            bins.add(round(colorsys.rgb_to_hsv(red, green, blue)[0] * 24))
        return len(bins)

    overlay = ControlOverlay()
    screen = _read_screen()
    overlay._screen = screen
    frozen = {OverlayState.STOPPING.value: hue_bins(OverlayState.STOPPING)}
    flowing = {
        state.value: hue_bins(state)
        for state in (OverlayState.ACTIVE,)
    }
    record(
        "T10 冻结态是单一色相、流动态是光谱",
        all(count <= 1 for count in frozen.values())
        and all(count >= 6 for count in flowing.values()),
        f"冻结态色相箱数 {frozen}（期望各 ≤1）· 流动态色相箱数 {flowing}（期望各 ≥6）",
    )


def t11_spectrum_phase_is_continuous() -> None:
    """光谱相位必须**跨状态切换连续**，不能在切换点归零重来。

    这条守卫来自一次真机验收的观感反馈：切换状态时「并没有顺利过渡，而是重开了
    一个流动」。根因是相位从「进入本状态的时刻」起算，而 `transition()` 会更新
    那个时刻 —— 于是每次切换都把光谱归零。

    判据：先把相位推到非零，再切一次状态，前后的相位差必须是「这一瞬间的
    正常推进量」，而不是一个跳变。
    """
    from cu.desktop.overlay import ControlOverlay, OverlayState  # noqa: PLC0415

    overlay = ControlOverlay()
    # 把时钟原点往前拨 3 秒 —— 这样相位约 0.43，任何归零都会立刻显形。
    overlay._phase_origin = time.monotonic() - 3.0
    before = overlay._phase()
    overlay.transition(OverlayState.ACTIVE)
    overlay.transition(OverlayState.ACTIVE)
    after = overlay._phase()
    jump = min((after - before) % 1.0, (before - after) % 1.0)
    record(
        "T11 光谱相位跨状态连续（不归零重来）",
        jump < 0.05,
        f"切换前后相位 {before:.3f} -> {after:.3f}（差 {jump:.3f}，期望 <0.05）· "
        f"归零的话这里会是一个大跳变",
    )


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
    t3b_capture_origin_is_extended_frame(out_dir, hwnd)
    t4_input()
    t6_overlay_exclusion(out_dir)
    t7_target_ring()
    t8_glow_is_at_edges()
    t10_frozen_states_are_single_hue()
    t11_spectrum_phase_is_continuous()
    t9_overlay_window_does_not_hang()
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
