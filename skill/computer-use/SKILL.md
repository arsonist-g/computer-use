---
name: computer-use
description: Any task that must operate a Windows desktop programmatically MUST use this skill: capture a screen or a window, list open windows, click or move the mouse, type text, send a key combination, drag, scroll, or turn a screenshot into structured elements. It is the command-line surface of a resident daemon that reads the screen and synthesizes real input, with every write gated by a natural-language description.
---

# Computer-Use

`computer-use` drives a Windows desktop from a shell. Every command is its own process talking to a resident daemon that owns the screen, the input queue, and the session record; the tool turns your coordinates and your text into input that really lands. The first call after an install builds the tool's own environment and can take a minute (`references/install-and-config.md`).

## Output boundary

This skill teaches you to call the `computer-use` CLI correctly. It covers this CLI only, and it decides nothing for you:

- Interpreting the screen and choosing the next action are your job. The tool has no notion of a task or a correct answer.
- Nothing is undoable: a click that already landed cannot be taken back.
- A write reports that input was dispatched, not that the application responded.
- Elevated windows are out of reach, because the OS blocks input to them (UIPI). Consent is not the tool's to give: the person at the machine can abort you at any moment, and you must stop.

## How to read this skill

Sentences with "must", "never", or "the rule is" are hard requirements; everything else is guidance you can adapt. Defaults (timeouts, intervals, image format) are yours to change, and `config show` prints the live values; who may do something is not.

Code blocks run as written once you replace the session ids (`s-...`) and the numbers. Command shapes and argument names are the contract; example values are illustrations.

Tables are reference. Two files under `references/` hold what you need only occasionally: `errors.md` (every `error_code` and the action it calls for) and `install-and-config.md` (first-run setup, installing this skill into an agent, the local commands, the configuration keys).

## Sessions: one session per task

A session owns the write lock, the on-disk record, and the parser's reference count. Start one before your first command that touches the screen:

```
computer-use begin --agent-hint claude-code
session s-20260913-183849-cgyg
```

Pass that id with `--session`, or export `COMPUTER_USE_SESSION` once and drop the flag. `--agent-hint` is free text kept in the session metadata, so `session list` stays readable a week later.

`session end` releases the write lock and runs storage cleanup, so run it when the task is done. `session list` marks each session `active`, `ended`, or `orphaned` (the daemon restarted and no longer tracks it). An orphan's files stay on disk: start a fresh session rather than building on it. All sessions contend for a single global write lock, so a loser waits `lock_wait_seconds` (10 by default) and then fails with `lock_timeout`.

The session directory holds `session.json`, `ops.md` (the append-only command log), the screenshots, and the parsed markdown. After a timeout `ops.md` is the only account that counts, so read it to learn whether an action executed. Screenshots stay on disk as files until cleanup removes them, and all sessions share `storage_limit_bytes` (1 GiB by default).

## The loop

```
computer-use begin --agent-hint claude-code
computer-use windows                                        # pick a target: note hwnd and elevated
computer-use screenshot --hwnd 0x1A2B --session s-...        # read origin out of the result
computer-use click 850 420 --hwnd 0x1A2B --session s-... --describe "open the font size field"
computer-use session end --session s-...
```

Look, act, look again: screenshot the target, decide the image coordinates of the thing you want to hit, add the origin, send one write, then screenshot again rather than assuming what the write opened. A first sanity run is `computer-use windows`: if it lists windows, the desktop layer works. When the coordinate arithmetic is new to you, validate it on a harmless point before a long run.

## Command usage

```
computer-use <command> [required arguments] [flags]
```

- Required arguments are positional: `key ctrl+s` passes the combination with no flag name. A flag starts with `--` and either takes a value (`--session s-...`) or stands alone (`--json`). A write that names a window, a position, and a reason reads like `computer-use key ctrl+s --hwnd 0x1A2B --session s-... --describe "save the file"`.
- Flags may be written before or after the command, nested commands included.
- **Do not guess a flag.** `computer-use --help` lists the commands, and `computer-use <command> --help` prints that command's arguments and the values each one accepts.

### Shared arguments

Every command accepts every argument below. The `Where` column says where each one is required, or where it does something.

| Argument | Where | Meaning |
|---|---|---|
| `--session <id>` | required by `screenshot`, `parse`, `session end`, and all six writes | Session id, also read from `COMPUTER_USE_SESSION`. |
| `--describe "<text>"` | required by all six writes | What this operation does and why. |
| `--hwnd <h>` | `screenshot`, `parse`, and every write except `scroll` | Target window: checked against your session's most recent screenshot of it (`window_stale` if the system recycled the handle), and brought to the foreground for `click`, `drag`, and `type`. |
| `--json` | everywhere | Machine-readable output on stdout. |
| `--inline` | `screenshot`, `parse` | Return the image or data as base64 instead of a path. |
| `--verbose` | everywhere | Lower-level detail, such as which capture layer served the request. |
| `--continue` | writes | Another command is coming: keep the overlay and input blocking. |
| `--end` | writes | That was the last command: begin the exit now. |
| `--version` | everywhere | Print the version and exit. |

## Commands

The groups below are the second level; this is the first.

| Group | Commands |
|---|---|
| Sessions and lifecycle | `begin`, `session list`, `session info`, `session end` |
| Reads | `windows`, `screenshot`, `parse` |
| Writes | `click`, `move`, `drag`, `scroll`, `type`, `key` |
| The write lock | `lock status`, `lock unlock` |

A row lists the parameters that belong to that command, and the shared ones are above it. Parameters are in the order you write them: positionals first, then flags.

### Sessions and lifecycle

| Command | Description | Parameters | Notes |
|---|---|---|---|
| `begin` | Creates the session | `--agent-hint` | Prints the session id and directory. Takes no write lock. |
| `session list` | Lists sessions, newest first | | Status, creation time, and per-session counts. |
| `session info` | Shows one session's metadata | | |
| `session end` | Ends the session: releases the write lock and runs quota cleanup | | Reports freed bytes and deleted files. |

### Reads

| Command | Description | Parameters | Notes |
|---|---|---|---|
| `windows` | Lists top-level windows, front to back | `--all` | `--all`: include invisible and untitled windows. `--verbose` adds `class`, `is_topmost`, `zorder`. Fields: `hwnd`, `title`, `pid`, `process`, `rect` (`x,y,w,h`), `monitor`, `is_foreground`, `is_minimized`, `elevated`. |
| `screenshot` | Captures a window or a display | `--hwnd \| --full` `--monitor` `--format` | `--monitor` is 0-based, defaults to the primary display, and needs `--full`; `--format` is `png` or `webp`. Returns the image path, its dimensions, its `origin`, and the capture `layer`. |
| `parse` | Detects elements in an image and writes markdown beside it | `--hwnd \| --image` `--ai` | The markdown holds `type`, `bbox`, `interactivity`, and `content` per element. Returns the path and a count; without the detector, `omni_not_installed`. |

### Writes

A write returns timing (`moved_ms`, `total_ms`) and may carry a `warning`: re-check the target when you see one. Which shared arguments the writes take is in the table above.

| Command | Description | Parameters | Notes |
|---|---|---|---|
| `click` | Clicks at a point | `x* y*` `--button` `--count` `--hwnd` | `--button` is `left` (default), `right`, or `middle`; `--count 2` is a double-click. |
| `move` | Moves the cursor | `x* y*` `--hwnd` | Hover only, no click. `--hwnd` does not change the foreground here. |
| `drag` | Presses at one point, moves, and releases at another | `x1* y1* x2* y2*` `--button` `--hwnd` | `--button` as for `click`. |
| `scroll` | Scrolls | `dx* dy*` `--at` | Positive `dy` scrolls up. `--at` is the point to scroll at, defaulting to the current cursor position. |
| `type` | Types text at the cursor | `text*` `--hwnd` | Unicode input path, so Chinese needs no clipboard. A newline is sent as Enter. If it falls back to the clipboard the result says so (`fallback: clipboard`, `clipboard_restored`), the restore can fail, and the whole string can be sent twice: verify the field. |
| `key` | Sends a key combination | `combo*` `--hwnd` `--force` | A blocked combination fails with `dangerous_key_blocked`. `--force` bypasses the blocklist (`win+l`, `ctrl+alt+del`, and anything in `danger_keys`). |

### The write lock

| Command | Description | Parameters | Notes |
|---|---|---|---|
| `lock status` | Shows who holds the write lock | | The holder, how long it has been held, and whether anyone is waiting. |
| `lock unlock` | Takes the lock away from its holder | `--force` `--reason` | Both are required. Always written to the log, and to the holder's `ops.md` when it has a session. |

`config`, `daemon`, `setup omni`, `env sync`, `skill install`, and the configuration keys are in `references/install-and-config.md`.

## Reading the screen

- Read `elevated` before you plan a click: the OS blocks input to an elevated window, so route around it or ask the user to do that step.
- Prefer the returned image path over `--inline`, because a full-screen PNG is megabytes, and trust the returned `origin`, `width`, and `height`: when the first capture layer fails the fallback stores the whole monitor, so a window screenshot can be larger than the window.
- A `parse` `bbox` is in image pixels, so the same origin arithmetic applies. Read the markdown only when you need the elements.

## Screen coordinates

Coordinates are physical pixels, origin at the top left of the primary display, y increasing downward, and the tool is per-monitor DPI aware before it reads any coordinate. Never apply a scale factor yourself.

A screenshot's `origin` is the screen position of the image's pixel `(0, 0)`: the window's top-left corner for a window screenshot, `(0, 0)` for a full-screen one. To hit what you saw in an image:

```
screen_x = origin_x + image_x
screen_y = origin_y + image_y
```

The rule is: never pass an image coordinate to a write command without adding the origin first. The window origin comes from the compositor's extended frame, so there is no offset for you to compensate for.

## Sending input

- Every write must carry `--describe`, or it fails with `describe_required` before anything reaches the desktop. Those coordinates are absolute screen coordinates, and `--hwnd` does not change their meaning; without `--hwnd` there is no preflight and no foreground change either, so input goes to whatever is focused.
- **Never retry a failed write.** A write never retries and reports a timeout as a failure; the click may already have landed, so read `ops.md` and then decide. Sending it again clicks twice.
- `--continue` and `--end` tell the tool whether another command follows. Pass `--continue` on every write except the last one: it keeps the overlay and input blocking, and waits up to `overlay_continue_seconds` (120 by default) for your next command. A write with no flag ends the sequence when it finishes: the overlay leaves and input comes back after the half-second exit hold. While input is in flight the overlay marks the screen and blocks the physical keyboard and mouse, so a person can interrupt: the physical Esc key aborts the operation, and your own `key esc` is unaffected. On `aborted_by_user`, stop and confirm with the user; the abort refuses one write and then clears itself, so call again once they say you may continue.
- Refusing a dangerous key is deliberate: it locks the session, and being locked out is worse than being refused. Use `--force` only when locking is genuinely what you want, and tell the user first.

## Red lines

- Never retry a failed write.
- Never pass an image coordinate that has not had `origin` added to it.
- Never continue after `aborted_by_user`.
- Never work around the overlay or the input blocking.

## Errors, exit codes, and `--json`

Every failure carries an `error_code` from a closed set, plus a message and a hint naming the next action; branch on the code, not on the prose. The ones you will meet most:

| `error_code` | What to do |
|---|---|
| `invalid_params` | Fix the command; `computer-use --help` lists every argument. |
| `session_not_found`, `session_already_ended` | Run `begin`. |
| `window_not_found`, `window_stale` | Enumerate again with `windows`, and do not reuse the handle. |
| `elevated_window` | Ask the user to do that step by hand. |
| `lock_timeout` | Wait, or read `lock status` to see who holds it and take it with `lock unlock`. |
| `aborted_by_user` | Stop and confirm with the user. |
| `omni_not_installed` | Run `computer-use setup omni`, or read the image yourself. |
| `internal_error` | Tell the user, and do not re-send the command. |

`references/errors.md` has the whole closed set. Exit codes group them for a shell: `2` arguments, `3` session or lock, `4` target, `5` capture or parse, `6` aborted, `1` anything else; prefer `error_code`.

`--json` gives a machine-readable envelope on stdout: `{"ok": true, "result": ...}` or `{"ok": false, "error": {"code", "message", "hint", "detail"}}`. Without it, human-readable errors go to stderr, and a few strings the tool prints are Chinese, so parse `--json` when you need stable field values.