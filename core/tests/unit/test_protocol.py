"""``protocol.py`` 的契约测试（JSON-RPC 2.0 信封 + NDJSON 分帧）。

oracle 标注：

- ``specified`` —— ``api-contract.md`` §2 / §3 约定 1、JSON-RPC 2.0 规范。
- ``derived`` —— 由 docstring 的「UTF-8、紧凑分隔符、一行一条」推导。
- ``implicit`` —— 公开 docstring 明写的行为承诺。
"""

from __future__ import annotations

import io

import pytest

from cu.errors import ErrorCode
from cu.protocol import (
    APP_ERROR,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    JSONRPC,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    AppError,
    CUError,
    dump_line,
    error_response,
    load_line,
    make_request,
    ok_response,
    read_line,
)


def test_jsonrpc_version_constant() -> None:
    # oracle: specified —— JSON-RPC 2.0 规范的版本字面量。
    assert JSONRPC == "2.0"


def test_standard_error_code_constants() -> None:
    # oracle: specified —— JSON-RPC 2.0 保留错误码。
    assert (PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR) == (
        -32700,
        -32600,
        -32601,
        -32602,
        -32603,
    )


def test_app_error_code_is_in_reserved_server_range() -> None:
    # oracle: specified —— DEC-040：应用级错误统一走 error.code = -32000。
    assert APP_ERROR == -32000


# --------------------------------------------------------------------------- #
# 信封构造
# --------------------------------------------------------------------------- #


def test_make_request_envelope() -> None:
    # oracle: specified —— §2.1 方法表 + JSON-RPC 2.0 请求对象四要素。
    req = make_request("desktop.windows", {"all": True}, request_id=7)
    assert req == {"jsonrpc": "2.0", "id": 7, "method": "desktop.windows", "params": {"all": True}}


def test_make_request_defaults_params_to_empty_object() -> None:
    # oracle: specified —— §2.1：无参方法（如 session.list / lock.status）传 {}。
    req = make_request("lock.status")
    assert req["params"] == {}
    assert req["id"] == 1


def test_ok_response_envelope() -> None:
    # oracle: specified —— JSON-RPC 2.0 成功响应形状。
    assert ok_response(7, {"path": "x.png"}) == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"path": "x.png"},
    }


def test_error_response_without_data_omits_data_key() -> None:
    # oracle: derived —— 约定 1：data 可选；没有时不应出现 null 占位。
    resp = error_response(7, APP_ERROR, "boom")
    assert resp == {"jsonrpc": "2.0", "id": 7, "error": {"code": -32000, "message": "boom"}}
    assert "data" not in resp["error"]


def test_error_response_with_data() -> None:
    # oracle: specified —— 约定 1：应用级错误把字符串码放 error.data.code。
    resp = error_response(7, APP_ERROR, "boom", {"code": "window_stale", "hint": "h", "detail": {}})
    assert resp["error"]["code"] == -32000
    assert resp["error"]["data"]["code"] == "window_stale"


# --------------------------------------------------------------------------- #
# dump_line：NDJSON 一行一条，UTF-8，紧凑分隔符
# --------------------------------------------------------------------------- #


def test_dump_line_is_compact_and_ends_with_single_newline() -> None:
    # oracle: derived —— docstring：紧凑分隔符 + 行尾换行。
    raw = dump_line({"a": 1, "b": 2})
    assert raw == b'{"a":1,"b":2}\n'


def test_dump_line_keeps_chinese_raw_ensure_ascii_false() -> None:
    """中文必须是原样 UTF-8，不能被转义成 \\uXXXX。

    项目大量使用「未命名 - 记事本」这类中文标题；转义会撑大管道且破坏人读调试。
    """
    # oracle: specified —— 领域规则：ensure_ascii=False；docstring：UTF-8。
    raw = dump_line({"title": "未命名 - 记事本"})
    assert raw == '{"title":"未命名 - 记事本"}\n'.encode()
    assert b"\\u" not in raw


def test_dump_line_keeps_emoji_raw() -> None:
    # oracle: derived —— ensure_ascii=False 对非 BMP 字符同样成立。
    raw = dump_line({"title": "截图-🎯-完成"})
    assert raw == '{"title":"截图-🎯-完成"}\n'.encode()
    assert b"\\ud83c" not in raw


def test_dump_line_escapes_control_characters() -> None:
    # oracle: specified —— JSON 规范要求转义 U+0000..U+001F。
    raw = dump_line({"t": "a\x00b\x1fc"})
    text = raw.decode("utf-8")
    assert "\\u0000" in text
    assert "\\u001f" in text
    # 转义后仍是可解析的一条合法 JSON。
    assert load_line(raw) == {"t": "a\x00b\x1fc"}


def test_dump_line_escapes_embedded_newline_so_one_line_is_one_message() -> None:
    # oracle: derived —— docstring：字符串内换行转义成 \n，因此一行必是一条完整消息。
    raw = dump_line({"t": "line1\nline2"})
    text = raw.decode("utf-8")
    assert text == '{"t":"line1\\nline2"}\n'
    assert text.count("\n") == 1
    assert load_line(raw) == {"t": "line1\nline2"}


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "未命名 - 记事本"},
        {"title": "中文/emoji 🎯/控制 \x1f"},
        {"text": "多行\n内容\r\n第二行"},
        {"nested": {"键": ["值1", "值2", ""]}},
        {"n": -1, "f": 1.5, "b": True, "null": None},
    ],
)
def test_dump_line_load_line_roundtrip(payload: dict) -> None:
    # oracle: derived —— 往返必须无损（含中文/emoji/控制字符/换行转义/嵌套）。
    assert load_line(dump_line(payload)) == payload


# --------------------------------------------------------------------------- #
# load_line：空行 / 非法 JSON / 非对象 一律 CUError(INVALID_PARAMS)
# --------------------------------------------------------------------------- #


def test_load_line_accepts_bytes_and_str() -> None:
    # oracle: derived —— 契约：同一分帧格式，输入可为 bytes 或 str。
    assert load_line(b'{"a":1}') == {"a": 1}
    assert load_line('{"a":1}') == {"a": 1}


def test_load_line_strips_surrounding_whitespace() -> None:
    # oracle: derived —— 一行的两端空白（如 \r\n 的 \r）不应影响解析。
    assert load_line(b'  {"a":1}  \r\n') == {"a": 1}


@pytest.mark.parametrize("raw", [b"", b"\n", b"   \r\n", "", "   "])
def test_load_line_rejects_empty_or_blank(raw) -> None:
    # oracle: specified —— 领域规则：空行抛 CUError(INVALID_PARAMS)。
    with pytest.raises(CUError) as ei:
        load_line(raw)
    assert ei.value.code is ErrorCode.INVALID_PARAMS


@pytest.mark.parametrize("raw", [b"{", b"not json", b'{"a":}', b"[1,2", b"{'single':1}"])
def test_load_line_rejects_invalid_json(raw: bytes) -> None:
    # oracle: specified —— 领域规则：非法 JSON 抛 CUError(INVALID_PARAMS)。
    with pytest.raises(CUError) as ei:
        load_line(raw)
    assert ei.value.code is ErrorCode.INVALID_PARAMS


@pytest.mark.parametrize("raw", [b"[1,2,3]", b"123", b'"a string"', b"true", b"null"])
def test_load_line_rejects_non_object_json(raw: bytes) -> None:
    # oracle: specified —— 领域规则：非对象（数组/标量）抛 CUError(INVALID_PARAMS)。
    with pytest.raises(CUError) as ei:
        load_line(raw)
    assert ei.value.code is ErrorCode.INVALID_PARAMS


def test_load_line_invalid_json_message_is_readable() -> None:
    # oracle: derived —— message 给人读，不应为空。
    with pytest.raises(CUError) as ei:
        load_line(b"{oops")
    assert ei.value.message


# --------------------------------------------------------------------------- #
# read_line：8MiB 上限、EOF 返回 None vs 有内容无换行
# --------------------------------------------------------------------------- #


def test_read_line_returns_line_including_newline() -> None:
    # oracle: specified —— docstring：读一行（含换行）。
    assert read_line(io.BytesIO(b"hello\nworld\n")) == b"hello\n"


def test_read_line_reads_successive_lines() -> None:
    # oracle: derived —— 一行一条，连续读应逐条返回。
    stream = io.BytesIO(b"a\nbb\nccc\n")
    assert read_line(stream) == b"a\n"
    assert read_line(stream) == b"bb\n"
    assert read_line(stream) == b"ccc\n"
    assert read_line(stream) is None


def test_read_line_returns_none_at_eof_with_no_data() -> None:
    # oracle: specified —— docstring：流结束返回 None。
    assert read_line(io.BytesIO(b"")) is None


def test_read_line_returns_trailing_content_without_newline_at_eof() -> None:
    """有内容但无换行 —— 与「空流返回 None」必须区分开。"""
    # oracle: derived —— docstring：先返回缓冲内容，只有缓冲为空才返回 None。
    assert read_line(io.BytesIO(b"no newline here")) == b"no newline here"
    # 后续再读才是 None。
    stream = io.BytesIO(b"tail")
    assert read_line(stream) == b"tail"
    assert read_line(stream) is None


def test_read_line_default_limit_is_8_mib() -> None:
    # oracle: specified —— 领域规则：read_line 有 8MiB 上限。
    # 通过恰好 8MiB 的数据应在 EOF 正常返回，8MiB+1 应抛错来间接确认默认值。
    limit = 8 * 1024 * 1024
    assert read_line(io.BytesIO(b"x" * limit)) == b"x" * limit
    with pytest.raises(CUError):
        read_line(io.BytesIO(b"x" * (limit + 1)))


def test_read_line_raises_when_over_limit() -> None:
    # oracle: specified —— 领域规则：超过上限抛 CUError。
    with pytest.raises(CUError) as ei:
        read_line(io.BytesIO(b"x" * 9), limit=8)
    assert ei.value.code is ErrorCode.INVALID_PARAMS


def test_read_line_content_exactly_at_limit_is_allowed() -> None:
    # oracle: derived —— 上限是「超过才拒」；恰好等于上限的内容（无换行，EOF）应返回。
    assert read_line(io.BytesIO(b"x" * 8), limit=8) == b"x" * 8


def test_read_line_newline_at_limit_boundary_is_not_punished() -> None:
    # oracle: derived —— 到换行即返回，换行本身不算超限。
    assert read_line(io.BytesIO(b"12345678\n"), limit=8) == b"12345678\n"


# --------------------------------------------------------------------------- #
# AppError.as_cu_error：前向兼容退化
# --------------------------------------------------------------------------- #


def test_app_error_as_cu_error_for_known_code() -> None:
    # oracle: specified —— 约定 1：字符串码还原成封闭枚举成员。
    src = AppError("window_stale", "hwnd 被复用", hint="重新枚举", detail={"hwnd": "0x0001A2B"})
    err = src.as_cu_error()
    assert err.code is ErrorCode.WINDOW_STALE
    assert err.message == "hwnd 被复用"
    assert err.detail == {"hwnd": "0x0001A2B"}


def test_app_error_as_cu_error_unknown_code_degrades_to_internal() -> None:
    """不在封闭枚举内的 code 退化为 internal_error（前向兼容）。"""
    # oracle: specified —— 领域规则 + docstring：码不在封闭枚举内时退化。
    src = AppError("some_future_code", "来自更新版 daemon")
    err = src.as_cu_error()
    assert err.code is ErrorCode.INTERNAL_ERROR
    assert err.message == "来自更新版 daemon"


def test_app_error_detail_defaults_to_empty_dict() -> None:
    # oracle: derived —— detail 可缺省，缺省即空。
    assert AppError("internal_error", "boom").detail == {}
    assert AppError("internal_error", "boom").as_cu_error().detail == {}


def test_app_error_as_cu_error_copies_detail_not_aliases() -> None:
    # oracle: derived —— 还原出的 CUError 不应与 AppError 共享同一个 detail 对象。
    src = AppError("capture_failed", "全失败", detail={"layers_tried": 4})
    err = src.as_cu_error()
    assert err.detail == src.detail
    err.detail["extra"] = 1
    assert "extra" not in src.detail


def test_app_error_message_is_exception_message() -> None:
    # oracle: derived —— AppError 是 Exception 子类。
    assert str(AppError("internal_error", "boom")) == "boom"
