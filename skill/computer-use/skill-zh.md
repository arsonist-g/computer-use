> 本文件是供人校对的译文；同目录下的 `SKILL.md` 是在用的正式版本，两者逐句对应。

# Computer-Use

`computer-use` 从 shell 驱动一台 Windows 桌面。每条命令是一个独立进程，与持有屏幕、输入队列与会话记录的常驻 daemon 通信；工具负责把你的坐标和文字变成真正落地的输入。装好之后的第一次调用会先把工具自己的环境建起来，可能要花一分钟（`references/install-and-config.md`）。

## 输出边界

本 skill 教你把 `computer-use` CLI 调对。它只覆盖这一个 CLI，也不替你决定任何事：

- 解读屏幕、决定下一步动作都是你的工作。工具没有任务或正确答案这类概念。
- 没有东西可以撤销：一次已经落下的点击收不回来。
- 写操作报告的是输入已投递，不是应用已经响应。
- 提权窗口够不着，因为操作系统会拦截发给它们的输入（UIPI）。「同意」也不是工具能给的：坐在机器前的人随时可以中止你，而你必须停下。

## 如何读本 skill

带「必须」「绝不」或「规则是」的句子是硬要求；其余内容是你可以按情况调整的指引。默认值（超时、间隔、图片格式）你可以改，当前生效值由 `config show` 打印；「谁可以做什么」你改不了。

代码块把会话 id（`s-...`）和那些数字换成你自己的就能照抄执行。命令的形状与参数名是契约；示例里的数值只是示意。

表格是参考资料。`references/` 下有两个文件，只有偶尔用得上：`errors.md`（每个 `error_code` 是什么意思、要求什么动作）与 `install-and-config.md`（首次运行的环境准备、把本 skill 装进 agent、本机命令、配置键）。

## 会话：一个任务一个会话

一个会话持有写锁、磁盘记录与解析器的引用计数。在第一条会碰到屏幕的命令之前先建一个：

```
computer-use begin --agent-hint claude-code
session s-20260913-183849-cgyg
```

后续命令用 `--session` 传这个 id，或者导出一次 `COMPUTER_USE_SESSION` 然后省掉这个参数。`--agent-hint` 是一段记进会话元数据的自由文本，能让一周之后的 `session list` 仍然读得懂。

`session end` 释放写锁并执行存储清理，所以任务做完就跑它。`session list` 把每个会话标成 `active`、`ended` 或 `orphaned`（daemon 重启过、已经不再跟踪它）。孤立会话的文件仍留在磁盘上：新建一个会话，不要在它上面接着干活。所有会话争抢同一把全局写锁，所以败者最多等 `lock_wait_seconds`（默认 10 秒），然后以 `lock_timeout` 失败。

会话目录里有 `session.json`、`ops.md`（追加式命令日志）、截图，以及解析出的 markdown。超时之后 `ops.md` 是唯一作数的账，读它来判断某个动作是否执行过。截图以文件形式留在磁盘上直到清理删掉，所有会话共享 `storage_limit_bytes`（默认 1 GiB）。

## 工作循环

```
computer-use begin --agent-hint claude-code
computer-use windows                                        # 挑一个目标：记下 hwnd 与 elevated
computer-use screenshot --hwnd 0x1A2B --session s-...        # 从结果里读出 origin
computer-use click 850 420 --hwnd 0x1A2B --session s-... --describe "打开字号输入框"
computer-use session end --session s-...
```

先看，再动，再看：对目标截图、判断你要操作的东西在图像里的坐标、加上 origin、发出一条写命令，然后重新截一次图，而不是假定它打开了什么。第一次用可以先跑 `computer-use windows`：它列出了窗口，说明桌面层工作正常。坐标换算对你是新的，就先拿一个无害的位置验证一遍，再开始长任务。

## 命令怎么发

```
computer-use <命令> [必需参数] [flag]
```

- 必需参数走位置传参：`key ctrl+s` 直接给组合键、不带 flag 名。flag 以 `--` 开头，要么带值（`--session s-...`），要么单独出现（`--json`）。一条同时点明窗口、位置与原因的写命令长这样：`computer-use key ctrl+s --hwnd 0x1A2B --session s-... --describe "保存文件"`。
- flag 写在命令之前或之后都可以，嵌套命令也一样。
- **不要猜 flag。** `computer-use --help` 列出全部命令，`computer-use <命令> --help` 打印那条命令的参数以及每个参数接受什么值。

### 共享参数

下面每个参数，每条命令都接受。`Where` 列说明哪个参数在哪里是必需的、在哪里真正起作用。

| 参数 | Where | 含义 |
|---|---|---|
| `--session <id>` | `screenshot`、`parse`、`session end` 与六条写命令必需 | 会话 id，也会从 `COMPUTER_USE_SESSION` 读取。 |
| `--describe "<text>"` | 六条写命令必需 | 这次操作做什么、为什么。 |
| `--hwnd <h>` | `screenshot`、`parse`，以及除 `scroll` 外的每条写命令 | 目标窗口：发送前会拿这个句柄与你这个会话里**最近一次对该窗口的截图**作比对（系统把句柄复用出去时是 `window_stale`），并且对 `click`、`drag`、`type` 先把窗口提到前台。 |
| `--json` | 到处都能用 | 机器可读输出，走 stdout。 |
| `--inline` | `screenshot`、`parse` | 图片或数据以 base64 返回，而不是只给路径。 |
| `--verbose` | 到处都能用 | 追加低层细节，比如这次请求由哪一层截图降级服务。 |
| `--continue` | 写命令 | 还有下一条命令：保持覆盖层与输入封锁。 |
| `--end` | 写命令 | 刚才那条是最后一条：立即开始退场。 |
| `--version` | 到处都能用 | 打印版本并退出。 |

## 命令

下面是第二级；这是第一级。

| 分组 | 命令 |
|---|---|
| 会话与生命周期 | `begin`、`session list`、`session info`、`session end` |
| 读命令 | `windows`、`screenshot`、`parse` |
| 写命令 | `click`、`move`、`drag`、`scroll`、`type`、`key` |
| 写锁 | `lock status`、`lock unlock` |

每一行列的是属于那条命令的参数，共享参数在上面那张表里。参数按你书写它们的顺序排列：位置参数在前，flag 在后。

### 会话与生命周期

| 命令 | 说明 | 参数 | 注意事项 |
|---|---|---|---|
| `begin` | 创建会话 | `--agent-hint` | 打印会话 id 与目录。不取写锁。 |
| `session list` | 会话列表，最新的在前 | | 状态、创建时间，以及每个会话的各种计数。 |
| `session info` | 显示一个会话的元数据 | | |
| `session end` | 结束会话：释放写锁并执行配额清理 | | 报告释放字节数与删除文件数。 |

### 读命令

| 命令 | 说明 | 参数 | 注意事项 |
|---|---|---|---|
| `windows` | 列出顶层窗口，从前到后 | `--all` | `--all`：连不可见或无标题的窗口一起列。`--verbose` 追加 `class`、`is_topmost`、`zorder`。字段：`hwnd`、`title`、`pid`、`process`、`rect`（`x,y,w,h`）、`monitor`、`is_foreground`、`is_minimized`、`elevated`。 |
| `screenshot` | 截取一个窗口或一个显示器 | `--hwnd \| --full` `--monitor` `--format` | `--monitor` 是 0 基、默认主显示器，且必须配 `--full`；`--format` 取 `png` 或 `webp`。返回图片路径、尺寸、`origin`，以及服务这次请求的截图 `layer`。 |
| `parse` | 检测一张图里的元素，把 markdown 写在图旁边 | `--hwnd \| --image` `--ai` | 那份 markdown 里是一张表，每个元素有 `type`、`bbox`、`interactivity`、`content`。返回路径与数量；检测器没装时是 `omni_not_installed`。 |

### 写命令

写操作返回耗时（`moved_ms`、`total_ms`），并可能带一个 `warning`：看到它就先重新确认目标再继续。写命令接受哪些共享参数，见上面那张表。

| 命令 | 说明 | 参数 | 注意事项 |
|---|---|---|---|
| `click` | 在某个点点击 | `x* y*` `--button` `--count` `--hwnd` | `--button` 取 `left`（默认）、`right` 或 `middle`；`--count 2` 是双击。 |
| `move` | 移动光标 | `x* y*` `--hwnd` | 只悬停、不点击。这里 `--hwnd` 不改变前台。 |
| `drag` | 在一点按下、移动、在另一点抬起 | `x1* y1* x2* y2*` `--button` `--hwnd` | `--button` 同 `click`。 |
| `scroll` | 滚动 | `dx* dy*` `--at` | 正 `dy` 向上滚。`--at` 是在哪个点上滚，默认当前光标位置。 |
| `type` | 在光标处输入文本 | `text*` `--hwnd` | 走 Unicode 输入路径，所以写中文不需要剪贴板。换行以 Enter 发送。降级走剪贴板时结果里会标出（`fallback: clipboard`、`clipboard_restored`），还原可能失败，而且整段字符串可能被发两遍：核对那个输入框。 |
| `key` | 发送组合键 | `combo*` `--hwnd` `--force` | 被挡下的组合键以 `dangerous_key_blocked` 失败。`--force` 越过危险键黑名单（`win+l`、`ctrl+alt+del`，以及 `danger_keys` 里的任何组合）。 |

### 写锁

| 命令 | 说明 | 参数 | 注意事项 |
|---|---|---|---|
| `lock status` | 显示谁持有写锁 | | 持有者、已持有时长，以及是否有人在等。 |
| `lock unlock` | 从持有者手里夺走锁 | `--force` `--reason` | 两者都必填。永远写进日志，持有者有会话时额外写它的 `ops.md`。 |

`config`、`daemon`、`setup omni`、`env sync`、`skill install` 与配置键在 `references/install-and-config.md`。

## 读屏幕

- 在计划点击之前先读 `elevated`：操作系统会拦截发给提权窗口的输入，所以应当绕开它，或者请用户来做这一步。
- 优先用返回的图片路径而不是 `--inline`，因为一张全屏 PNG 有数兆；并且以返回的 `origin`、`width`、`height` 为准：第一层截图失败时兜底层存的是整个显示器，所以窗口截图有时会比窗口本身更大。
- `parse` 的 `bbox` 是图像像素，所以同样适用那套 origin 换算。只在你需要元素时才读那份 markdown。

## 屏幕坐标

坐标是物理像素，原点在主显示器左上角，y 轴向下递增，而工具在读取任何坐标之前已经是逐显示器 DPI 感知的。绝不自己再套一个缩放系数。

截图的 `origin` 是图像像素 `(0, 0)` 对应的屏幕位置：窗口截图给的是该窗口左上角，全屏截图给的是 `(0, 0)`。要命中你在图里看到的东西：

```
screen_x = origin_x + image_x
screen_y = origin_y + image_y
```

规则是：绝不把图像坐标不加 origin 就传给写命令。窗口的 origin 来自合成器的扩展框，没有任何需要你补偿的偏移。

## 发送输入

- 每条写命令都必须带 `--describe`，否则会在碰到桌面之前以 `describe_required` 失败。那些坐标是屏幕绝对坐标，`--hwnd` 不改变它们的含义；不传 `--hwnd` 就没有前置校验、也不改变前台，输入发给当前获得焦点的那个窗口。
- **失败的写操作绝不重试。** 写命令从不重试，超时会被报告为失败；那次点击可能已经落下去了，所以读 `ops.md` 再决定。再发一次就是点两下。
- `--continue` 与 `--end` 告诉工具后面还有没有命令。除最后一条写命令外，每条写命令都应带 `--continue`：它保持覆盖层与输入封锁，等你的下一条命令，最长 `overlay_continue_seconds`（默认 30 秒）。不带任何标志时，这条命令一结束序列就结束：覆盖层随即撤下，输入在 0.5 秒的退出保留期后还给人。输入发送期间，覆盖层标记屏幕并封锁物理键鼠，好让人能中断：按物理 Esc 键会中止当前操作，而你自己发的 `key esc` 不受影响。收到 `aborted_by_user` 就停下，与用户确认；这次中止只拒绝一条写命令、然后自己清掉，所以用户说了「可以继续」之后直接再调一次。
- 拒绝危险键是刻意的：它们会锁住当前会话，而被锁在门外比被拒绝更糟。只在你确实想锁屏时用 `--force`，并且先告知用户。

## 红线

- 失败的写操作绝不重试。
- 绝不把没有加过 `origin` 的图像坐标传给写命令。
- 收到 `aborted_by_user` 之后绝不继续。
- 绝不要绕开覆盖层或输入封锁。

## 错误、退出码与 `--json`

每个失败都带一个取自封闭集合的 `error_code`，外加一条消息和一条写明下一步动作的 hint；按码分支，不要按散文分支。你最容易碰到的几个：

| `error_code` | 该做什么 |
|---|---|
| `invalid_params` | 修正命令；`computer-use --help` 列出全部参数。 |
| `session_not_found`、`session_already_ended` | 跑 `begin`。 |
| `window_not_found`、`window_stale` | 用 `windows` 重新枚举，不要复用那个句柄。 |
| `elevated_window` | 请用户手工做这一步。 |
| `lock_timeout` | 等着，或者读 `lock status` 看是谁持有、用 `lock unlock` 夺过来。 |
| `aborted_by_user` | 停下，与用户确认。 |
| `omni_not_installed` | 跑 `computer-use setup omni`，或者自己读图。 |
| `internal_error` | 告诉用户，不要把同一条命令再发一次。 |

`references/errors.md` 有完整的封闭集合。退出码把这些码按大类分给 shell：`2` 参数、`3` 会话或锁、`4` 目标、`5` 采集或解析、`6` 已中止、`1` 其它；优先用 `error_code`。

`--json` 在 stdout 给出机器可读信封：`{"ok": true, "result": ...}` 或 `{"ok": false, "error": {"code", "message", "hint", "detail"}}`。不带它时，人读错误走 stderr，而工具打印的少数几处字符串是中文，所以需要稳定的字段值时请解析 `--json`。