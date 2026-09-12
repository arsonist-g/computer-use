"""daemon 诊断日志（Q-022 / DEC-038）。

oracle: specified —— 上限按 `daemon_log_limit_bytes` **字节**封顶；滚动是**截头保尾**
（与 `storage.cleanup` 的最旧优先方向刻意相反，日志的价值在最新那几行）；
日志里绝不出现凭据。
"""

from __future__ import annotations

from pathlib import Path

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
