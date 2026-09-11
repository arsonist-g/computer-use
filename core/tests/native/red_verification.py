"""红相验证（变异法）—— 脚本化，避免手工还原漏改。

为什么要脚本：本项目还没有 git，手工「改了再改回来」没有 diff 可对，
一旦漏还原，缺陷就静默进了产品代码。这里每轮都用**逐字节一致**保证还原。

对每处源码做一次临时突变 → 跑测试 → 断言「确实红了」→ 还原 → 断言「逐字节还原」。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import utf8_console  # noqa: E402

utf8_console()

ROOT = Path(__file__).resolve().parents[2]           # core/
SRC = ROOT / "src" / "cu"
PYTHON = Path(sys.executable)

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # (说明, 文件, 原文, 突变为, 期望)]
    (
        "错误码 ↔ 退出码映射缺一项",
        "errors.py",
        '    ErrorCode.INVALID_PARAMS: ExitCode.PARAMS,\n',
        '',
        "映射表少一个键，一一对应守卫必须红",
    ),
    (
        "错误码的线上字符串值被改动",
        "errors.py",
        '    CAPTURE_BLACK = "capture_black"',
        '    CAPTURE_BLACK = "capture_dark"',
        "线上契约值被改，契约值测试必须红",
    ),
    (
        "hint 表键与枚举错位",
        "errors.py",
        "    ErrorCode.CAPTURE_BLACK: \"截到全黑帧；该窗口可能受保护或仍在渲染，稍后重试。\",\n",
        '',
        "hint 缺失，一一对应守卫必须红",
    ),
    (
        "NDJSON 分帧改为 ensure_ascii（中文被转义）",
        "protocol.py",
        'ensure_ascii=False, separators=(",", ":")',
        'ensure_ascii=True, separators=(",", ":")',
        "中文/emoji 往返测试必须红",
    ),
    (
        "read_line 上限判断由 > 放宽为 >=（差一）",
        "protocol.py",
        "        if len(buf) > limit:",
        "        if len(buf) >= limit:",
        "上限差一测试必须红",
    ),
    (
        "配置校验不再拒绝非法图片格式（静默放行）",
        "config.py",
        '        need(self.image_format in ("png", "webp"),',
        '        need(True or self.image_format in ("png", "webp"),',
        "非法 image_format 必须被拒，校验测试必须红",
    ),
    (
        "config set 白名单外键被放行",
        "config.py",
        "    if key not in SETTABLE_KEYS:",
        "    if False and key not in SETTABLE_KEYS:",
        "白名单守卫必须红",
    ),
    (
        "清单校验不再拒绝非法 status（应视为 orphaned）",
        "manifest.py",
        "        if not (isinstance(status, str) and status in SESSION_STATUSES):",
        "        if not (isinstance(status, str) and (True or status in SESSION_STATUSES)):",
        "契约层之外：这里应当红（若仍绿说明该行为未被覆盖）",
    ),
    (
        "seq 计数器不再自增（同一会话内工件撞名）",
        "manifest.py",
        "        self.seq_counter += 1",
        "        self.seq_counter += 0",
        "seq 单调性未被测试覆盖的话会漏红",
    ),
    (
        "工件文件名不再零填充序号",
        "ids.py",
        '    parts.append(f"{seq:04d}")',
        '    parts.append(f"{seq}")',
        "字典序=时间序的保证；未被覆盖则漏红",
    ),
    (
        "title_slug 不再过滤非法字符",
        "ids.py",
        "    cleaned = _ILLEGAL_IN_FILENAME.sub(\"\", title or \"\")",
        "    cleaned = (title or \"\")",
        "文件名注入防护；未被覆盖则漏红",
    ),
    (
        "hwnd 不再按 0x%08X 格式化",
        "ids.py",
        '    return f"0x{hwnd:08X}"',
        '    return f"0x{hwnd:x}"',
        "hwnd 统一格式的契约；未被覆盖则漏红",
    ),
    (
        "守卫阳性：契约层真的 import 了 numpy（DEC-039 违约）",
        "config.py",
        "from .errors import CUError, ErrorCode",
        "import numpy  # 故意的违约，用于验证守卫有效\nfrom .errors import CUError, ErrorCode",
        "import 链守卫必须红",
    ),
]


def run_pytest() -> tuple[int, str]:
    proc = subprocess.run(
        [str(PYTHON), "-m", "pytest", "-q", "--no-header", "-x", "--tb=no"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    # 基线：必须全绿，否则后面的红相没有意义
    code, out = run_pytest()
    baseline = out.strip().splitlines()[-1] if out.strip() else "?"
    print(f"[基线] exit={code}  {baseline}")
    if code != 0:
        print("基线不绿，红相验证无意义。中止。")
        return 1

    failures: list[str] = []
    for label, filename, old, new, why in MUTATIONS:
        path = SRC / filename
        original = path.read_bytes()
        text = original.decode("utf-8")
        if old not in text:
            failures.append(f"{label}：突变锚点未命中（{filename}）—— 锚点可能已随代码演进失效")
            print(f"[锚点未命中] {label} ({filename})")
            continue

        path.write_bytes(text.replace(old, new, 1).encode("utf-8"))
        try:
            code, out = run_pytest()
            summary = out.strip().splitlines()[-1] if out.strip() else "?"
        finally:
            path.write_bytes(original)
            restored = path.read_bytes()
            if restored != original:
                failures.append(f"{label}：还原后与原文不一致！")
                print(f"[还原失败] {label} —— 必须立刻人工检查 {path}")
                return 2

        red = code != 0
        verdict = "红✓" if red else "绿✗（未被覆盖）"
        print(f"[{verdict}] {label}  ({filename})  -> {summary}")
        if not red:
            failures.append(f"{label}：突变后测试仍全绿，该行为未被覆盖（{why}）")

    print("\n===== 红相验证汇总 =====")
    if failures:
        for item in failures:
            print(f"  未达预期：{item}")
    else:
        print(f"  全部 {len(MUTATIONS)} 处突变都让测试变红，且源码全部逐字节还原。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
