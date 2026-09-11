"""``errors.py`` 的契约测试。

期望值来源（oracle）逐条标注：

- ``specified`` —— 直接抄自权威契约：``api-contract.md`` §4（错误码表）、§5（退出码表）、
  ``DEC-040``（结构化错误三元组）。
- ``derived`` —— 由契约条款推导出的性质（集合相等、一一对应等）。
- ``implicit`` —— 公开 docstring 明写的承诺。

绝不从当前实现的输出反推期望值。
"""

from __future__ import annotations

import pytest

from cu.errors import (
    EXIT_CODES,
    HINTS,
    WRITE_COMMANDS,
    CUError,
    ErrorCode,
    ExitCode,
)

# oracle: specified —— api-contract.md §4 错误码表，逐行抄录（19 行）。
CONTRACT_ERROR_CODES: dict[str, str] = {
    "INVALID_PARAMS": "invalid_params",
    "PROTOCOL_VERSION_MISMATCH": "protocol_version_mismatch",
    "SESSION_NOT_FOUND": "session_not_found",
    "SESSION_ALREADY_ENDED": "session_already_ended",
    "DESCRIBE_REQUIRED": "describe_required",
    "LOCK_TIMEOUT": "lock_timeout",
    "WINDOW_NOT_FOUND": "window_not_found",
    "WINDOW_STALE": "window_stale",
    "WINDOW_MINIMIZED": "window_minimized",
    "ELEVATED_WINDOW": "elevated_window",
    "FOREGROUND_FAILED": "foreground_failed",
    "CAPTURE_FAILED": "capture_failed",
    "CAPTURE_BLACK": "capture_black",
    "OMNI_NOT_INSTALLED": "omni_not_installed",
    "OMNI_FAILED": "omni_failed",
    "VLM_FAILED": "vlm_failed",
    "DANGEROUS_KEY_BLOCKED": "dangerous_key_blocked",
    "ABORTED_BY_USER": "aborted_by_user",
    "INTERNAL_ERROR": "internal_error",
}

# oracle: specified —— api-contract.md §5 退出码表 + 决策日志里的大类划分规则。
# 键为错误码名，值为契约规定的退出码数字。
CONTRACT_EXIT_NUMBER: dict[str, int] = {
    # 2 = 参数错误
    "INVALID_PARAMS": 2,
    "DESCRIBE_REQUIRED": 2,
    "DANGEROUS_KEY_BLOCKED": 2,
    # 3 = 会话/锁问题
    "SESSION_NOT_FOUND": 3,
    "SESSION_ALREADY_ENDED": 3,
    "LOCK_TIMEOUT": 3,
    # 4 = 目标问题（窗口）
    "WINDOW_NOT_FOUND": 4,
    "WINDOW_STALE": 4,
    "WINDOW_MINIMIZED": 4,
    "ELEVATED_WINDOW": 4,
    "FOREGROUND_FAILED": 4,
    # 5 = 采集/解析失败
    "CAPTURE_FAILED": 5,
    "CAPTURE_BLACK": 5,
    "OMNI_NOT_INSTALLED": 5,
    "OMNI_FAILED": 5,
    "VLM_FAILED": 5,
    # 6 = 用户中止
    "ABORTED_BY_USER": 6,
    # 1 = 其他内部错误
    "PROTOCOL_VERSION_MISMATCH": 1,
    "INTERNAL_ERROR": 1,
}

# oracle: specified —— DEC-019 / 入口文档约束 11：写命令就是这六个。
CONTRACT_WRITE_COMMANDS = {"click", "move", "drag", "scroll", "type", "key"}


def test_error_code_values_match_contract() -> None:
    """每个枚举成员的名字与线上字符串值都必须与契约表逐字一致。"""
    # oracle: specified —— api-contract.md §4
    actual = {member.name: member.value for member in ErrorCode}
    assert actual == CONTRACT_ERROR_CODES


def test_error_code_is_closed_set_of_19() -> None:
    # oracle: specified —— §4 表共 19 行；DEC-040 亦称 19 个。
    assert len(ErrorCode) == 19


def test_error_code_is_str_enum_so_value_goes_on_the_wire() -> None:
    # oracle: derived —— 值是「直接进 JSON-RPC 的 error.data.code」，故必须是 str。
    assert isinstance(ErrorCode.WINDOW_STALE, str)
    assert ErrorCode.WINDOW_STALE == "window_stale"


def test_guard_error_codes_hints_exit_codes_fully_paired() -> None:
    """具名守卫测试①：错误码 ↔ hint 表 ↔ 退出码映射三者一一对应。

    源码 docstring 明写这条由本文件守卫；缺任何一项都是契约违约。
    """
    all_codes = set(ErrorCode)
    # oracle: implicit —— errors.py docstring：「每个码必须同时有静态 hint 与退出码分类」。
    assert set(HINTS) == all_codes, f"HINTS 与错误码不一一对应，差异：{all_codes ^ set(HINTS)}"
    assert set(EXIT_CODES) == all_codes, f"EXIT_CODES 与错误码不一一对应，差异：{all_codes ^ set(EXIT_CODES)}"


def test_hint_values_are_nonempty_strings() -> None:
    # oracle: derived —— hint 是「下一步该干什么」，必须是非空可读文本。
    for code, hint in HINTS.items():
        assert isinstance(hint, str) and hint.strip(), f"{code} 的 hint 为空"


def test_exit_codes_map_by_category_not_one_to_one() -> None:
    """退出码按大类划分，逐码核对 §5 的映射。"""
    # oracle: specified —— api-contract.md §5 + 大类划分规则
    actual = {code.name: int(EXIT_CODES[code]) for code in ErrorCode}
    assert actual == CONTRACT_EXIT_NUMBER


def test_exit_codes_all_distinct_categories_are_used() -> None:
    # oracle: derived —— 大类映射应实际用到 1/2/3/4/5/6 六档。
    assert {int(v) for v in EXIT_CODES.values()} == {1, 2, 3, 4, 5, 6}


def test_exit_code_enum_values_match_shell_contract() -> None:
    # oracle: specified —— api-contract.md §5 的数字。
    assert {m.name: int(m) for m in ExitCode} == {
        "OK": 0,
        "INTERNAL": 1,
        "PARAMS": 2,
        "SESSION": 3,
        "TARGET": 4,
        "CAPTURE": 5,
        "ABORTED": 6,
    }


def test_write_commands_is_exactly_the_six_write_verbs() -> None:
    # oracle: specified —— DEC-019 / 入口约束 11。
    assert WRITE_COMMANDS == CONTRACT_WRITE_COMMANDS
    assert isinstance(WRITE_COMMANDS, frozenset)


@pytest.mark.parametrize("verb", sorted(CONTRACT_WRITE_COMMANDS))
def test_write_commands_contains_each_verb(verb: str) -> None:
    assert verb in WRITE_COMMANDS


@pytest.mark.parametrize("verb", ["windows", "screenshot", "parse", "begin", "session"])
def test_read_or_lifecycle_verbs_are_not_write_commands(verb: str) -> None:
    # oracle: specified —— 只读命令不取写锁，不属于写命令集合。
    assert verb not in WRITE_COMMANDS


def test_cu_error_carries_message_like_an_exception() -> None:
    # oracle: derived —— docstring：贯穿客户端与 daemon 的错误载体；是 Exception 子类。
    err = CUError(ErrorCode.WINDOW_NOT_FOUND, "窗口不存在")
    assert isinstance(err, Exception)
    assert str(err) == "窗口不存在"


def test_cu_error_hint_and_exit_code_come_from_the_tables() -> None:
    # oracle: derived —— hint / exit_code 是查表，不是另存一份。
    err = CUError(ErrorCode.LOCK_TIMEOUT, "等锁超时", {"holder_pid": 1234})
    assert err.hint == HINTS[ErrorCode.LOCK_TIMEOUT]
    assert err.exit_code is EXIT_CODES[ErrorCode.LOCK_TIMEOUT]
    assert err.exit_code is ExitCode.SESSION


def test_cu_error_detail_defaults_to_empty_dict() -> None:
    # oracle: derived —— detail 承载运行时数据，缺省为空。
    assert CUError(ErrorCode.INTERNAL_ERROR, "boom").detail == {}


def test_to_rpc_data_shape_matches_convention_1() -> None:
    """error.data 的三元组形状：{code, hint, detail}（api-contract.md §3 约定 1）。"""
    detail = {"holder_session_id": "s-example", "held_for_s": 3}
    err = CUError(ErrorCode.LOCK_TIMEOUT, "等锁超时", detail)
    data = err.to_rpc_data()
    # oracle: specified —— 约定 1：error.data{code,hint,detail}
    assert set(data) == {"code", "hint", "detail"}
    assert data["code"] == "lock_timeout"  # 字符串码，不是枚举对象
    assert isinstance(data["code"], str)
    assert data["hint"] == HINTS[ErrorCode.LOCK_TIMEOUT]
    assert data["detail"] == detail


def test_to_rpc_data_detail_is_the_runtime_payload() -> None:
    # oracle: derived —— detail 逐键保留。
    err = CUError(ErrorCode.CAPTURE_FAILED, "截图失败", {"layers_tried": 4})
    assert err.to_rpc_data()["detail"] == {"layers_tried": 4}


def test_render_without_detail_has_no_detail_line() -> None:
    """detail 为空的渲染分支：不应凭空多出一行 detail。"""
    err = CUError(ErrorCode.WINDOW_MINIMIZED, "窗口已最小化")
    out = err.render()
    # oracle: derived —— 约定 1：错误是 code + message + hint 三元组；detail 仅在有时才出现。
    assert "invalid" not in out
    assert err.code.value in out
    assert "窗口已最小化" in out
    assert err.hint in out
    assert "detail" not in out


def test_render_with_detail_includes_each_key_value() -> None:
    err = CUError(ErrorCode.CAPTURE_FAILED, "四层降级全失败", {"layers_tried": 4, "hwnd": "0x0001A2B"})
    out = err.render()
    # oracle: derived —— detail 是运行时数据，渲染时须逐项可见。
    assert "layers_tried=4" in out
    assert "hwnd=0x0001A2B" in out
    assert err.message in out
    assert err.hint in out


def test_render_is_multiline_text() -> None:
    # oracle: derived —— 人读渲染，至少 code/message/hint 三行。
    out = CUError(ErrorCode.INTERNAL_ERROR, "未预期错误").render()
    assert len(out.splitlines()) >= 3
