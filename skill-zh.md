> 本文件是供人校对的译文；同目录下的 `SKILL.md` 是在用的正式版本，两者逐句对应。

# Computer-Use

`computer-use` 从 shell 驱动一台 Windows 桌面。每条命令是一个独立进程，它通过本机命名管道与常驻 daemon 通信；屏幕、输入队列、会话记录都由 daemon 持有。你看屏幕、决定做什么；工具负责把你的坐标和文字变成真正落地的输入。

## 输出边界

本 skill 教你把 `computer-use` CLI 调对。它不替你决定任何事，而且它只覆盖这一个 CLI：

- 解读截图是你的工作。工具保存像素并告诉你这些像素来自屏幕的哪里，它不认识画面里有什么。
- 决定下一步动作是你的工作。工具没有任务、目标或正确答案这类概念。
- 没有东西可以撤销。Windows 没有通用撤销栈，一次已经落下的点击收不回来。
- 写操作不确认成功。它报告的是输入已投递，不是应用已经响应。
- 提权窗口够不着。操作系统会拦截发给它们的输入（UIPI），工具会明确报错而不是静默失败。
- 「同意」不是工具能给的。坐在机器前的人随时可以中止你，而你必须停下。

## 如何读本 skill

带「必须」「绝不」或「规则是」的句子是硬要求。违反一条就会得到错误的坐标、一段无法撤销的输入，或者一个把你自己关在外面的会话。其余内容是你可以按情况调整的指引。

代码块是可以照抄执行的命令。把会话 id（`s-...`）和那些数字换成你自己的。命令的形状与参数名是契约；示例里的数值只是示意，不是常量。

本 skill 给出默认值的地方（超时、间隔、图片格式），你可以改，当前生效值在命令参考与 `config` 的输出里。它规定「谁可以做什么」的地方，你改不了。

表格是参考资料。带规则的章节会自己说明。

## 会话与工作循环

一个会话持有写锁、磁盘记录与解析器的引用计数。在第一条会碰到屏幕的命令之前先建一个：

```
computer-use begin --agent-hint claude-code
session s-20260913-183849-cgyg
dir C:\Users\you\.computer-use\sessions\s-20260913-183849-cgyg
```

后续命令用 `--session` 传这个 id，或者导出一次然后省掉这个参数：

```
export COMPUTER_USE_SESSION=s-20260913-183849-cgyg
```

`--agent-hint` 是一段记进会话元数据的自由文本。它不花任何代价，却能让一周之后的 `session list` 仍然读得懂。

这些命令需要会话：`screenshot`、`parse`、`session end`，以及全部六条写命令。这些不需要：`begin`、`session list`、`session info`、`windows`、`lock status`、`lock unlock`、`config show`、`config set`、`daemon status`、`daemon stop`、`setup omni`。

你会反复跑的循环：

1. 枚举窗口，挑一个目标（`windows`）。
2. 对目标截图，从结果里读出 `origin`（`screenshot`）。
3. 判断你要操作的东西在图像里的坐标。
4. 加上 origin，然后发出写命令，并带上描述（`click`、`type`、`key`……）。

会话目录里有 `session.json`（元数据）、`ops.md`（追加式命令日志）、截图，以及解析出的 markdown。当你拿不准某个动作是否已经执行过，读 `ops.md`。它记录的是实际执行了什么，而超时之后，那份日志是唯一作数的账。

任务做完就结束会话。这会释放写锁并执行存储清理：

```
computer-use session end --session s-...
```

一个任务一个会话。所有会话争抢同一把全局写锁，败者最多等 `lock_wait_seconds`（默认 10 秒），然后以 `lock_timeout` 失败。

## 屏幕坐标

屏幕坐标是物理像素，原点在主显示器左上角，y 轴向下递增。工具在读取任何坐标之前已经声明了逐显示器 DPI 感知，所以它报出来的值本身就是正确的物理像素。绝不自己再套一个缩放系数。

截图会在文件路径旁边返回一个 `origin`。那个 `origin` 是图像像素 `(0, 0)` 对应的屏幕位置：

- 窗口截图返回该窗口的左上角，通常不是 `(0, 0)`。
- 全屏截图返回 `(0, 0)`。

要命中你在图里看到的东西：

```
screen_x = origin_x + image_x
screen_y = origin_y + image_y
```

规则是：绝不把图像坐标不加 origin 就传给写命令。这是最容易让工具「看起来坏了」而它其实完全在照你的指令工作的方式。

窗口截图的 `origin` 取的是该窗口**扩展框**的左上角，那才是合成器实际交付的东西，而不是 `GetWindowRect` 的返回值。两者在 Windows 10 与 11 上相差一条不可见的调整边框。没有需要你补偿的偏移。

读完整条结果，别只看路径。第一层截图失败时，兜底层存的是整个显示器而不是那个窗口，所以窗口截图有时会比窗口本身更大。以返回的 `origin`、`width`、`height` 为准。

## 命令面

| 分组 | 命令 |
|---|---|
| 会话 | `begin`、`session list`、`session info`、`session end` |
| 读 | `windows`、`screenshot`、`parse` |
| 写 | `click`、`move`、`drag`、`scroll`、`type`、`key` |
| 本机 | `lock status`、`lock unlock`、`config show`、`config set`、`daemon status`、`daemon stop`、`setup omni` |
| 启动器 | `env sync`、`skill install`、`skill uninstall` |

在猜某个参数之前，先读本文末尾命令参考里的参数表。

## 读命令

读命令不取写锁，而且除非你主动要求，它们永远不需要解析器。

`windows` 按 z-order 从前到后列出顶层窗口。在计划点击之前先读 `elevated`：发给提权窗口的输入会被操作系统拦截，所以 `elevated=true` 不是你能打中的目标，应当绕开它，或者请用户来做这一步。

`screenshot` 需要 `--hwnd` 或 `--full` 之一。它返回文件路径、尺寸与 `origin`。优先用路径而不是 `--inline`：一张全屏 PNG 有数兆，把它拉进你的上下文远比真正需要看图时去读那个文件更贵。

`parse` 对一张图跑元素检测，把 markdown 写在图旁边。它返回 markdown 路径与元素数量，刻意不返回元素本身，因为一个内容多的屏幕会产生几百个元素。那份 markdown 里是一张表，每个元素有 `type`、`bbox`、`interactivity`、`content`；`bbox` 是图像像素，所以同样适用于那套 origin 换算。只在你需要元素时才读那个文件。

`--ai` 会额外让一个已配置的视觉模型去修正元素描述。它要付一次网络往返，只在元素说明确实错了时才有帮助。普通的 `parse` 才是默认路径，这是有理由的。

如果检测器没有安装，`parse` 会以 `omni_not_installed` 失败并告诉你安装命令。其余所有命令照常工作，因为解析器是一个可选组件。

## 写命令

### 每条写命令都带描述

每条写命令都必须带 `--describe "<做什么、为什么>"`。省略它会在碰到桌面之前就以 `describe_required` 失败。描述不是装饰：它是让记录可复盘的东西。只有坐标的日志，读者无法看出你当时想做什么。

```
computer-use click 850 420 --session s-... --describe "打开字号输入框"
computer-use click 850 420 --count 2 --session s-... --describe "双击选中这个词"
computer-use move 400 300 --session s-... --describe "悬停工具栏以显示标签"
computer-use drag 100 200 700 600 --session s-... --describe "拖住角调整面板大小"
computer-use scroll 0 -3 --at 850 420 --session s-... --describe "在文档里向下滚"
computer-use type "hello world" --session s-... --describe "填入搜索框"
computer-use key ctrl+s --session s-... --describe "保存文件"
```

坐标是屏幕绝对坐标，含义就是上面定义的那套。`--hwnd` 是可选的，它不改变那些数字的含义。只要你心里有明确的窗口就带上它，因为正是它打开了下面那几道前置校验。

### `--hwnd` 是保护你的东西

当你传了 `--hwnd`，工具会在发送输入之前校验这个句柄，并把该窗口的进程 id 与窗口类，和你这个会话里**最近一次对该窗口的截图**作比对。正是这道比对能抓住被系统复用给了另一个进程的句柄，否则它只会表现为一次落在意外位置的点击，而且完全不报错。校验失败是 `window_stale`，从来不是警告。

对 `click`、`drag`、`type`，`--hwnd` 还会先把那个窗口提到前台，好让输入落在你指定的窗口上，而不是碰巧在最前面的那个。`move` 与 `key` 不改变前台，`scroll` 根本不接受 `--hwnd`。

不传 `--hwnd` 就没有前置校验、也不改变前台：输入发给当前获得焦点的那个窗口。这合法，有时也正是你要的，但它同样是无人看管的 agent 把字打到错误窗口里的方式。

对 `scroll`，正数 `dy` 表示向上滚。这与很多 API 相反，所以方向重要时请核对符号。

`type` 通过 Unicode 输入路径发送文本，所以写中文不需要绕道剪贴板。文本里的换行会以 Enter 键发送，因为多数控件只认按键。

### 写操作不重试，你也不许重试

写命令从不重试，超时会被报告为失败而不是被悄悄重复。规则是：失败的写操作你自己不要重试。一次超时的点击可能已经点下去了。再发一次就是点两下，而第二下会落在第一下打开的东西上。

写操作失败时，读 `ops.md` 看它是否执行过，然后再决定。作数的是那份日志，不是命令的返回值。

### 告诉工具你还要不要继续

在你发送输入期间，工具会标记屏幕、封锁物理键鼠，让旁边看着的人知道你还在工作。这个状态持续多久，是工具不得不自己猜的：它无从知道你的下一条命令是一秒后还是三十秒后。

你知道。说出来：

```
computer-use click 400 300 --session s-... --continue --describe "聚焦地址栏"
computer-use type "https://example.com" --session s-... --end --describe "跳转"
```

- `--continue` 表示还有下一条。覆盖层保持、输入保持封锁，并且续期保持窗口，不再重跑武装前摇。
- `--end` 表示刚才那条是最后一条。退场立刻开始，输入会在一个短暂的退出保留期之后放行。
- 两个标志都不带时，工具保持 `overlay_hold_seconds`（默认 30 秒），然后自行离开。

任务没做完时就带 `--continue`。不带的话，一次超过保持窗口的停顿会让工具在下一条命令之前重新武装，旁边的人会再次感到键盘被拿走。

### 覆盖层，与旁边看着的人

在输入发送期间，工具会显示一个覆盖层并封锁物理键鼠。这不是缺陷，也不是需要绕开的东西。它存在的意义就是让人能中断。

按物理 Esc 键会中止当前操作。你自己发的 `key esc` 命令不受影响，因为工具只吞掉物理事件，合成输入照常放行。

当你收到 `aborted_by_user`，停下。不要恢复任务，也不要重新发出被中断的那条命令。先与用户确认。中止会把写通路闩住：后续写操作会持续以 `aborted_by_user` 失败，直到 daemon 重启（`daemon stop`，或 daemon 自己的空闲退出）。另建一个会话并不能清除它。

### 有些按键组合被拒绝

`win+l`、`ctrl+alt+del`，以及 `danger_keys` 配置里列出的任何组合，都会以 `danger_key_blocked` 被拒绝。这是刻意的：它们会锁住当前会话，而一个能把你锁在门外的工具比一个会拒绝的工具更糟。`--force` 可以越过检查。只在你确实想锁屏时用它，并且先告知用户。

## 错误、退出码与 `--json`

每个失败都带一个取自封闭集合的 `error_code`，外加一条消息和一条写明下一步动作的 hint。按码分支，不要按散文分支。完整表在命令参考里。

退出码把这些码按大类分给 shell：`2` 参数、`3` 会话或锁、`4` 目标、`5` 采集或解析、`6` 已中止、`1` 其它。优先用 `error_code`；退出码是同一事实的粗粒度版本。

给任意命令加 `--json` 会得到机器可读信封。成功是 `{"ok": true, "result": ...}`，失败是 `{"ok": false, "error": {"code", "message", "hint", "detail"}}`。带 `--json` 时两者都走 stdout，所以只读 stdout 的调用方绝不会静默漏掉错误。不带 `--json` 时，人读错误走 stderr，符合 Unix 惯例。

工具打印的人读文本里有少数几处是中文，所以当你需要稳定的字段值时，请解析 `--json`。

## 磁盘数据与清理

工具留下的一切默认都在 `~/.computer-use/` 下；用 `COMPUTER_USE_HOME` 可以搬走。它是一个普通目录，你可以读、可以拷、可以删。工具不加密它，也不把它上传到任何地方。

- `config.json` 保存全部配置项，其中包括 `vlm.api_key`，它**以明文**存储。把那个文件当机密对待。
- `sessions/` 下每个会话一个目录：截图、`session.json`、`ops.md`，以及解析出的 markdown。
- `logs/daemon.log` 是 daemon 自己的日志。当错误码不足以定位问题时，读它：它记的是 daemon 自己做过什么，而 `ops.md` 只记你的命令。`daemon_log_level` 过滤它，`daemon_log_limit_bytes` 给它封顶，方式是截掉最旧的部分、保留最新的。
- `venv/` 是启动器建起的 Python 环境。

你的屏幕截图会以文件形式留在本机，它们不是一次性缓冲。会话目录会一直持有它们，直到清理把它们删掉；所有会话共享 `storage_limit_bytes`（默认 1 GiB）。清理在会话结束时执行：按最旧优先驱逐，绝不碰活动会话，先删图片及其解析产物、之后才删操作日志，只有当把所有图片都删掉仍然不够时才会删整个会话目录。如果你需要某张截图更早消失，请自己删掉那个文件。

`session list` 显示每个会话的状态：`active` 是你正在用的那个，`ended` 是正常关闭的，`orphaned` 表示 daemon 重启过、已经不再跟踪它。孤立会话的文件仍留在磁盘上。不要在它上面接着干活：新建一个会话。

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

这个包可以把本文件拷进调用方 agent 的技能目录：

```
computer-use skill install       # 写到 ~/.claude/skills/computer-use/SKILL.md
computer-use skill uninstall
```

用 `COMPUTER_USE_SKILL_DIR` 可以装到别处。`computer-use --launcher-help` 列出启动器自己处理的命令，那也是它唯一自己处理的那些；其余命令都交给 Python 客户端。

## 确认它工作正常

```
computer-use windows
```

如果它列出了窗口，说明工具可达、桌面层工作正常。如果它失败，错误码会告诉你到底是 daemon 没起来、会话不存在，还是桌面拒绝了。

长任务开始前有个好用的自检：对目标截图，读出 `origin`，用 origin 换算点一个无害的位置。如果这一下点在你想点的地方，你的坐标处理就是对的，后面的也都会对。

<!-- 命令参考：与 `computer-use --help` 及各子命令自己的 help 保持同步。 -->

## 命令参考

每条命令都接受下面这些全局选项。子命令的选项可以写在子命令之前或之后，嵌套子命令也不例外（`daemon status --json` 与 `session list --json` 都是合法的）。

### 全局选项

| 选项 | 含义 |
|---|---|
| `--session <id>` | 会话 id。也会从 `COMPUTER_USE_SESSION` 读取。`screenshot`、`parse`、`session end` 与全部写命令必需。 |
| `--describe "<text>"` | 这次操作做什么、为什么。**全部写命令必需。** |
| `--json` | 机器可读输出：`{"ok": true, "result": ...}` 或 `{"ok": false, "error": {...}}`，两者都在 stdout。 |
| `--inline` | 截图与结构化数据以 base64 内联返回，而不是只给路径。默认关闭，为的是保护你的上下文。 |
| `--verbose` | 追加低层细节：这次请求由哪一层截图降级服务、耗时分解、窗口的 `class` 与 `zorder`。 |
| `--continue` | 还有下一条命令：保持覆盖层与输入封锁，不再重跑武装前摇。 |
| `--end` | 刚才那条是最后一条：立刻开始退场。 |
| `--version` | 打印版本并退出。 |

### 会话与生命周期

| 命令 | 参数 | 返回 |
|---|---|---|
| `begin` | `--agent-hint "<text>"` | 创建会话；打印会话 id 与目录。不取写锁。 |
| `session list` | | 会话列表，最新的在前，带状态、创建时间，以及截图数、解析文件数与操作数。 |
| `session info` | `--session <id>` | 该会话的元数据。 |
| `session end` | `--session <id>` | 释放写锁、解析器引用计数减一、执行配额清理。报告释放字节数、删除文件数与删除会话数。 |

### 读命令

| 命令 | 参数 | 返回 |
|---|---|---|
| `windows` | `--all`（连不可见或无标题的窗口一起列）、`--verbose`（追加 `class`、`is_topmost`、`zorder`） | 窗口，按 z-order 从前到后。字段：`hwnd`、`title`、`pid`、`process`、`rect`（`x,y,w,h`）、`monitor`、`is_foreground`、`is_minimized`、`elevated`。人读文本那一行不含 `monitor`。 |
| `screenshot` | `--hwnd <h>` 或 `--full` 之一，配 `--full` 时可加 `--monitor <n>`（0 基，默认主显示器），另可 `--format png\|webp` | 图片路径、尺寸、`origin`，以及服务这次请求的截图 `layer`。 |
| `parse` | `--hwnd <h>` 或 `--image <path>` 之一，另可 `--ai` | markdown 路径、元素数，以及跑过 `--ai` 时的模型名。 |

### 写命令

这些全都接受 `--describe`，除 `scroll` 外全都接受 `--hwnd`。

| 命令 | 位置参数 | 可选参数 |
|---|---|---|
| `click <x> <y>` | 屏幕坐标 | `--button left\|right\|middle`（默认 `left`）、`--count <n>`（默认 1，2 即双击） |
| `move <x> <y>` | 屏幕坐标 | 只移动光标，不点击。不改变前台。 |
| `drag <x1> <y1> <x2> <y2>` | 起终点屏幕坐标 | `--button left\|right\|middle` |
| `scroll <dx> <dy>` | 滚动量；正 `dy` 向上 | `--at <x> <y>`（默认当前光标位置） |
| `type "<text>"` | 文本 | 支持空白与换行；换行以 Enter 发送。 |
| `key "<combo>"` | 组合键串，如 `ctrl+c`、`alt+tab`、`win+r`、`shift+f10`、`esc` | `--force` 越过危险键黑名单 |

写操作返回耗时（`moved_ms`、`total_ms`），并可能带一个 `warning`。看到它就先重新确认目标再继续：那意味着工具在告诉你，你脚下的情况可能已经变了。

当 `type` 用不了 Unicode 路径时，它会降级走剪贴板，结果在 `detail` 里如实标出，带 `fallback: clipboard` 与 `clipboard_restored`。关于这条路径有两件事要知道。还原可能失败，那会把你的剪贴板留在工具输入的那段文本上。而且降级会把整段字符串重发一遍，所以失败之前已经打进去的字符可能出现两次。继续之前先核对那个输入框。

### 本机命令

| 命令 | 参数 | 返回 |
|---|---|---|
| `lock status` | | 锁持有者、已持有时长，以及是否有人在等。 |
| `lock unlock` | `--force` 与 `--reason "<text>"`，两者都必填 | 从持有者手里夺走锁。永远写进 daemon 日志；持有者有会话时额外写它的 `ops.md`。 |
| `config show` | | 全部配置，JSON 形态。`vlm.api_key` 在输出中被遮蔽，文本与 `--json` 两种形态一致。 |
| `config set <key> <value>` | | 写入一项并立即落盘。只接受 `config show` 列出的键。 |
| `daemon status` | | PID、启动时间、管道名、活动会话数、解析器引用计数、空闲时长、常驻内存、协议版本，以及覆盖层与输入封锁状态。 |
| `daemon stop` | | 停止 daemon。它会按需重新起来。 |
| `setup omni` | `--force`（重装依赖，用来修损坏的环境）、`--skip-weights`（不下权重） | 建起解析器环境并下载权重（约 1.4 GB）。只有 `parse` 需要。 |
| `env sync` | | 启动器命令。重建 Python 环境。 |
| `skill install` / `skill uninstall` | | 启动器命令。把本文件拷进调用方 agent 的技能目录，或移除它。 |

### 错误码

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
| `omni_failed` | 检测器进程异常。重试一次；持续失败就读 daemon 日志。 |
| `vlm_failed` | 多模态端点调用失败。检查 `vlm.base_url` 与 `vlm.api_key`；原始结构化数据仍然在。 |
| `dangerous_key_blocked` | 该组合在黑名单里。只在你确实想做时加 `--force`，并且先告知用户。 |
| `aborted_by_user` | 用户按了物理 Esc。停下并与用户确认。 |
| `internal_error` | 上报。detail 里写了 daemon 日志路径。 |

### 退出码

| 码 | 含义 |
|---|---|
| `0` | 成功。 |
| `1` | 未预期的内部错误。 |
| `2` | 参数错误。修正命令。 |
| `3` | 会话或锁问题。读 `error_code` 决定是新建会话还是等一等。 |
| `4` | 目标问题：窗口不存在、被复用、已最小化或已提权。重新枚举，或放弃该目标。 |
| `5` | 采集或解析失败。读操作可以重试；写操作**不可以**。 |
| `6` | 用户中止。停下并确认。 |

### 配置键

`config show` 打印这些键及其当前值，`config set` 用同样的点号名写入。

| 键 | 默认 | 含义 |
|---|---|---|
| `data_dir` | 空 | 空表示 `~/.computer-use`。 |
| `storage_limit_bytes` | 1 GiB | `sessions/` 的总容量上限，在 `session end` 时执行，最旧优先。 |
| `image_format` | `png` | 截图默认格式；`--format` 可按次覆盖。 |
| `lock_wait_seconds` | 10 | 写操作在报 `lock_timeout` 之前等写锁的时长。 |
| `overlay_arm_ms` | 500 | 一段写序列开始前的延迟，给人把手从键盘上拿开的时间。这段时间里输入已经被封锁。 |
| `overlay_hold_seconds` | 30 | 你既不带 `--continue` 也不带 `--end` 时的兜底保持时长。 |
| `overlay_exit_hold_ms` | 500 | 覆盖层退场之后输入还要再扣住这么久，免得那一瞬间的物理按键被吞掉。 |
| `daemon_idle_exit_seconds` | 600 | 空闲这么久之后 daemon 退出。覆盖层或写锁在场时它绝不退出。 |
| `daemon_log_level` | `info` | `info`、`warning` 或 `error`。调高可让嘈杂的日志安静下来。 |
| `daemon_log_limit_bytes` | 500 MiB | 日志上限；截掉最旧部分，保留最新部分。 |
| `danger_keys` | `win+l`、`ctrl+alt+del` | 除非写命令带 `--force`，这些组合一律被拒绝。 |
| `mouse_step_ms` | 10 | 合成光标移动的节奏。 |
| `mouse_max_points` | 30 | 那次移动最多取多少个点。 |
| `vlm.base_url`、`vlm.api_key`、`vlm.model_name`、`vlm.user_agent` | 空 | 供 `parse --ai` 使用的 OpenAI 兼容端点。key 每次被打印时都遮蔽，在 `config.json` 里以明文存储。 |
| `omni.env_path`、`omni.weights_dir`、`omni.mirror` | 空 | 解析器环境、权重与 HuggingFace 镜像。空表示按 `data_dir` 推导。 |
