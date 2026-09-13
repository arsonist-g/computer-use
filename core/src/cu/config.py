"""配置：默认值、校验、原子读写。

每个默认值都对应一条已定决策，改动前请先看 `reference` 列指向的条目 ——
这些数字不是随手填的。

| 字段 | 默认 | 依据 |
|---|---|---|
| `storage_limit_bytes` | 1 GiB | DEC-017 / DEC-038（字节上限 + 最旧优先） |
| `daemon_log_limit_bytes` / `daemon_log_level` | 500 MiB / info | DEC-038 / DEC-053（截头保尾；级别过滤，info = 全写） |
| `lock_wait_seconds` | 10 | DEC-022 |
| `overlay_arm_ms` | 500 | DEC-045（原 1500，缩短后仍够「让手离开」） |
| `overlay_continue_seconds` | 120 | DEC-075（`--continue` 的保持窗口；不带标志时不留兜底保持） |
| `overlay_exit_hold_ms` | 500 | DEC-045（退出保留期，避免与用户的物理动作撞上） |
| `mouse_step_ms` / `mouse_max_points` | 10 / 30 | DEC-008（Q-015 待实测调整） |
| `daemon_idle_exit_seconds` | 600 | DEC-035 |
| `image_format` | png | DEC-016 |
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .errors import CUError, ErrorCode

DEFAULT_DATA_DIR_NAME = ".computer-use"
SCHEMA_VERSION = 1

#: daemon 日志的合法级别（由低到高）。**必须与 `daemon/log.py` 的 `LEVELS` 一致** ——
#: 两者分居契约层与 daemon 层（config 不能 import daemon：那会把桌面层拖进 CLI 冷路径），
#: 所以这份清单写了两处，由 `tests/unit/test_daemon_log.py` 断言它们相同。
LOG_LEVELS: tuple[str, ...] = ("info", "warning", "error")

#: 危险键序列黑名单（DEC-019）。命中则拒绝执行，需 `--force` 越过。
#: 注：Ctrl+Alt+Del 是系统安全注意序列，任何用户态钩子都拦不住，这里记录它是为了
#: 在调用方尝试时直接给出「做不到」而不是静默失败。
DEFAULT_DANGER_KEYS: list[str] = ["win+l", "ctrl+alt+del"]


def default_data_dir() -> Path:
    return Path.home() / DEFAULT_DATA_DIR_NAME


@dataclass(slots=True)
class VlmConfig:
    """OpenAI 兼容的多模态端点（requirements §4 / DEC-011）。三者都可自定义以接第三方供应商。"""

    base_url: str = ""
    api_key: str = ""
    model_name: str = ""
    #: 请求的 User-Agent。**默认不是 urllib 的默认值** —— 实测某端点（Cloudflare 前置）
    #: 对 `Python-urllib/3.12` 直接返回 403 error code 1010（浏览器完整性检查），
    #: 换成普通浏览器或 curl 的 UA 就通。不少 WAF 都拦默认客户端 UA，
    #: 而这不是我们能控制的第三方行为，所以给一个能过大多数 WAF 的默认值并允许覆盖。
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )


@dataclass(slots=True)
class OmniConfig:
    """OmniParser 的安装位置（DEC-002 可选依赖 / DEC-037 独立环境）。"""

    env_path: str = ""          # 留空表示按 data_dir/venv-omni 推导
    weights_dir: str = ""       # 留空表示按 data_dir/models 推导
    mirror: str = ""            # HuggingFace 镜像，国内网络必需


@dataclass(slots=True)
class Config:
    schema: int = SCHEMA_VERSION
    data_dir: str = ""                       # 留空表示 default_data_dir()

    storage_limit_bytes: int = 1 * 1024**3
    daemon_log_limit_bytes: int = 500 * 1024**2
    #: daemon 日志的级别下限：低于它的记录直接丢掉。默认 `info` = 全都写。
    daemon_log_level: str = "info"
    lock_wait_seconds: int = 10
    overlay_arm_ms: int = 500
    #: `--continue` 的保持窗口：调用方说了还要继续，覆盖层与输入封锁就再留这么久
    #: （下一条命令什么时候来只有调用方知道，DEC-045）。不带这个标志时**不留兜底保持**
    #: （DEC-075）—— 「没说继续」的意思就是「这条之后结束了」，猜一个时长只会白扣用户的键鼠。
    overlay_continue_seconds: int = 120
    #: 退出保留期：覆盖层撤下后，输入再扣住这么久才放行。
    overlay_exit_hold_ms: int = 500
    daemon_idle_exit_seconds: int = 600

    image_format: str = "png"                # png | webp
    mouse_step_ms: int = 10
    mouse_max_points: int = 30

    danger_keys: list[str] = field(default_factory=lambda: list(DEFAULT_DANGER_KEYS))

    vlm: VlmConfig = field(default_factory=VlmConfig)
    omni: OmniConfig = field(default_factory=OmniConfig)

    # ---- 派生的路径（不落盘，由 data_dir 推出） ----

    @property
    def root(self) -> Path:
        return Path(self.data_dir) if self.data_dir else default_data_dir()

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def daemon_log(self) -> Path:
        return self.logs_dir / "daemon.log"

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def daemon_lock_path(self) -> Path:
        return self.root / "daemon.lock"

    @property
    def omni_env(self) -> Path:
        return Path(self.omni.env_path) if self.omni.env_path else self.root / "venv-omni"

    @property
    def omni_weights(self) -> Path:
        return Path(self.omni.weights_dir) if self.omni.weights_dir else self.root / "models"

    # ---- 校验 ----

    def validate(self) -> None:
        """非法值一律抛错，不静默夹取 —— 静默改配置会让用户以为设置生效了。"""
        def need(cond: bool, msg: str) -> None:
            if not cond:
                raise CUError(ErrorCode.INVALID_PARAMS, f"配置项非法：{msg}")

        need(self.schema == SCHEMA_VERSION, f"schema 应为 {SCHEMA_VERSION}，实际 {self.schema}")
        need(self.storage_limit_bytes > 0, "storage_limit_bytes 必须为正")
        need(self.daemon_log_limit_bytes > 0, "daemon_log_limit_bytes 必须为正")
        need(self.daemon_log_level in LOG_LEVELS,
             f"daemon_log_level 只能是 {'/'.join(LOG_LEVELS)}，实际 {self.daemon_log_level!r}")
        need(self.lock_wait_seconds >= 0, "lock_wait_seconds 不能为负")
        need(self.overlay_arm_ms >= 0, "overlay_arm_ms 不能为负")
        need(self.overlay_continue_seconds >= 0, "overlay_continue_seconds 不能为负")
        need(self.overlay_exit_hold_ms >= 0, "overlay_exit_hold_ms 不能为负")
        need(self.daemon_idle_exit_seconds > 0, "daemon_idle_exit_seconds 必须为正")
        need(self.image_format in ("png", "webp"), f"image_format 只能是 png/webp，实际 {self.image_format}")
        need(self.mouse_step_ms > 0, "mouse_step_ms 必须为正")
        need(self.mouse_max_points >= 1, "mouse_max_points 至少为 1")
        need(isinstance(self.danger_keys, list) and all(isinstance(k, str) for k in self.danger_keys),
             "danger_keys 必须是字符串列表")

    # ---- 读写 ----

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Config:
        """只接受已知字段；未知字段忽略（前向兼容：新版写的配置能被旧版读）。"""
        known = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for k, v in raw.items():
            if k not in known:
                continue
            if k == "vlm" and isinstance(v, dict):
                kwargs["vlm"] = VlmConfig(**{kk: vv for kk, vv in v.items()
                                             if kk in {f.name for f in fields(VlmConfig)}})
            elif k == "omni" and isinstance(v, dict):
                kwargs["omni"] = OmniConfig(**{kk: vv for kk, vv in v.items()
                                               if kk in {f.name for f in fields(OmniConfig)}})
            else:
                kwargs[k] = v
        cfg = cls(**kwargs)
        cfg.validate()
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """读配置。文件不存在时返回默认配置并落盘一次 —— 首次运行即自解释。"""
        target = path or default_data_dir() / "config.json"
        if not target.exists():
            cfg = cls()
            cfg.validate()
            return cfg
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CUError(ErrorCode.INVALID_PARAMS,
                          f"配置文件无法解析：{target}（{exc}）") from exc
        if not isinstance(raw, dict):
            raise CUError(ErrorCode.INVALID_PARAMS, f"配置文件根节点应为对象：{target}")
        return cls.from_dict(raw)

    def save(self, path: Path | None = None) -> Path:
        """原子写：先写临时文件再 `os.replace`，避免崩溃留下半个文件（data-model.md §3.3）。"""
        self.validate()
        target = path or self.config_path
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return target


#: 允许通过 `config set` 修改的字段（嵌套项用点号）。白名单而非黑名单：
#: 防止误改 `data_dir` 这类会改变全局状态的项。
SETTABLE_KEYS: dict[str, str] = {
    "storage_limit_bytes": "int",
    "daemon_log_limit_bytes": "int",
    "daemon_log_level": "str",
    "lock_wait_seconds": "int",
    "overlay_arm_ms": "int",
    "overlay_continue_seconds": "int",
    "overlay_exit_hold_ms": "int",
    "daemon_idle_exit_seconds": "int",
    "image_format": "str",
    "mouse_step_ms": "int",
    "mouse_max_points": "int",
    "vlm.base_url": "str",
    "vlm.api_key": "str",
    "vlm.model_name": "str",
    "vlm.user_agent": "str",
    "omni.env_path": "str",
    "omni.weights_dir": "str",
    "omni.mirror": "str",
}


def apply_set(cfg: Config, key: str, value: str) -> Config:
    """`config set` 的落地点。返回新配置（不改原对象）。"""
    if key not in SETTABLE_KEYS:
        raise CUError(ErrorCode.INVALID_PARAMS,
                      f"不可设置的配置项：{key}",
                      {"allowed": sorted(SETTABLE_KEYS)})
    kind = SETTABLE_KEYS[key]
    try:
        parsed: Any = int(value) if kind == "int" else value
    except ValueError as exc:
        raise CUError(ErrorCode.INVALID_PARAMS, f"{key} 应为整数，实际 {value!r}") from exc

    cfg = Config.from_dict(cfg.to_dict())
    if "." in key:
        head, tail = key.split(".", 1)
        setattr(getattr(cfg, head), tail, parsed)
    else:
        setattr(cfg, key, parsed)
    cfg.validate()
    return cfg
