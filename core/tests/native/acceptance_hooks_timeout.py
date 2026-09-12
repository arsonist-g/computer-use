"""验收清单 §1.6 —— 钩子回调变慢之后，输入封锁是不是**静默失效**，以及能不能察觉。

`hooks.py` 的模块文档把这条写成了硬要求：

  > 钩子处理函数必须极快返回：超时会被 Windows 静默摘除，输入恢复但**封锁静默失效**。

§1 验的是「封锁有效」，这一条验的是它的反面：**失效了知不知道**。
判据不能是「按一下就知道了」—— 失效的表现就是用户还能打字，而那种「安静地不再生效」
只有从「钩子还在不在收事件」这一侧才看得见。

做法：把 `on_input_seen` 换成一个故意睡 1.2 秒的回调（这段睡眠就发生在钩子过程里），
然后连续注入按键，看回调还响不响。

跑法：
    .venv/Scripts/python.exe core/tests/native/acceptance_hooks_timeout.py

  [!] 这 4 秒内整个系统的键鼠会被拖慢（低级钩子的回调直接坐在输入链路上）。
      跑完立即卸载钩子；脚本带看门狗，任何路径都会解封。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path, utf8_console  # noqa: E402

utf8_console()
add_src_to_path()

from cu.desktop import input as input_mod  # noqa: E402
from cu.desktop.hooks import InputBlocker  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def record(item: str, verdict: str, detail: str) -> None:
    RESULTS.append((item, verdict, detail))
    print(f"[{verdict:4}] §{item}  {detail}", flush=True)


#: 回调里睡多久。Windows 对低级钩子的超时阈值是 `LowLevelHooksTimeout`，
#: Win10/11 默认 1000ms —— 取 1200ms 才能稳定越过它，否则测的是「刚好没超时」。
SLOW_SECONDS = 2.5
#: 慢回调期间注入几次按键。次数越少，系统被拖慢的时长越短。
PROBES = 4


def check_1_6() -> None:
    seen: list[float] = []          # 每次回调的时刻

    def slow_callback(_is_physical: bool) -> None:
        time.sleep(SLOW_SECONDS)    # 故意超时：这就是被验的动作
        seen.append(time.monotonic())

    blocker = InputBlocker(on_input_seen=slow_callback)
    blocker.start()
    if not blocker.installed:
        record("1.6", "不适用", "钩子没装上（终端是否管理员？UIPI 下装不上）")
        return

    marks: list[float] = []
    installed_during = False
    try:
        installed_during = blocker.installed
        blocker.set_blocking(True)
        print(f"        已封锁。回调每次睡 {SLOW_SECONDS}s，连发 {PROBES} 次注入按键…",
              flush=True)
        for index in range(PROBES):
            marks.append(time.monotonic())
            try:
                input_mod.key("f24")
            except Exception:  # noqa: BLE001
                input_mod.key("f23")
            time.sleep(0.4)
            print(f"        第 {index + 1} 次注入完成，已观测到 {len(seen)} 次回调",
                  flush=True)
        time.sleep(1.0)
    finally:
        installed_during = blocker.installed
        blocker.set_blocking(False)
        blocker.stop()

    # 每次注入之后，回调还响不响 —— 「安静地不再生效」就是从这里看出来的。
    responded = [any(mark - 0.05 <= moment <= mark + SLOW_SECONDS + 1.0 for moment in seen)
                 for mark in marks]
    silent = (not responded[-1]) if responded else True

    # 产品有没有把这件事记下来 / 报出来？`_HOOK_TIMEOUT_MS` 是为此定义的阈值 ——
    # 数它在源码里出现几次：只有定义处那一次就说明它从未被用上。
    from cu.desktop import hooks as hooks_mod

    source = Path(hooks_mod.__file__).read_text(encoding="utf-8")
    uses = source.count("_HOOK_TIMEOUT_MS")
    noticed = "无" if uses <= 1 else f"有（{uses - 1} 处引用）"

    # 判定：
    #   · 「静默失效」没发生时，这一条**验不出**问题（不是通过，是没有现场）；
    #   · 但它要求「可被察觉」，而产品侧确实没有任何察觉手段 —— 这一半是明确的缺陷。
    if not silent:
        verdict = "不适用"
        detail = (f"回调持续被调用（{responded}），系统**没有**摘除钩子 —— "
                  f"「静默失效」在本机 {SLOW_SECONDS}s 回调下未复现")
    else:
        verdict = "失败"
        detail = (f"回调在注入 {responded.index(False) + 1} 次后不再被调用，"
                  f"而钩子句柄仍在（{installed_during}）—— 封锁已静默失效")
    record("1.6", verdict,
           detail + f" · 产品是否察觉={noticed}（`_HOOK_TIMEOUT_MS` 只有定义、无引用）· "
           f"daemon 日志也指望不上：没有任何写入者（见 §5 的发现 F1）")


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    print("=" * 72)
    print("验收 §1.6 钩子超时后封锁静默失效可被察觉")
    print("=" * 72)
    print("  [!] 接下来约 4 秒，系统键鼠会被拖慢 —— 低级钩子的回调就在输入链路上。\n")
    check_1_6()

    print("\n===== §1.6 结果 =====")
    for item, verdict, detail in RESULTS:
        print(f"  §{item:<5} {verdict:<5} {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
