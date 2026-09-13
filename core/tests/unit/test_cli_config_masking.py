"""守卫（T2）：`config show` / `config set` 的输出里绝不出现明文 api_key。

oracle: specified —— api-contract.md 的 Delta（`config show` / `config set` 的 key 一律遮蔽）。
与 DEC-053「凭据不进 daemon 日志」是同一约束的另一面：那个管**落盘**，这个管**回显**。

为什么必须由测试守：SKILL 文档教 AI agent 用 `config show` 读配置，明文回显会让用户的
key 进入 agent 的上下文、进而进入模型服务商的日志 —— 暴露面比本地文件大得多
（本地文件还有 ACL 兜着）。而遮蔽只发生在渲染层，`config.json` 与 daemon 内存里的值
必须保持不变，这两件事都要钉住。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from cu import client
from cu.config import Config, VlmConfig

#: 占位符，不是任何真实凭据。24 字符，足以走「保留首尾」那条分支。
KEY = "sk-live-0123456789abcdef"
MASKED_KEY = "sk-l…cdef"


def _payload() -> dict:
    """`config.get` / `config.set` 的返回形态（daemon 侧的 `Config.to_dict()`）。"""
    return {
        "schema": 1,
        "data_dir": "",
        "vlm": {
            "base_url": "http://example.invalid/v1",
            "api_key": KEY,
            "model_name": "example-model",
            "user_agent": "",
        },
        "omni": {"env_path": "", "weights_dir": "", "mirror": ""},
    }


# --------------------------------------------------------------------------- #
# mask_secret 本身
# --------------------------------------------------------------------------- #


def test_mask_secret_keeps_only_the_ends() -> None:
    assert client.mask_secret(KEY) == MASKED_KEY
    assert KEY[4:-4] not in client.mask_secret(KEY), "中间那段必须看不见"


def test_partial_reveal_hides_at_least_half() -> None:
    masked = client.mask_secret(KEY)
    visible = len(masked) - 1          # 减去省略号
    assert visible <= len(KEY) / 2, "保留首尾不能保留到「等于没遮」"


def test_short_and_empty_secrets_are_handled() -> None:
    assert client.mask_secret("") == "", "空值保持空（未配置就是未配置）"
    for value in ("k", "short", "sk-1234567890", "sk-12345678901"):
        assert client.mask_secret(value) == "***", f"{value!r} 太短，必须整段抹掉"


# --------------------------------------------------------------------------- #
# 两种渲染路径
# --------------------------------------------------------------------------- #


def test_text_rendering_masks_the_api_key() -> None:
    text = client.render_text("config", client._mask_for_output("config", _payload()))
    assert KEY not in text
    assert MASKED_KEY in text
    assert "http://example.invalid/v1" in text, "非密钥字段照常显示"


def test_json_rendering_masks_the_api_key() -> None:
    rendered = client.render_json(client._mask_for_output("config", _payload()))
    assert KEY not in rendered
    parsed = json.loads(rendered)
    assert parsed["result"]["vlm"]["api_key"] == MASKED_KEY
    assert parsed["result"]["vlm"]["model_name"] == "example-model"


def test_emit_masks_in_both_output_modes(capsys: pytest.CaptureFixture[str]) -> None:
    """走真正的出口 `emit` —— 文本与 `--json` 两条路都不能漏。

    `config show` 与 `config set` 共用这条路径：后者回显的是**更新后的整份配置**，
    同样带着 api_key。
    """
    args = argparse.Namespace(command="config", as_json=True, verbose=False)
    client.emit(args, _payload())
    assert KEY not in capsys.readouterr().out

    args.as_json = False
    client.emit(args, _payload())
    text = capsys.readouterr().out
    assert KEY not in text
    assert MASKED_KEY in text


def test_non_config_commands_are_not_touched() -> None:
    """遮蔽只针对 config —— 别的命令的返回原样透传（不做无谓的对象拷贝）。"""
    payload = {"anything": KEY}
    assert client._mask_for_output("session", payload) is payload
    assert client._mask_for_output("config", "不是 dict") == "不是 dict"


# --------------------------------------------------------------------------- #
# 只是不打印：内存与磁盘上的值都不许改
# --------------------------------------------------------------------------- #


def test_masking_does_not_touch_the_value_or_the_file(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    cfg = Config(vlm=VlmConfig(api_key=KEY))
    cfg.save(target)

    client._mask_for_output("config", cfg.to_dict())        # 「渲染」一次

    assert cfg.vlm.api_key == KEY                            # 内存里的值没变
    assert Config.load(target).vlm.api_key == KEY            # 读回来的值也没变
    assert KEY in target.read_text(encoding="utf-8")         # 文件仍是明文（契约如此）


def test_masked_copy_does_not_alias_the_original() -> None:
    """遮蔽返回的是副本：不能把原对象的嵌套 dict 改掉。"""
    payload = _payload()
    masked = client._mask_for_output("config", payload)

    assert payload["vlm"]["api_key"] == KEY
    assert masked["vlm"]["api_key"] == MASKED_KEY
    assert masked["vlm"] is not payload["vlm"]
