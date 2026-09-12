"""嵌套子命令也认「选项跟在子命令之后」（F11）。

oracle: specified —— 契约 §1.5 的示例写法就是 `computer-use screenshot --hwnd … --session …`，
选项在子命令**之后**；`common` 父解析器存在的全部理由就是让这种写法成立。

这里只验解析（argparse 才是当初坏掉的那一层）：真机跑一遍的版本在
`tests/native/acceptance_lifecycle.py` 里（它现在把 `--json` 写在子命令之后）。
"""

from __future__ import annotations

import pytest

from cu.client import _resolve_common, build_parser, to_request

SESSION = "s-20260913-000000-aaaa"

#: 每条嵌套子命令：argv（不含 `--json`）+ 它应当映射到的线协议方法。
NESTED: list[tuple[list[str], str]] = [
    (["session", "list"], "session.list"),
    (["session", "info"], "session.list"),
    (["session", "end"], "session.end"),
    (["config", "show"], "config.get"),
    (["config", "set", "image_format", "png"], "config.set"),
    (["lock", "status"], "lock.status"),
    (["lock", "unlock", "--force", "--reason", "验收"], "lock.forceUnlock"),
    (["daemon", "status"], "daemon.status"),
    (["daemon", "stop"], "daemon.stop"),
    (["setup", "omni"], "daemon.setup_omni"),
]


@pytest.mark.parametrize(("argv", "method"), NESTED)
def test_nested_subcommand_accepts_json_after_it(argv: list[str], method: str) -> None:
    """`… --json` 写在**子命令之后**也要认，而且要真的生效（不是被吞掉）。"""
    args = _resolve_common(build_parser().parse_args([*argv, "--json"]))

    assert args.as_json is True
    assert to_request(args)[0] == method


@pytest.mark.parametrize(("argv", "method"), NESTED)
def test_nested_subcommand_accepts_session_after_it(argv: list[str], method: str) -> None:
    """`--session` 同理 —— `session info` / `session end` 曾经各有一份自己的定义。"""
    args = _resolve_common(build_parser().parse_args([*argv, "--session", SESSION]))

    assert args.session == SESSION


@pytest.mark.parametrize(("argv", "method"), NESTED)
def test_options_before_the_top_level_subcommand_still_work(argv: list[str], method: str) -> None:
    """旧写法（选项写最前面）不能坏掉 —— 契约的示例写法从来不是唯一写法。"""
    args = _resolve_common(build_parser().parse_args(["--json", *argv]))

    assert args.as_json is True
    assert to_request(args)[0] == method


def test_session_end_sends_the_session_id_passed_after_the_subcommand() -> None:
    """`session end --session …` 必须真的把那个 id 送进请求。

    这条曾经靠一份独立的 `--session`（dest 覆盖）实现，统一回 common 之后
    要确认「值仍然到位」，而不只是「参数被认出来了」。
    """
    args = _resolve_common(build_parser().parse_args(
        ["session", "end", "--session", SESSION]))

    assert to_request(args) == ("session.end", {"session_id": SESSION})


def test_lock_status_accepts_json_and_session_together() -> None:
    """多个共用项同时写在子命令之后（真实调用里最常见的形态）。"""
    args = _resolve_common(build_parser().parse_args(
        ["lock", "status", "--json", "--session", SESSION]))

    assert args.as_json is True
    assert args.session == SESSION
