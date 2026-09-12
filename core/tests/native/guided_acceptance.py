"""引导式人眼验收 —— `acceptance.md` §1（输入封锁与逃生）与 §4（覆盖层观感）。

**为什么必须是引导式的**：这些项只有人坐在机器前才能判断。脚本能做的只有
「制造出那个状态 + 问你看到/摸到了什么 + 把答案记下来」。自动断言在这里没有意义 ——
它没法知道你的键盘有没有被吞。

安全设计（三层保底，缺一不可）：
  1. 每一项封锁都有**看门狗**，到点自动解封（默认 15s）；
  2. 物理 Esc 随时中止（这正是被测机制本身）；
  3. Ctrl+Alt+Del 无法被任何用户态钩子拦截，永远可用。

跑法：
    .venv/Scripts/python.exe core/tests/native/guided_acceptance.py [--section 1,4]
"""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path, utf8_console  # noqa: E402

utf8_console()
add_src_to_path()

from cu.desktop.dpi import ensure_dpi_awareness  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def ask(question: str, options: str = "y/n") -> str:
    """问一个问题，等真人回答。"""
    while True:
        answer = input(f"  {question} [{options}] ").strip().lower()
        if answer:
            return answer
        print("    （请给一个答案，直接回车不算）")


def record(item: str, verdict: str, detail: str) -> None:
    RESULTS.append((item, verdict, detail))
    print(f"    → §{item}: {verdict}  {detail}\n", flush=True)


def pause(text: str = "准备好了按回车继续") -> None:
    input(f"\n  {text} ... ")


# ---------------------------------------------------------------------------
# §1 输入封锁与逃生
# ---------------------------------------------------------------------------


def section1() -> None:
    from cu.desktop.hooks import InputBlocker

    print("\n" + "=" * 72)
    print("§1 输入封锁与逃生 —— 这一节错了的后果是把用户锁在电脑外")
    print("=" * 72)
    print("""
  接下来会真的封锁物理键鼠。三层保底：
    ① 看门狗 15 秒后自动解封
    ② 随时按【物理 Esc】立刻中止
    ③ Ctrl+Alt+Del 永远可用（用户态钩子拦不住它）
""")

    # ---- 先开一个记事本当输入接收端，这样能直接看出按键有没有送达 ----
    notepad = subprocess.Popen(["notepad.exe"])
    time.sleep(3.5)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.restype = ctypes.c_void_p
    hwnd = user32.FindWindowW("Notepad", None)
    if not hwnd:
        print("  ⚠️ 没能开起记事本，§1 无法进行（需要它来接收按键）")
        notepad.terminate()
        return
    print(f"  已开记事本（hwnd=0x{hwnd:08X}）当输入接收端。")
    print("  先点一下记事本的编辑区，让光标在里面，再去点终端回来。")
    pause("光标已放进记事本、终端已激活后按回车")

    blocker = InputBlocker()
    blocker.start()
    if not blocker.installed:
        print("  ⚠️ 钩子未装上，§1 无法进行")
        blocker.stop()
        notepad.terminate()
        return

    # ---- 1.1 物理键被吞 ----
    print("\n  【1.1】马上会封锁键鼠 12 秒。")
    print("        请在封锁期间，**按几个字母键**，试着在记事本里打出字。")
    print("        （看门狗 12 秒后自动解封）")
    watchdog = threading.Timer(12.0, blocker.set_blocking, args=(False,))
    watchdog.start()
    blocker.set_blocking(True)
    print("        >>> 已封锁 <<< 现在按字母键。")
    time.sleep(9.0)
    blocker.set_blocking(False)
    watchdog.cancel()
    print("        已解封。")
    # 自动旁证：UIA 的 Document 名字会带上「已修改」之类的状态。
    # 但它只是旁证 —— 最终判断仍以你看到的内容为准（脚本没法读编辑器正文）。
    try:
        from cu.desktop import uia
        doc = next((e.name for e in uia.read_window(int(hwnd)).elements
                    if e.control_type == "document"), "")
        if doc:
            print(f"        （旁证：记事本 Document = {doc!r}）")
    except Exception:  # noqa: BLE001
        pass
    answer = ask("封锁期间你按的字母键，有没有出现在记事本里？")
    if answer.startswith("n"):
        record("1.1", "通过", "物理键被吞（记事本未收到字符）")
    else:
        record("1.1", "失败", "物理键在封锁期间仍送达了应用")

    # ---- 1.2 物理 Esc 中止并解封 ----
    print("\n  【1.2】再次封锁 15 秒。请**按物理 Esc**。")
    print("        若机制正常，封锁会立刻解除、脚本立即继续（不必等满 15 秒）。")
    aborted = threading.Event()
    blocker.on_abort = aborted.set
    watchdog2 = threading.Timer(15.0, blocker.set_blocking, args=(False,))
    watchdog2.start()
    blocker.set_blocking(True)
    started = time.monotonic()
    print("        >>> 已封锁 <<< 请按物理 Esc。")
    while time.monotonic() - started < 15.0 and not aborted.is_set():
        if not blocker.blocking:      # 看门狗先解封了，说明 Esc 没被识别
            break
        time.sleep(0.05)
    elapsed = time.monotonic() - started
    reached_esc = aborted.is_set()
    if reached_esc:
        blocker.set_blocking(False)
    watchdog2.cancel()
    print(f"        已{'通过物理 Esc 中止' if reached_esc else '由看门狗解封'}（耗时 {elapsed:.1f}s）")
    record("1.2", "通过" if reached_esc else "失败",
           f"物理 Esc {'被识别并中止' if reached_esc else '未触发'}（{elapsed:.1f}s）")

    # ---- 1.3 注入输入在封锁期间被放行 ----
    print("\n  【1.3】封锁 8 秒。这次由**脚本自己注入**几个按键（F24，无副作用）。")
    print("        注入的按键应当被放行 —— 否则 AI 自己的输入会被自己吞掉。")
    seen: list[bool] = []
    blocker.on_input_seen = seen.append
    watchdog3 = threading.Timer(8.0, blocker.set_blocking, args=(False,))
    watchdog3.start()
    blocker.set_blocking(True)
    injected = 0
    try:
        from cu.desktop import input as input_mod
        for _ in range(5):
            input_mod.key("f24")
            injected += 1
            time.sleep(0.15)
    except Exception as exc:  # noqa: BLE001
        print("        注入失败:", exc)
    time.sleep(1.0)
    blocker.set_blocking(False)
    watchdog3.cancel()
    injected_seen = sum(1 for flag in seen if flag is False)
    record("1.3", "通过" if injected_seen > 0 else "失败",
           f"注入 {injected} 次，钩子观测到注入事件 {injected_seen} 次（应为 5）")

    blocker.on_input_seen = None
    blocker.on_abort = None

    # ---- 1.4 进程被杀后输入恢复 ----
    print("\n  【1.4】这一项要**换一种做法**：由你亲手杀掉进程，才能证明「钩子随进程消失」。")
    print("        我会在**另一个进程**里装钩子并封锁，然后杀掉它。")
    helper = _spawn_blocking_helper()
    if helper is None:
        record("1.4", "不适用", "未能启动子进程辅助程序")
    else:
        print(f"        子进程 pid={helper.pid}，它已封锁输入 20 秒。")
        pause("请现在**用任务管理器杀掉该 python 进程**（或直接按回车让我杀），然后试打字")
        if helper.poll() is None:
            print("        脚本替你杀掉它……")
            helper.terminate()
            time.sleep(1.0)
        print("        请试着在记事本里打几个字。")
        answer = ask("杀掉进程后，键盘是否立即恢复？")
        record("1.4", "通过" if answer.startswith("y") else "失败",
               "钩子随进程死亡被 OS 摘除，输入恢复" if answer.startswith("y")
               else "进程死后输入仍被封锁（严重）")

    # ---- 1.5 Ctrl+Alt+Del ----
    print("\n  【1.5】封锁 15 秒，请按 **Ctrl+Alt+Del**。")
    print("        应该能看到安全界面 —— 用户态钩子拦不住它，这是保底逃生。")
    seen.clear()
    watchdog4 = threading.Timer(15.0, blocker.set_blocking, args=(False,))
    watchdog4.start()
    blocker.set_blocking(True)
    print("        >>> 已封锁 <<< 请按 Ctrl+Alt+Del。")
    time.sleep(12.0)
    blocker.set_blocking(False)
    watchdog4.cancel()
    answer = ask("按 Ctrl+Alt+Del 后出现安全界面了吗？")
    record("1.5", "通过" if answer.startswith("y") else "失败",
           "Ctrl+Alt+Del 可穿透封锁" if answer.startswith("y") else "被拦截（不可能，需排查）")

    # ---- 1.6 钩子超时会被系统摘除 ----
    print("\n  【1.6】这一项验「钩子超时后输入恢复」—— 需要人为让钩子回调变慢。")
    print("        脚本会把回调里塞一个 sleep，然后封锁并按键。")
    print("        预期：输入恢复（封锁失效），因为 Windows 会摘掉超时的钩子。")
    seen.clear()
    watchdog5 = threading.Timer(20.0, blocker.set_blocking, args=(False,))
    watchdog5.start()
    _slow_down_hook(blocker)
    blocker.set_blocking(True)
    print("        >>> 已封锁（钩子已被人为拖慢）<<< 现在按几个键。")
    time.sleep(15.0)
    blocker.set_blocking(False)
    watchdog5.cancel()
    answer = ask("按的键有没有出现？（预期：出现了 —— 说明超时钩子被摘除）")
    record("1.6", "通过" if answer.startswith("y") else "失败",
           "钩子超时被系统摘除，输入恢复" if answer.startswith("y")
           else "输入仍被封锁（说明钩子未被摘除，需记录）")

    blocker.stop()
    notepad.terminate()
    print("\n  §1 完成。记事本已关闭。\n")


def _spawn_blocking_helper() -> subprocess.Popen | None:
    """在**独立进程**里装钩子并封锁 —— 这样杀掉它能验证「钩子随进程消失」。"""
    script = (
        "import sys; sys.path.insert(0, r'{src}')\n"
        "from cu.desktop.hooks import InputBlocker\n"
        "import time\n"
        "b = InputBlocker(); b.start(); b.set_blocking(True)\n"
        "print('blocked', flush=True)\n"
        "time.sleep(60)\n"
    ).format(src=str(Path(__file__).resolve().parents[2] / "src"))
    try:
        proc = subprocess.Popen([sys.executable, "-c", script],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # 等它真的封锁了再往下走
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return None
            time.sleep(0.3)
            if proc.stdout and proc.stdout.readline().strip() == "blocked":
                return proc
        return proc
    except OSError:
        return None


def _slow_down_hook(blocker) -> None:
    """把键盘钩子回调包一层 sleep，模拟「处理超时」。

    Windows 对低级钩子有超时（默认约 300ms），超时会跳过并最终摘除该钩子 ——
    这正是「daemon 挂死时输入仍会恢复」那条可用性保证的机制。
    """
    original = blocker._keyboard_hook

    def slow(code, wparam, lparam):
        time.sleep(0.8)
        return original(code, wparam, lparam)

    blocker._keyboard_hook = slow
    # 回调对象必须重新包一次并持有引用（spike 陷阱 5）

    from cu.desktop import win32 as w
    blocker._keyboard_proc = w.HOOKPROC(slow)


# ---------------------------------------------------------------------------
# §4 覆盖层观感
# ---------------------------------------------------------------------------


def section4() -> None:
    from cu.desktop.overlay import ControlOverlay, OverlayState

    print("\n" + "=" * 72)
    print("§4 覆盖层观感 —— 这些只能人眼看（覆盖层被 WDA 排除出所有截图管线，")
    print("     所以脚本截不到它，只有 T7 验过「渲染缓冲里的 2px 环」）")
    print("=" * 72)

    overlay = ControlOverlay()
    overlay.start()

    def show(state: OverlayState, seconds: float, target=None, cursor=None) -> None:
        overlay.set_target(target)
        overlay.set_cursor(cursor)
        overlay.transition(state)
        time.sleep(seconds)

    try:
        # ---- 4.4 五态视觉与文案 ----
        print("\n  【4.4】逐个显示五个状态，每态停 4 秒。请对照 overlay.md §2.1 的状态表。")
        for state, note in (
            (OverlayState.ARMING, "快流光谱 / 胶囊：AI is using your computer · [Esc] to cancel"),
            (OverlayState.ACTIVE, "慢流光谱 / 同上胶囊 / 目标框（琥珀）/ 光标光晕"),
            (OverlayState.STOPPING, "光谱冻结为琥珀 / 胶囊：Stopping"),
            (OverlayState.ERROR, "光谱冻结为红 / 胶囊：Something went wrong · [Esc] to dismiss"),
        ):
            print(f"        显示 {state.value} ……（{note}）")
            show(state, 4.0, target=(900, 600, 200, 120), cursor=(900, 600))
        overlay.transition(OverlayState.OFF)
        time.sleep(0.5)
        answer = ask("四态的胶囊文案与光谱颜色都对吗？")
        record("4.4", "通过" if answer.startswith("y") else "失败",
               "四态文案与光谱符合 overlay.md §2.1" if answer.startswith("y")
               else "与规范不符，详见用户描述")

        # ---- 4.5 无硬边界 ----
        print("\n  【4.5】再次显示 Active（8 秒）。请**盯住光晕的最外缘**。")
        print("        预期：看不出「光在哪里结束」—— 没有可感知的内边界，也不像色块。")
        print("        建议把壁纸换成深色再试（深色是最苛刻的场景）。")
        show(OverlayState.ACTIVE, 8.0)
        answer = ask("光晕外缘是否没有任何可感知的硬边界/内边界？")
        record("4.5", "通过" if answer.startswith("y") else "失败",
               "无硬边界、无非线性衰减留下的内边界" if answer.startswith("y")
               else "能看出边界或内边界（DEC-031 要求非线性衰减）")

        # ---- 4.6 光谱流动 ----
        print("\n  【4.6】显示 Arming 6 秒（应约 2s 一圈），再显示 Active 9 秒（应约 7s 一圈）。")
        print("        请盯住四个角的颜色变化 —— 关键是有没有**在动**。")
        print("        （DEC-031 第 7 轮踩过这个坑：渐变曾被固化，光谱根本不流动）")
        show(OverlayState.ARMING, 6.0)
        overlay.transition(OverlayState.OFF)
        time.sleep(0.3)
        show(OverlayState.ACTIVE, 9.0)
        answer = ask("光谱在流动吗？速度大致符合 2s/圈与 7s/圈吗？")
        record("4.6", "通过" if answer.startswith("y") else "失败",
               "光谱在流动，速度符合 DEC-030" if answer.startswith("y")
               else "光谱未流动或速度不符")

        overlay.transition(OverlayState.OFF)

        # ---- 4.2 点击穿透 ----
        print("\n  【4.2】覆盖层会显示 10 秒。请在这期间**点击屏幕上的任意可点位置**")
        print("        （比如这个终端窗口本身）—— 点击应当**穿过**覆盖层到达下面的窗口。")
        subprocess.Popen(["notepad.exe"])
        time.sleep(3.0)
        show(OverlayState.ACTIVE, 10.0)
        answer = ask("覆盖层显示期间，你的点击有没有到达下方的窗口？")
        record("4.2", "通过" if answer.startswith("y") else "失败",
               "点击穿透生效（WS_EX_TRANSPARENT）" if answer.startswith("y")
               else "点击被覆盖层挡住（严重 —— 用户无法操作）")

        # ---- 4.3 不抢焦点 ----
        print("\n  【4.3】覆盖层再次显示 8 秒。请留意**你当前激活的窗口有没有变**。")
        print("        预期：前台窗口不变（覆盖层带 WS_EX_NOACTIVATE）。")
        show(OverlayState.ACTIVE, 8.0)
        answer = ask("覆盖层显示期间，前台窗口有没有被抢走？")
        record("4.3", "通过" if answer.startswith("n") else "失败",
               "不抢焦点（WS_EX_NOACTIVATE）" if answer.startswith("n")
               else "抢走了焦点")

        # ---- 4.7 胶囊在深色桌面可辨识 ----
        print("\n  【4.7】请把壁纸/背景换成**深色**，然后按回车。")
        pause("已换成深色背景")
        show(OverlayState.ACTIVE, 8.0)
        answer = ask("深色背景下，顶部胶囊是否清晰可辨（靠那层 1px 浅色扩散环）？")
        record("4.7", "通过" if answer.startswith("y") else "失败",
               "深色背景下胶囊与背景分离" if answer.startswith("y")
               else "胶囊在深色背景下与背景融合")

        # ---- 4.9 光标处理 ----
        print("\n  【4.9】当前实现是「只画光晕 + 保留系统光标」（overlay.md §3.1 的降级路径）。")
        print("        请确认：1) 能看到光标处的白色光晕 2) 系统光标仍在、无闪烁。")
        show(OverlayState.ACTIVE, 8.0, cursor=(900, 600))
        answer = ask("光标光晕可见、且系统光标正常无闪烁？")
        record("4.9", "通过" if answer.startswith("y") else "失败",
               "只画光晕 + 保留系统光标（已验证的降级路径）" if answer.startswith("y")
               else "光晕不可见或系统光标异常")

        # ---- 4.8 多分辨率 ----
        record("4.8", "不适用", "本机只有一台显示器，1080p/4K 对照无法做（用户确认）")

    finally:
        overlay.transition(OverlayState.OFF)
        overlay.stop()


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--section", default="1,4", help="要跑的节，如 1 或 4 或 1,4")
    args = parser.parse_args()

    ensure_dpi_awareness()
    sections = {s.strip() for s in args.section.split(",")}

    print("=" * 72)
    print("Computer-Use 引导式人眼验收")
    print("=" * 72)
    print("""
  说明：脚本会制造状态并问你观察到什么。答案由你给 —— 自动化断言在这里
  没有意义（脚本没法知道你的键盘有没有被吞）。

  保底：物理 Esc 随时中止封锁 / Ctrl+Alt+Del 永远可用 / 每项都有看门狗。
""")

    if "1" in sections:
        section1()
    if "4" in sections:
        section4()

    print("\n" + "=" * 72)
    print("结果汇总")
    print("=" * 72)
    for item, verdict, detail in RESULTS:
        print(f"  §{item:<5} {verdict:<5} {detail}")
    passed = sum(1 for _, v, _ in RESULTS if v == "通过")
    failed = [r for r in RESULTS if r[1] == "失败"]
    print(f"\n  通过 {passed} · 失败 {len(failed)} · 不适用 "
          f"{sum(1 for _, v, _ in RESULTS if v == '不适用')}")
    if failed:
        print("\n  失败的项：")
        for item, _, detail in failed:
            print(f"    §{item}: {detail}")
    print("\n  结果同时写入 tmp-doc/2026-09-12/guided-acceptance-result.md")
    # 落文件：用户在**自己的终端**里跑这个脚本（我的工具没法把键盘输入传给它），
    # 所以结果要靠文件回传，比复制粘贴可靠。
    out = Path(__file__).resolve().parents[3] / "tmp-doc" / "2026-09-12" / "guided-acceptance-result.md"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# 引导式验收结果", "",
                 f"- 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"- 通过 {passed} · 失败 {len(failed)}", ""]
        lines += ["| 项 | 结论 | 说明 |", "|---|---|---|"]
        lines += [f"| §{i} | {v} | {d} |" for i, v, d in RESULTS]
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  已写入: {out}")
    except OSError as exc:
        print(f"  ⚠️ 写文件失败（{exc}）—— 请把上面的输出贴回对话。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
