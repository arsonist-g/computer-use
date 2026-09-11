"""JSON-RPC 2.0 消息与 NDJSON 分帧（api-contract.md §2）。

管道是内部的、AI 看不到，所以信封的冗长不进入任何人的上下文 —— 为内部协议自造信封
只会把「请求响应关联 / 错误表达 / 通知」三件事都变成自己发明（decisions.md D2）。

分帧：一行一个 JSON 对象，UTF-8，紧凑分隔符。
`json.dumps` 会把字符串内的换行转义成 `\\n`，因此**一行必然是一条完整消息**，无需长度前缀。
"""

from __future__ import annotations

import json
from typing import Any, BinaryIO

from .errors import CUError, ErrorCode

JSONRPC = "2.0"

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: 应用级错误的统一 code。具体原因在 `error.data.code`（ErrorCode 字符串）——
#: 这样既守住 JSON-RPC 规范（error.code 是数字），又让 AI 拿到可分支的字符串码。
APP_ERROR = -32000

__all__ = [
    "APP_ERROR",
    "AppError",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "JSONRPC",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "dump_line",
    "error_response",
    "load_line",
    "make_request",
    "ok_response",
    "read_line",
]


class AppError(Exception):
    """对端返回了应用级错误。携带原始 CUError 信息供上层渲染。"""

    def __init__(self, code: str, message: str, hint: str = "", detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.detail = detail or {}

    def as_cu_error(self) -> CUError:
        """尝试还原成 CUError。码不在封闭枚举内时退化为 internal_error（前向兼容）。"""
        try:
            ec = ErrorCode(self.code)
        except ValueError:
            ec = ErrorCode.INTERNAL_ERROR
        return CUError(ec, self.message, dict(self.detail))


def make_request(method: str, params: dict[str, Any] | None = None, request_id: int = 1) -> dict:
    return {"jsonrpc": JSONRPC, "id": request_id, "method": method, "params": params or {}}


def ok_response(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": JSONRPC, "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str, data: dict | None = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC, "id": request_id, "error": err}


def dump_line(message: dict) -> bytes:
    """序列化成一行 NDJSON（含行尾换行）。紧凑分隔符以省管道带宽。"""
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def load_line(raw: bytes | str) -> dict:
    """解析一行。空行与非法 JSON 一律抛 CUError（调用方决定是回错误还是断开）。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    raw = raw.strip()
    if not raw:
        raise CUError(ErrorCode.INVALID_PARAMS, "收到空消息")
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CUError(ErrorCode.INVALID_PARAMS, f"消息不是合法 JSON：{exc}") from exc
    if not isinstance(msg, dict):
        raise CUError(ErrorCode.INVALID_PARAMS, "消息不是 JSON 对象")
    return msg


def read_line(stream: BinaryIO, limit: int = 8 * 1024 * 1024) -> bytes | None:
    """从二进制流读一行（含换行）。流结束返回 None。

    `limit` 防止对端不发换行导致内存无限增长 —— 管道是本机同用户进程，但仍不该无界。
    """
    buf = bytearray()
    while True:
        ch = stream.read(1)
        if not ch:
            return bytes(buf) if buf else None
        buf += ch
        if ch == b"\n":
            return bytes(buf)
        if len(buf) > limit:
            raise CUError(ErrorCode.INVALID_PARAMS, f"单条消息超过 {limit} 字节上限")
