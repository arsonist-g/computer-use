# 安装与配置 Computer-Use

本文件覆盖「在布置东西、而不是在操作桌面」时的全部内容：首次运行把工具自己的环境建起来、把本 skill 装进 agent、本机命令，以及配置键。

## 首次运行

启动器为工具本身维护一个 Python 环境，因为工具不需要你的项目里的任何依赖。首次调用时，或者升级改动了项目元数据之后，它会用 `uv` 建起这个环境，这可能要花上一分钟。它把进度打在 stderr 上，并且需要 `uv` 在 `PATH` 上；如果 `uv` 缺失，它会打印安装指引然后停下，而不会退回系统 Python。

之后的调用就是一次普通的进程启动。daemon 你不用管：它按需启动，空闲 `daemon_idle_exit_seconds` 之后退出，除非覆盖层或写锁还在场。

```
computer-use env sync           # 需要时重建环境
computer-use --version
computer-use daemon status
computer-use lock status
```

在你断定写操作卡住之前，值得先读一下 `lock status`。它报告谁持有锁、持有了多久，这能把「工具卡了」与「另一个会话正在干活」区分开。

## 把本 skill 装进 agent

这个包可以把本 skill 拷进调用方 agent 的技能目录：

```
computer-use skill install       # 把 SKILL.md 与 references/ 写到 ~/.claude/skills/computer-use/
computer-use skill uninstall
```

用 `COMPUTER_USE_SKILL_DIR` 可以装到别处。`computer-use --launcher-help` 列出启动器自己处理的命令，那也是它唯一自己处理的那些；其余命令都交给 Python 客户端。只装英文正式版；`*-zh.md` 校对译本留在包里不装。

## 本机命令

| 命令 | 参数 | 返回 |
|---|---|---|
| `config show` | | 全部配置，JSON 形态。`vlm.api_key` 在输出中被遮蔽，文本与 `--json` 两种形态一致。 |
| `config set <key> <value>` | | 写入一项并立即落盘。只接受 `config show` 列出的键。 |
| `daemon status` | | PID、启动时间、管道名、活动会话数、解析器引用计数、空闲时长、常驻内存、协议版本，以及覆盖层与输入封锁状态。 |
| `daemon stop` | | 停止 daemon。它会按需重新起来。 |
| `setup omni` | `--force`（重装依赖，用来修损坏的环境）、`--skip-weights`（不下权重） | 建起解析器环境并下载权重（约 1.4 GB）。只有 `parse` 需要。 |
| `env sync` | | 启动器命令。重建 Python 环境。 |
| `skill install` / `skill uninstall` | | 启动器命令。把本 skill（SKILL.md 与 references/）拷进调用方 agent 的技能目录，或移除它。 |

## 配置键

`config show` 打印这些键及其当前值，`config set` 用同样的点号名写入。

| 键 | 默认 | 含义 |
|---|---|---|
| `data_dir` | 空 | 空表示 `~/.computer-use`。 |
| `storage_limit_bytes` | 1 GiB | `sessions/` 的总容量上限，在 `session end` 时执行，最旧优先。 |
| `image_format` | `png` | 截图默认格式；`--format` 可按次覆盖。 |
| `lock_wait_seconds` | 10 | 写操作在报 `lock_timeout` 之前等写锁的时长。 |
| `overlay_arm_ms` | 1500 | 一段写序列开始前的延迟，给人把手从键盘上拿开的时间。这段时间里输入已经被封锁。 |
| `overlay_continue_seconds` | 30 | 带 `--continue` 时，等你的下一条命令而保持覆盖层与输入封锁的时长。不带标志的写命令一结束，序列就结束。 |
| `overlay_exit_hold_ms` | 500 | 覆盖层退场之后输入还要再扣住这么久，免得那一瞬间的物理按键被吞掉。 |
| `daemon_idle_exit_seconds` | 600 | 空闲这么久之后 daemon 退出。覆盖层或写锁在场时它绝不退出。 |
| `daemon_log_level` | `info` | `info`、`warning` 或 `error`。调高可让嘈杂的日志安静下来。 |
| `daemon_log_limit_bytes` | 500 MiB | 日志上限；截掉最旧部分，保留最新部分。 |
| `danger_keys` | `win+l`、`ctrl+alt+del` | 除非写命令带 `--force`，这些组合一律被拒绝。 |
| `mouse_step_ms` | 10 | 合成光标移动的节奏。 |
| `mouse_max_points` | 30 | 那次移动最多取多少个点。 |
| `vlm.base_url`、`vlm.api_key`、`vlm.model_name`、`vlm.user_agent` | 空 | 供 `parse --ai` 使用的 OpenAI 兼容端点。key 每次被打印时都遮蔽，在 `config.json` 里以明文存储。 |
| `omni.env_path`、`omni.weights_dir`、`omni.mirror` | 空 | 解析器环境、权重与 HuggingFace 镜像。空表示按 `data_dir` 推导。 |
