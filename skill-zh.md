> 本文件是供人校对的译文；同目录下的 `SKILL.md` 是在用的正式版本，两者逐句对应。

# Computer-Use

你通过命令行驱动一台 Windows 桌面。每条命令是一个独立进程，它通过本机管道与常驻 daemon 通信；
屏幕、输入队列、会话记录都由 daemon 持有。

## 输出边界

本 skill 只教你如何正确调用这个工具。它不产出自动化逻辑本身，也不决定屏幕上该做什么。
读截图、决定下一步动作，是你的职责，不是工具的。

工具不做这些事：

- 不识别截图里有什么。它保存像素，并告诉你这些像素来自屏幕的哪里。解读是你的工作。
- 不撤销任何东西。Windows 没有通用撤销栈。一次已经落下的点击，工具无法回退。
- 不保证点击命中了目标。它报告的是输入是否已投递，不是应用是否响应了。
- 不操作提权窗口。发给它们的输入会被操作系统拦截，工具会明确报错而不是静默失败。

## 如何读本 skill

标了「必须」「绝不」或「规则是」的句子是硬要求。违反它们会导致坐标算错、输入无法恢复，
或者把用户锁在电脑外。其余内容是你可以按情况调整的指引。

下文有两处带真实数值的示例。把那些数字当作示意，不是常量。命令的形状与参数名是契约；数值不是。

本 skill 给出默认值的地方（超时、间隔、步数），你可以覆盖。它规定「谁可以做什么」的地方，你不可以覆盖。

## 首先要做对的一件事：坐标

屏幕坐标是物理像素，原点在主显示器左上角，y 轴向下递增。工具在读取任何坐标之前已经声明了
逐显示器 DPI 感知，所以它报出来的值本身就是正确的物理像素。你不要再自己套缩放系数。

截图会在文件路径之外同时返回一个 `origin`。那个 `origin` 是图像像素 `(0, 0)` 对应的屏幕位置：

- 窗口截图返回该窗口的左上角，通常不是 `(0, 0)`。
- 全屏截图返回 `(0, 0)`。

要点击你在图里看到的东西，用这个换算：

```
screen_x = origin_x + image_x
screen_y = origin_y + image_y
```

规则是：绝不把图像坐标不加 origin 就传给 `click`。这是最容易让工具「看起来坏了」而它其实
完全按指令工作的方式。

窗口截图包含标题栏，它的 `(0, 0)` 与窗口外框矩形精确对齐。没有需要补偿的偏移。

## 工作循环

1. 建一个会话。
2. 枚举窗口，找到目标。
3. 对目标截图，读出 `origin`。
4. 判断你想操作的东西在图像里的坐标。
5. 加上 origin，发出写命令，并带上描述。

会话是持有写锁与磁盘记录的单元。在第一次写操作之前先建一个。

```
computer-use begin --agent-hint claude-code
```

它会打印一个会话 id 与一个目录。后续命令用 `--session` 传这个 id，或者导出一次
`COMPUTER_USE_SESSION` 然后省掉这个参数。

会话目录里有 `ops.md`，它是每条命令及其描述与结果的追加日志。当你拿不准某个动作是否已经执行过，
读那个文件。它记录的是**实际执行了什么**，这也是超时之后唯一作数的账。

## 读命令

这些命令不取锁，也不会阻塞其他调用方。

```
computer-use windows
computer-use windows --all --verbose
```

按 z-order 从前到后列出顶层窗口。默认隐藏没有标题的窗口。每行带有 `hwnd`、`title`、`pid`、
`process`、`rect`、`monitor`、`is_foreground`、`is_minimized` 和 `elevated`。

在计划点击之前先看 `elevated`。发给提权窗口的输入会被操作系统拦截，所以标着 `elevated=1` 的窗口
不是你能打中的目标；绕开它。

```
computer-use screenshot --hwnd 0x0001A2B --session s-...
computer-use screenshot --full
computer-use screenshot --full --monitor 0
```

它打印文件名、尺寸与 `origin`。加 `--inline` 会让响应里带 base64 而不是路径。优先用路径：
一张全屏 PNG 有数兆，把它塞进上下文远比你需要时去读那个文件更贵。

`--full` 截主显示器，除非你用 `--monitor` 指明另一个。

```
computer-use parse --hwnd 0x0001A2B --session s-...
computer-use parse --image C:\path\to\shot.png --session s-...
```

对一张图跑元素检测，写出一个 markdown 文件，返回它的路径与元素数量。它刻意不返回元素本身：
一个内容多的屏幕会产生几百个元素，你应该只在需要时才读那个文件。

`--ai` 会额外让一个已配置的视觉模型去修正描述。它要付一次网络往返，只在元素说明确实错了时才有帮助。
普通的 `parse` 才是默认路径，这是有理由的。

如果检测器没有安装，`parse` 会以 `omni_not_installed` 失败并给出安装提示。其余功能照常工作，
因为检测器是一个可选组件。

### `--ai` 背后的视觉端点

`--ai` 是唯一需要多模态端点的功能，它默认不指向任何地方。用配置命令接上一个：

```
computer-use config show
computer-use config set vlm.base_url https://api.example.com/v1
computer-use config set vlm.api_key sk-YOUR-KEY
computer-use config set vlm.model_name gpt-4o-mini
computer-use config show --json
```

`config show` 打印全部配置，只有 `vlm.api_key` 会被遮蔽（文本与 `--json` 都是 `sk-a2b1…fbd2` 这种形式），key 因此不会进入 agent 的上下文。`config set <key> <value>` 写入一项并落盘到 `config.json`，立即生效，
下一条 `parse --ai` 就能用上，不需要重启任何东西。这三个键描述一个 OpenAI 兼容端点：
`vlm.base_url` 是它的根地址（到 `/v1` 为止）、`vlm.api_key` 是 bearer token、`vlm.model_name`
是要调用的模型 id。`config set` 只接受 `config show` 列出的键；写别的会以 `invalid_params`
失败，并在 detail 里给出允许的集合。

api key 以明文存在 `config.json` 里，所以把那个文件当机密对待，不要把值带进日志与记录。
不带 `--ai` 的普通 `parse` 完全不碰这个端点。

## 写命令

每条写命令都必须带 `--describe "<做什么、为什么>"`。省略它会在碰到桌面之前就以
`describe_required` 失败。描述不是装饰：它是让记录可复盘的东西。只有坐标的日志，
读者无法看出操作者当时想做什么。

```
computer-use click 850 420 --session s-... --describe "打开字号输入框"
computer-use click 850 420 --count 2 --session s-... --describe "双击选中这个词"
computer-use move 400 300 --session s-... --describe "悬停工具栏以显示标签"
computer-use drag 100 200 700 600 --session s-... --describe "拖住角调整面板大小"
computer-use scroll 0 -3 --at 850 420 --session s-... --describe "在文档里向下滚"
computer-use type "hello world" --session s-... --describe "填入搜索框"
computer-use key ctrl+s --session s-... --describe "保存文件"
```

坐标是屏幕绝对坐标。`--hwnd` 是可选的，它不改变那些数字的含义；它只是要求工具先校验目标，
并在发送输入之前把那个窗口提到前面。当你心里有明确的窗口时就带上它，因为正是它能在点击落到
别处之前抓住被复用的句柄或已最小化的窗口。

对 `scroll`，正数 `dy` 表示向上滚。这与很多 API 相反，所以方向重要时请核对符号。

### 告诉工具你还要继续

两条写命令之间，工具会让屏幕保持标记、并继续封锁物理键鼠，让旁边看着的人知道你还在工作。
它靠时钟决定保持多久，而时钟是个猜：它不知道你下一条命令是一秒后还是三十秒后。

你知道。说出来：

```
computer-use click 400 300 --session s-... --continue --describe "聚焦地址栏"
computer-use type "https://example.com" --session s-... --end --describe "跳转"
```

`--continue` 表示还有下一条。`--end` 表示刚才那条是最后一条。两者都不是必填的，
都不带你也没关系，工具会等一会儿然后自己停。

任务没做完时就带 `--continue`。不带的话，你两次命令之间只要隔得比内置窗口长，
工具就会重新封锁键鼠 —— 旁边的人会感到输入被反复切断。

`type` 通过 Unicode 输入路径处理非 ASCII 文本，所以输入中文不需要绕道剪贴板。

当这条 Unicode 路径失败时，`type` 会降级走剪贴板：先读走你原来的剪贴板内容，把待输入文本写进剪贴板，
发 `Ctrl+V`，然后尽量把原来的内容还原。还原可能失败，结果里会如实标出 —— 失败时
`detail.clipboard_restored` 为 `false` —— 而这种情况下你的剪贴板就被留在了我们输入的文本上。
这条写命令仍然报 `ok`；如果剪贴板对你有意义，请读一下 detail。

### 写操作不重试，你也不许重试

写命令从不重试，超时会被报告为失败而不是被悄悄重复。规则是：失败的写操作你自己不要重试。
一次超时的点击可能已经点下去了。再发一次就是点两下，而第二下会落在第一下打开的东西上。

写操作失败时，读 `ops.md` 看它是否执行过，然后再决定。作数的是那份日志，不是命令的返回值。

### 有些按键组合被拦截

`win+l`、`ctrl+alt+del` 及类似组合会以 `dangerous_key_blocked` 被拒绝。这是刻意的：
它们会锁住当前会话，而一个能把你锁在门外的工具比一个会拒绝的工具更糟。`--force` 可以越过检查。
只在你确实想锁屏时用它，并且先告知用户。

## 用户可以叫停你，你必须尊重

工具在发送输入期间会在屏幕上显示一个覆盖层，并封锁物理键鼠。这不是缺陷，也不是需要绕开的东西；
它存在的意义就是让旁边看着的人能中断。

按物理 Esc 会中止当前操作。你自己发的 `key esc` 不受影响，因为工具能区分合成输入与真实按键。

当你收到 `aborted_by_user`，停下。不要恢复任务，也不要重新发出被中断的那条命令。先与用户确认。

覆盖层会在一次写序列的第一条命令之前出现一小会儿，并在写操作密集连续时保持。它们停下来之后，
覆盖层会自行退场。

## 错误

每个失败都带一个取自封闭集合的 `error_code`，外加一条人读消息和一条写明下一步动作的 hint。
按码分支。

| `error_code` | 对你意味着什么 |
|---|---|
| `invalid_params` | 修正命令。 |
| `describe_required` | 补上 `--describe`。 |
| `session_not_found` | 会话没了。新建一个。 |
| `lock_timeout` | 另一个会话持有写锁。detail 里写了它是谁、持有了多久。 |
| `window_not_found` | 重新枚举。 |
| `window_stale` | 句柄被系统复用给了别的进程。重新枚举；不要复用那个句柄。 |
| `window_minimized` | 请用户还原它，或换一个窗口。 |
| `elevated_window` | 工具无法操作它。请用户手动完成这一步。 |
| `capture_failed` | 全部截图路径都失败了。确认窗口还在。 |
| `capture_black` | 取到的是黑帧。重试；若持续如此，该表面可能受保护。 |
| `omni_not_installed` | 元素检测器未安装。装上它，或者你自己从图里看。 |
| `dangerous_key_blocked` | 该组合在黑名单里。 |
| `aborted_by_user` | 停下，与用户确认。 |
| `internal_error` | 上报。detail 里写了日志路径。 |

退出码把这些按大类分给 shell：`2` 参数、`3` 会话或锁、`4` 目标、`5` 采集或解析、`6` 已中止。
优先用 `error_code`；退出码是同一事实的粗粒度版本。

给任意命令加 `--json` 会得到机器可读信封：`ok` 加 `result`，或者 `error` 带 `code`、`message`、
`hint`、`detail`。

## 会话与清理

```
computer-use session list
computer-use session end --session s-...
```

结束会话会释放写锁并执行存储清理。清理按最旧优先驱逐，且绝不碰活动会话。它先删图片与其解析产物，
之后才删操作日志，因为日志是「发生过什么」的记录，而且它很小。

daemon 按需启动，空闲自行退出。你不用管它。有两条命令用于诊断：

```
computer-use daemon status
computer-use lock status
```

在你断定写操作卡住之前，值得先读一下 `lock status`。它报告谁持有锁、持有了多久，
这能把「工具卡了」与「另一个会话正在干活」区分开。

## 数据落点

工具留下的一切都在 `~/.computer-use/`（用 `COMPUTER_USE_HOME` 可以搬走）。它是一个普通目录，
你可以读、可以拷、可以删 —— 工具不加密它，也不把它上传到任何地方。里面会有：

- `config.json` —— 全部配置项，其中包括 `vlm.api_key`，它**以明文**存在这里。密钥一旦落进这个文件，
  就把这个文件本身当作秘密。
- `sessions/` —— 每个会话一个目录，以会话 id 命名。里面是该会话的截图 PNG、`session.json`
  （会话元数据）、`ops.md`（追加式命令日志），以及 `parse` 写出的结构化 markdown。
- `logs/daemon.log` —— daemon 自己的日志（见下）。
- `venv/` —— wrapper 首次运行时建起的 base Python 环境。
- `venv-omni/`、`models/`、`OmniParser/` —— 检测器的独立环境、权重与上游源码。只有跑过
  `computer-use setup omni` 之后才存在。

你的屏幕截图会以文件形式留在本机。它们不是一次性缓冲：一个 `sessions/` 目录会一直持有它们，
直到清理把它们删掉；会话共享默认 1 GiB 的配额。配额只在 `session end` 时执行，按最旧优先清理，
且绝不删除活动会话。如果你希望某张截图比这更早消失，请自己删掉它。

### daemon 日志

daemon 遇到它自己也解释不了的情况时，会返回 `internal_error`，`detail.log` 里写明日志路径，
完整的现场落在 `~/.computer-use/logs/daemon.log`。当错误码不足以定位问题时，就去看那个文件：
它记的是 daemon 自己做过什么，而 `ops.md` 只记你的命令。

`daemon_log_level` 决定写多少。合法值是 `info` / `warning` / `error`，默认 `info`（全都写）。
把级别提到 `warning` 或 `error` 可以让日志安静下来：

```
computer-use config set daemon_log_level warning
computer-use daemon status --json
```

日志有 500 MB 上限（`daemon_log_limit_bytes`）。超过之后，较旧的部分被截掉、保留最新的，
所以即使机器连续跑了很久，最近的失败现场仍然读得到。

## 确认它工作正常

```
computer-use windows
```

如果它列出了窗口，说明工具可达、桌面层工作正常。如果它失败，错误码会告诉你到底是 daemon 没起来、
会话不存在，还是桌面拒绝了。

长任务开始前有个好用的自检：对目标截图，读出 `origin`，用 origin 换算点一个无害的位置。
如果这一下点在你想点的地方，你的坐标处理就是对的，后面的也都会对。
