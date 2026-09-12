"""引导式人眼验收 —— `acceptance.md` §1（输入封锁与逃生）与 §4（覆盖层观感）。

## 上一版错在哪（保留这段，免得再犯）

1. **把「让光标进记事本」与「按回车激活终端」当成了可以同时成立的事。**
   它们是互斥的 —— 你要么在记事本要么在终端。正确的做法是**根本不碰记事本**：
   你人就在终端里，光标也在终端里，直接在终端打字就能验证封锁。
2. **1.1 那一步「按 Esc 没反应」。** 根因在产品代码：钩子里
   `if self.on_abort is not None: self.on_abort()` 是有条件的，
   而 `return 1`（吞掉 Esc）是**无条件**的。于是没挂回调时，Esc 被吞掉且什么都不发生。
   **物理 Esc 是安全底线，不能依赖回调是否存在** —— 已修：
   先把 `_blocking` 清掉再通知回调，且清封锁是无条件的。
3. **用固定时长而不是用 Esc 结束。** 这既浪费你的时间，又把「Esc 能不能用」
   这个**最该先验的**东西排在了后面。

## 现在的设计

- **每一项都由你按 Esc 结束**，不设固定时长（只有 8 秒的兜底看门狗，防钩子没装上时卡住）。
- **不碰任何编辑器**。要验证「键被吞了」，你直接在**终端里敲几个字母**看有没有出现。
  终端里不按回车就不会提交，安全。
- §1.1 验的就是「**进了封锁能不能用 Esc 出来**」—— 那是第一位的问题。

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
#: 兜底看门狗时长。够长，让你有时间反应；够短，不至于在钩子没装上时把终端卡住。
WATCHDOG_SECONDS = 8.0


def say(text: str = "") -> None:
    print(text, flush=True)


def ask(question: str, options: str = "y/n") -> str:
    """正常提问（封锁已解除，输入可用）。"""
    while True:
        answer = input(f"  {question} [{options}] ").strip().lower()
        if answer:
            return answer


def record(item: str, verdict: str, detail: str) -> None:
    RESULTS.append((item, verdict, detail))
    say(f"    → §{item}: {verdict}  {detail}\n")


def block_until_escape(blocker, what_to_do: str) -> tuple[bool, float]:
    """封锁输入，直到用户按物理 Esc（或看门狗兜底）。

    返回 `(是否由 Esc 解除, 耗时秒)`。

    **Esc 是唯一的主要退出手段** —— 这正是在验「用户能不能随时拿回控制」。
    看门狗只是防止钩子状态异常时把你卡住。
    """
    aborted = threading.Event()
    blocker.on_abort = aborted.set

    watchdog = threading.Timer(WATCHDOG_SECONDS, blocker.set_blocking, args=(False,))
    watchdog.start()

    blocker.set_blocking(True)
    say(f"        >>> 已封锁 <<<  {what_to_do}")
    say(f"        （按【物理 Esc】结束；{WATCHDOG_SECONDS:.0f} 秒无操作会自动解封兜底）")

    started = time.monotonic()
    while time.monotonic() - started < WATCHDOG_SECONDS + 2:
        if aborted.is_set() or not blocker.blocking:
            break
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
  每一项都是：脚本封锁 → 你做一件事 → **按物理 Esc 结束**。

  安全保底（三层）：
    ① 物理 Esc 随时解封（这正是被验的机制）
    ② 每项都有 8 秒看门狗，到点自动解封
    ③ Ctrl+Alt+Del 无法被任何用户态钩子拦截，永远可用

  验证「键被吞了」的方式：**直接在终端里敲几个字母**，看它们有没有出现。
  不按回车就不会提交，安全。**不需要开记事本**（上一版让你去记事本，那是错的）。
""")

    blocker = InputBlocker()
    blocker.start()
    if not blocker.installed:
        say("  ⚠️ 钩子没装上，§1 无法进行（可能终端以管理员身份运行 —— 见下方提示）")
        blocker.stop()
        return

    # ---- 1.1 封锁期间打字被吞 + Esc 能出来（合并成一项：这是最该先验的） ----
    say("\n  【1.1】先验最重要的一件：**进了封锁，物理 Esc 能不能出来。**")
    say("        封锁开始后，请在终端里敲几个字母（比如 asdf），然后按【物理 Esc】。")
    by_escape, elapsed = block_until_escape(
        blocker, "敲几个字母，然后按【物理 Esc】结束。")
    if not by_escape:
        record("1.1a", "失败",
               f"物理 Esc 未能解除封锁（{elapsed:.1f}s 后由看门狗兜底）—— 安全底线失效")
    else:
        record("1.1a", "通过", f"物理 Esc 解除封锁（{elapsed:.1f}s）")

    typed = ask("刚才敲的字母，有没有出现在终端里？")
    if typed.startswith("n"):
        record("1.1b", "通过", "物理键在封锁期间被吞（终端未收到字符）")
    else:
        record("1.1b", "失败", "物理键在封锁期间仍到达了终端")

    # ---- 1.2 连续两次封锁都能用 Esc 出来（证明不是一次性的） ----
    say("\n  【1.2】再来一次，确认 Esc 每次都管用。")
    by_escape2, elapsed2 = block_until_escape(blocker, "随便做点什么，然后按【物理 Esc】。")
    record("1.2", "通过" if by_escape2 else "失败",
           f"第二次封锁同样由 Esc 解除（{elapsed2:.1f}s）" if by_escape2
           else "第二次 Esc 失效（不可重复的逃生手段等于没有）")

    # ---- 1.3 注入输入在封锁期间被放行 ----
    say("\n  【1.3】封锁 3 秒，期间由**脚本注入**按键（F24，无副作用）。")
    say("        注入应当被放行 —— 否则 AI 自己的输入会被自己吞掉。")
    seen: list[bool] = []
    blocker.on_input_seen = seen.append
    blocker.set_blocking(True)
    injected = 0
    try:
        from cu.desktop import input as input_mod
        for _ in range(5):
            # F24 无副作用（键盘上没有、应用不绑），但个别环境可能占它 —— 退到 F23。
            try:
                input_mod.key("f24")
            except Exception:  # noqa: BLE001
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
    say("\n  【1.4】我会在**另一个进程**里装钩子并封锁，然后杀掉它。")
    say("        预期：杀掉后键盘立即恢复（钩子随进程死亡被 OS 摘除）。")
    helper = _spawn_blocking_helper()
    if helper is None:
        record("1.4", "不适用", "未能启动子进程辅助程序")
    else:
        say(f"        子进程 pid={helper.pid} 已封锁。")
        input("        按回车让脚本杀掉它 …… ")
        helper.terminate()
        time.sleep(1.2)
        say("        现在请试着敲几个字。")
        answer = ask("杀掉进程后键盘是否立即恢复？")
        record("1.4", "通过" if answer.startswith("y") else "失败",
               "钩子随进程死亡被摘除，输入恢复" if answer.startswith("y")
               else "进程死后输入仍被封锁（严重）")

    # ---- 1.5 Ctrl+Alt+Del 永远可用 ----
    say("\n  【1.5】封锁后请按 **Ctrl+Alt+Del**，应该能看到安全界面。")
    say("        这是保底逃生 —— 用户态钩子拦不住它。看完按 Esc 回来。")
    block_until_escape(blocker, "按 Ctrl+Alt+Del，看到安全界面后按 Esc 回来。")
    answer = ask("按 Ctrl+Alt+Del 出现安全界面了吗？")
    record("1.5", "通过" if answer.startswith("y") else "失败",
           "Ctrl+Alt+Del 可穿透封锁" if answer.startswith("y") else "未出现（需排查）")

    blocker.stop()
    say("\n  §1 完成。\n")


def _spawn_blocking_helper() -> subprocess.Popen | None:
    """在独立进程里装钩子并封锁 —— 杀掉它可验证「钩子随进程消失」。"""
    script = (
        "import sys, time\n"
        f"sys.path.insert(0, r'{Path(__file__).resolve().parents[2] / 'src'}')\n"
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
    from cu.desktop.overlay import ControlOverlay, OverlayState

    say("\n" + "=" * 72)
    say("§4 覆盖层观感")
    say("=" * 72)
    say("""
  覆盖层被 WDA 排除出所有截图管线，所以**脚本截不到它** —— 只能你看。
  （渲染缓冲里有没有画出目标框，已由 desktop_smoke.py 的 T7 自动验过。）

  这一节**不封锁输入**，只是把覆盖层显示出来。
""")

    overlay = ControlOverlay()
    overlay.start()

    def show(state, seconds, target=None, cursor=None):
        overlay.set_target(target)
        overlay.set_cursor(cursor)
        overlay.transition(state)
        time.sleep(seconds)

    try:
        # ---- 4.9 光标光晕（先看这个，因为它最不容易看漏） ----
        say("\n  【4.9】显示 8 秒。请找屏幕上的**白色光晕**（在光标位置）。")
        say("        当前实现是「只画光晕 + 保留系统光标」—— overlay.md §3.1 的降级路径。")
        show(OverlayState.ACTIVE, 8.0, cursor=(900, 600))
        answer = ask("能看到光标处的白色光晕、且系统光标正常无闪烁？")
        record("4.9", "通过" if answer.startswith("y") else "失败",
               "只画光晕 + 保留系统光标" if answer.startswith("y")
               else "光晕不可见或系统光标异常")

        # ---- 4.4 四态文案与颜色 ----
        say("\n  【4.4】逐个显示四个状态，每态 5 秒。对照 overlay.md §2.1：")
        say("        arming  → 快流光谱 / 胶囊: AI is using your computer · [Esc] to cancel")
        say("        active  → 慢流光谱 / 同胶囊 / 琥珀目标框 / 光标光晕")
        say("        stopping→ 冻结琥珀 / 胶囊: Stopping")
        say("        error   → 冻结红   / 胶囊: Something went wrong · [Esc] to dismiss")
        for state in (OverlayState.ARMING, OverlayState.ACTIVE,
                      OverlayState.STOPPING, OverlayState.ERROR):
            say(f"        显示 {state.value} ……")
            show(state, 5.0, target=(900, 600, 200, 120), cursor=(900, 600))
        overlay.transition(OverlayState.OFF)
        time.sleep(0.4)
        answer = ask("四态的胶囊文案与光谱颜色都对吗？")
        record("4.4", "通过" if answer.startswith("y") else "失败",
               "四态文案与光谱符合 overlay.md §2.1" if answer.startswith("y")
               else "与规范不符")

        # ---- 4.5 无硬边界 ----
        say("\n  【4.5】显示 Active 10 秒。请**盯住光晕的最外缘**。")
        say("        预期：看不出「光在哪里结束」，没有硬边、也不像一块色块。")
        show(OverlayState.ACTIVE, 10.0)
        answer = ask("光晕外缘没有任何可感知的硬边界/内边界？")
        record("4.5", "通过" if answer.startswith("y") else "失败",
               "无硬边界、无非线性衰减留下的内边界" if answer.startswith("y")
               else "能看出边界或内边界（DEC-031 要求非线性衰减）")

        # ---- 4.6 光谱流动 ----
        say("\n  【4.6】arming 7 秒（应约 2s 一圈）→ active 10 秒（应约 7s 一圈）。")
        say("        关键：**颜色有没有在动**。DEC-031 第 7 轮踩过这个坑，光谱曾根本不流动。")
        show(OverlayState.ARMING, 7.0)
        overlay.transition(OverlayState.OFF)
        time.sleep(0.3)
        show(OverlayState.ACTIVE, 10.0)
        answer = ask("光谱在流动吗？速度大致符合吗？")
        record("4.6", "通过" if answer.startswith("y") else "失败",
               "光谱在流动、速度符合 DEC-030" if answer.startswith("y") else "未流动或速度不符")
        overlay.transition(OverlayState.OFF)

        # ---- 4.2 点击穿透 ----
        say("\n  【4.2】覆盖层显示 12 秒。期间请**点一下这个终端窗口**（或任何窗口）。")
        say("        点击应当**穿过**覆盖层到达下面的窗口。")
        show(OverlayState.ACTIVE, 12.0)
        answer = ask("覆盖层显示期间，点击有没有到达下方的窗口（比如能选中终端文字）？")
        record("4.2", "通过" if answer.startswith("y") else "失败",
               "点击穿透生效（WS_EX_TRANSPARENT）" if answer.startswith("y")
               else "点击被覆盖层挡住（严重 —— 用户无法操作）")

        # ---- 4.3 不抢焦点 ----
        say("\n  【4.3】覆盖层再显示 8 秒。留意**你当前激活的窗口有没有变**。")
        show(OverlayState.ACTIVE, 8.0)
        answer = ask("前台窗口有没有被覆盖层抢走？")
        record("4.3", "通过" if answer.startswith("n") else "失败",
               "不抢焦点（WS_EX_NOACTIVATE）" if answer.startswith("n") else "抢走了焦点")

        # ---- 4.7 深色背景下胶囊可辨识 ----
        say("\n  【4.7】请把背景换成**深色**（深色壁纸或深色窗口铺满），然后回来。")
        input("        换好后按回车 …… ")
        show(OverlayState.ACTIVE, 10.0)
        answer = ask("深色背景下，顶部胶囊是否清晰可辨（靠那层 1px 浅色扩散环）？")
        record("4.7", "通过" if answer.startswith("y") else "失败",
               "深色背景下胶囊与背景分离" if answer.startswith("y") else "胶囊与背景融合")

        record("4.8", "不适用", "本机只有一台显示器，1080p/4K 对照无法做")

    finally:
        overlay.transition(OverlayState.OFF)
        overlay.stop()


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--section", default="1,4", help="要跑的节：1 / 4 / 1,4")
    args = parser.parse_args()

    ensure_dpi_awareness()
    sections = {s.strip() for s in args.section.split(",")}

    say("=" * 72)
    say("Computer-Use 引导式人眼验收")
    say("=" * 72)
    say("""
  每题都是：脚本制造状态 → 你做一件事 → **按物理 Esc 结束**。

  保底三层：物理 Esc / 8 秒看门狗 / Ctrl+Alt+Del。

  ⚠️ 如果本终端是**以管理员身份**运行的，钩子收不到事件、1.1 会失败 ——
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
        say(f"\n  ⚠️ 写文件失败（{exc}）—— 请把上面的输出贴回对话。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
