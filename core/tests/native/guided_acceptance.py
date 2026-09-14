"""引导式人眼验收 —— `acceptance.md` §1（输入封锁与逃生）与 §4（覆盖层观感）。

## 历次修订（保留，免得再犯）

1. **第一版把「让光标进记事本」与「按回车激活终端」当成了能同时成立的事** ——
   它们互斥。正确做法是**不碰记事本**：你人就在终端里，直接在终端敲字就能验证封锁。
2. **第一版 1.1 那步「按 Esc 没反应」** —— 根因在产品代码（钩子里通知回调是有条件的、
   而吞掉 Esc 是无条件的）。**物理 Esc 是安全底线，不能依赖回调是否存在**。已修。
3. **第一版让用户自己去任务管理器杀进程** —— 任务管理器里一堆 python、PID 难找，
   用户把脚本本身也杀了，那步没留下任何结论。现在改成**脚本自己杀**。
4. **第一版看门狗 8 秒** —— 用户反馈还没反应过来就解封了，那种情况下
   「Esc 生效」与「看门狗兜底」观感上分不开，等于把要验的东西搅浑。
   现在 **30 秒**，且**显示实时倒计时**。
5. **第一版 §4 全项「看不到覆盖层」** —— 那不是观感问题，是两个真实缺陷：
   窗口建在从不抽消息的主线程上，5 秒后被判定无响应、进程被 WER 结束（AppHangB1）；
   光晕的几何写成了「到屏幕中心的距离」而不是「到最近边的距离」，
   画出来是屏幕正中一块十字形色块。两个都已修，各留了一条守卫（desktop_smoke T9/T8）。

## 现在的设计

- **每一项都由你按物理 Esc 结束**，看门狗只是兜底（30 秒，有倒计时）。
- **不碰任何编辑器**。验「键被吞了」就在终端里敲字看有没有出现（不回车不会提交）。
- §4 先做**前置确认**：覆盖层到底有没有显示，并先把「该看哪儿、多大」讲清楚。

跑法（在**你自己的终端**里）：
    .venv/Scripts/python.exe core/tests/native/guided_acceptance.py
"""

from __future__ import annotations

import argparse
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

#: `--item` 选中的条目（如 `{"4.4"}`）；None = 全跑。
#: 一个段可以挂多个 id —— 1.1a 与 1.1b 是同一个动作引出的两个问题，选中任一个都跑那段。
_ONLY: set[str] | None = None


def selected(*items: str) -> bool:
    """这一段要不要跑。没给 `--item` 就跑全部。"""
    return _ONLY is None or any(item in _ONLY for item in items)

#: 兜底看门狗时长（秒）。
#:
#: **30 秒**：用户反馈 8 秒根本不够 —— 还没反应过来就被解封了，而那种情况下
#: 「Esc 生效」与「看门狗兜底」长得一模一样，等于把要验的东西搅浑了。
WATCHDOG_SECONDS = 30.0


def say(text: str = "") -> None:
    print(text, flush=True)


def ask(question: str, options: str = "y/n") -> str:
    """提问（此时封锁已解除，输入可用）。"""
    while True:
        answer = input(f"  {question} [{options}] ").strip().lower()
        if answer:
            return answer


def record(item: str, verdict: str, detail: str) -> None:
    RESULTS.append((item, verdict, detail))
    say(f"    -> §{item}: {verdict}  {detail}\n")


def block_until_escape(blocker, what_to_do: str) -> tuple[bool, float]:
    """封锁输入，直到用户按物理 Esc（或看门狗兜底）。

    返回 `(是否由 Esc 解除, 耗时秒)`。

    **Esc 是主要退出手段** —— 这正是在验「用户能不能随时拿回控制」。
    **带实时倒计时**：没有它，「Esc 生效」与「看门狗兜底」在观感上分不开。
    """
    aborted = threading.Event()
    blocker.on_abort = aborted.set

    watchdog = threading.Timer(WATCHDOG_SECONDS, blocker.set_blocking, args=(False,))
    watchdog.start()

    blocker.set_blocking(True)
    say(f"        >>> 已封锁 <<<  {what_to_do}")
    say(f"        （按【物理 Esc】立即结束；{WATCHDOG_SECONDS:.0f} 秒不动会自动解封兜底）")

    started = time.monotonic()
    last_shown = None
    while time.monotonic() - started < WATCHDOG_SECONDS + 3:
        if aborted.is_set() or not blocker.blocking:
            break
        remain = int(WATCHDOG_SECONDS - (time.monotonic() - started))
        if remain != last_shown and remain >= 0:
            last_shown = remain
            say(f"        倒计时 {remain:3d} 秒 —— 按【物理 Esc】立即结束")
        time.sleep(0.02)

    elapsed = time.monotonic() - started
    by_escape = aborted.is_set()

    blocker.set_blocking(False)
    watchdog.cancel()
    blocker.on_abort = None

    say(f"        已解封（{'物理 Esc' if by_escape else '看门狗兜底'}，耗时 {elapsed:.1f}s）")
    return by_escape, elapsed


# ---------------------------------------------------------------------------
# §1 输入封锁与逃生
# ---------------------------------------------------------------------------


def section1() -> None:
    from cu.desktop.hooks import InputBlocker

    say("\n" + "=" * 72)
    say("§1 输入封锁与逃生")
    say("=" * 72)
    say("""
  每一项都是：脚本封锁 -> 你做一件事 -> **按物理 Esc 结束**（有 30 秒倒计时）。

  安全保底（三层）：
    1) 物理 Esc 随时解封（这正是被验的机制）
    2) 每项 30 秒看门狗，到点自动解封
    3) Ctrl+Alt+Del 无法被任何用户态钩子拦截，永远可用

  验证「键被吞了」的方式：**直接在终端里敲几个字母**，看它们有没有出现。
  不按回车就不会提交，安全。**不需要开记事本**。
""")

    blocker = InputBlocker()
    blocker.start()
    if not blocker.installed:
        say("  [!] 钩子没装上，§1 无法进行（终端是否以管理员身份运行？）")
        blocker.stop()
        return

    # ---- 1.1a Esc 能不能解除封锁（最该先验的） ----
    # 1.1a 与 1.1b 是同一个动作引出的两问，选中任一个都跑这一段。
    if selected("1.1a", "1.1b"):
        say("\n  【1.1a】先验最重要的一件：**进了封锁，物理 Esc 能不能出来。**")
        by_escape, elapsed = block_until_escape(blocker, "敲几个字母，然后按【物理 Esc】。")
        if by_escape:
            record("1.1a", "通过", f"物理 Esc 解除封锁（{elapsed:.1f}s）")
        else:
            record("1.1a", "失败",
                   f"物理 Esc 未解除封锁（{elapsed:.1f}s 后由看门狗兜底）—— 安全底线失效")

        # ---- 1.1b 物理键被吞 ----
        typed = ask("刚才敲的字母，有没有出现在终端里？")
        if typed.startswith("n"):
            record("1.1b", "通过", "物理键在封锁期间被吞（终端未收到字符）")
        else:
            record("1.1b", "失败", "物理键在封锁期间仍到达了终端")

    # ---- 1.2 再验一次（不可重复的逃生手段等于没有） ----
    if selected("1.2"):
        say("\n  【1.2】再来一次，确认 Esc 每次都管用。")
        by_escape2, elapsed2 = block_until_escape(blocker, "随意做点什么，然后按【物理 Esc】。")
        record("1.2", "通过" if by_escape2 else "失败",
               f"第二次封锁同样由 Esc 解除（{elapsed2:.1f}s）" if by_escape2
               else "第二次 Esc 失效")

    # ---- 1.3 注入输入在封锁期间被放行 ----
    if selected("1.3"):
        say("\n  【1.3】封锁 4 秒，期间由**脚本注入**按键（F24，无副作用）。")
        say("        注入应当被放行 —— 否则 AI 自己的输入会被自己吞掉。")
        seen: list[bool] = []
        blocker.on_input_seen = seen.append
        blocker.set_blocking(True)
        injected = 0
        try:
            from cu.desktop import input as input_mod
            for _ in range(5):
                try:
                    input_mod.key("f24")
                except Exception:  # noqa: BLE001 —— 个别环境占用 F24 时退到 F23
                    input_mod.key("f23")
                injected += 1
                time.sleep(0.15)
        except Exception as exc:  # noqa: BLE001
            say(f"        注入失败: {exc}")
        time.sleep(1.0)
        blocker.set_blocking(False)
        blocker.on_input_seen = None
        injected_seen = sum(1 for flag in seen if flag is False)
        record("1.3", "通过" if injected_seen >= 5 else "失败",
               f"注入 {injected} 次，钩子观测到注入事件 {injected_seen} 次")

    # ---- 1.4 钩子随进程死亡被摘除 ----
    if selected("1.4"):
        say("\n  【1.4】我会在**另一个进程**里装钩子并封锁，然后**脚本自己**杀掉它。")
        say("        预期：杀掉后键盘立即恢复（钩子随进程死亡被 OS 摘除）。")
        say("        期间你可以试着敲字 —— 应当没反应。")
        helper = _spawn_blocking_helper()
        if helper is None:
            record("1.4", "不适用", "未能启动子进程辅助程序")
        else:
            say(f"        子进程 pid={helper.pid} 已封锁。")
            for remaining in (6, 5, 4, 3, 2, 1):
                say(f"        … {remaining} 秒后杀掉子进程")
                time.sleep(1.0)
            helper.terminate()
            time.sleep(1.2)
            say("        已杀掉。")
            answer = ask("键盘是否已恢复（现在能正常打字）？")
            record("1.4", "通过" if answer.startswith("y") else "失败",
                   "钩子随进程死亡被摘除，输入恢复" if answer.startswith("y")
                   else "进程死后输入仍被封锁（严重）")

    # ---- 1.5 Ctrl+Alt+Del 永远可用 ----
    if selected("1.5"):
        say("\n  【1.5】封锁后请按 **Ctrl+Alt+Del**，应该能看到安全界面。")
        say("        看完按【物理 Esc】回来。")
        block_until_escape(blocker, "按 Ctrl+Alt+Del，看到安全界面后按【物理 Esc】回来。")
        answer = ask("按 Ctrl+Alt+Del 出现安全界面了吗？")
        record("1.5", "通过" if answer.startswith("y") else "失败",
               "Ctrl+Alt+Del 可穿透封锁" if answer.startswith("y") else "未出现（需排查）")

    blocker.stop()
    say("\n  §1 完成。\n")


def _spawn_blocking_helper() -> subprocess.Popen | None:
    """在独立进程里装钩子并封锁 —— 杀掉它可验证「钩子随进程消失」。"""
    src = Path(__file__).resolve().parents[2] / "src"
    script = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(src)!r})\n"
        "from cu.desktop.hooks import InputBlocker\n"
        "b = InputBlocker(); b.start(); b.set_blocking(True)\n"
        "print('blocked', flush=True)\n"
        "time.sleep(120)\n"
    )
    try:
        proc = subprocess.Popen([sys.executable, "-c", script],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return None
            time.sleep(0.2)
            if proc.stdout and proc.stdout.readline().strip() == "blocked":
                return proc
        return proc if proc.poll() is None else None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# §4 覆盖层观感
# ---------------------------------------------------------------------------


def section4() -> None:
    from cu.desktop.overlay import (  # noqa: PLC0415
        _CURSOR_GLOW_PX,
        _PILL_TOP_RATIO,
        _TARGET_GLOW_PX,
        _TARGET_RING_PX,
        ControlOverlay,
        OverlayState,
        _pill_font_px,
        _read_screen,
    )

    say("\n" + "=" * 72)
    say("§4 覆盖层观感")
    say("=" * 72)

    overlay = ControlOverlay()
    overlay.start()

    def show(state, seconds, target=None, cursor=None):
        overlay.set_target(target)
        overlay.set_cursor(cursor)
        overlay.transition(state)
        # **等首帧真的上屏再计时**。设了状态与画上去了是两件事；不等的话，
        # 「还没画」会被读成「画得不对」。
        if not overlay.wait_ready(timeout=8.0):
            say(f"        [!] 这一帧没能上屏：{overlay.last_error!r}")
        for remaining in range(int(seconds), 0, -1):
            say(f"        显示中… 剩余 {remaining:2d} 秒")
            time.sleep(1.0)

    screen = _read_screen()
    pill_top = int(screen.short_side * _PILL_TOP_RATIO)
    reach = max(24, int(screen.short_side * 0.24))
    say(f"""
  覆盖层被 WDA 排除出所有截图管线，**脚本截不到它** —— 只能你看。
  （另有一条上屏取证：overlay_visual_check.py 会临时关掉 WDA 再截图，
    那条验的是「在不在屏幕上」，观感仍然只能人眼看。）

  先说清楚该看哪儿、多大，免得对着屏幕猜：
    · 四边的光谱光晕：从每条边向内约 {reach}px 羽化到完全透明（本机短边 {screen.short_side}px 的 24%）。
      **没有边框、没有硬边**，越靠边越浓，顶部最浓、底部最淡。
    · 顶部胶囊：屏幕水平居中，距顶边约 {pill_top}px。整颗随屏幕短边缩放
      （本机字号约 {_pill_font_px(screen)}px，胶囊高约它的 2.5 倍，见 DEC-048）。
      深色实体胶囊 + 白字 + 左侧一个状态色圆点。
    · Active 态才有：琥珀色目标框（{_TARGET_RING_PX}px 实线 + {_TARGET_GLOW_PX}px 外发光）、
      光标处 {_CURSOR_GLOW_PX}px 白色光晕。

  [!] 4.0 是**所有其他项的前提**，所以即使你用 `--item` 只挑了一项，4.0 也会先跑一遍。
      上一轮你「完全没看到」—— 根因是三个真实缺陷（窗口 5 秒后被判定无响应而进程被杀、
      光晕几何写反成屏幕中央的十字、光晕缓冲被交给一个不会拉伸它的上屏调用）。
      都已修并各留了一条守卫。如果这一节你**依然什么都看不到**，请直接说。
""")

    try:
        # ---- 4.0 前置确认：覆盖层到底有没有出现（永远跑，其余项的前提） ----
        say("\n  【4.0】先确认覆盖层有没有显示。接下来 10 秒，请看屏幕：")
        say("        应当看到：**四边的光谱光晕** + **顶部一个深色胶囊**（带文字）。")
        show(OverlayState.ACTIVE, 10.0, target=(900, 600, 400, 300), cursor=(1700, 700))
        answer = ask("你看到覆盖层了吗？（四边光晕 + 顶部胶囊）")
        if not answer.startswith("y"):
            record("4.0", "失败", "覆盖层完全不可见 —— 下面的观感项全部无从验证")
            return
        record("4.0", "通过", "覆盖层可见（光晕 + 胶囊）")

        # 每一项独立成函数，再按 `--item` 逐个决定跑不跑 —— 这样可以直接跳到
        # 「只看 4.4」而不用把前面几条重做一遍。
        def check_4_9() -> None:
            say("\n  【4.9】显示 10 秒。请看**屏幕中央偏右那一处**有没有白色光晕")
            say("        （脚本把光晕画在固定坐标 (1700,700)，不是你鼠标的位置）。")
            say("        同时确认系统光标本身正常、无闪烁。")
            show(OverlayState.ACTIVE, 10.0, cursor=(1700, 700))
            answer = ask("那一处能看到白色光晕、且系统光标正常无闪烁？")
            record("4.9", "通过" if answer.startswith("y") else "失败",
                   "只画光晕 + 保留系统光标" if answer.startswith("y")
                   else "光晕不可见或系统光标异常")

        def check_4_4() -> None:
            say("\n  【4.4】逐个显示两个状态，每态 8 秒。对照 overlay.md §2.1（含 Delta）：")
            say("        active   -> 流动光谱 / 胶囊: AI is using your computer · [Esc] to cancel")
            say("                    / 琥珀目标框 / 光标光晕")
            say("        stopping -> **冻结成单一琥珀** / 胶囊: Stopping")
            say("        注意：**没有独立的「武装期」样子** —— 前摇与 Active 完全同相")
            say("        （同文案、同色相、同流速），所以它不再是一个要画出来的状态。")
            say("        也没有 error 态（DEC-080）：写操作失败不再占用覆盖层。")
            for state, note in (
                (OverlayState.ACTIVE, "流动光谱 + 琥珀目标框 + 光标光晕"),
                (OverlayState.STOPPING, "冻结单一琥珀（**不是**彩色的、只是不动的光谱）+ Stopping"),
            ):
                say(f"        —— {state.value}：{note}")
                show(state, 8.0, target=(900, 600, 400, 300), cursor=(1700, 700))
            overlay.transition(OverlayState.OFF)
            time.sleep(0.4)
            answer = ask("两态的胶囊文案与光谱颜色都对吗？")
            record("4.4", "通过" if answer.startswith("y") else "失败",
                   "三态文案与光谱符合 overlay.md §2.1 及其 Delta" if answer.startswith("y")
                   else "与规范不符")

        def check_4_5() -> None:
            say("\n  【4.5】显示 Active 15 秒。请**盯住光晕的最外缘**。")
            say("        预期：看不出「光在哪里结束」，没有硬边、也不像一块色块。")
            show(OverlayState.ACTIVE, 15.0)
            answer = ask("光晕外缘没有任何可感知的硬边界/内边界？")
            record("4.5", "通过" if answer.startswith("y") else "失败",
                   "无硬边界、无非线性衰减留下的内边界" if answer.startswith("y")
                   else "能看出边界或内边界")

        def check_4_6() -> None:
            say("\n  【4.6】显示 Active 16 秒，看光谱**有没有在流动**、速度约 7s 一圈。")
            say("        DEC-031 第 7 轮踩过这个坑：光谱曾经根本不流动（四个角的颜色是固定的）。")
            say("        只有一种流速了（DEC-047），所以不必再比较两态的差别。")
            show(OverlayState.ACTIVE, 16.0)
            answer = ask("光谱在流动吗？约 7s 一圈吗？")
            record("4.6", "通过" if answer.startswith("y") else "失败",
                   "光谱在流动、约 7s 一圈" if answer.startswith("y")
                   else "未流动或速度明显不符")
            overlay.transition(OverlayState.OFF)

        def check_4_2() -> None:
            say("\n  【4.2】覆盖层显示 15 秒。期间请**点一下这个终端窗口**。")
            say("        点击应当**穿过**覆盖层到达下面的窗口（比如能选中终端文字）。")
            show(OverlayState.ACTIVE, 15.0)
            answer = ask("点击有没有到达下方的窗口？")
            record("4.2", "通过" if answer.startswith("y") else "失败",
                   "点击穿透生效（WS_EX_TRANSPARENT）" if answer.startswith("y")
                   else "点击被覆盖层挡住（严重）")

        def check_4_3() -> None:
            say("\n  【4.3】覆盖层再显示 10 秒。留意**你当前激活的窗口有没有变**。")
            show(OverlayState.ACTIVE, 10.0)
            answer = ask("前台窗口有没有被覆盖层抢走？")
            record("4.3", "通过" if answer.startswith("n") else "失败",
                   "不抢焦点（WS_EX_NOACTIVATE）" if answer.startswith("n") else "抢走了焦点")

        def check_4_7() -> None:
            say("\n  【4.7】请把背景换成**深色**（深色壁纸或深色窗口铺满），然后回来。")
            input("        换好后按回车 …… ")
            show(OverlayState.ACTIVE, 12.0)
            answer = ask("深色背景下，顶部胶囊是否清晰可辨？")
            record("4.7", "通过" if answer.startswith("y") else "失败",
                   "深色背景下胶囊与背景分离" if answer.startswith("y") else "胶囊与背景融合")

        def check_4_8() -> None:
            record("4.8", "不适用", "本机只有一台显示器，1080p/4K 对照无法做")

        def check_4_10() -> None:
            say("\n  【4.10】状态切换时画面**不该卡顿**、光谱也不该跳回起点。")
            say("        背景：切换那一帧要重建胶囊与目标框，而帧预算只有 83ms；")
            say("        重建一次胶囊约 280ms —— 超出的部分就是肉眼看到的「卡一下」")
            say("        （实测切换后那一帧 392ms，正常帧 43ms）。胶囊现在按内容缓存、")
            say("        并在**不可见时**先画好，所以切换不该再有停顿。")
            say("        接下来切换几次，请盯住流动的光谱，看切换的**那一瞬间**：")
            overlay.transition(OverlayState.OFF)
            time.sleep(0.8)
            say("        —— OFF → Active（覆盖层出现）")
            show(OverlayState.ACTIVE, 8.0, target=(900, 600, 400, 300), cursor=(1700, 700))
            say("        —— Active → Stopping")
            show(OverlayState.STOPPING, 5.0)
            say("        —— Stopping → OFF（冻结态撤下）")
            overlay.transition(OverlayState.OFF)
            time.sleep(0.8)
            say("        —— OFF → Active（再出现一次，看冻结态回到流动是否连续）")
            show(OverlayState.ACTIVE, 5.0, target=(900, 600, 400, 300), cursor=(1700, 700))
            overlay.transition(OverlayState.OFF)
            time.sleep(0.3)
            answer = ask("切换的那一瞬间，光谱有没有顿一下、或者颜色跳回起点？")
            record("4.10", "通过" if answer.startswith("n") else "失败",
                   "切换时画面连续，无卡顿、无相位跳变" if answer.startswith("n")
                   else "切换时出现卡顿或相位跳变")

        # 顺序即阅读顺序，不是依赖顺序 —— 每个 check 自己把覆盖层摆到它要的状态。
        for item, check in (
            ("4.9", check_4_9),
            ("4.4", check_4_4),
            ("4.10", check_4_10),
            ("4.5", check_4_5),
            ("4.6", check_4_6),
            ("4.2", check_4_2),
            ("4.3", check_4_3),
            ("4.7", check_4_7),
            ("4.8", check_4_8),
        ):
            if selected(item):
                check()

    finally:
        overlay.transition(OverlayState.OFF)
        overlay.stop()


# ---------------------------------------------------------------------------


def main() -> int:
    global _ONLY

    parser = argparse.ArgumentParser(
        description="引导式人眼验收（需要真人坐在机器前）",
        epilog="可用条目：§1 的 1.1a 1.1b 1.2 1.3 1.4 1.5；"
               "§4 的 4.0 4.2 4.3 4.4 4.5 4.6 4.7 4.8 4.9 4.10。")
    parser.add_argument(
        "--section", default=None,
        help="要跑的节：1 / 4 / 1,4。默认按 --item 推断；都没给就跑 1,4。")
    parser.add_argument(
        "--item", default=None,
        help="只跑指定条目，逗号分隔（如 `--item 4.4` 或 `--item 1.1a,1.2`）。"
             "不适用「跳节」：指定了条目就只跑它，但 §4 的 4.0 是其余项的前提，"
             "所以选任何 4.x 时它都会先跑一遍。")
    parser.add_argument(
        "--list", action="store_true", help="只列出可用条目，不跑")
    args = parser.parse_args()

    if args.list:
        print("§1: 1.1a 1.1b 1.2 1.3 1.4 1.5")
        print("§4: 4.0 4.2 4.3 4.4 4.5 4.6 4.7 4.8 4.9 4.10")
        return 0

    if args.item:
        _ONLY = {part.strip() for part in args.item.split(",") if part.strip()}

    ensure_dpi_awareness()
    if args.section is not None:
        sections = {s.strip() for s in args.section.split(",")}
    elif _ONLY is not None:
        # `--item 4.4` 就只跑 §4 —— 否则 §1 会白跑一趟（所有条目都被过滤掉）。
        sections = {item.split(".")[0] for item in _ONLY}
    else:
        sections = {"1", "4"}

    say("=" * 72)
    say("Computer-Use 引导式人眼验收")
    say("=" * 72)
    say("""
  每题都是：脚本制造状态 -> 你做一件事 -> **按物理 Esc 结束**（有 30 秒倒计时）。

  保底三层：物理 Esc / 30 秒看门狗 / Ctrl+Alt+Del。

  直接跳到某一项：`--item 4.4`（逗号分隔可多选）。§4 的 4.0 是其余项的前提，
  所以挑任何 4.x 时它都会先跑一遍。

  [!] 如果本终端是**以管理员身份**运行的，钩子收不到事件、1.1 会失败 ——
      那种情况请换一个普通权限的终端重跑（不是产品的问题，是 UIPI）。
""")

    if "1" in sections:
        section1()
    if "4" in sections:
        section4()

    say("\n" + "=" * 72)
    say("结果汇总")
    say("=" * 72)
    for item, verdict, detail in RESULTS:
        say(f"  §{item:<6} {verdict:<5} {detail}")
    passed = sum(1 for _, v, _ in RESULTS if v == "通过")
    failed = [r for r in RESULTS if r[1] == "失败"]
    say(f"\n  通过 {passed} · 失败 {len(failed)} · "
        f"不适用 {sum(1 for _, v, _ in RESULTS if v == '不适用')}")
    if failed:
        say("\n  失败的项：")
        for item, _, detail in failed:
            say(f"    §{item}: {detail}")

    out = (Path(__file__).resolve().parents[3] / "tmp-doc" / "2026-09-12"
           / "guided-acceptance-result.md")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# 引导式验收结果", "",
                 f"- 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"- 通过 {passed} · 失败 {len(failed)}", "",
                 "| 项 | 结论 | 说明 |", "|---|---|---|"]
        lines += [f"| §{i} | {v} | {d} |" for i, v, d in RESULTS]
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        say(f"\n  结果已写入: {out}")
    except OSError as exc:
        say(f"\n  [!] 写文件失败（{exc}）—— 请把上面的输出贴回对话。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
