"""验收清单 §6（输入行为）的自动化落地 —— 靶子是脚本自己起的记事本。

对应 `core/tests/acceptance.md` §6 的 6.1 ~ 6.14。

这一节原本全是「目视」项，但它们其实都有**客观读数**：

| 项 | 读数从哪来 |
|---|---|
| 6.1 移动点不被合并 | 移动期间高频采样 `GetCursorPos`，看去重后的落点数与最大跳距 |
| 6.2 轨迹自然 | 同一串采样：到弦的最大垂距（不像直线）+ 方向反转占比（不像抖动） |
| 6.3 中文输入 | 打完 Ctrl+A / Ctrl+C，把剪贴板读回来逐字比对 |
| 6.5 双击选词 | `EM_POSFROMCHAR` 定位到词首像素，双击后把选中内容读回来 |
| 6.6 拖拽 | 拖右边框，量窗口矩形的前后差 |
| 6.7 滚轮方向 | 编辑控件的 `EM_GETFIRSTVISIBLELINE` 前后差 |
| 6.8/6.9/6.10 | 命令返回的错误码 |
| 6.15 抢前台 | 发键前后的 `GetForegroundWindow`，加上靶子里的选区读回 |

只有 6.2 的「自然」是审美判断，这里用两个可量化代理替代目视，并在结论里写明。

跑法：
    .venv/Scripts/python.exe core/tests/native/acceptance_input.py

**期间会多次真实移动鼠标、投递按键，并短暂封锁输入**（每次写命令都会）。
靶子是脚本自己起的记事本，全程只对它操作；跑完关掉它，并把你的剪贴板还回去。
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _winutil as W  # noqa: E402
from _bootstrap import add_src_to_path, local_pipe, utf8_console  # noqa: E402

utf8_console()
CORE = add_src_to_path()

PIPE = local_pipe()
RESULTS: list[tuple[str, str, str]] = []
SESSION = ""


def record(item: str, verdict: str, detail: str) -> None:
    RESULTS.append((item, verdict, detail))
    print(f"[{verdict:4}] §{item}  {detail}", flush=True)


def cu(*args: str, timeout: float = 90.0) -> tuple[int, str, str]:
    env = {**os.environ, "COMPUTER_USE_PIPE": PIPE, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-m", "cu", *args], cwd=str(CORE), env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def cu_json(*args: str, timeout: float = 90.0) -> dict:
    _code, out, _err = cu("--json", *args, timeout=timeout)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {}


def do(*args: str, describe: str, keep: bool = True) -> tuple[int, str, str]:
    """发一条写命令（自动带 session / describe）。

    **默认带 `--continue`**：一次验收里的连续操作应当跑在**同一条保持窗口**里 ——
    每条都结束序列的话，每条都要重走一遍前摇，覆盖层刚武装就退场，看起来就像
    「工具接管了屏幕」这件事根本没发生过。只有收尾那条才传 `keep=False`（`--end`）。
    """
    flag = "--continue" if keep else "--end"
    return cu(*args, "--session", SESSION, "--describe", describe, flag)


# ---------------------------------------------------------------------------
# 6.1 / 6.2 拟人轨迹：不合并、不直线、不抖动
# ---------------------------------------------------------------------------


def _sample_cursor(duration: float, out: list[tuple[int, int]]) -> None:
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        out.append(W.cursor_pos())
        time.sleep(0.001)


def check_6_1_and_6_2(inside: tuple[int, int]) -> None:
    start = inside
    end = (inside[0] + 620, inside[1] + 260)
    code, _out, err = do("move", str(start[0]), str(start[1]),
                         describe="验收 6.1：把光标放到起点")
    if code != 0:
        record("6.1", "失败", f"起点 move 失败：{err.splitlines()[:1]}")
        record("6.2", "失败", "同上")
        return

    samples: list[tuple[int, int]] = []
    thread = threading.Thread(target=_sample_cursor, args=(2.5, samples), daemon=True)
    thread.start()
    time.sleep(0.15)
    started = time.monotonic()
    code2, _out2, err2 = do("move", str(end[0]), str(end[1]),
                            describe="验收 6.1：走一条长轨迹")
    elapsed = time.monotonic() - started
    thread.join(timeout=1.0)
    if code2 != 0:
        record("6.1", "失败", f"长轨迹 move 失败：{err2.splitlines()[:1]}")
        record("6.2", "失败", "同上")
        return

    # 只看真正在移动的那一段：首末位置附近的静止采样会把统计稀释掉。
    distinct: list[tuple[int, int]] = []
    for point in samples:
        if not distinct or point != distinct[-1]:
            distinct.append(point)
    if len(distinct) < 3:
        record("6.1", "失败", f"整个过程只采到 {len(distinct)} 个不同位置 —— 采样太稀或没动")
        record("6.2", "失败", "同上")
        return

    # ---- 6.1：最大跳距。系统若把连续移动点合并，光标会一步跨过一大段。----
    total = ((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2) ** 0.5
    gaps = [((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
            for a, b in zip(distinct, distinct[1:], strict=False)]
    worst = max(gaps)
    moved = sum(gaps)
    # 判据：既不能一步跨过整段（合并），也不能只动了两三下（点太少）。
    record("6.1", "通过" if (worst <= max(40.0, total * 0.35) and len(distinct) >= 8) else "失败",
           f"轨迹 {total:.0f}px / 耗时 {elapsed * 1000:.0f}ms · 采样到 {len(distinct)} 个不同位置 · "
           f"最大单步 {worst:.0f}px（阈值 {max(40.0, total * 0.35):.0f}px）· "
           f"累计走过 {moved:.0f}px（≥ 直线距离 {total:.0f}px）")

    # ---- 6.2：两个代理量。垂距大 = 不是直线；反转少 = 不是机械抖动。----
    sx, sy = distinct[0]
    ex, ey = distinct[-1]
    span = ((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5 or 1.0
    deviations = [abs((ex - sx) * (sy - y) - (sx - x) * (ey - sy)) / span
                  for x, y in distinct]
    max_dev = max(deviations) if deviations else 0.0
    # 沿弦的推进量，数方向反转次数 —— 来回抖会把它抬得极高。
    progress = [((x - sx) * (ex - sx) + (y - sy) * (ey - sy)) / span for x, y in distinct]
    reversals = sum(1 for a, b, c in zip(progress, progress[1:], progress[2:], strict=False)
                    if (b - a) * (c - b) < 0)
    ratio = reversals / max(1, len(progress))
    record("6.2", "通过" if (max_dev >= 2.0 and ratio <= 0.25) else "失败",
           f"到弦的最大垂距 {max_dev:.1f}px（≥2 才算不直）· 方向反转 {reversals}/{len(progress)} "
           f"= {ratio:.0%}（≤25% 才算不抖）· 中位步长 "
           f"{statistics.median(gaps):.1f}px")


# ---------------------------------------------------------------------------
# 6.3 中文输入
# ---------------------------------------------------------------------------


def _clear_editor(edit: int, hwnd: int) -> None:
    do("click", *map(str, W.client_to_screen(edit, 30, 30)),
       "--hwnd", hex(hwnd), describe="验收：点进编辑区")
    do("key", "ctrl+a", "--hwnd", hex(hwnd), describe="验收：全选")
    do("key", "delete", "--hwnd", hex(hwnd), describe="验收：清空")


def _select_all_and_copy(hwnd: int, note: str) -> str | None:
    do("key", "ctrl+a", "--hwnd", hex(hwnd), describe=f"{note}：全选")
    do("key", "ctrl+c", "--hwnd", hex(hwnd), describe=f"{note}：复制")
    time.sleep(0.4)
    return W.clipboard_get()


#: 读「当前选区」前先把剪贴板写成它 —— 没选中东西时 Ctrl+C 不会覆盖剪贴板，
#: 不设哨兵就会把上一次的残留读成「选中了」。
_SENTINEL = "<<cu-acceptance-nothing-copied>>"


def _copy_selection(hwnd: int, note: str) -> str | None:
    """只复制**当前选区**（不 Ctrl+A）—— 双击选词的判据必须是选区本身。"""
    W.clipboard_set(_SENTINEL)
    time.sleep(0.2)
    do("key", "ctrl+c", "--hwnd", hex(hwnd), describe=f"{note}：复制当前选区")
    time.sleep(0.4)
    got = W.clipboard_get()
    return None if got is not None and got.strip() == _SENTINEL else got


def _show(text: str) -> str:
    """把非 BMP 字符写成 `<U+XXXX>` 再 repr —— 控制台打不出 emoji 时结论仍可复核。"""
    for ch in text:
        if ord(ch) > 0xFFFF:
            text = text.replace(ch, f"<U+{ord(ch):X}>")
    return repr(text)


def check_6_3(hwnd: int, edit: int) -> None:
    wanted = "中文测试 ABC 123"
    _clear_editor(edit, hwnd)
    code, _out, err = do("type", wanted, "--hwnd", hex(hwnd),
                         describe="验收 6.3：输入中文（走 KEYEVENTF_UNICODE）")
    time.sleep(0.5)
    got = (_select_all_and_copy(hwnd, "验收 6.3") or "").strip()
    record("6.3", "通过" if (code == 0 and got == wanted) else "失败",
           f"输入 {wanted!r} → 读回 {got!r}（逐字相等={got == wanted}）"
           + ("" if code == 0 else f" · exit={code} {err.splitlines()[:1]}"))


# ---------------------------------------------------------------------------
# 6.5 双击不被识别为两次单击
# ---------------------------------------------------------------------------


def check_6_5(hwnd: int, edit: int) -> None:
    _clear_editor(edit, hwnd)
    do("type", "hello world", "--hwnd", hex(hwnd), describe="验收 6.5：准备文本")
    time.sleep(0.4)
    # `EM_POSFROMCHAR` 给出字符在客户区的像素位置 → 换算成屏幕坐标再点。
    x, y = W.char_point(edit, 6)                # 'w'
    sx, sy = W.client_to_screen(edit, x + 3, y + 8)   # 往里挪一点，落在字身上
    code, _out, err = do("click", str(sx), str(sy), "--count", "2",
                         "--hwnd", hex(hwnd),
                         describe="验收 6.5：双击 world 这个词")
    time.sleep(0.5)
    got = (_copy_selection(hwnd, "验收 6.5") or "").strip()
    # 双击若被当成两次单击，选区会是空（哨兵原样返回）或单个字符；正确时是整词。
    record("6.5", "通过" if (code == 0 and got == "world") else "失败",
           f"双击词首像素 ({sx},{sy}) 后**选区内容**={got!r}（期望 'world'）"
           + ("" if code == 0 else f" · exit={code} {err.splitlines()[:1]}"))


# ---------------------------------------------------------------------------
# 6.7 滚轮方向（正 dy = 向上）
# ---------------------------------------------------------------------------


def check_6_7(hwnd: int, edit: int) -> None:
    _clear_editor(edit, hwnd)
    text = "\n".join(f"L{index:02d} 验收滚轮方向" for index in range(1, 61))
    do("type", text, "--hwnd", hex(hwnd), describe="验收 6.7：写满 60 行")
    time.sleep(0.8)
    do("key", "ctrl+home", "--hwnd", hex(hwnd), describe="验收 6.7：回到首行")
    time.sleep(0.4)
    top0 = W.first_visible_line(edit)

    point = W.client_to_screen(edit, 60, 60)
    down = do("scroll", "0", "-3", "--at", str(point[0]), str(point[1]),
              describe="验收 6.7：负 dy（应向下滚）")
    time.sleep(0.5)
    top_down = W.first_visible_line(edit)

    up = do("scroll", "0", "3", "--at", str(point[0]), str(point[1]),
            describe="验收 6.7：正 dy（应向上滚）")
    time.sleep(0.5)
    top_up = W.first_visible_line(edit)

    # 正 dy 向上 = 首行号变小；负 dy 向下 = 首行号变大。
    ok = (down[0] == 0 and up[0] == 0 and top0 == 0
          and top_down > top0 and top_up < top_down)
    record("6.7", "通过" if ok else "失败",
           f"首行号：初始 {top0} → 滚(-3) {top_down}（应变大）→ 滚(+3) {top_up}"
           f"（应变小）· 正 dy = 向上={top_up < top_down}")


# ---------------------------------------------------------------------------
# 6.6 拖拽
# ---------------------------------------------------------------------------


def check_6_6(hwnd: int) -> None:
    before = W.rect(hwnd)
    if before is None:
        record("6.6", "失败", "读不到窗口矩形")
        return
    x, y, width, height = before
    grip = (x + width - 2, y + height // 2)
    target = (x + width + 118, y + height // 2)
    code, _out, err = do("drag", str(grip[0]), str(grip[1]), str(target[0]), str(target[1]),
                         "--hwnd", hex(hwnd), describe="验收 6.6：拖右边框改宽度")
    time.sleep(0.6)
    after = W.rect(hwnd)
    if after is None:
        record("6.6", "失败", "拖拽后窗口不见了")
        return
    delta = after[2] - width
    record("6.6", "通过" if (code == 0 and abs(delta - 118) <= 12) else "失败",
           f"宽度 {width} → {after[2]}（Δ={delta}，期望 ≈118±12）· 高度 "
           f"{height} → {after[3]}" + ("" if code == 0 else f" · exit={code} {err.splitlines()[:1]}"))


# ---------------------------------------------------------------------------
# 6.8 / 6.9 / 6.10 三类前置拒绝
# ---------------------------------------------------------------------------


def check_6_9(hwnd: int) -> None:
    import ctypes

    ctypes.WinDLL("user32").ShowWindow(hwnd, 6)          # SW_MINIMIZE
    time.sleep(0.8)
    code, _out, err = do("click", "400", "400", "--hwnd", hex(hwnd),
                         describe="验收 6.9：对最小化窗口点击")
    ctypes.WinDLL("user32").ShowWindow(hwnd, 9)          # SW_RESTORE
    time.sleep(0.8)
    record("6.9", "通过" if (code == 4 and "window_minimized" in err) else "失败",
           f"exit={code}（应 4）· 错误码="
           f"{'window_minimized' if 'window_minimized' in err else err.splitlines()[:1]}")


def check_6_8() -> None:
    """提权窗口：本机不一定存在。**用枚举到的 `elevated` 字段**决定验还是标不适用。"""
    rows = (cu_json("windows", "--all").get("result") or {}).get("windows") or []
    elevated = [row for row in rows if row.get("elevated")]
    if not elevated:
        record("6.8", "不适用",
               f"当前 {len(rows)} 个顶层窗口里没有提权窗口 —— "
               f"本机也起不来一个（UAC 不能无人值守确认）")
        return
    victim = elevated[0]
    code, _out, err = do("click", "400", "400", "--hwnd", victim["hwnd"],
                         describe="验收 6.8：对提权窗口点击")
    record("6.8", "通过" if (code == 4 and "elevated_window" in err) else "失败",
           f"目标 hwnd={victim['hwnd']} pid={victim['pid']} {victim['process']} · "
           f"exit={code}（应 4）· 错误码="
           f"{'elevated_window' if 'elevated_window' in err else err.splitlines()[:1]}")


def check_6_10(hwnd: int) -> None:
    W.close(hwnd)
    time.sleep(0.6)
    code, _out, err = do("click", "400", "400", "--hwnd", hex(hwnd),
                         describe="验收 6.10：对已关闭窗口的 hwnd 点击")
    ok = code == 4 and ("window_not_found" in err or "window_stale" in err)
    record("6.10", "通过" if ok else "失败",
           f"窗口关闭后按旧 hwnd 点击 → exit={code}（应 4）· "
           f"{'window_not_found' if 'window_not_found' in err else err.splitlines()[:1]} · "
           f"注：hwnd 复用那条（window_stale）在实现里**不可达**，见验收记录的发现 F4")


# ---------------------------------------------------------------------------
# 6.11 文本里的换行不合成 Enter（DEC-077）
# ---------------------------------------------------------------------------


def check_6_11(hwnd: int, edit: int) -> None:
    """两半：真机看换行确实落进去，事件流看**一个 Enter 事件都没发**。

    记事本里「换行」与「按 Enter」的结果一样（都是 CRLF），所以真机那一半证明不了
    「没按 Enter」；后者只能看事件流 —— 把 `input._send` 换成记录器，走一次含换行的
    `type_text`，断言记录里没有 VK_RETURN（0x0D）。
    """
    wanted = "AA\nBB\nCC"
    _clear_editor(edit, hwnd)
    answer = cu_json("type", wanted, "--hwnd", hex(hwnd), "--session", SESSION,
                     "--describe", "验收 6.11：含换行的文本", "--end")
    detail = answer.get("result") or {}

    from cu.desktop import input as input_mod
    from cu.desktop import win32 as w
    from cu.desktop import windows as windows_mod

    _clear_editor(edit, hwnd)   # 上面那条已经写进去一份，读回只留这一份
    windows_mod.bring_to_foreground(hwnd)
    time.sleep(0.3)
    vks: list[int] = []
    original = input_mod._send

    def recorder(*inputs):
        vks.extend(item.ki.wVk for item in inputs
                   if item.type == w.INPUT_KEYBOARD
                   and not item.ki.dwFlags & w.KEYEVENTF_UNICODE)
        return original(*inputs)

    input_mod._send = recorder
    try:
        input_mod.type_text(wanted)
    finally:
        input_mod._send = original
    time.sleep(0.5)

    got = (_select_all_and_copy(hwnd, "验收 6.11") or "").replace("\r\n", "\n")
    via_ok = detail.get("via") == "clipboard" and detail.get("reason") == "newline"
    record("6.11", "通过" if (via_ok and got == wanted and 0x0D not in vks) else "失败",
           f"含换行的文本落下去是 {got!r}（期望 {wanted!r}）；取道 via={detail.get('via')!r} "
           f"reason={detail.get('reason')!r}；事件流里的按键 VK={vks}"
           f"（VK_RETURN=0x0D 出现 {vks.count(0x0D)} 次）")


# ---------------------------------------------------------------------------
# 6.12 单键 `key enter`（`key` 不只吃组合键）
# ---------------------------------------------------------------------------


def check_6_12(hwnd: int, edit: int) -> None:
    """文本里一个换行都没有，两行只可能来自按键 —— 这条测的就是 `key` 的单键写法。"""
    _clear_editor(edit, hwnd)
    do("type", "AA", "--hwnd", hex(hwnd), describe="验收 6.12：先写一行")
    code, _out, err = do("key", "enter", "--hwnd", hex(hwnd), describe="验收 6.12：单键 enter")
    do("type", "BB", "--hwnd", hex(hwnd), describe="验收 6.12：再写一行")
    time.sleep(0.5)
    got = _select_all_and_copy(hwnd, "验收 6.12") or ""
    record("6.12", "通过" if (code == 0 and got == "AA\r\nBB") else "失败",
           f"`type AA` → `key enter` → `type BB` 读回 {got!r}（期望 AA、BB 两行）"
           + ("" if code == 0 else f" · exit={code} {err.splitlines()[:1]}"))


# ---------------------------------------------------------------------------
# 6.14 非 BMP 字符（emoji）不被截断（DEC-078）
# ---------------------------------------------------------------------------


def check_6_14(hwnd: int, edit: int) -> None:
    """读回整串：BMP 之外的字符必须原样落下，而不是被截断成另一个字符。

    真机是唯一能证伪「`wScan` 装不下」的地方 —— 单测只看得见两个码元投了出去，
    看不见系统把它们合并回一个字符。截断的签名很具体：`U+1F600` 变成 `U+F600`（私用区）。

    日志里把这两个字符写成 `<U+1F600>` 形态，免得控制台打不出 emoji 时结论无法复核。
    """
    wanted = "emoji：\U0001F600\U0001F389 中文尾"
    _clear_editor(edit, hwnd)
    answer = cu_json("type", wanted, "--hwnd", hex(hwnd), "--session", SESSION,
                     "--describe", "验收 6.14：非 BMP 字符", "--end")
    detail = answer.get("result") or {}
    time.sleep(0.5)
    got = (_select_all_and_copy(hwnd, "验收 6.14") or "").replace("\r\n", "\n")
    record("6.14", "通过" if got == wanted else "失败",
           f"读回 {_show(got)}（期望 {_show(wanted)}）；"
           f"截断签名 U+F600 出现 {got.count(chr(0xF600))} 次；via={detail.get('via')!r}")



# ---------------------------------------------------------------------------
# 6.15 带 `--hwnd` 的 `key` 把目标提到前台（DEC-082）
# ---------------------------------------------------------------------------


def check_6_15(hwnd: int, edit: int) -> None:
    """构造「目标不在前台」，再看 `key` 能不能自己把前台抢回来。

    干扰窗口是第二个记事本。带 `--hwnd` 的写命令自己会抢前台，所以对它点一下
    就足以把前台让出去；此后对靶子发 `key ctrl+a` —— 抢前台没生效时这一串键会
    落进干扰窗口：靶子既不会成为前台，也读不到选区。
    """
    other = W.spawn_notepad()
    if other is None:
        record("6.15", "跳过", "第二个记事本起不来，这条构造不出「目标不在前台」")
        return
    try:
        W.move(other, 1250, 160, 620, 380, top=True)
        time.sleep(1.2)
        other_edit = W.child_window(other)
        if other_edit is None:
            record("6.15", "跳过", "干扰窗口里找不到编辑区")
            return
        _clear_editor(edit, hwnd)
        do("type", "靶子在后台", "--hwnd", hex(hwnd), describe="验收 6.15：准备靶子文本")
        time.sleep(0.4)
        do("click", *map(str, W.client_to_screen(other_edit, 40, 40)),
           "--hwnd", hex(other), describe="验收 6.15：把前台让给干扰窗口")
        time.sleep(0.5)
        before = W.foreground_hwnd()
        code, _out, err = do("key", "ctrl+a", "--hwnd", hex(hwnd),
                             describe="验收 6.15：对不在前台的靶子发键")
        time.sleep(0.4)
        after = W.foreground_hwnd()
        got = (_copy_selection(hwnd, "验收 6.15") or "").strip()
        ok = code == 0 and before == other and after == hwnd and got == "靶子在后台"
        record("6.15", "通过" if ok else "失败",
               f"发键前前台={before:#x}（干扰={other:#x}）· 发键后前台={after:#x}"
               f"（靶子={hwnd:#x}）· 靶子选区读回={got!r}"
               + ("" if code == 0 else f" · exit={code} {err.splitlines()[:1]}"))
    finally:
        W.close(other)


# ---------------------------------------------------------------------------
# 6.4 剪贴板降级路径（进程内注入失败，验真实的降级分支）
# ---------------------------------------------------------------------------


def check_6_4(hwnd: int) -> None:
    """人为让 Unicode 路径失败，看它是否真的走剪贴板并如实报告。

    做法是给 `input._send` 打一个「遇到 UNICODE 事件就抛错」的桩 ——
    这是唯一能在真机上构造出这条分支的办法（本机 Unicode 输入是好的）。
    """
    from cu.desktop import input as input_mod
    from cu.desktop import windows as windows_mod
    from cu.errors import CUError, ErrorCode

    windows_mod.bring_to_foreground(hwnd)
    time.sleep(0.4)
    original = input_mod._send

    def flaky(*inputs):
        if any(getattr(item.ki, "dwFlags", 0) & input_mod.w.KEYEVENTF_UNICODE
               for item in inputs if item.type == input_mod.w.INPUT_KEYBOARD):
            raise CUError(ErrorCode.INTERNAL_ERROR, "验收注入：Unicode 路径失败")
        return original(*inputs)

    input_mod._send = flaky
    try:
        result = input_mod.type_text("剪贴板降级")
    finally:
        input_mod._send = original
    time.sleep(0.5)
    got = (W.clipboard_get() or "").strip()
    detail = result.detail or {}
    ok = (detail.get("fallback") == "clipboard" and "clipboard_restored" in detail)
    record("6.4", "通过" if ok else "失败",
           f"注入 Unicode 失败后 fallback={detail.get('fallback')!r} · "
           f"clipboard_restored={detail.get('clipboard_restored')!r} · "
           f"降级前剪贴板内容={got!r}（脚本随后会还原它）")


# ---------------------------------------------------------------------------


def main() -> int:
    global SESSION
    sys.stdout.reconfigure(line_buffering=True)
    only = None
    for index, arg in enumerate(sys.argv):
        if arg == "--only" and index + 1 < len(sys.argv):
            only = {part.strip() for part in sys.argv[index + 1].split(",") if part.strip()}
    if not only:
        only = {"6.1", "6.2", "6.3", "6.4", "6.5", "6.6", "6.7", "6.8", "6.9", "6.10",
                "6.11", "6.12", "6.14", "6.15"}

    def want(*items: str) -> bool:
        return any(item in only for item in items)

    print("=" * 72)
    print("验收 §6 输入行为（靶子：脚本自建的记事本）")
    print("=" * 72)
    print("  [!] 期间会多次移动鼠标、投递按键，并短暂封锁输入。不会点任何非靶子窗口。\n")

    clipboard_backup = W.clipboard_get()

    hwnd = W.spawn_notepad()
    if hwnd is None:
        print("  无法启动记事本，§6 无法进行")
        return 1
    try:
        W.move(hwnd, 260, 160, 900, 560, top=True)
        time.sleep(1.2)
        edit = W.child_window(hwnd)
        if edit is None:
            print("  找不到记事本的编辑区，§6 无法进行")
            return 1

        SESSION = ((cu_json("begin", "--agent-hint", "acceptance-6").get("result") or {})
                   .get("session_id") or "")
        # 先把靶子点到前台：后面 `key` / `scroll` 没有自动前台这一步。
        do("click", *map(str, W.client_to_screen(edit, 60, 60)),
           "--hwnd", hex(hwnd), describe="验收 §6：把靶子窗口点到前台")
        time.sleep(0.5)
        inside = W.client_to_screen(edit, 80, 80)

        if want("6.1", "6.2"):
            check_6_1_and_6_2(inside)
        if want("6.3"):
            check_6_3(hwnd, edit)
        if want("6.5"):
            check_6_5(hwnd, edit)
        if want("6.7"):
            check_6_7(hwnd, edit)
        if want("6.6"):
            check_6_6(hwnd)
        if want("6.11"):
            check_6_11(hwnd, edit)
        if want("6.12"):
            check_6_12(hwnd, edit)
        if want("6.14"):
            check_6_14(hwnd, edit)
        if want("6.15"):
            check_6_15(hwnd, edit)
        if want("6.4"):
            check_6_4(hwnd)      # 需要靶子还在前台，所以放在 6.10 之前
        if want("6.9"):
            check_6_9(hwnd)
        if want("6.8"):
            check_6_8()
        if want("6.10"):
            check_6_10(hwnd)     # 最后做：它会把靶子关掉
    finally:
        # 收尾：显式结束写序列，覆盖层撤下、输入还给用户，然后才去关窗口。
        if SESSION:
            do("move", *map(str, W.cursor_pos()),
               describe="验收：收尾，结束这条写序列", keep=False)
        # 只关我们自己起的那一个窗口。**不要按进程名杀 notepad.exe** ——
        # 用户可能正开着记事本，那样会连他的窗口和未保存的内容一起杀掉。
        W.close(hwnd)
        if clipboard_backup is not None:
            W.clipboard_set(clipboard_backup)
        if SESSION:
            cu("session", "end", "--session", SESSION)

    print("\n===== §6 结果 =====")
    failed = [r for r in RESULTS if r[1] == "失败"]
    for item, verdict, detail in RESULTS:
        print(f"  §{item:<5} {verdict:<5} {detail}")
    print(f"\n  通过 {len(RESULTS) - len(failed) - sum(1 for r in RESULTS if r[1] == '不适用')}"
          f" · 失败 {len(failed)} · 不适用 {sum(1 for r in RESULTS if r[1] == '不适用')}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
