"""daemon 诊断日志（Q-022 / DEC-038）。

oracle: specified —— 上限按 `daemon_log_limit_bytes` **字节**封顶（默认 500MiB）；滚动是
**截头保尾**（与 `storage.cleanup` 的最旧优先方向刻意相反，日志的价值在最新那几行）；
级别低于 `daemon_log_level`（默认 `info` = 全写）的记录直接丢掉；日志里绝不出现凭据。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cu.daemon.log import DaemonLog


def test_log_file_appears_on_first_write(tmp_path: Path) -> None:
    """`~/.computer-use/logs/` 此前根本不存在 —— 第一次写要把目录一并建出来。"""
    log = DaemonLog(tmp_path / "logs" / "daemon.log", 4096)

    log.info("daemon 启动", pid=1234)

    assert log.path.is_file()
    text = log.path.read_text(encoding="utf-8")
    assert "daemon 启动" in text and "pid=1234" in text
    assert "info" in text


def test_rotation_keeps_the_tail_and_drops_the_head(tmp_path: Path) -> None:
    """超限之后**尾部的行还在**，被截掉的是头部 —— 这是与工件清理相反的方向。"""
    log = DaemonLog(tmp_path / "daemon.log", 600)

    for index in range(200):
        log.info(f"第 {index:03d} 行" + "x" * 40)

    text = log.path.read_text(encoding="utf-8")
    assert log.path.stat().st_size <= 600 + 200, "上限之外只允许那行轮转标记"
    assert "第 199 行" in text, "最新一行必须还在"
    assert "第 000 行" not in text, "最旧一行应当被截掉"
    assert "已截去较旧的部分" in text, "截断本身要留痕，否则读日志的人不知道少了东西"


def test_rotation_never_leaves_a_torn_line(tmp_path: Path) -> None:
    """保留段的起点落在某一行中间时，把那一行的残段也丢掉 —— 半个时间戳更难读。"""
    log = DaemonLog(tmp_path / "daemon.log", 300)

    for index in range(50):
        log.info(f"{index:03d}" + "y" * 50)

    body = [line for line in log.path.read_text(encoding="utf-8").splitlines()
            if not line.startswith("---")]
    assert body, "轮转之后不该只剩标记行"
    assert all(line.endswith("y" * 50) for line in body), "每条留下的记录都必须是完整的"


def test_api_key_never_lands_in_the_log(tmp_path: Path) -> None:
    """硬约束：日志里出现一次密钥就等于把密钥落盘。

    oracle: specified —— 任务约束 1 与 DEC-038 的约束段。
    """
    secret = "sk-live-0123456789abcdef"
    log = DaemonLog(tmp_path / "daemon.log", 1 << 20, secrets=[secret])

    log.error("调用端点失败", detail=f"Authorization: Bearer {secret}")

    text = log.path.read_text(encoding="utf-8")
    assert secret not in text
    assert "***" in text


def test_register_secrets_covers_a_later_config_set(tmp_path: Path) -> None:
    """`config set vlm.api_key` 之后必须重新登记 —— 否则旧值被抹、新值直接落盘。"""
    log = DaemonLog(tmp_path / "daemon.log", 1 << 20, secrets=["old-key"])

    log.register_secrets(["new-key"])
    log.warning("强夺写锁", reason="new-key 出现在 reason 里")

    text = log.path.read_text(encoding="utf-8")
    assert "new-key" not in text
    assert "old-key" not in text


def test_empty_secret_is_ignored(tmp_path: Path) -> None:
    """空串会匹配一切（`str.replace("")` 在每个字符间插占位符）—— 必须剔除。"""
    log = DaemonLog(tmp_path / "daemon.log", 1 << 20, secrets=["", None])  # type: ignore[list-item]

    log.info("api_key 未配置")

    text = log.path.read_text(encoding="utf-8")
    assert "api_key 未配置" in text
    assert "***" not in text


def test_multiline_field_becomes_one_line(tmp_path: Path) -> None:
    """traceback 是多行的，但**一行一条记录**是「截掉整条」这个前提。"""
    log = DaemonLog(tmp_path / "daemon.log", 1 << 20)

    log.error("未捕获异常", traceback="Traceback (most recent call last):\n  a.py\n  b.py")

    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "traceback=Traceback (most recent call last): a.py b.py" in lines[0]


def test_write_failure_is_swallowed(tmp_path: Path) -> None:
    """日志写不进去不能让正在处理的那条命令失败（那才是真正的因小失大）。"""
    blocked = tmp_path / "daemon.log"
    blocked.mkdir()                      # 占成一个目录：写文件必然失败
    log = DaemonLog(blocked, 4096)

    log.info("这条写不进去，但不该抛")    # 不抛即通过


# ---------------------------------------------------------------------------
# 级别过滤（`config.daemon_log_level`）
# ---------------------------------------------------------------------------


def test_default_level_is_info_so_nothing_is_dropped(tmp_path: Path) -> None:
    """默认 `info` = 全写：三个级别都要落盘。"""
    log = DaemonLog(tmp_path / "daemon.log", 1 << 20)

    log.info("级别 info 的记录")
    log.warning("级别 warning 的记录")
    log.error("级别 error 的记录")

    text = log.path.read_text(encoding="utf-8")
    assert "级别 info 的记录" in text
    assert "级别 warning 的记录" in text
    assert "级别 error 的记录" in text


def test_warning_threshold_drops_info(tmp_path: Path) -> None:
    """门槛提到 `warning`：info 丢掉，warning / error 留下。"""
    log = DaemonLog(tmp_path / "daemon.log", 1 << 20, level="warning")

    log.info("这条不该出现")
    log.warning("这条要出现")
    log.error("这条也要出现")

    text = log.path.read_text(encoding="utf-8")
    assert "这条不该出现" not in text
    assert "这条要出现" in text
    assert "这条也要出现" in text


def test_error_threshold_drops_everything_below_it(tmp_path: Path) -> None:
    log = DaemonLog(tmp_path / "daemon.log", 1 << 20, level="error")

    log.info("级别 info 不该落盘")
    log.warning("级别 warning 也不该落盘")
    log.error("级别 error 该落盘")

    text = log.path.read_text(encoding="utf-8")
    assert "级别 info 不该落盘" not in text
    assert "级别 warning 也不该落盘" not in text
    assert "级别 error 该落盘" in text


def test_filtered_write_creates_no_file(tmp_path: Path) -> None:
    """被过滤掉就是「什么都没发生」—— 连文件都不该被建出来（否则 `logs/` 目录
    会因为一条被丢掉的 info 而无端出现）。"""
    log = DaemonLog(tmp_path / "daemon.log", 1 << 20, level="error")

    log.info("被丢掉")

    assert not log.path.exists()


def test_unknown_level_is_never_dropped(tmp_path: Path) -> None:
    """拼错的级别按**最高**严重度处理 —— 宁可多写一行，也不要静默丢消息。"""
    log = DaemonLog(tmp_path / "daemon.log", 1 << 20, level="error")

    log.write("verbose", "这个级别不存在，但它不该被吞掉")

    assert "这个级别不存在，但它不该被吞掉" in log.path.read_text(encoding="utf-8")


def test_level_names_agree_with_config() -> None:
    """`daemon/log.py` 与 `cu.config` 各存了一份级别清单 —— 必须一致。

    为什么会有两份：`config` 属于契约层，**不能** import daemon 模块（那会把桌面层
    拖进 CLI 的冷路径，`test_guards.py` 会红）。所以用这条测试把漂移钉住。
    """
    import cu.config as config_mod
    from cu.daemon import log as log_mod

    assert tuple(config_mod.LOG_LEVELS) == tuple(log_mod.LEVELS)
    assert config_mod.Config().daemon_log_level in log_mod.LEVELS


def test_config_rejects_an_unknown_level() -> None:
    from cu.config import Config
    from cu.errors import CUError, ErrorCode

    with pytest.raises(CUError) as info:
        Config.from_dict({"daemon_log_level": "verbose"})
    assert info.value.code is ErrorCode.INVALID_PARAMS


def test_config_default_log_budget_is_500_mib() -> None:
    from cu.config import Config

    assert Config().daemon_log_limit_bytes == 500 * 1024**2


# ---------------------------------------------------------------------------
# 滚动的边界
# ---------------------------------------------------------------------------


def test_rotation_handles_a_file_far_larger_than_the_scan_window(tmp_path: Path) -> None:
    """滚动只扫一小段找行首，但保留段是**整段分块拷贝**的 —— 用一个远大于扫描窗口的
    上限跑一遍，确认保留段没被截短、也没有把整文件读进内存之外的行为差异。"""
    limit = 512 * 1024
    log = DaemonLog(tmp_path / "daemon.log", limit)

    for _ in range(4000):                       # 约 900KB > 512KB 上限
        log.info("x" * 200)

    text = log.path.read_text(encoding="utf-8")
    body = [line for line in text.splitlines() if not line.startswith("---")]
    assert len(body) > 1000, "保留段应当是整整一截，而不是只剩几行"
    assert all(line.endswith("x" * 200) for line in body)
    assert log.path.stat().st_size <= limit + 200


def test_rotation_of_a_single_oversized_line_keeps_the_log_alive(tmp_path: Path) -> None:
    """一行就超过上限：留不下完整的一行，但**不能把文件清空** ——
    那等于「日志自己把自己删了」，比留一条残行糟得多。"""
    log = DaemonLog(tmp_path / "daemon.log", 200)

    log.info("z" * 5000)
    log.info("后面的行还在")

    text = log.path.read_text(encoding="utf-8")
    assert text.strip(), "文件不该被清空"
    assert "后面的行还在" in text, "滚动之后日志必须还能继续用"
    assert log.path.stat().st_size <= 200 + 200
