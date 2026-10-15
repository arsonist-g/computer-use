"""红相验证（变异法）—— 脚本化，避免手工还原漏改。

为什么要脚本：本项目还没有 git，手工「改了再改回来」没有 diff 可对，
一旦漏还原，缺陷就静默进了产品代码。这里每轮都用**逐字节一致**保证还原。

对每处源码做一次临时突变 → 跑测试 → 断言「确实红了」→ 还原 → 断言「逐字节还原」。
"""
from __future__ import annotations

import os
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
    # ---- DEC-094：截图之后自动解析 ----
    # 锚点一律按 `\n` 写，跨行片段照常写 —— 目标文件用 CRLF 时由
    # `_match_line_endings` 补齐，锚点不必迁就文件的行尾。
    (
        "截图之后不再自动解析",
        "daemon/core.py",
        'payload["parse"] = self._parse_after_capture(session_id, session, result, seq, params)',
        'payload["parse"] = {"status": "off", "reason": "config"}',
        "默认解析是这条改动的全部意义；未被覆盖则漏红",
    ),
    (
        "解析失败不再按原错误码回（塌缩成 internal_error）",
        "daemon/core.py",
        "        except CUError as exc:\n            # 失败不留引用",
        "        except ZeroDivisionError as exc:\n            # 失败不留引用",
        "错误码按码透传（DEC-052 同源）；未被覆盖则漏红",
    ),
    (
        "解析产物改吃一个新 seq（不再与来源图成对）",
        "daemon/core.py",
        "                shot=shot, ai=False, out_dir=session.directory, seq=seq)",
        "                shot=shot, ai=False, out_dir=session.directory,\n"
        "                seq=self.sessions.next_seq(session_id))",
        "md 与来源图同一个 seq；未被覆盖则漏红",
    ),
    (
        "omni 未装不再提前短路",
        "daemon/core.py",
        "        ready, why = omni_bridge.available()",
        "        ready, why = True, ''",
        "「没装」必须报成可修的状态而不是失败；未被覆盖则漏红",
    ),
    (
        "内联索引不再按 interactivity 过滤",
        "desktop/real.py",
        'if not isinstance(element, dict) or not element.get("interactivity"):',
        "if not isinstance(element, dict):",
        "只内联可交互元素是 token 预算的全部依据；未被覆盖则漏红",
    ),
    (
        "md 名不再跟着来源图",
        "desktop/real.py",
        "name = parsed_name_for(Path(source_shot.path).name, suffix)",
        "name = artifact_name('img', seq, suffix=suffix, ext='md')",
        "配套命名；未被覆盖则漏红",
    ),
    (
        "UIA 元素不再按截图的 origin 归一化",
        "desktop/real.py",
        "                    box[0] -= origin[0]\n                    box[1] -= origin[1]",
        "                    pass",
        "屏幕坐标与图像坐标混用会让合并静默失效；未被覆盖则漏红",
    ),
    (
        "自动解析默认关掉",
        "config.py",
        "    screenshot_parse: bool = True",
        "    screenshot_parse: bool = False",
        "默认值；未被覆盖则漏红",
    ),
    (
        "`--no-parse` 不再传到线上",
        "client.py",
        'params["parse"] = False',
        "pass",
        "单次关闭的入口；未被覆盖则漏红",
    ),
    (
        "worker 不再回传元素列表",
        # 唯一一处落在 core/src/cu 之外的锚点：worker 跑在 omni 环境里（DEC-037），
        # 路径按 `SRC / filename` 拼，所以用相对段回到仓库根。
        "../../../omni/worker.py",
        '"model_name": model_name, "elements": payload.get("elements") or []}',
        '"model_name": model_name}',
        "元素的坐标是这条改动的交付物；未被覆盖则漏红",
    ),
    # ---- DEC-097：UIA 通道的单位、enabled 列、现在能不能点 ----
    (
        "UIA 的 bbox 退回 (left, top, width, height)",
        "desktop/uia.py",
        '                "bbox": [left, top, left + width, top + height],',
        '                "bbox": [left, top, width, height],',
        "「左边距大于宽度」的元素会被静默丢掉；未被覆盖则漏红",
    ),
    (
        "interactive 不再看 IsEnabled",
        "desktop/uia.py",
        "    return control_id in _INTERACTIVE_TYPES and enabled",
        "    return control_id in _INTERACTIVE_TYPES",
        "灰掉的按钮会照样报可交互；未被覆盖则漏红",
    ),
    (
        "origin 归一化只减左上角",
        "desktop/real.py",
        "                    box[2] -= origin[0]\n                    box[3] -= origin[1]",
        "                    pass",
        "右下角留在屏幕坐标里，框会横跨半个屏幕；未被覆盖则漏红",
    ),
    (
        "元素表去掉 enabled 列",
        "../../../omni/worker.py",
        'COLUMNS = ("#", "type", "bbox", "interactivity", "enabled", "content", "source")',
        'COLUMNS = ("#", "type", "bbox", "interactivity", "content", "source")',
        "「为什么点不了」这条信息在最后一步丢掉；未被覆盖则漏红",
    ),
    (
        "合并时丢掉 enabled",
        "../../../omni/worker.py",
        '        if isinstance(e.get("enabled"), bool):\n            row["enabled"] = e["enabled"]',
        "        pass",
        "同上，丢在合并这一步；未被覆盖则漏红",
    ),
    (
        "诊断脚本的读表器退回按列位置取",
        # 这一处落在 core/tests/native/：它是四个原生脚本共用的读表器。
        "../../tests/native/_element_table.py",
        "parts = _SPLIT.split(line)",
        'parts = line.split("|")',
        "内容里的 `\\|` 会把一行切宽、整行被丢；加一列也会静默错位；未被覆盖则漏红",
    ),
    (
        "诊断脚本的读表器不再丢弃坏框",
        "../../tests/native/_element_table.py",
        '        box = _bbox(row.get("bbox", ""))\n        if box is None:\n            continue',
        '        box = _bbox(row.get("bbox", ""))',
        "坏框会带着 None 流到上面；未被覆盖则漏红",
    ),
    (
        "行里不再渲染 enabled 单元格（表头 7 列、行 6 格）",
        "../../../omni/worker.py",
        '            _cell(_flag(element.get("enabled"))),\n',
        "",
        "表结构对不上；未被覆盖则漏红",
    ),
    (
        "worker 自检的表头断言退回写死字符串",
        "../../../omni/worker.py",
        '    assert "| " + " | ".join(COLUMNS) + " |" in text, text',
        '    assert "| # | type | bbox | interactivity | content |" in text, text',
        "写死的表头一加列就断，而 `setup omni` 会跑这个自检；未被覆盖则漏红",
    ),
    # ---- DEC-098：翻看上限与返回上限是两件事 ----
    (
        "扫描上限退回与返回上限同值",
        "desktop/uia.py",
        "MAX_SCAN = 4000",
        "MAX_SCAN = 400",
        "两个界限相等 = 把修复撤销；未被覆盖则漏红",
    ),
    (
        "扫描预算改按返回上限算（回到旧行为）",
        "desktop/uia.py",
        "    return min(total, MAX_SCAN)",
        "    return min(total, MAX_ELEMENTS)",
        "「树里前 N 个下标」又被当成「前 N 个可用元素」；未被覆盖则漏红",
    ),
    (
        "两种截断合成一句",
        "desktop/uia.py",
        '    if returned >= MAX_ELEMENTS:\n        notes.append(f"可用元素已取满 {MAX_ELEMENTS} 个")',
        "    pass",
        "「为什么少了」会变得无从判断；未被覆盖则漏红",
    ),
    (
        "没截断也返回说明",
        "desktop/uia.py",
        '    return "已截断：" + "；".join(notes) if notes else ""',
        '    return "已截断：" + "；".join(notes)',
        "读的人会去找一个不存在的问题；未被覆盖则漏红",
    ),
    # ---- DEC-099：检测器读不出名字的内容一律清空 ----
    (
        "内容过滤器一律放行",
        "../../../omni/worker.py",
        "    stripped = text.strip()\n    if not stripped:\n        return False",
        "    stripped = text.strip()\n    if True:\n        return True",
        "过滤器整个失效；未被覆盖则漏红",
    ),
    (
        "路径形状规则取消",
        "../../../omni/worker.py",
        "    if _PATH_ONLY.fullmatch(stripped) and len(_PATH_DATA.findall(stripped)) >= 2:\n"
        "        return False\n",
        "",
        "801 次的那个 `M0,0L9,0 4.5,` 会回到索引里；未被覆盖则漏红",
    ),
    (
        "版本号例外取消",
        "../../../omni/worker.py",
        "    if _VERSION.fullmatch(stripped):\n        return True\n",
        "",
        "`v0.16.1` / `3.0` 这类真标签会被误伤；未被覆盖则漏红",
    ),
    (
        "字母段阈值放宽到 1",
        "../../../omni/worker.py",
        "    return _longest_letter_run(stripped) >= 2",
        "    return _longest_letter_run(stripped) >= 1",
        "`j147` / `21` / `W` 这类碎片会漏过；未被覆盖则漏红",
    ),
    (
        "弃权词集合清空",
        "../../../omni/worker.py",
        '    if stripped.strip(".").lower() in _ABSTENTIONS:\n        return False\n',
        "",
        "`unanswerable` 会被当成名字；未被覆盖则漏红",
    ),
    (
        "清空改成删元素",
        "../../../omni/worker.py",
        '        if isinstance(element, dict) and not looks_like_a_label('
        'str(element.get("content") or "")):\n            element["content"] = ""',
        "        pass",
        "框与可交互性会跟着内容一起丢；未被覆盖则漏红",
    ),
    (
        "索引不再排除无名元素",
        "desktop/real.py",
        '        content = _clip(str(element.get("content") or ""))\n        if not content:\n',
        '        content = _clip(str(element.get("content") or ""))\n        if False:\n',
        "无名条目会占满索引；未被覆盖则漏红",
    ),
]


def run_pytest() -> tuple[int, str]:
    proc = subprocess.run(
        [str(PYTHON), "-m", "pytest", "-q", "--no-header", "-x", "--tb=no"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def purge_bytecode() -> None:
    """清掉 `__pycache__`。

    必须做：突变期间有机会加载被改过的源码，CPython 会把它编译并缓存下来。
    还原只是写回**源文件**，那份缓存的字节码仍在，于是后续的 pytest 会拿它去跑，
    表现为「源码明明是对的，测试却按变异后的行为失败」。

    这不是假想的场景，本轮实测踩到过：`manifest.next_seq` 源码写着 `+= 1`，
    直接调用却返回 0，清掉 `__pycache__` 后立刻恢复。责任在这一侧，
    因为只有这个脚本会改写源码。
    """
    import shutil

    for cache in ROOT.rglob("__pycache__"):
        if ".venv" in cache.parts:
            continue
        shutil.rmtree(cache, ignore_errors=True)


def _match_line_endings(snippet: str, text: str) -> str:
    """把锚点里的 `\\n` 补成目标文件实际用的行尾。

    锚点按 `\\n` 写是唯一好读的写法，而本仓的 .py **行尾并不统一**
    （`.gitattributes` 里是 `* -text`，检出字节必须与索引一致）：有的是 CRLF、
    有的是 LF。按字节比对时，CRLF 文件里的跨行锚点永远命中不了，
    而脚本把这件事报成「锚点可能已随代码演进失效」—— 一个会把人引向错误方向的诊断
    （实测本轮 12 处锚点因此失配，其中多数是刚写下的新锚点，根本没有「演进」可言）。

    只改锚点、不碰源码，所以写回时不需要任何还原动作：替换出来的文本用的就是
    文件自己的行尾。文件里行尾不统一时不做转换 —— 那种情况下按 `\\n` 原样匹配，
    宁可让锚点失配（有明确报错）也不要悄悄改到文件的行尾。
    """
    body = snippet.replace("\r\n", "\n")
    has_crlf = "\r\n" in text
    has_lone_lf = text.replace("\r\n", "").count("\n") > 0
    if has_crlf and not has_lone_lf:
        return body.replace("\n", "\r\n")
    return body


def main() -> int:
    # CI 里默认跳过：本脚本会**临时改写 core/src 下的源码**，
    # 在一台正在被使用的机器上跑它，等于给别人的编辑埋雷。
    # `--ci` 显式开启（CI 的 runner 是隔离且一次性的，没有这个问题）。
    if "--ci" not in sys.argv and os.environ.get("CI") != "true":
        print("跳过红相验证：它会临时改写 core/src。用 --ci 显式运行。")
        return 0

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
        old = _match_line_endings(old, text)
        new = _match_line_endings(new, text)
        if old not in text:
            failures.append(f"{label}：突变锚点未命中（{filename}）—— 锚点可能已随代码演进失效")
            print(f"[锚点未命中] {label} ({filename})")
            continue

        restore_failed = False
        path.write_bytes(text.replace(old, new, 1).encode("utf-8"))
        try:
            code, out = run_pytest()
            summary = out.strip().splitlines()[-1] if out.strip() else "?"
        finally:
            path.write_bytes(original)
            # 还原必须逐字节一致：本项目靠这条保证「变异不残留」。
            restore_failed = path.read_bytes() != original
        if restore_failed:
            # 在 finally **之外**返回：finally 里的 return 会把正在传播的异常吞掉。
            print(f"[还原失败] {label} —— 必须立刻人工检查 {path}")
            return 2
        purge_bytecode()      # 见 purge_bytecode 的 docstring：陈旧字节码会假装缺陷还在

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
