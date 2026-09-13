# 错误码

每个失败都带一个取自封闭集合的 `error_code`，外加一条消息和一条写明下一步动作的 hint。读码，不要读散文：下面的表写明每个码对你意味着什么、该做什么。

## 错误码

| `error_code` | 对你意味着什么 |
|---|---|
| `invalid_params` | 修正命令。`computer-use --help` 列出全部参数。 |
| `protocol_version_mismatch` | 客户端与 daemon 的协议不一致。客户端会重启 daemon 并重试一次；仍失败就 `daemon stop` 后重试。 |
| `session_not_found` | 会话没了。执行 `begin`。 |
| `session_already_ended` | 该会话已结束。为当前任务新建一个。 |
| `describe_required` | 补上 `--describe "做什么、为什么"`。 |
| `lock_timeout` | 另一个会话持有写锁；detail 里写了它是谁、持有了多久。等一等，或用 `lock unlock --force` 夺过来。 |
| `window_not_found` | 窗口没了。重新用 `windows` 枚举。 |
| `window_stale` | 句柄被系统复用给了别的进程。重新枚举；不要复用那个句柄。 |
| `window_minimized` | 请用户还原它，或换一个窗口。 |
| `elevated_window` | 该窗口属于提权进程，操作系统会拦截发给它的输入。请用户手动完成这一步。 |
| `foreground_failed` | 无法把该窗口提到前台。重试一次；仍失败则该窗口当前不可激活。 |
| `capture_failed` | 每一层截图都失败了。确认窗口还在，并请用户退出独占全屏。 |
| `capture_black` | 取到的是黑帧。重试；该表面可能受保护或仍在渲染。 |
| `omni_not_installed` | 元素检测器未安装。执行 `computer-use setup omni`，或者你自己读图。 |
| `omni_failed` | 检测器进程异常。重试一次；持续失败就改为自己读图。 |
| `vlm_failed` | 多模态端点调用失败。检查 `vlm.base_url` 与 `vlm.api_key`；原始结构化数据仍然在。 |
| `dangerous_key_blocked` | 该组合在黑名单里。只在你确实想做时加 `--force`，并且先告知用户。 |
| `aborted_by_user` | 用户按了物理 Esc。停下并与用户确认。 |
| `internal_error` | 工具自身出错，不是你的命令造成的。告诉用户；不要重试同一条命令。 |

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 成功。 |
| `1` | 未预期的内部错误。 |
| `2` | 参数错误。修正命令。 |
| `3` | 会话或锁问题。读 `error_code` 决定是新建会话还是等一等。 |
| `4` | 目标问题：窗口不存在、被复用、已最小化或已提权。重新枚举，或放弃该目标。 |
| `5` | 采集或解析失败。读操作可以重试；写操作**不可以**。 |
| `6` | 用户中止。停下并确认。 |
