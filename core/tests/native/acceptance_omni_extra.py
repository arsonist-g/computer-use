"""验收清单 §7（OmniParser）里能自动化的两条 —— 7.5 与 7.7。

  - **7.5 结构化数据不裁剪**（DEC-015）：md 里的元素行数必须与 `element_count`
    逐一对上，坐标一位不少、也没有「…」这类截断标记。
  - **7.7 VLM 失败不破坏原始数据**：故意把端点配错，`parse --ai` 应当报 `vlm_failed`，
    而**检测那一步的成果必须还在**（DEC-011 的硬契约是「AI 只改描述、不动 bbox」，
    更基本的是：别把原始数据弄丢）。

§7 其余各条已在 2026-09-12 的实测里有了结论（见 `tests/acceptance.md` §7），
不在这里重复跑：那些要真跑一次解析（每次约 45 秒），而它们验的是**质量**，
不是**契约**，重复跑一遍不会改变结论。

跑法：
    .venv/Scripts/python.exe core/tests/native/acceptance_omni_extra.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import add_src_to_path, local_pipe, utf8_console  # noqa: E402

utf8_console()
CORE = add_src_to_path()

PIPE = local_pipe()
RESULTS: list[tuple[str, str, str]] = []
#: 故意配错的端点：本机 9 端口没有任何东西在听，连接会立刻失败（不会挂 45 秒）。
DEAD_ENDPOINT = "http://127.0.0.1:9"


def record(item: str, verdict: str, detail: str) -> None:
    RESULTS.append((item, verdict, detail))
    print(f"[{verdict:4}] §{item}  {detail}", flush=True)


def cu(*args: str, timeout: float = 1200.0) -> tuple[int, str, str]:
    env = {**os.environ, "COMPUTER_USE_PIPE": PIPE, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-m", "cu", *args], cwd=str(CORE), env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def cu_json(*args: str, timeout: float = 1200.0) -> dict:
    # `--json` 写在**最后**（子命令之后）：契约 §1.5 的写法，顺带覆盖 F11 的形态。
    _code, out, _err = cu(*args, "--json", timeout=timeout)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {}


def pick_window() -> str | None:
    """挑面积最大的可见窗口当靶子。"""
    rows = (cu_json("windows").get("result") or {}).get("windows") or []
    best, best_area = None, 0
    for row in rows:
        _x, _y, w, h = row["rect"]
        if w * h > best_area:
            best, best_area = row, w * h
    return best["hwnd"] if best else None


def has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def pick_chinese_window() -> str | None:
    """挑一个**标题里有中文**的窗口 —— 7.3 要的就是中文界面。"""
    rows = (cu_json("windows").get("result") or {}).get("windows") or []
    chinese = [row for row in rows if has_cjk(row.get("title") or "")]
    if not chinese:
        return None
    best = max(chinese, key=lambda row: row["rect"][2] * row["rect"][3])
    return best["hwnd"]


def element_rows(text: str) -> list[list[str]]:
    """把 md 里的元素行拆出来（表格里第一格是序号的那些）。"""
    rows = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and cells[0].isdigit():
            rows.append(cells)
    return rows


def parse_bbox(raw: str) -> tuple | None:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


# ---------------------------------------------------------------------------


def check_7_5(hwnd: str, session: str) -> tuple[str, int] | None:
    started = time.monotonic()
    # **只跑一次**：解析在 CPU 上要几十秒，跑两遍纯粹是浪费（第一版就这么写错了）。
    code, out, err = cu("--json", "parse", "--hwnd", hwnd, "--session", session)
    elapsed = time.monotonic() - started
    if code != 0:
        record("7.5", "失败", f"parse 失败：exit={code} {err.splitlines()[:2]}")
        return None
    try:
        payload = json.loads(out).get("result") or {}
    except json.JSONDecodeError:
        record("7.5", "失败", f"parse 输出无法解析：{out[:200]}")
        return None
    path = Path(payload.get("path") or "")
    count = int(payload.get("element_count") or 0)
    if not path.exists():
        record("7.5", "失败", f"parse 报成功但文件不存在：{path}")
        return None
    text = path.read_text(encoding="utf-8")
    rows = element_rows(text)
    boxes = [parse_bbox(row[2]) for row in rows if len(row) > 2]
    bad_boxes = [box for box in boxes if box is None]
    truncated = ("…" in text) or ("..." in text) or not text.endswith("\n")
    declared = None
    for line in text.splitlines():
        if line.startswith("- elements:"):
            declared = int(line.split(":")[1].strip())
    ok = (len(rows) == count and declared == count and not bad_boxes and not truncated)
    record("7.5", "通过" if ok else "失败",
           f"元素 {count} 个 · md 表里 {len(rows)} 行 · md 头部声明 {declared} 个 · "
           f"bbox 解析失败 {len(bad_boxes)} 个 · 有截断标记={truncated} · "
           f"耗时 {elapsed:.0f}s · {path.name}")
    return str(path), count


def check_7_3(session: str) -> None:
    """中文界面上解析，看检测/OCR 能不能把中文读出来。

    这一条原写作「元素描述可用」——那半是主观的。能客观回答的是**识别端**：
    解析出的 md 里到底有没有中文文本、有几个、长什么样。
    """
    hwnd = pick_chinese_window()
    if hwnd is None:
        record("7.3", "不适用", "当前没有标题含中文的窗口可作靶子")
        return
    started = time.monotonic()
    code, out, err = cu("--json", "parse", "--hwnd", hwnd, "--session", session)
    elapsed = time.monotonic() - started
    if code != 0:
        record("7.3", "失败", f"parse 失败：exit={code} {(out or err)[:200]}")
        return
    try:
        payload = json.loads(out or err).get("result") or {}
    except json.JSONDecodeError:
        record("7.3", "失败", "输出无法解析")
        return
    path = Path(payload.get("path") or "")
    if not path.exists():
        record("7.3", "失败", f"文件不存在：{path}")
        return
    rows = element_rows(path.read_text(encoding="utf-8"))
    chinese = [row[4] for row in rows if len(row) > 4 and has_cjk(row[4])]
    samples = "；".join(chinese[:4])[:120]
    record("7.3", "通过" if chinese else "失败",
           f"元素 {len(rows)} 个，其中 {len(chinese)} 个含中文文本 · 样本：{samples or '（无）'}"
           f" · 耗时 {elapsed:.0f}s · {path.name}")


def check_7_7(hwnd: str, session: str) -> None:
    """把端点故意配错，看 `--ai` 失败时原始结构化数据还在不在。"""
    before = cu_json("config", "show").get("result") or {}
    saved = (before.get("vlm") or {}).get("base_url", "")
    try:
        code, _out, err = cu("config", "set", "vlm.base_url", DEAD_ENDPOINT)
        if code != 0:
            record("7.7", "失败", f"改不了端点：exit={code} {err.splitlines()[:1]}")
            return
        started = time.monotonic()
        # `--json` 写在**子命令之后** —— 契约 §1.5 的写法。
        code, out, err = cu("parse", "--hwnd", hwnd, "--ai", "--session", session, "--json")
        elapsed = time.monotonic() - started
        # **错误信封也走 stdout**（Q-025 / 契约 §5 Delta）：`--json` 决定格式，流跟着它走 ——
        # 集成方只读 stdout 也不会漏掉错误。这里**只读 stdout**，正是为了让
        # 「错误跑回 stderr」这件事一旦回归就立刻变红。
        raw = out
        try:
            error = (json.loads(raw).get("error") or {}) if raw else {}
        except json.JSONDecodeError:
            error = {}
        failed_properly = code != 0 and error.get("code") == "vlm_failed"
        detail = error.get("detail") or {}
        raw_base = detail.get("base_markdown") or ""
        base_path = Path(raw_base) if raw_base else Path("<未给出路径>")
        # 必须**是文件**：`Path("")` 会退化成 `.`，而目录的 size 也大于 0，
        # 只判 size 会把「压根没落盘」误判成「还在」。
        survived = base_path.is_file() and base_path.stat().st_size > 0
        rows = element_rows(base_path.read_text(encoding="utf-8")) if survived else []
        if not failed_properly:
            print(f"        原始响应：stdout {len(out)} 字符 / stderr {len(err)} 字符 · "
                  f"解析出的 error={error!r}", flush=True)
        record("7.7", "通过" if (failed_properly and survived and rows) else "失败",
               f"配错端点后 parse --ai：exit={code} 错误码={error.get('code')!r}"
               f"（应 vlm_failed）耗时 {elapsed:.0f}s · "
               f"原始结构化数据仍在={survived}"
               + (f"（{base_path.name}，{len(rows)} 行）" if survived else "（没有留下任何文件）")
               + f" · 提示语={error.get('hint', '')[:40]}")
    finally:
        cu("config", "set", "vlm.base_url", saved or "")


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    print("=" * 72)
    print("验收 §7 OmniParser（7.5 数据不裁剪 / 7.7 VLM 失败不丢原始数据）")
    print("=" * 72)
    print("  [!] 每次 parse 要跑一次真实推理（CPU 上约 45 秒）。\n")

    status = cu_json("daemon", "status").get("result") or {}
    if not status.get("omni_ready"):
        record("7.5", "不适用", f"omni 环境未就绪：{status.get('omni_reason')}")
        record("7.7", "不适用", "同上")
        return 0

    only = set()
    for index, arg in enumerate(sys.argv):
        if arg == "--only" and index + 1 < len(sys.argv):
            only = {part.strip() for part in sys.argv[index + 1].split(",") if part.strip()}

    hwnd = pick_window()
    if hwnd is None:
        record("7.5", "失败", "没有可用窗口")
        record("7.7", "失败", "同上")
        return 1
    session = ((cu_json("begin", "--agent-hint", "acceptance-7").get("result") or {})
               .get("session_id") or "")
    try:
        if not only or "7.3" in only:
            check_7_3(session)
        if not only or "7.5" in only:
            check_7_5(hwnd, session)
        if not only or "7.7" in only:
            check_7_7(hwnd, session)
    finally:
        if session:
            cu("session", "end", "--session", session)

    print("\n===== §7 结果 =====")
    failed = [r for r in RESULTS if r[1] == "失败"]
    for item, verdict, detail in RESULTS:
        print(f"  §{item:<5} {verdict:<5} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
