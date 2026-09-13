---
name: computer-use
description: Any task that must operate a Windows desktop programmatically MUST use this skill: capture a screen or a window, list open windows, click or move the mouse, type text, send a key combination, drag, scroll, or turn a screenshot into structured elements. It is the command-line surface of a resident daemon that reads the screen and synthesizes real input, with every write gated by a natural-language description.
---

# Computer-Use

`computer-use` drives a Windows desktop from a shell. Every command is its own process that talks to a resident daemon over a local named pipe; the daemon owns the screen, the input queue, and the session record. You look at the screen and decide what to do. The tool turns your coordinates and your text into input that really lands.

## Output boundary

This skill teaches you to call the `computer-use` CLI correctly. It does not decide anything for you, and it covers this CLI only:

- Interpreting a screenshot is your job. The tool saves pixels and tells you where they came from. It does not recognize what is in them.
- Choosing the next action is your job. The tool has no notion of a task, a goal, or a correct answer.
- Nothing is undoable. Windows has no general undo stack, and a click that already landed cannot be taken back.
- A write does not confirm success. It reports that input was dispatched, not that the application responded.
- Elevated windows are out of reach. The OS blocks input to them (UIPI), and the tool reports that instead of failing silently.
- Consent is not the tool's to give. The person at the machine can abort you at any moment, and you must stop.

## How to read this skill

Sentences with "must", "never", or "the rule is" are hard requirements. Breaking one gives you wrong coordinates, an input sequence you cannot undo, or a session you have locked out. Everything else is guidance you can adapt to the situation.

Code blocks are commands you can run as written. Replace the session ids (`s-...`) and the numbers with your own. The command shapes and the argument names are the contract; the values in the examples are illustrations, not constants.

Where this skill names a default (a timeout, an interval, an image format), you may change it. The live values are in the command reference and in the `config` output. Where it says who may do something, you may not change it.

Tables are reference. The sections that carry a rule say so.

## Sessions and the working loop

A session owns the write lock, the on-disk record, and the parser's reference count. Start one before your first command that touches the screen:

```
computer-use begin --agent-hint claude-code
session s-20260913-183849-cgyg
dir C:\Users\you\.computer-use\sessions\s-20260913-183849-cgyg
```

Pass that id on later commands with `--session`, or export it once and drop the flag:

```
export COMPUTER_USE_SESSION=s-20260913-183849-cgyg
```

`--agent-hint` is free text recorded in the session metadata. It costs nothing and makes `session list` readable a week later.

These commands need a session: `screenshot`, `parse`, `session end`, and all six write commands. These do not: `begin`, `session list`, `session info`, `windows`, `lock status`, `lock unlock`, `config show`, `config set`, `daemon status`, `daemon stop`, `setup omni`.

The loop you will run over and over:

1. Enumerate windows and pick a target (`windows`).
2. Screenshot the target and read the `origin` from the result (`screenshot`).
3. Decide the image coordinates of the thing you want to hit.
4. Add the origin, then send the write, with a description (`click`, `type`, `key`, ...).

The session directory holds `session.json` (metadata), `ops.md` (the append-only command log), the screenshots, and the parsed markdown. When you are unsure whether an action already happened, read `ops.md`. It records what was actually executed, and after a timeout that log is the only account that counts.

End the session when the task is done. That releases the write lock and runs storage cleanup:

```
computer-use session end --session s-...
```

One session per task. All sessions contend for a single global write lock, and a loser waits up to `lock_wait_seconds` (10 by default) before failing with `lock_timeout`.

## Screen coordinates

Screen coordinates are physical pixels, origin at the top left of the primary display, y increasing downward. The tool declares per-monitor DPI awareness before it reads any coordinate, so what it reports is already correct physical pixels. Never apply a scale factor yourself.

A screenshot returns an `origin` next to the file path. That `origin` is the screen position of the image's pixel `(0, 0)`:

- A window screenshot returns that window's top-left corner, which is usually not `(0, 0)`.
- A full-screen screenshot returns `(0, 0)`.

To hit something you saw in an image:

```
screen_x = origin_x + image_x
screen_y = origin_y + image_y
```

The rule is: never pass an image coordinate to a write command without adding the origin first. This is the single most common way to make the tool look broken while it is obeying you exactly.

The window screenshot's `origin` is the top-left of the window's extended frame, which is what the compositor actually delivers, and not the value `GetWindowRect` returns. The two differ by an invisible resize border on Windows 10 and 11. There is no offset for you to compensate for.

Read the whole result, not just the path. When the first capture layer fails, the fallback stores the entire monitor instead of the window, so a window screenshot is sometimes larger than the window. Trust the returned `origin`, `width`, and `height`.

## The command surface

| Group | Commands |
|---|---|
| Session | `begin`, `session list`, `session info`, `session end` |
| Read | `windows`, `screenshot`, `parse` |
| Write | `click`, `move`, `drag`, `scroll`, `type`, `key` |
| Local | `lock status`, `lock unlock`, `config show`, `config set`, `daemon status`, `daemon stop`, `setup omni` |
| Launcher | `env sync`, `skill install`, `skill uninstall` |

Read the parameter tables in the command reference at the end of this file before you guess at a flag.

## Read commands

Read commands take no write lock, and they never need the parser unless you ask for it.

`windows` lists top-level windows in z-order, front to back. Read `elevated` before you plan a click: input to an elevated window is blocked by the OS, so `elevated=true` is not a target you can hit, and you should route around it or ask the user to do that step.

`screenshot` needs either `--hwnd` or `--full`. It returns a file path, the dimensions, and the `origin`. Prefer the path over `--inline`: a full-screen PNG is megabytes, and pulling it into your context costs far more than reading the file when you actually need to look at it.

`parse` runs an element detector over an image and writes markdown next to it. It returns the markdown path and an element count, deliberately not the elements themselves, because a busy screen produces hundreds of them. The markdown holds a table of `type`, `bbox`, `interactivity`, and `content` per element, and its `bbox` is in image pixels, so the same origin arithmetic applies. Read that file only when you need the elements.

`--ai` additionally asks a configured vision model to correct the element descriptions. It costs a network round trip and only helps when the captions are wrong. Plain `parse` is the default for a reason.

If the detector is not installed, `parse` fails with `omni_not_installed` and tells you the install command. Every other command keeps working, because the parser is an optional component.

## Write commands

### Every write carries a description

Every write command must carry `--describe "<what and why>"`. Omitting it fails with `describe_required` before anything touches the desktop. The description is not decoration: it is what makes the record replayable. A log of coordinates alone cannot tell a reader what you were trying to do.

```
computer-use click 850 420 --session s-... --describe "open the font size field"
computer-use click 850 420 --count 2 --session s-... --describe "double-click to select the word"
computer-use move 400 300 --session s-... --describe "hover the toolbar to reveal labels"
computer-use drag 100 200 700 600 --session s-... --describe "resize the panel by its corner"
computer-use scroll 0 -3 --at 850 420 --session s-... --describe "scroll down in the document"
computer-use type "hello world" --session s-... --describe "fill the search box"
computer-use key ctrl+s --session s-... --describe "save the file"
```

Coordinates are absolute screen coordinates, in the sense defined above. `--hwnd` is optional and does not change what the numbers mean. Supply it whenever you have a specific window in mind, because it is what turns on the preflight checks below.

### `--hwnd` is what protects you

When you pass `--hwnd`, the tool checks the handle before dispatching input, and it compares the window's process id and class against the same window in your session's most recent screenshot of it. That comparison is what catches a handle that the system recycled to a different process, which otherwise shows up as a click that lands somewhere unexpected with no error at all. A failed check is `window_stale`, never a warning.

`--hwnd` also brings that window to the foreground first, for `click`, `drag` and `type`, so the input lands on the window you named rather than on whatever happens to be in front. `move` and `key` do not change the foreground, and `scroll` takes no `--hwnd` at all.

Without `--hwnd` there is no preflight and no foreground change: input goes to whatever is focused. That is legal and sometimes what you want, but it is also the way an unattended agent types into the wrong window.

For `scroll`, a positive `dy` scrolls up. That is the opposite of many APIs, so check the sign when direction matters.

`type` sends text through the Unicode input path, so writing Chinese does not need a clipboard round trip. A newline in the text is sent as the Enter key, because most controls only accept a key press.

### Writes never retry, and neither may you

A write never retries, and a timeout is reported as a failure rather than silently repeated. The rule is: do not retry a failed write yourself. A click that timed out may already have landed. Sending it again clicks twice, and the second click lands on whatever the first one opened.

When a write fails, read `ops.md` to see whether it executed, then decide. The log is the record of what happened, not the command's return value.

### Tell the tool whether you are continuing

While you are sending input, the tool marks the screen and blocks the physical keyboard and mouse, so the person watching knows you are still working. How long that lasts is a guess the tool has to make on its own: it cannot know whether your next command comes in a second or in thirty.

You know. Say so:

```
computer-use click 400 300 --session s-... --continue --describe "focus the address bar"
computer-use type "https://example.com" --session s-... --end --describe "navigate"
```

- `--continue` says another command is coming. The overlay stays, input stays blocked, and the hold is renewed without re-running the arm delay.
- `--end` says that was the last one. The exit starts immediately, and input is released after a short exit hold.
- With neither flag, the tool holds for `overlay_hold_seconds` (30 by default) and then leaves on its own.

Use `--continue` whenever you are mid-task. Without it, a pause longer than the hold window makes the tool re-arm before your next command, and the person watching feels their keyboard being taken away again.

### The overlay, and the person watching

While input is in flight the tool shows an overlay and blocks the physical keyboard and mouse. This is not a bug and not something to work around. It exists so that a person can interrupt.

Pressing the physical Esc key aborts the current operation. Your own `key esc` command is unaffected, because the tool swallows only physical events and lets synthetic input through.

When you receive `aborted_by_user`, stop. Do not resume the task, and do not re-issue the interrupted command. Confirm with the user first. An abort latches the write path: further writes keep failing with `aborted_by_user` until the daemon restarts (`daemon stop`, or the daemon's own idle exit). Starting a new session does not clear it.

### Some key combinations are refused

`win+l`, `ctrl+alt+del`, and anything else listed in the `danger_keys` config are refused with `danger_key_blocked`. This is deliberate: they lock the session, and a tool that locks you out is worse than one that refuses. `--force` bypasses the check. Use it only when locking the session is genuinely what you want, and tell the user first.

## Errors, exit codes, and `--json`

Every failure carries an `error_code` from a closed set, plus a message and a hint naming the next action. Branch on the code, not on the prose. The full table is in the command reference.

Exit codes group those codes for a shell: `2` arguments, `3` session or lock, `4` target, `5` capture or parse, `6` aborted, `1` anything else. Prefer `error_code`; the exit code is the coarse version of the same fact.

Add `--json` to any command for a machine-readable envelope. Success is `{"ok": true, "result": ...}` and failure is `{"ok": false, "error": {"code", "message", "hint", "detail"}}`. With `--json`, both go to stdout, so a caller that reads stdout alone never silently misses an error. Without it, human-readable errors go to stderr, as Unix convention expects.

A few human-readable strings the tool prints are Chinese, so parse `--json` when you need stable field values.

## Data on disk and cleanup

Everything the tool keeps lives under `~/.computer-use/` by default; set `COMPUTER_USE_HOME` to move it. It is an ordinary directory you can read, copy, or delete. The tool does not encrypt it and does not upload it anywhere.

- `config.json` holds every setting, including `vlm.api_key`, which is stored **in plain text**. Treat that file as the secret.
- `sessions/` holds one directory per session: the screenshots, `session.json`, `ops.md`, and the parsed markdown.
- `logs/daemon.log` is the daemon's own log. When an error code is not enough, read it: it records what the daemon was doing, where `ops.md` records only your commands. `daemon_log_level` filters it and `daemon_log_limit_bytes` caps it, trimming the oldest part and keeping the newest.
- `venv/` is the Python environment the launcher builds.

Your screenshots stay on this machine as files, and they are not a transient buffer. A session directory holds them until cleanup removes them, and all sessions share `storage_limit_bytes` (1 GiB by default). Cleanup runs when a session ends: it evicts oldest first, never touches an active session, deletes images and their parsed markdown before it deletes an operation log, and only removes whole session directories if dropping every image was still not enough. If you need a screenshot gone sooner, delete the file yourself.

`session list` shows each session's status: `active` is the one you are using, `ended` is closed normally, and `orphaned` means the daemon restarted and no longer tracks it. An orphan's files stay on disk. Do not build on one: start a fresh session.

## First run

The launcher keeps a Python environment for the tool itself, because the tool needs no dependencies from your project. On the first call, or after an upgrade changes the project metadata, it builds that environment with `uv`, which can take a minute. It prints progress on stderr, and it needs `uv` on `PATH`; if `uv` is missing it prints install instructions and stops rather than falling back to a system Python.

A later call is a plain process start. You do not manage the daemon: it starts on demand and exits after `daemon_idle_exit_seconds` idle, unless the overlay or the write lock is present.

```
computer-use env sync           # rebuild the environment on demand
computer-use --version
computer-use daemon status
computer-use lock status
```

`lock status` is worth reading before you conclude that a write is hanging. It reports who holds the lock and for how long, which separates "the tool is stuck" from "another session is working".

## Installing this skill into an agent

The package can copy this file into the skill directory of the calling agent:

```
computer-use skill install       # writes to ~/.claude/skills/computer-use/SKILL.md
computer-use skill uninstall
```

Set `COMPUTER_USE_SKILL_DIR` to install somewhere else. `computer-use --launcher-help` lists the launcher's own commands, which are the only ones the launcher handles itself; everything else goes to the Python client.

## Checking that it works

```
computer-use windows
```

If this lists windows, the tool is reachable and the desktop layer is working. If it fails, the error code says whether the daemon is down, the session is missing, or the desktop refused.

A useful check before a long run: screenshot the target, read `origin`, and click a harmless point using the origin arithmetic. If the click lands where you aimed, your coordinate handling is right, and everything after it will be too.

<!-- Command reference: kept in sync with `computer-use --help` and each subcommand's own help. -->

## Command reference

Every command accepts the global options below. Subcommand options may be written before or after the subcommand, and nested subcommands are no exception (`daemon status --json` and `session list --json` are both fine).

### Global options

| Option | Meaning |
|---|---|
| `--session <id>` | Session id. Also read from `COMPUTER_USE_SESSION`. Required by `screenshot`, `parse`, `session end`, and every write command. |
| `--describe "<text>"` | What this operation does and why. **Required by every write command.** |
| `--json` | Machine-readable output: `{"ok": true, "result": ...}` or `{"ok": false, "error": {...}}`, both on stdout. |
| `--inline` | Return screenshots and structured data as base64 in the response instead of a path. Off by default to protect your context. |
| `--verbose` | Add lower-level detail: which capture layer served the request, timing breakdowns, `class` and `zorder` on windows. |
| `--continue` | Another command is coming: keep the overlay and input blocking, without re-running the arm delay. |
| `--end` | That was the last command: begin the exit immediately. |
| `--version` | Print the version and exit. |

### Session and lifecycle

| Command | Arguments | Result |
|---|---|---|
| `begin` | `--agent-hint "<text>"` | Creates the session; prints its id and directory. Takes no write lock. |
| `session list` | | Sessions, newest first, with status, creation time, and counts of screenshots, parsed files, and operations. |
| `session info` | `--session <id>` | That session's metadata. |
| `session end` | `--session <id>` | Releases the write lock, decrements the parser reference count, and runs quota cleanup. Reports freed bytes, deleted files, and deleted sessions. |

### Read commands

| Command | Arguments | Result |
|---|---|---|
| `windows` | `--all` (also list windows that are invisible or untitled), `--verbose` (adds `class`, `is_topmost`, `zorder`) | Windows in z-order, front to back. Fields: `hwnd`, `title`, `pid`, `process`, `rect` as `x,y,w,h`, `monitor`, `is_foreground`, `is_minimized`, `elevated`. The human-readable line omits `monitor`. |
| `screenshot` | One of `--hwnd <h>` or `--full`, plus `--monitor <n>` with `--full` (0-based, defaults to the primary display) and `--format png\|webp` | Image path, dimensions, `origin`, and the capture `layer` that served it. |
| `parse` | One of `--hwnd <h>` or `--image <path>`, plus `--ai` | Markdown path, element count, and the model name when `--ai` ran. |

### Write commands

All of these take `--describe`, and all of them take `--hwnd` except `scroll`.

| Command | Positional | Options |
|---|---|---|
| `click <x> <y>` | Screen coordinates | `--button left\|right\|middle` (default `left`), `--count <n>` (default 1, and 2 is a double-click) |
| `move <x> <y>` | Screen coordinates | Hover only, no click. Does not change the foreground. |
| `drag <x1> <y1> <x2> <y2>` | Start and end screen coordinates | `--button left\|right\|middle` |
| `scroll <dx> <dy>` | Scroll amount; positive `dy` is up | `--at <x> <y>` (default: the current cursor position) |
| `type "<text>"` | The text | Whitespace and newlines are supported; a newline is sent as Enter. |
| `key "<combo>"` | A combination such as `ctrl+c`, `alt+tab`, `win+r`, `shift+f10`, `esc` | `--force` bypasses the dangerous-key blocklist |

A write returns timing (`moved_ms`, `total_ms`) and may carry a `warning`. When you see one, re-check the target before continuing: it means the tool is telling you the situation may have changed under you.

When `type` cannot use the Unicode path it falls back to the clipboard, and the result says so in `detail`, with `fallback: clipboard` and `clipboard_restored`. Two things to know about that path. The restore can fail, which leaves your clipboard holding the text the tool typed. And the fallback re-sends the whole string, so characters entered before the failure may appear twice. Verify the field before you continue.

### Local commands

| Command | Arguments | Result |
|---|---|---|
| `lock status` | | The lock holder, how long it has been held, and whether anyone is waiting. |
| `lock unlock` | `--force` and `--reason "<text>"`, both required | Takes the lock away from its holder. Always written to the daemon log, and to the holder's `ops.md` when it has a session. |
| `config show` | | Every setting as JSON. `vlm.api_key` is masked in the output, in both the text and the `--json` form. |
| `config set <key> <value>` | | Writes one setting and persists it immediately. Only the keys `config show` lists are accepted. |
| `daemon status` | | PID, start time, pipe name, active sessions, parser reference count, idle time, resident memory, protocol version, and the overlay and input-blocking state. |
| `daemon stop` | | Stops the daemon. It restarts on demand. |
| `setup omni` | `--force` (reinstall the dependencies, to repair a broken environment), `--skip-weights` (build the environment without the weights) | Builds the parser environment and downloads its weights (about 1.4 GB). Needed only for `parse`. |
| `env sync` | | Launcher command. Rebuilds the Python environment. |
| `skill install` / `skill uninstall` | | Launcher commands. Copies this file into the calling agent's skill directory, or removes it. |

### Error codes

| `error_code` | What it means for you |
|---|---|
| `invalid_params` | Fix the command. `computer-use --help` lists every parameter. |
| `protocol_version_mismatch` | Client and daemon disagree on the protocol. The client restarts the daemon and retries once; if it still fails, `daemon stop` and retry. |
| `session_not_found` | The session is gone. Run `begin`. |
| `session_already_ended` | That session is over. Start a new one for the current task. |
| `describe_required` | Add `--describe "what and why"`. |
| `lock_timeout` | Another session holds the write lock; the detail names it and how long it has held. Wait, or take it with `lock unlock --force`. |
| `window_not_found` | The window is gone. Enumerate again with `windows`. |
| `window_stale` | The handle was recycled to a different process. Enumerate again; do not reuse the handle. |
| `window_minimized` | Ask the user to restore it, or pick another window. |
| `elevated_window` | The window belongs to an elevated process and the OS blocks input to it. Ask the user to do this step by hand. |
| `foreground_failed` | The window could not be brought to the front. Retry once; if it still fails, that window cannot be activated right now. |
| `capture_failed` | Every capture layer failed. Check that the window still exists, and ask the user to leave exclusive fullscreen. |
| `capture_black` | The frame came back black. Retry; the surface may be protected or still rendering. |
| `omni_not_installed` | The element detector is absent. Run `computer-use setup omni`, or read the image yourself. |
| `omni_failed` | The detector process failed. Retry once; if it keeps failing, read the daemon log. |
| `vlm_failed` | The multimodal endpoint failed. Check `vlm.base_url` and `vlm.api_key`; the plain structured data is still there. |
| `dangerous_key_blocked` | The combination is on the blocklist. Add `--force` only if you truly mean it, and tell the user first. |
| `aborted_by_user` | The user pressed physical Esc. Stop and confirm with the user. |
| `internal_error` | Report it. The detail names the daemon log. |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | Unexpected internal error. |
| `2` | Argument error. Fix the command. |
| `3` | Session or lock problem. Read the `error_code` to decide between a new session and waiting. |
| `4` | Target problem: the window is missing, recycled, minimized, or elevated. Enumerate again or give up on that target. |
| `5` | Capture or parse failure. Reads may be retried; writes must not be. |
| `6` | Aborted by the user. Stop and confirm. |

### Configuration keys

`config show` prints these with their live values, and `config set` writes them by the same dotted names.

| Key | Default | Meaning |
|---|---|---|
| `data_dir` | empty | Empty means `~/.computer-use`. |
| `storage_limit_bytes` | 1 GiB | Total size cap for `sessions/`, enforced at `session end`, oldest first. |
| `image_format` | `png` | Default screenshot format; `--format` overrides it per call. |
| `lock_wait_seconds` | 10 | How long a write waits for the write lock before `lock_timeout`. |
| `overlay_arm_ms` | 500 | Delay before a write sequence starts, giving the person time to take their hands off the keyboard. Input is already blocked during it. |
| `overlay_hold_seconds` | 30 | Fallback hold when you send neither `--continue` nor `--end`. |
| `overlay_exit_hold_ms` | 500 | Input stays blocked this long after the overlay leaves, so a physical keystroke in that instant is not swallowed. |
| `daemon_idle_exit_seconds` | 600 | The daemon exits after this much idle time. It never exits while the overlay or the write lock is present. |
| `daemon_log_level` | `info` | `info`, `warning`, or `error`. Raise it to quiet a noisy log. |
| `daemon_log_limit_bytes` | 500 MiB | Log cap; the oldest part is trimmed and the newest kept. |
| `danger_keys` | `win+l`, `ctrl+alt+del` | Combinations refused unless a write carries `--force`. |
| `mouse_step_ms` | 10 | Pacing of the synthetic cursor movement. |
| `mouse_max_points` | 30 | Maximum number of points in that movement. |
| `vlm.base_url`, `vlm.api_key`, `vlm.model_name`, `vlm.user_agent` | empty | An OpenAI-compatible endpoint for `parse --ai`. The key is masked whenever it is printed, and stored in plain text in `config.json`. |
| `omni.env_path`, `omni.weights_dir`, `omni.mirror` | empty | Parser environment, weights, and HuggingFace mirror. Empty means derived from `data_dir`. |
