"""``config.py`` 本轮新增/变更字段的契约测试：写序列时序与 VLM User-Agent。

``test_config.py`` 已覆盖 config 的整体契约（默认值表、原子写、白名单精确集合），
这里只补本轮变更引入、且那份文件没有覆盖的部分，避免重复：

1. ``overlay_exit_hold_ms`` 的负数校验（原拒绝表只覆盖了另外两个时序字段）；
2. ``vlm.user_agent`` 的默认非空、可设、可持久化、可从 dict 读入；
3. 白名单确实包含 ``vlm.user_agent`` 与 ``overlay_exit_hold_ms``，总数为 17。

时序默认值（1500 / 30 / 500）已由 ``test_config.py::test_default_values_match_contract``
覆盖，这里不再重复。

oracle 标注：
- ``specified`` —— config.py docstring 默认值表 / DEC-045 / ``VlmConfig`` 字段注释。
- ``derived`` —— 由校验语义与「任意字符串」推导出的边界。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cu.config import SETTABLE_KEYS, Config, apply_set
from cu.errors import CUError, ErrorCode

# --------------------------------------------------------------------------- #
# 写序列时序字段（DEC-045）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field",
    ["overlay_arm_ms", "overlay_continue_seconds", "overlay_exit_hold_ms"],
)
def test_timing_fields_reject_negative(field: str) -> None:
    # oracle: specified —— validate 的 need()：三个时序字段都要求 >= 0。
    cfg = Config(**{field: -1})
    with pytest.raises(CUError) as ei:
        cfg.validate()
    # oracle: specified —— 非法值一律抛 INVALID_PARAMS，不静默夹取。
    assert ei.value.code is ErrorCode.INVALID_PARAMS


@pytest.mark.parametrize(
    "field",
    ["overlay_arm_ms", "overlay_continue_seconds", "overlay_exit_hold_ms"],
)
def test_timing_fields_accept_zero(field: str) -> None:
    # oracle: derived —— `>= 0` 的闭区间下界 0 应被接受。
    Config(**{field: 0}).validate()


def test_overlay_exit_hold_ms_default_and_settable() -> None:
    # oracle: specified —— DEC-045：退出保留期默认 500ms，且已进白名单可设。
    assert Config().overlay_exit_hold_ms == 500
    assert SETTABLE_KEYS.get("overlay_exit_hold_ms") == "int"
    updated = apply_set(Config(), "overlay_exit_hold_ms", "1200")
    assert updated.overlay_exit_hold_ms == 1200


# --------------------------------------------------------------------------- #
# vlm.user_agent
# --------------------------------------------------------------------------- #


def test_vlm_user_agent_default_is_nonempty_browser_string() -> None:
    ua = Config().vlm.user_agent
    # oracle: specified —— 字段注释：默认给一个能过大多数 WAF 的浏览器 UA，不能是空串。
    assert isinstance(ua, str)
    assert ua.strip() != ""
    # oracle: specified —— 默认值不得是 urllib 的默认 UA（实测会被 WAF 直接 403）。
    assert not ua.startswith("Python-urllib")
    # oracle: derived —— 注释写明默认是浏览器 UA；含 Mozilla 标记。
    assert "Mozilla" in ua


def test_vlm_user_agent_is_settable_to_arbitrary_strings() -> None:
    # oracle: specified —— 白名单声明 vlm.user_agent 为可设的 str。
    assert SETTABLE_KEYS.get("vlm.user_agent") == "str"
    # oracle: derived —— 任意字符串（含空格 / 中文 / 空串）都应被接受，不做内容校验。
    for value in ["curl/8.4.0", "custom agent 中文", "", "  "]:
        updated = apply_set(Config(), "vlm.user_agent", value)
        assert updated.vlm.user_agent == value


def test_apply_set_vlm_user_agent_does_not_pollute_original() -> None:
    original = Config()
    updated = apply_set(original, "vlm.user_agent", "curl/8.4.0")
    # oracle: specified —— apply_set 返回新配置，不改原对象（含嵌套对象）。
    assert updated.vlm.user_agent == "curl/8.4.0"
    assert original.vlm.user_agent == Config().vlm.user_agent
    assert updated.vlm is not original.vlm


def test_vlm_user_agent_roundtrips_through_save_load(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    cfg = apply_set(Config(), "vlm.user_agent", "custom-ua/1.0")
    cfg.save(target)
    # oracle: derived —— user_agent 是持久字段，存档后读回不变。
    assert Config.load(target).vlm.user_agent == "custom-ua/1.0"


def test_vlm_user_agent_parsed_from_dict() -> None:
    cfg = Config.from_dict({"vlm": {"user_agent": "curl/8.4.0"}})
    # oracle: derived —— from_dict 认识 vlm.user_agent 子字段。
    assert cfg.vlm.user_agent == "curl/8.4.0"


def test_user_agent_is_exported_in_to_dict() -> None:
    # oracle: specified —— user_agent 是 VlmConfig 的正式字段，应随 to_dict 落盘。
    assert "user_agent" in Config().to_dict()["vlm"]


# --------------------------------------------------------------------------- #
# 白名单
# --------------------------------------------------------------------------- #


def test_settable_keys_include_new_fields_and_total_eighteen() -> None:
    # oracle: specified —— DEC-045 新增 overlay_exit_hold_ms；VlmConfig 新增 user_agent；
    # DEC-053 新增 daemon_log_level。
    assert "overlay_exit_hold_ms" in SETTABLE_KEYS
    assert "vlm.user_agent" in SETTABLE_KEYS
    assert "daemon_log_level" in SETTABLE_KEYS
    # oracle: specified —— 白名单总数 18。
    assert len(SETTABLE_KEYS) == 18
