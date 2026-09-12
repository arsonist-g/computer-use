"""`--json` 时错误信封走哪条流（Q-025 / api-contract.md §5 Delta）。

oracle: specified —— 契约 Delta 写死了「`--json` ⟹ 错误也走 stdout」。

为什么这几条不用起 daemon：它们挑的都是**在连接之前**就失败的命令
（`to_request` 的参数校验、argparse 自己报错），所以断言完全确定，
也不会有任何真机副作用。
"""

from __future__ import annotations

import json

import pytest

from cu.client import main


def test_json_error_goes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """`--json` + 一个必然失败的调用 ⟹ stdout 可直接 `json.loads`，退出码不变。"""
    code = main(["--json", "lock", "unlock"])       # 少了 --force，to_request 直接拒绝

    captured = capsys.readouterr()
    assert code == 2, "退出码的分类不变（invalid_params → 2）"
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_params"
    assert payload["error"]["hint"], "hint 不能空 —— 它是 AI 的下一步动作"


def test_json_error_goes_to_stdout_when_argparse_itself_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """argparse 报错时命名空间拿不到，`--json` 的意图仍必须守住。

    `_Parser.error` 把参数错误抛成 CUError，此刻 `args` 还是 None ——
    只看 `args.as_json` 就会退回 stderr，正是 Q-025 要堵的那条缝里最容易漏的一格。
    """
    code = main(["--json", "screenshot"])           # 缺 --hwnd / --full

    captured = capsys.readouterr()
    assert code == 2
    payload = json.loads(captured.out)
    assert payload["error"]["code"] == "invalid_params"


def test_text_error_still_goes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """没有 `--json` 时人读错误仍走 stderr（Unix 惯例不变），stdout 保持为空。"""
    code = main(["lock", "unlock"])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "invalid_params" in captured.err
    assert "hint:" in captured.err


def test_internal_error_carries_the_log_path(capsys: pytest.CaptureFixture[str],
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """`internal_error` 的 hint 承诺「路径见 detail.log」，错误详情必须真的带上它。"""
    import cu.client as client_mod

    def boom(*_args, **_kwargs):
        raise RuntimeError("刻意炸一个未预期异常")

    monkeypatch.setattr(client_mod, "run", boom)
    code = main(["--json", "daemon", "status"])

    captured = capsys.readouterr()
    assert code == 1
    payload = json.loads(captured.out)
    assert payload["error"]["code"] == "internal_error"
    assert payload["error"]["detail"]["log"].endswith("daemon.log")
