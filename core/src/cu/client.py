"""CLI 客户端 —— 命令面契约的落地（api-contract.md §1）。

它**无状态**：一次命令一个进程，连上 daemon、发一条请求、渲染响应、以对应退出码结束。
不持有任何状态，不直接碰桌面。

输出两条路：
  - 默认是**人读文本**（AI 直接读，token 友好）；
  - `--json` 给机器读，形状就是线协议的 `result`。
两者都是契约，改字段名要当成破坏性变更。

退出码与 `error_code` **并存而不互相替代**：退出码给 shell 与人，`error_code` 给 AI 与程序
（api-contract.md §5）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION, __version__
from .errors import EXIT_CODES, CUError, ErrorCode
from .ipc import PIPE_NAME, IpcClient
from .protocol import AppError, make_request

#: 与 daemon 约定的常量。`--describe` 在写命令上必填（DEC-019）。
ENV_SESSION = "COMPUTER_USE_SESSION"
#: 管道名覆盖。默认是固定名（同机同用户只有一条），测试与多实例场景可以覆盖 ——
#: 否则跑一次集成测试就要动用户真实的 daemon。
ENV_PIPE = "COMPUTER_USE_PIPE"
DAEMON_MODULE = "cu.daemon"

#: 冷启动窗口。daemon 要 import windows-capture（连带 numpy/opencv），
#: 实测远超 1 秒；给足余量比事后排查「管道不存在」便宜得多。
DAEMON_START_TIMEOUT = 25.0


def default_pipe() -> str:
    return os.environ.get(ENV_PIPE) or PIPE_NAME

#: 子进程标志：不占终端、不随父进程退出（DEC-035 的自动拉起）。
_DETACHED = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
_NO_WINDOW = 0x08000000


class _Parser(argparse.ArgumentParser):
    """参数错误要报成 `invalid_params` + 退出码 2，而不是 argparse 默认的退出码 2 + 英文散文。

    契约里退出码 2 的含义是「参数错误，修正命令」—— 这条与 argparse 恰好一致，
    但 `error_code` 必须是我们封闭枚举里的那个，AI 才能可靠分支。
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        raise CUError(ErrorCode.INVALID_PARAMS, message)


def _common_options() -> argparse.ArgumentParser:
    """所有子命令共用的选项。

    为什么做成 parent parser 而不是只放在主 parser 上：argparse 的全局选项
    **只能写在子命令之前**，而契约里的命令面示例（api-contract.md §1.5）是
    `computer-use screenshot --hwnd 0x1A2B --session s-...`——选项在子命令**之后**。
    只在主 parser 上加，用户按契约写就会得到 `unrecognized arguments`。
    """
    common = argparse.ArgumentParser(add_help=False)
    # 共用项的默认值必须是 `argparse.SUPPRESS`。
    #
    # 为什么不是 `None`：子命令解析器在收尾时会把**自己**的默认值 setattr 回命名空间，
    # 从而顶掉主 parser 已经解析出来的值。实测：`--json windows` 会静默退化成
    # 人读文本，`None` 也拦不住（子命令照样把 None 写回去）。`SUPPRESS` 让
    # 「用户没写」变成「压根不设这个属性」，父层的值才留得住。
    # 读取侧统一走 `getattr(..., default)`，见 `_resolve_common`。
    common.add_argument("--session", default=argparse.SUPPRESS,
                        help=f"会话 id（也可用环境变量 {ENV_SESSION}）")
    common.add_argument("--describe", default=argparse.SUPPRESS,
                        help="自然语言说明这次操作要做什么、为什么（写命令必填）")
    common.add_argument("--json", action="store_true", dest="as_json",
                        default=argparse.SUPPRESS, help="输出机器可读 JSON")
    common.add_argument("--inline", action="store_true", default=argparse.SUPPRESS,
                        help="截图 / 结构化数据内联返回 base64，而不是只给路径")
    common.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="追加低层细节")
    #: 写序列的显式续期 / 结束（DEC-045）。默认「不续期」—— 阈值仍是兜底，
    #: 但连续操作时带上 `--continue` 就不会重复走前摇、也不会中断输入封锁。
    common.add_argument("--continue", action="store_true", dest="keep_alive",
                        default=argparse.SUPPRESS,
                        help="我还要接着操作：保持覆盖层与输入封锁，不重新武装")
    common.add_argument("--end", action="store_true", dest="end_sequence",
                        default=argparse.SUPPRESS,
                        help="这是本次操作序列的最后一条：立即开始退场")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = _Parser(prog="computer-use", description="Computer-Use —— 面向 AI Agent 的桌面控制",
                     parents=[common])
    parser.add_argument("--version", action="version", version=f"computer-use {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    def add(name: str, help_text: str, **kwargs) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, parents=[common], **kwargs)

    # ---- 会话与生命周期 ----
    begin = add("begin", "创建会话（不取写锁）")
    begin.add_argument("--agent-hint", default=None, help="调用方自报身份，写入会话元数据")

    session = add("session", "会话查询与结束")
    session_sub = session.add_subparsers(dest="session_command", metavar="<list|info|end>")
    # 嵌套子命令也挂 common 父解析器。argparse 的选项只能写在它**所属的那一级**
    # 之前，而契约 §1.5 的示例写法正是「选项跟在子命令之后」—— 不挂就认不出
    # `session list --json`（实测 unrecognized arguments，退出码 2）。
    # `session info` / `session end` 曾经各自加过一个 `--session`（dest 覆盖），
    # 现在统一回 common 这一份，不在同一个选项上留两套定义。
    session_sub.add_parser("list", help="会话列表，按创建时间倒序", parents=[common])
    session_sub.add_parser("info", help="会话元数据", parents=[common])
    session_sub.add_parser("end", help="结束会话：释放锁 + 配额清理", parents=[common])

    # ---- 只读 ----
    windows = add("windows", "窗口列表，按 z-order 从上到下")
    windows.add_argument("--all", action="store_true", help="列出全部顶层窗口，不只可见有标题的")

    shot = add("screenshot", "截图")
    target = shot.add_mutually_exclusive_group(required=True)
    target.add_argument("--hwnd", default=None, help="窗口句柄，如 0x1A2B")
    target.add_argument("--full", action="store_true", help="截主显示器全屏")
    shot.add_argument("--monitor", type=int, default=None, help="配合 --full，指定显示器序号")
    shot.add_argument("--format", default=None, choices=["png", "webp"], help="图片格式")

    parse = add("parse", "把图片解析成结构化数据（OmniParser）")
    parse_target = parse.add_mutually_exclusive_group(required=True)
    parse_target.add_argument("--hwnd", default=None)
    parse_target.add_argument("--image", default=None, help="外部图片路径")
    parse.add_argument("--ai", action="store_true", help="再用多模态端点优化描述")

    # ---- 写 ----
    click = add("click", "点击屏幕绝对坐标")
    click.add_argument("x", type=int)
    click.add_argument("y", type=int)
    click.add_argument("--button", default="left", choices=["left", "right", "middle"])
    click.add_argument("--count", type=int, default=1, help="2 = 双击")
    click.add_argument("--hwnd", default=None)

    move = add("move", "移动光标（悬停）")
    move.add_argument("x", type=int)
    move.add_argument("y", type=int)
    move.add_argument("--hwnd", default=None)

    drag = add("drag", "按下 → 拟人移动 → 抬起")
    for name in ("x1", "y1", "x2", "y2"):
        drag.add_argument(name, type=int)
    drag.add_argument("--button", default="left", choices=["left", "right", "middle"])
    drag.add_argument("--hwnd", default=None)

    scroll = add("scroll", "滚动（正 dy = 向上）")
    scroll.add_argument("dx", type=int)
    scroll.add_argument("dy", type=int)
    scroll.add_argument("--at", nargs=2, type=int, default=None, metavar=("X", "Y"))

    type_cmd = add("type", "输入文本")
    type_cmd.add_argument("text")
    type_cmd.add_argument("--hwnd", default=None)

    key = add("key", "按下组合键，如 ctrl+c / alt+tab / esc")
    key.add_argument("combo")
    key.add_argument("--hwnd", default=None)
    key.add_argument("--force", action="store_true", help="越过危险键黑名单（DEC-019）")

    # ---- 锁 / 配置 / daemon ----
    lock = add("lock", "写锁")
    lock_sub = lock.add_subparsers(dest="lock_command", metavar="<status|unlock>")
    lock_sub.add_parser("status", help="锁持有者 / 持有时长 / 空闲状态", parents=[common])
    force = lock_sub.add_parser("unlock", help="强夺写锁（记入操作日志）", parents=[common])
    force.add_argument("--force", action="store_true", help="必须显式给出才算数")
    force.add_argument("--reason", default=None, help="强夺原因（必填，会写进日志）")

    config = add("config", "配置")
    config_sub = config.add_subparsers(dest="config_command", metavar="<show|set>")
    config_sub.add_parser("show", help="显示全部配置项", parents=[common])
    setter = config_sub.add_parser("set", help="修改单项配置", parents=[common])
    setter.add_argument("key")
    setter.add_argument("value")

    setup = add("setup", "安装可选组件")
    setup_sub = setup.add_subparsers(dest="setup_target", metavar="<omni>")
    omni_cmd = setup_sub.add_parser("omni", help="装配 OmniParser 环境（约 1.4GB 权重）",
                                    parents=[common])
    omni_cmd.add_argument("--force", action="store_true", help="重装依赖")
    omni_cmd.add_argument("--skip-weights", action="store_true", help="不下权重")

    daemon = add("daemon", "daemon")
    daemon_sub = daemon.add_subparsers(dest="daemon_command", metavar="<status|stop>")
    daemon_sub.add_parser("status", help="PID / 启动时间 / 管道名 / 活跃会话 / 占用",
                          parents=[common])
    daemon_sub.add_parser("stop", help="停止常驻进程（调试用）", parents=[common])

    return parser


# ---------------------------------------------------------------------------
# 命令面 → 线协议
# ---------------------------------------------------------------------------


def to_request(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """把解析好的命令行映射成一条 RPC 请求。"""
    command = args.command
    session_id = args.session or os.environ.get(ENV_SESSION) or None
    verbose = bool(getattr(args, "verbose", False))

    if command == "begin":
        return "session.begin", {"agent_hint": getattr(args, "agent_hint", None)}
    if command == "session":
        sub = args.session_command
        if sub == "list":
            return "session.list", {}
        if sub == "info":
            return "session.list", {}          # 客户端侧筛出一条，避免多一个方法
        if sub == "end":
            return "session.end", {"session_id": session_id}
        raise CUError(ErrorCode.INVALID_PARAMS, "session 需要一个子命令：list / info / end")
    if command == "windows":
        return "desktop.windows", {"all": args.all, "verbose": verbose}
    if command == "screenshot":
        if args.hwnd:
            params = {"hwnd": args.hwnd}
        else:
            params = {"monitor": args.monitor if args.monitor is not None else 0}
        params.update({"session_id": session_id, "format": args.format,
                       "inline": args.inline, "describe": args.describe})
        return "desktop.capture", params
    if command == "parse":
        return "omni.parse", {"hwnd": args.hwnd, "image": args.image, "ai": args.ai,
                              "session_id": session_id, "inline": args.inline}
    if command in ("click", "move", "drag", "scroll", "type", "key"):
        params: dict[str, Any] = {
            "session_id": session_id, "describe": args.describe,
            "continue": bool(getattr(args, "keep_alive", False)),
            "end": bool(getattr(args, "end_sequence", False)),
        }
        if command == "click":
            params.update({"x": args.x, "y": args.y, "button": args.button, "count": args.count})
        elif command == "move":
            params.update({"x": args.x, "y": args.y})
        elif command == "drag":
            params.update({"x1": args.x1, "y1": args.y1, "x2": args.x2, "y2": args.y2,
                           "button": args.button})
        elif command == "scroll":
            params.update({"dx": args.dx, "dy": args.dy, "at": args.at})
        elif command == "type":
            params["text"] = args.text
        elif command == "key":
            params.update({"combo": args.combo, "force": args.force})
        if getattr(args, "hwnd", None):
            params["hwnd"] = args.hwnd
        return f"input.{command}", params
    if command == "lock":
        if args.lock_command == "status":
            return "lock.status", {}
        if args.lock_command == "unlock":
            if not args.force:
                raise CUError(ErrorCode.INVALID_PARAMS,
                              "强夺写锁必须显式加 --force（它会让另一个会话的操作半途而废）")
            return "lock.forceUnlock", {"reason": args.reason, "session_id": session_id}
        raise CUError(ErrorCode.INVALID_PARAMS, "lock 需要一个子命令：status / unlock")
    if command == "config":
        if args.config_command == "show":
            return "config.get", {}
        if args.config_command == "set":
            return "config.set", {"key": args.key, "value": args.value}
        raise CUError(ErrorCode.INVALID_PARAMS, "config 需要一个子命令：show / set")
    if command == "setup":
        if args.setup_target == "omni":
            return "daemon.setup_omni", {
                "force": bool(getattr(args, "force", False)),
                "skip_weights": bool(getattr(args, "skip_weights", False)),
            }
        raise CUError(ErrorCode.INVALID_PARAMS, "setup 需要一个目标：omni")
    if command == "daemon":
        if args.daemon_command == "status":
            return "daemon.status", {}
        if args.daemon_command == "stop":
            return "daemon.stop", {}
        raise CUError(ErrorCode.INVALID_PARAMS, "daemon 需要一个子命令：status / stop")
    raise CUError(ErrorCode.INVALID_PARAMS, f"未知命令：{command}")


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

#: 包一层统一的 `--json` 信封：`{"ok":true,"result":...}` 或 `{"ok":false,"error":{...}}`。
#: 这样 AI 不必先判断顶层有没有 `error` 键。
def render_json(result: Any) -> str:
    return json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2)


def render_error_json(exc: CUError) -> str:
    return json.dumps({"ok": False, "error": {
        "code": exc.code.value, "message": exc.message,
        "hint": exc.hint, "detail": exc.detail,
    }}, ensure_ascii=False, indent=2)


def render_text(command: str, result: Any, verbose: bool = False) -> str:
    """人读文本（api-contract.md §1.5 的形态）。"""
    if command == "begin":
        return f"session {result['session_id']}\ndir {result['dir']}"
    if command == "windows":
        rows = result.get("windows", [])
        if not rows:
            return "（没有匹配的窗口）"
        lines = []
        for row in rows:
            x, y, w, h = row["rect"]
            lines.append(
                f"hwnd={row['hwnd']}  pid={row['pid']}  {row['process']:<20} "
                f"rect={x},{y},{w},{h}  fg={int(row['is_foreground'])}  "
                f"min={int(row['is_minimized'])}  elev={int(row['elevated'])}  "
                f'"{row["title"]}"'
                + (f"  class={row['class']}  z={row['zorder']}" if verbose and "class" in row else "")
            )
        return "\n".join(lines)
    if command == "screenshot":
        ox, oy = result["origin"]
        return (f"{Path(result['path']).name}  {result['width']}x{result['height']}  "
                f"origin={ox},{oy}  layer={result['layer']}")
    if command == "parse":
        tail = f"  model={result['model_name']}" if result.get("model_name") else ""
        return f"{Path(result['path']).name}  elements={result['element_count']}{tail}"
    if command in ("click", "move", "drag", "scroll", "type", "key"):
        parts = [f"ok  {command}"]
        if "moved_ms" in result:
            parts.append(f"moved={result['moved_ms']}ms")
        if "total_ms" in result:
            parts.append(f"total={result['total_ms']}ms")
        if result.get("warning"):
            parts.append(f"WARNING {result['warning']}")
        return "  ".join(parts)
    if command == "session":
        sub = result.get("_sub")
        if sub == "list":
            rows = result.get("sessions", [])
            if not rows:
                return "（还没有会话）"
            return "\n".join(
                f"{r['session_id']}  {r['status']:<8} created={r['created_at']}  "
                f"shots={r['screenshot_count']}  parsed={r['structured_count']}  "
                f"ops={r['op_count']}"
                for r in rows)
        if sub == "info":
            one = result.get("session")
            if not one:
                return "（没有该会话）"
            return "\n".join(f"{k}: {v}" for k, v in one.items())
        if sub == "end":
            return (f"ended  freed={result['freed_bytes']} bytes  "
                    f"files={result['deleted_files']}  "
                    f"sessions={len(result.get('deleted_sessions', []))}")
    if command == "lock":
        holder = result.get("holder")
        if not holder:
            return "锁空闲" + ("（有等待者）" if result.get("waiting") else "")
        return (f"持有者 {holder['session_id']}  pid={holder['pid']}  "
                f"held={holder['held_for_s']}s  idle={holder['idle_for_s']}s")
    if command == "config":
        return json.dumps(result, ensure_ascii=False, indent=2)
    if command == "setup":
        lines = [f"{key}: {value}" for key, value in result.items() if key != "steps"]
        if result.get("steps"):
            lines.append("本次执行: " + "、".join(result["steps"]))
        return "\n".join(lines)
    if command == "daemon":
        if "stopping" in result:
            return f"daemon 正在停止（pid={result['pid']}）"
        return "\n".join(
            f"{key}: {value}" for key, value in result.items()
            if key in ("pid", "started_at", "pipe_name", "active_sessions", "omni_refcount",
                       "idle_for_s", "resident_bytes", "protocol_version", "version",
                       "overlay_state", "input_blocked", "omni_ready", "omni_reason"))
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 连接（含自动拉起）
# ---------------------------------------------------------------------------


def _daemon_command(pipe: str) -> list[str]:
    """拉起 daemon 的命令行。

    用 `sys.executable -m cu.daemon` 而不是 `cu-daemon` 可执行文件：CLI 与 daemon
    来自同一个 Python 环境，用同一个解释器就绝不会出现版本错配。
    """
    return [sys.executable, "-m", DAEMON_MODULE, "--pipe", pipe]


def connect(pipe: str | None = None, timeout: float = 10.0,
            autostart: bool = True) -> IpcClient:
    """连接 daemon；连不上就以 detached 方式拉起它再连（DEC-035）。

    `CREATE_NO_WINDOW | DETACHED_PROCESS`：不占终端、不随父进程退出 ——
    用户不应该知道 daemon 的存在。

    **首次连接要等得住**：daemon 冷启动要 import `windows-capture`（它连带拉入
    numpy 与 opencv），实测远超过 1 秒。早先这里只等 2 秒就放弃，于是出现
    一条很难看的竞态 —— 第一个 daemon 还在启动，客户端已判定失败并去拉第二个，
    第二个被单实例锁挡住直接退出，等第一个的管道终于建好时，客户端早已结束。
    表现是「daemon 明明在跑，CLI 却报管道不存在」。
    """
    pipe = pipe or default_pipe()
    # 先给一小段窗口，让**已经存在的** daemon 有机会应答 —— 热路径通常几十毫秒。
    client = IpcClient(pipe, connect_timeout=min(timeout, 2.0))
    try:
        client.connect()
        return client
    except CUError:
        client.close()
        if not autostart:
            raise

    _spawn_daemon(pipe)
    # 冷启动窗口：足够覆盖解释器启动 + import + 建管道。
    client = IpcClient(pipe, connect_timeout=max(timeout, DAEMON_START_TIMEOUT))
    client.connect()
    return client


def _spawn_daemon(pipe: str) -> None:
    try:
        subprocess.Popen(
            _daemon_command(pipe),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_DETACHED | _NO_WINDOW, close_fds=True,
            # 管道名走环境变量而不是命令行：`\\.\pipe\` 的反斜杠在拼接命令行时
            # 容易被吃掉，双方会各连一条管道。实测踩到过，排查成本很高。
            env={**os.environ, ENV_PIPE: pipe},
        )
    except OSError as exc:
        raise CUError(ErrorCode.INTERNAL_ERROR,
                      f"无法拉起 daemon：{exc}", {"pipe": pipe}) from exc


def handshake(client: IpcClient) -> dict:
    """握手。版本不匹配时**重启 daemon 并重试一次**（api-contract.md §3 约定 4）。

    CLI 与 daemon 来自同一个包，唯一的不匹配场景是升级时旧 daemon 还在跑。
    """
    for attempt in (1, 2):
        result = client.call(make_request("system.handshake", {
            "protocol_version": PROTOCOL_VERSION, "client_version": __version__}))
        if result.get("protocol_version") == PROTOCOL_VERSION:
            return result
        if attempt == 2:
            raise CUError(ErrorCode.PROTOCOL_VERSION_MISMATCH,
                          f"协议版本不一致：客户端 {PROTOCOL_VERSION}，"
                          f"daemon {result.get('protocol_version')}")
        _restart_daemon(client)
    return {}


def _restart_daemon(client: IpcClient) -> None:
    try:
        client.call(make_request("daemon.stop"))
    except (AppError, CUError):
        pass
    client.close()
    time.sleep(1.0)


def _resolve_common(args: argparse.Namespace) -> argparse.Namespace:
    """把缺席的共用项补成确定的默认值。

    见 `_common_options` 的注释：共用项用 `SUPPRESS`，所以「用户没写」意味着
    属性不存在。这里统一补齐，让后面的渲染逻辑不必关心它是在哪一侧给的。
    """
    for flag in ("as_json", "inline", "verbose", "keep_alive", "end_sequence"):
        setattr(args, flag, bool(getattr(args, flag, False)))
    if getattr(args, "session", None) is None:
        args.session = None
    if getattr(args, "describe", None) is None:
        args.describe = None
    return args


def run(argv: list[str] | None = None) -> int:
    """执行一条命令，返回退出码。异常由 `main` 统一转成退出码。"""
    parser = build_parser()
    args = _resolve_common(parser.parse_args(argv))
    if not args.command:
        parser.print_help()
        return 0

    method, params = to_request(args)
    # `session info` 在客户端侧从列表里筛一条 —— 少一个线协议方法。
    if args.command == "session" and args.session_command == "info":
        client = connect()
        try:
            handshake(client)
            result = client.call(make_request("session.list"))
        finally:
            client.close()
        wanted = args.session or os.environ.get(ENV_SESSION)
        found = next((row for row in result.get("sessions", [])
                      if row["session_id"] == wanted), None)
        result = {"_sub": "info", "session": found}
        emit(args, result)
        return 0 if found else 0
    if args.command == "session" and args.session_command is not None:
        params["_sub"] = args.session_command

    client = connect()
    try:
        handshake(client)
        result = client.call(make_request(method, params))
    finally:
        client.close()

    if args.command == "session":
        result = {"_sub": args.session_command, **result}
    emit(args, result)
    return 0


def emit(args: argparse.Namespace, result: Any) -> None:
    if args.as_json:
        print(render_json(result))
    else:
        print(render_text(args.command, result, verbose=args.verbose))


def main(argv: list[str] | None = None) -> int:
    """入口。把 `CUError` 渲染成人读或 JSON 错误，并给出对应退出码。"""
    _force_utf8_stdout()
    args = None
    parser = build_parser()
    try:
        args = _resolve_common(parser.parse_args(argv))
        return run(argv)
    except CUError as exc:
        _emit_error(exc, _wants_json(argv, args))
        return int(exc.exit_code.value)
    except AppError as exc:
        cu = exc.as_cu_error()
        _emit_error(cu, _wants_json(argv, args))
        return int(cu.exit_code.value)
    except KeyboardInterrupt:
        # 用户中止也是「这次调用失败了」：`--json` 下 stdout 不能是空的 ——
        # 否则只读 stdout 的集成方看到的是「成功，但什么都没输出」。
        if _wants_json(argv, args):
            _emit_error(CUError(ErrorCode.ABORTED_BY_USER, "已中断"), True)
        else:
            print("已中断", file=sys.stderr)
        return int(EXIT_CODES[ErrorCode.ABORTED_BY_USER].value)
    except Exception as exc:  # noqa: BLE001 —— 顶层边界：任何异常都要变成可读错误 + 退出码
        cu = CUError(ErrorCode.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}",
                     _daemon_log_detail())
        _emit_error(cu, _wants_json(argv, args))
        return int(EXIT_CODES[ErrorCode.INTERNAL_ERROR].value)


def _wants_json(argv: list[str] | None, args: argparse.Namespace | None) -> bool:
    """这次调用要不要 JSON 输出。

    `args` 为 None 说明 argparse 已经失败（`_Parser.error` 把参数错误抛成了 CUError），
    命名空间拿不到了 —— 但 `--json` 的意图必须保住：否则「`--json` + 参数写错」又退回
    stderr 上的人读文本，正是 Q-025 要堵的那条缝。所以退一步扫一遍 argv。
    """
    if args is not None:
        return bool(getattr(args, "as_json", False))
    return "--json" in (sys.argv[1:] if argv is None else argv)


def _emit_error(exc: CUError, as_json: bool) -> None:
    """错误输出的**流**跟着 `--json` 走，不跟着「成功还是失败」走（Q-025）。

    契约只在 §5 画了「stdout + exit code」，没说错误信封走哪条流；实测的行为是
    「成功走 stdout、错误走 stderr」，于是只读 stdout 的集成方会**静默**漏掉全部错误
    （验收 §7.7 实测：退出码 5、stdout 0 字符、stderr 432 字符）。

    自 2026-09-13 起：`--json` ⟹ 错误信封也走 stdout，与成功同一条流。
    没有 `--json` 时人读错误仍走 stderr（Unix 惯例不变）。
    """
    stream = sys.stdout if as_json else sys.stderr
    print(render_error_json(exc) if as_json else exc.render(), file=stream)


def _daemon_log_detail() -> dict[str, Any]:
    """`internal_error` 的 hint 承诺「路径见 detail.log」，这里把那个承诺兑现。

    取不到（配置文件读不了）就返回空字典 —— 错误路径上的兜底不能再抛错。
    """
    try:
        from .config import Config

        return {"log": str(Config.load().daemon_log)}
    except Exception:  # noqa: BLE001 —— 见上：错误路径的兜底绝不再失败
        return {}


def _force_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
