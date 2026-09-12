"""验收清单 §2（坐标与 DPI）里可自动化的部分 —— 2.2 与 2.3。

对应 `core/tests/acceptance.md` §2。2.4 / 2.5 要多显示器，本机只有一台。

判据不是「看起来点对了」，而是**可客观读回的**两件事：

  - 窗口截图的 `origin` 与尺寸必须等于该窗口的 **DWM 扩展框**
    （CONSTRAINT-003：截图像素坐标系与 click 坐标系必须同源）。
    注意**不是** `GetWindowRect` —— 它多含一条不可见调整边框（本机左右各 7px）；
    第一轮验收正是把这条偏差实测出来，产品因此改用了扩展框；
  - 按 `origin + 图像坐标` 算出来的点，点到关闭按钮上，窗口就**真的关了**。
    这是坐标映射最硬的自证 —— 偏一度，点到的就是别处。

跑法：
    .venv/Scripts/python.exe core/tests/native/acceptance_coords.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _winutil as W  # noqa: E402
from _bootstrap import add_src_to_path, local_pipe, utf8_console  # noqa: E402

utf8_console()
CORE = add_src_to_path()

PIPE = local_pipe()
RESULTS: list[tuple[str, str, str]] = []


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


def check_2_3() -> None:
    """全屏截图 origin 恒为 (0,0)（DEC-001：坐标系原点就是主显示器左上角）。"""
    result = cu_json("begin", "--agent-hint", "acceptance-2.3")
    session = (result.get("result") or {}).get("session_id")
    shot = cu_json("screenshot", "--full", "--session", session)
    payload = shot.get("result") or {}
    origin = tuple(payload.get("origin") or ())
    record("2.3", "通过" if origin == (0, 0) else "失败",
           f"--full 的 origin={origin}（应 (0, 0)）· {payload.get('width')}x"
           f"{payload.get('height')} layer={payload.get('layer')}")
    cu("session", "end", "--session", session)


def check_2_2() -> None:
    session = None
    target = W.spawn_notepad()
    if target is None:
        record("2.2", "失败", "起不来记事本窗口")
        return
    try:
        W.move(target, 300, 220, 900, 560, top=True)
        # 记事本会在被激活之后再套用自己记住的位置/尺寸 —— 等它停下来再量。
        rect = W.settle(target)
        if rect is None:
            record("2.2", "失败", "读不到靶子窗口的矩形")
            return

        result = cu_json("begin", "--agent-hint", "acceptance-2.2")
        session = (result.get("result") or {}).get("session_id")

        shot = cu_json("screenshot", "--hwnd", hex(target), "--session", session)
        payload = shot.get("result") or {}
        origin = tuple(payload.get("origin") or ())
        width, height = int(payload.get("width") or 0), int(payload.get("height") or 0)
        # 截图那一刻窗口有没有又动过 —— 不查的话，差异会被误读成坐标偏移。
        after = W.rect(target)
        if not origin or not width:
            record("2.2", "失败", f"窗口截图没拿到 origin/尺寸：{payload}")
            return

        # ① 映射的基石：origin 与尺寸必须**正好等于 DWM 扩展框**。
        #
        #    不能用 `GetWindowRect` 当基准：它含 Win10/11 那条不可见调整边框
        #    （本机 125% 缩放下左右各多 7px）。WGC 交付的图像覆盖的是扩展框，
        #    拿 GetWindowRect 当原点会让 `origin + 图像坐标` 横向偏 7px —— 违反
        #    CONSTRAINT-003。这个偏差在第一次跑这条验收时被实测抓到，随后修掉。
        frame = W.extended_frame(target)
        if frame is None:
            record("2.2", "失败", "取不到 DWM 扩展框，无法判定坐标基准")
            return
        origin_ok = origin == (frame[0], frame[1])
        size_ok = abs(width - frame[2]) <= 1 and abs(height - frame[3]) <= 1
        # 两者之差就是那条不可见边框 —— 报出来，免得下次又踩。
        border = tuple(a - b for a, b in zip(rect, frame, strict=False))
        drift = None if after == rect else f"{rect} → {after}"

        # ② 点一个「点在别处就不会发生」的地方：关闭按钮（图像坐标里在右上角）。
        #
        #    这一条验的是**坐标映射**，所以落点用的是屏幕绝对坐标；`--hwnd` 只是顺带
        #    走一遍 DEC-013 的前置（自动前台）。`SetForegroundWindow` 会被系统规则
        #    拒绝（DEC-013 已把这个失效模式写进设计），那种情况下退回不带 --hwnd 的
        #    绝对坐标点击 —— 它照样落在目标窗口上，且验的还是同一件事。
        close_point = (origin[0] + width - 30, origin[1] + 15)
        attempts: list[str] = []

        def fire(with_hwnd: bool) -> tuple[int, str]:
            args = ["click", str(close_point[0]), str(close_point[1])]
            if with_hwnd:
                args += ["--hwnd", hex(target)]
            code, _out, err = cu(*args, "--session", session,
                                 "--describe", "验收 2.2：按 origin+图像坐标 点关闭按钮",
                                 "--end")
            attempts.append(f"{'带' if with_hwnd else '不带'} --hwnd: exit={code}"
                            + ("" if code == 0 else f" {err.splitlines()[:1]}"))
            return code, err

        code, err = fire(True)
        if code != 0 and "foreground_failed" in err:
            time.sleep(1.0)
            code, err = fire(False)
        for _ in range(30):
            if not W.alive(target):
                break
            time.sleep(0.1)
        closed = not W.alive(target)

        record("2.2", "通过" if (origin_ok and size_ok and closed and drift is None) else "失败",
               f"截图 origin={origin} vs DWM 扩展框左上={frame[:2]} 一致={origin_ok} · "
               f"截图 {width}x{height} vs 扩展框 {frame[2]}x{frame[3]} 一致={size_ok} · "
               f"GetWindowRect 比扩展框多出的不可见边框(左,上,右,下)={border} · "
               f"截图期间窗口漂移={drift or '无'} · "
               f"按图像坐标({width - 30},15)点关闭按钮后窗口已关闭={closed} · "
               f"点击尝试：{'；'.join(attempts)}")
    except Exception as exc:  # noqa: BLE001
        record("2.2", "失败", f"{type(exc).__name__}: {exc}")
    finally:
        if session:
            cu("session", "end", "--session", session)
        W.close(target)


def check_2_4_and_2_5() -> None:
    """多显示器两项：本机只有一台 —— 这个结论要用**数出来的**显示器数量支撑。"""
    from cu.desktop import windows as windows_mod

    context = windows_mod.display_context()
    count = len(context.monitors)
    scales = sorted({round(m.scale, 3) for m in context.monitors})
    verdict = "不适用" if count < 2 else "待验"
    detail = (f"本机显示器数={count}（缩放 {scales}）—— 多显示器 / 混合缩放场景无法构造"
              if count < 2 else f"检测到 {count} 台显示器，可构造")
    record("2.4", verdict, detail)
    record("2.5", verdict, detail)


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    print("=" * 72)
    print("验收 §2 坐标与 DPI（可自动化部分）")
    print("=" * 72)
    check_2_3()
    check_2_2()
    check_2_4_and_2_5()

    print("\n===== §2 结果 =====")
    failed = [r for r in RESULTS if r[1] == "失败"]
    for item, verdict, detail in RESULTS:
        print(f"  §{item:<5} {verdict:<5} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
