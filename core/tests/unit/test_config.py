"""``config.py`` 的契约测试：默认值、校验、原子读写、白名单 apply_set。

oracle 标注：

- ``specified`` —— 默认值表（config.py docstring + 决策日志 DEC-017/038/022/030/008/035/016）、
  白名单（领域规则）、``data-model.md`` §3.3 原子写。
- ``derived`` —— 由默认值 / 校验语义推导出的边界。
- ``implicit`` —— docstring 明写的行为承诺。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cu.config as config_mod
from cu.config import (
    DEFAULT_DANGER_KEYS,
    DEFAULT_DATA_DIR_NAME,
    SCHEMA_VERSION,
    SETTABLE_KEYS,
    Config,
    OmniConfig,
    VlmConfig,
    apply_set,
    default_data_dir,
)
from cu.errors import CUError, ErrorCode

# oracle: specified —— config.py docstring 的默认值表 + 决策日志。
CONTRACT_DEFAULTS = {
    "schema": 1,
    "data_dir": "",
    "storage_limit_bytes": 1 * 1024**3,  # 1 GiB = 1073741824
    "daemon_log_limit_bytes": 500 * 1024**2,  # 500 MiB = 524288000
    "daemon_log_level": "info",
    "lock_wait_seconds": 10,
    # DEC-045：前摇 1500→500（缩短后仍够「让手离开」）。
    # DEC-075 / DEC-076：`--continue` 的保持窗口 30s；不带标志的写命令不留兜底保持。
    "overlay_arm_ms": 500,
    "overlay_continue_seconds": 30,
    "overlay_exit_hold_ms": 500,
    "daemon_idle_exit_seconds": 600,
    "image_format": "png",
    "mouse_step_ms": 10,
    "mouse_max_points": 30,
}

# oracle: specified —— 领域规则：白名单，含 4 个 vlm.* 与 3 个 omni.*，不含 data_dir。
CONTRACT_SETTABLE_KEYS = {
    "storage_limit_bytes",
    "daemon_log_limit_bytes",
    "daemon_log_level",
    "lock_wait_seconds",
    "overlay_arm_ms",
    "overlay_continue_seconds",
    "overlay_exit_hold_ms",
    "daemon_idle_exit_seconds",
    "image_format",
    "mouse_step_ms",
    "mouse_max_points",
    "vlm.base_url",
    "vlm.api_key",
    "vlm.model_name",
    "vlm.user_agent",
    "omni.env_path",
    "omni.weights_dir",
    "omni.mirror",
}


# --------------------------------------------------------------------------- #
# 默认值
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field,expected", sorted(CONTRACT_DEFAULTS.items()))
def test_default_values_match_contract(field: str, expected) -> None:
    # oracle: specified —— docstring 默认值表。
    assert getattr(Config(), field) == expected


def test_storage_limit_literals() -> None:
    # oracle: specified —— 决策日志里的字面量。
    cfg = Config()
    assert cfg.storage_limit_bytes == 1073741824
    assert cfg.daemon_log_limit_bytes == 524288000


def test_default_danger_keys() -> None:
    # oracle: specified —— DEC-019：win+l 与 ctrl+alt+del。
    assert Config().danger_keys == ["win+l", "ctrl+alt+del"]
    assert DEFAULT_DANGER_KEYS == ["win+l", "ctrl+alt+del"]


def test_danger_keys_default_is_per_instance_not_shared() -> None:
    # oracle: derived —— 可变默认值必须每实例一份，否则一个实例的改动会污染另一个。
    a, b = Config(), Config()
    a.danger_keys.append("f4")
    assert b.danger_keys == ["win+l", "ctrl+alt+del"]


def test_schema_version_constant() -> None:
    # oracle: specified —— data-model.md §3.3：session.json / config 的 schema 为 1。
    assert SCHEMA_VERSION == 1
    assert Config().schema == 1


def test_nested_defaults_are_empty_objects() -> None:
    # oracle: specified —— VlmConfig / OmniConfig 三字段皆默认为空串。
    cfg = Config()
    assert (cfg.vlm.base_url, cfg.vlm.api_key, cfg.vlm.model_name) == ("", "", "")
    assert (cfg.omni.env_path, cfg.omni.weights_dir, cfg.omni.mirror) == ("", "", "")


# --------------------------------------------------------------------------- #
# 派生路径
# --------------------------------------------------------------------------- #


def test_default_data_dir_uses_home_and_fixed_name() -> None:
    # oracle: specified —— data-model.md §3.1：~/.computer-use/。
    assert DEFAULT_DATA_DIR_NAME == ".computer-use"
    assert default_data_dir() == Path.home() / ".computer-use"


def test_derived_paths_from_data_dir() -> None:
    # oracle: specified —— data-model.md §3.1 目录布局。
    cfg = Config(data_dir="C:/example-root")
    root = Path("C:/example-root")
    assert cfg.root == root
    assert cfg.sessions_dir == root / "sessions"
    assert cfg.logs_dir == root / "logs"
    assert cfg.daemon_log == root / "logs" / "daemon.log"
    assert cfg.config_path == root / "config.json"
    assert cfg.daemon_lock_path == root / "daemon.lock"


def test_empty_data_dir_falls_back_to_default() -> None:
    # oracle: implicit —— docstring：留空表示 default_data_dir()。
    assert Config(data_dir="").root == default_data_dir()


def test_omni_paths_fall_back_then_override() -> None:
    # oracle: implicit —— docstring：留空按 data_dir 推导。
    cfg = Config(data_dir="C:/example-root")
    assert cfg.omni_env == Path("C:/example-root") / "venv-omni"
    assert cfg.omni_weights == Path("C:/example-root") / "models"

    overridden = Config(
        data_dir="C:/example-root",
        omni=OmniConfig(env_path="D:/omni-env", weights_dir="D:/omni-weights"),
    )
    assert overridden.omni_env == Path("D:/omni-env")
    assert overridden.omni_weights == Path("D:/omni-weights")


# --------------------------------------------------------------------------- #
# validate：非法值一律抛错，不静默夹取
# --------------------------------------------------------------------------- #


def test_default_config_is_valid() -> None:
    # oracle: derived —— 默认配置必须自洽。
    Config().validate()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"schema": 2},
        {"storage_limit_bytes": 0},
        {"storage_limit_bytes": -1},
        {"daemon_log_limit_bytes": 0},
        {"daemon_log_limit_bytes": -1},
        {"lock_wait_seconds": -1},
        {"overlay_arm_ms": -1},
        {"overlay_continue_seconds": -1},
        {"daemon_idle_exit_seconds": 0},
        {"daemon_idle_exit_seconds": -5},
        {"image_format": "jpg"},
        {"image_format": "PNG"},
        {"image_format": ""},
        {"mouse_step_ms": 0},
        {"mouse_step_ms": -10},
        {"mouse_max_points": 0},
        {"mouse_max_points": -1},
        {"danger_keys": "win+l"},
        {"danger_keys": [1, 2]},
        {"danger_keys": ["ok", 5]},
    ],
)
def test_validate_rejects_each_illegal_value(kwargs: dict) -> None:
    # oracle: specified —— validate 的每条 need() 条款；「非法值一律抛错，不静默夹取」。
    cfg = Config(**kwargs)
    with pytest.raises(CUError) as ei:
        cfg.validate()
    assert ei.value.code is ErrorCode.INVALID_PARAMS


def test_validate_does_not_silently_coerce_image_format() -> None:
    """image_format="jpg" 必须抛错，而不是被悄悄改成 png。"""
    # oracle: specified —— docstring：静默改配置会让用户以为设置生效了。
    cfg = Config(image_format="jpg")
    with pytest.raises(CUError):
        cfg.validate()
    assert cfg.image_format == "jpg"  # 未被就地改写


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lock_wait_seconds": 0},
        {"overlay_arm_ms": 0},
        {"overlay_continue_seconds": 0},
        {"mouse_max_points": 1},
        {"image_format": "webp"},
        {"image_format": "png"},
        {"danger_keys": []},
    ],
)
def test_validate_accepts_boundary_legal_values(kwargs: dict) -> None:
    # oracle: derived —— `>= 0` / `>= 1` / png|webp 的闭区间边界应被接受。
    Config(**kwargs).validate()


# --------------------------------------------------------------------------- #
# from_dict：未知字段忽略（前向兼容）与嵌套处理
# --------------------------------------------------------------------------- #


def test_from_dict_ignores_unknown_top_level_fields() -> None:
    """前向兼容：新版写的配置里的未知字段必须被忽略，不报错。"""
    # oracle: specified —— docstring：只接受已知字段；未知字段忽略。
    cfg = Config.from_dict({"schema": 1, "future_feature_flag": True, "another": [1, 2]})
    assert cfg.to_dict() == Config().to_dict()
    assert not hasattr(cfg, "future_feature_flag")


def test_from_dict_applies_known_fields() -> None:
    # oracle: derived —— 已知字段照常生效。
    cfg = Config.from_dict({"schema": 1, "lock_wait_seconds": 20, "image_format": "webp"})
    assert cfg.lock_wait_seconds == 20
    assert cfg.image_format == "webp"


def test_from_dict_missing_fields_use_defaults() -> None:
    # oracle: derived —— 缺字段回落到默认值。
    assert Config.from_dict({}).to_dict() == Config().to_dict()


def test_from_dict_builds_nested_vlm_and_ignores_unknown_subkeys() -> None:
    # oracle: specified —— docstring：嵌套 vlm/omni 只保留已知子字段。
    cfg = Config.from_dict(
        {"vlm": {"base_url": "http://example.invalid/v1", "api_key": "k", "future_knob": 1}}
    )
    assert isinstance(cfg.vlm, VlmConfig)
    assert cfg.vlm.base_url == "http://example.invalid/v1"
    assert cfg.vlm.api_key == "k"
    assert cfg.vlm.model_name == ""
    assert set(cfg.to_dict()["vlm"]) == {"base_url", "api_key", "model_name", "user_agent"}


def test_from_dict_builds_nested_omni_and_ignores_unknown_subkeys() -> None:
    # oracle: specified —— 同上，omni 三子字段。
    cfg = Config.from_dict({"omni": {"mirror": "https://hf-mirror.example", "bogus": "x"}})
    assert isinstance(cfg.omni, OmniConfig)
    assert cfg.omni.mirror == "https://hf-mirror.example"
    assert set(cfg.to_dict()["omni"]) == {"env_path", "weights_dir", "mirror"}


def test_from_dict_validates_and_rejects_illegal_values() -> None:
    # oracle: derived —— from_dict 内部调用 validate，非法值不得被接受。
    with pytest.raises(CUError) as ei:
        Config.from_dict({"image_format": "jpg"})
    assert ei.value.code is ErrorCode.INVALID_PARAMS


def test_to_dict_from_dict_roundtrip() -> None:
    # oracle: derived —— to_dict / from_dict 应无损往返。
    cfg = Config(data_dir="C:/example-root")
    cfg.vlm.model_name = "example-model"
    cfg.omni.mirror = "https://hf-mirror.example"
    assert Config.from_dict(cfg.to_dict()).to_dict() == cfg.to_dict()


# --------------------------------------------------------------------------- #
# save / load：往返一致 + 原子写
# --------------------------------------------------------------------------- #


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    # oracle: derived —— 存档后读回值不变（含中文，data-model 中标题/路径是常态）。
    target = tmp_path / "config.json"
    cfg = Config(vlm=VlmConfig(base_url="http://example.invalid/v1", model_name="中文模型-示例"))
    cfg.danger_keys.append("示例键")
    cfg.save(target)

    loaded = Config.load(target)
    assert loaded.to_dict() == cfg.to_dict()


def test_save_creates_missing_parent_dirs(tmp_path: Path) -> None:
    # oracle: derived —— save 应能自建目录（首次运行路径）。
    target = tmp_path / "nested" / "deep" / "config.json"
    cfg = Config()
    cfg.save(target)
    assert target.exists()
    assert Config.load(target).to_dict() == cfg.to_dict()


def test_save_writes_utf8_json_object(tmp_path: Path) -> None:
    # oracle: derived —— 落盘形态是 UTF-8 的 JSON 对象（data-model §3.1）。
    target = tmp_path / "config.json"
    Config().save(target)
    raw = json.loads(target.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert raw["schema"] == 1
    assert raw["image_format"] == "png"


def test_save_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    # oracle: derived —— 原子写的临时文件必须被 os.replace 消费掉。
    target = tmp_path / "config.json"
    Config().save(target)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "config.json"]
    assert leftovers == []


def test_save_overwrites_existing_target(tmp_path: Path) -> None:
    # oracle: derived —— 再次 save 应覆盖为目标新值。
    target = tmp_path / "config.json"
    Config().save(target)
    Config(lock_wait_seconds=99).save(target)
    assert Config.load(target).lock_wait_seconds == 99


def test_save_rejects_invalid_config_before_writing(tmp_path: Path) -> None:
    # oracle: derived —— save 内部先 validate，非法配置不得落盘。
    target = tmp_path / "config.json"
    with pytest.raises(CUError):
        Config(image_format="jpg").save(target)
    assert not target.exists()


def test_save_failure_keeps_old_target_and_cleans_temp(tmp_path: Path, monkeypatch) -> None:
    """原子写失败：目标文件不被破坏，也无残留临时文件（data-model §3.3）。"""
    # oracle: specified —— §3.3：先写临时文件再 os.replace，避免崩溃留下半个文件。
    target = tmp_path / "config.json"
    Config().save(target)
    original = target.read_bytes()

    def boom(src, dst):  # noqa: ANN001 - 模拟 replace 失败
        raise OSError("simulated replace failure")

    monkeypatch.setattr(config_mod.os, "replace", boom)

    with pytest.raises(OSError):
        Config(lock_wait_seconds=77).save(target)

    assert target.read_bytes() == original  # 旧内容完好
    assert [p.name for p in tmp_path.glob(".config-*")] == []  # 无残留 tmp


def test_save_into_non_directory_parent_fails_without_side_effects(tmp_path: Path) -> None:
    """目标父路径是文件而非目录：不得创建/破坏任何东西。"""
    # oracle: derived —— 目录创建失败时目标文件不可能被写出来。
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir", encoding="utf-8")
    target = blocker / "config.json"

    with pytest.raises(OSError):
        Config().save(target)

    assert blocker.is_file()
    assert blocker.read_text(encoding="utf-8") == "i am a file, not a dir"
    assert not target.exists()


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    # oracle: implicit —— docstring：文件不存在时返回默认配置。
    cfg = Config.load(tmp_path / "does-not-exist.json")
    assert cfg.to_dict() == Config().to_dict()
    cfg.validate()


def test_load_rejects_malformed_json(tmp_path: Path) -> None:
    # oracle: derived —— 配置文件无法解析时抛 CUError(INVALID_PARAMS)。
    target = tmp_path / "config.json"
    target.write_text("{ not json", encoding="utf-8")
    with pytest.raises(CUError) as ei:
        Config.load(target)
    assert ei.value.code is ErrorCode.INVALID_PARAMS


def test_load_rejects_non_object_root(tmp_path: Path) -> None:
    # oracle: derived —— 配置根节点应为对象。
    target = tmp_path / "config.json"
    target.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(CUError) as ei:
        Config.load(target)
    assert ei.value.code is ErrorCode.INVALID_PARAMS


# --------------------------------------------------------------------------- #
# SETTABLE_KEYS 白名单
# --------------------------------------------------------------------------- #


def test_settable_keys_whitelist_is_exact() -> None:
    # oracle: specified —— 领域规则：白名单（含 4 vlm.* / 3 omni.*）。
    # 18 项是 DEC-045 / vlm.user_agent / DEC-053 的 daemon_log_level 之后的数量。
    assert set(SETTABLE_KEYS) == CONTRACT_SETTABLE_KEYS
    assert len(SETTABLE_KEYS) == 18


def test_settable_keys_excludes_data_dir() -> None:
    # oracle: specified —— 领域规则：data_dir 刻意不可改。
    assert "data_dir" not in SETTABLE_KEYS
    assert "schema" not in SETTABLE_KEYS


def test_settable_keys_kinds_are_int_or_str() -> None:
    # oracle: derived —— 每项声明 int/str，供解析用。
    assert set(SETTABLE_KEYS.values()) == {"int", "str"}


# --------------------------------------------------------------------------- #
# apply_set：白名单校验、类型解析、不改原对象
# --------------------------------------------------------------------------- #


def test_apply_set_returns_new_object_and_does_not_mutate_original() -> None:
    # oracle: specified —— docstring：返回新配置（不改原对象）。
    original = Config()
    updated = apply_set(original, "lock_wait_seconds", "20")
    assert updated is not original
    assert updated.lock_wait_seconds == 20
    assert original.lock_wait_seconds == 10


@pytest.mark.parametrize("key", ["data_dir", "schema", "bogus_key", "vlm", "vlm.bogus", ""])
def test_apply_set_rejects_keys_outside_whitelist(key: str) -> None:
    # oracle: specified —— 白名单而非黑名单；白名单外一律拒绝。
    with pytest.raises(CUError) as ei:
        apply_set(Config(), key, "x")
    assert ei.value.code is ErrorCode.INVALID_PARAMS


def test_apply_set_rejects_outside_key_with_allowed_list_in_detail() -> None:
    # oracle: specified —— 领域规则：detail.allowed 给出可用项。
    with pytest.raises(CUError) as ei:
        apply_set(Config(), "data_dir", "C:/elsewhere")
    allowed = ei.value.detail["allowed"]
    assert allowed == sorted(SETTABLE_KEYS)
    assert "data_dir" not in allowed


def test_apply_set_parses_int_values() -> None:
    # oracle: derived —— 声明为 int 的项按整数解析。
    cfg = apply_set(Config(), "storage_limit_bytes", "2048")
    assert cfg.storage_limit_bytes == 2048
    assert isinstance(cfg.storage_limit_bytes, int)


@pytest.mark.parametrize("key", ["storage_limit_bytes", "lock_wait_seconds", "mouse_max_points"])
@pytest.mark.parametrize("value", ["abc", "3.5", "", "10x", "1e3", "0x10"])
def test_apply_set_rejects_non_integer_value_for_int_keys(key: str, value: str) -> None:
    # oracle: specified —— 领域规则：对 int 项传非法字符串抛错。
    with pytest.raises(CUError) as ei:
        apply_set(Config(), key, value)
    assert ei.value.code is ErrorCode.INVALID_PARAMS


def test_apply_set_parses_str_values() -> None:
    # oracle: derived —— str 项原样接收。
    cfg = apply_set(Config(), "image_format", "webp")
    assert cfg.image_format == "webp"


def test_apply_set_validates_result() -> None:
    # oracle: derived —— 白名单内的 key 也可能取值非法；validate 必须拦住。
    with pytest.raises(CUError) as ei:
        apply_set(Config(), "image_format", "jpg")
    assert ei.value.code is ErrorCode.INVALID_PARAMS


def test_apply_set_nested_vlm_does_not_pollute_original() -> None:
    """对嵌套项赋值后，原对象（及其嵌套对象）必须不受影响。"""
    # oracle: specified —— docstring：不改原对象。
    original = Config()
    updated = apply_set(original, "vlm.base_url", "http://example.invalid/v1")
    assert updated.vlm.base_url == "http://example.invalid/v1"
    assert original.vlm.base_url == ""
    assert updated.vlm is not original.vlm


def test_apply_set_nested_omni_does_not_pollute_original() -> None:
    # oracle: specified —— 同上，omni 嵌套项。
    original = Config()
    updated = apply_set(original, "omni.mirror", "https://hf-mirror.example")
    assert updated.omni.mirror == "https://hf-mirror.example"
    assert original.omni.mirror == ""
    assert updated.omni is not original.omni


def test_apply_set_nested_is_isolated_from_other_nested_fields() -> None:
    # oracle: derived —— 只改 vlm.base_url，不应影响 vlm 的其余字段或其他对象。
    updated = apply_set(Config(), "vlm.api_key", "secret-placeholder")
    assert updated.vlm.api_key == "secret-placeholder"
    assert updated.vlm.base_url == ""
    assert updated.omni.mirror == ""


def test_apply_set_result_is_persistable(tmp_path: Path) -> None:
    # oracle: derived —— apply_set 的产物应是合法配置，可直接 save/load。
    updated = apply_set(Config(), "overlay_continue_seconds", "9")
    target = tmp_path / "config.json"
    updated.save(target)
    assert Config.load(target).overlay_continue_seconds == 9
