---
name: computer-use
description: Any task that operates a Windows desktop programmatically MUST use this skill: take a screenshot of a screen or window, list open windows, click or move the mouse, type text, send key combos, drag, scroll, or parse a UI screenshot into structured elements. It is the command-line surface of a resident daemon that reads the screen and synthesizes real input, with every write operation gated by a required natural-language description.
---

# Computer-Use

You drive a Windows desktop through a command line. Every command is one process that talks to a resident daemon over a local pipe; the daemon owns the screen, the input queue, and the session record.

## Output boundary

This skill tells you how to call the tool correctly. It does not produce the automation itself, and it does not decide what to do on screen. Reading a screenshot and choosing the next action is your job, not the tool's.

The tool does not:

- Recognize what is in a screenshot. It saves pixels and tells you where they came from. Interpreting them is yours.
- Undo anything. There is no undo stack on Windows. A click that already landed cannot be reverted by the tool.
- Guarantee that a click reached its target. It reports whether the input was dispatched, not whether the application responded.
- Act on elevated windows. Input to them is blocked by the OS, and the tool reports that explicitly instead of failing silently.

## How to read this skill

Sentences marked "must", "never", or "the rule is" are hard requirements. Their violation causes wrong coordinates, unrecoverable input, or a lockout. Everything else is guidance you can adapt to the situation.

Two worked examples appear below with real values. Treat their numbers as illustrations, not constants. The command shapes and the argument names are the contract; the values are not.

Where this skill states a default (a timeout, an interval, a step count), you may override it. Where it states a rule about who may do something, you may not.

## The one thing to get right first: coordinates

Screen coordinates are physical pixels, origin at the top left of the primary display, y increasing downward. The tool declares per-monitor DPI awareness before it reads any coordinate, so what it reports is already correct physical pixels. Do not apply a scaling factor yourself.

A screenshot returns an `origin` alongside the file path. That `origin` is the screen position of the image's pixel `(0, 0)`:

- A window screenshot returns that window's top-left corner, which is usually not `(0, 0)`.
- A full-screen screenshot returns `(0, 0)`.

To click something you saw in an image, use:

```
screen_x = origin_x + image_x
screen_y = origin_y + image_y
```

The rule is: never pass an image coordinate to `click` without adding the origin. This is the single most common way to make the tool look broken while it is working exactly as told.

A window screenshot includes the title bar, and its `(0, 0)` lines up exactly with the window's outer rectangle. There is no offset to compensate for.

## The working loop

1. Start a session.
2. Enumerate windows to find your target.
3. Screenshot the target and read `origin`.
4. Decide the image coordinates of the thing you want.
5. Add the origin and send the write command, with a description.

Sessions are the unit that owns the write lock and the on-disk record. Start one before your first write.

```
computer-use begin --agent-hint claude-code
```

It prints a session id and a directory. Pass that id to later commands with `--session`, or export `COMPUTER_USE_SESSION` once and omit the flag.

The session directory holds `ops.md`, an append-only log of every command with its description and outcome. When you are unsure whether an action already happened, read that file. It records what was actually executed, which is the only account that matters after a timeout.

## Read commands

These take no lock and never block another caller.

```
computer-use windows
computer-use windows --all --verbose
```

Lists top-level windows in z-order, front to back. The default hides windows without a title. Each row carries `hwnd`, `title`, `pid`, `process`, `rect`, `monitor`, `is_foreground`, `is_minimized`, and `elevated`.

Read `elevated` before you plan a click. Input to an elevated window is blocked by the OS, so a window marked `elevated=1` is not a target you can hit; route around it.

```
computer-use screenshot --hwnd 0x0001A2B --session s-...
computer-use screenshot --full
computer-use screenshot --full --monitor 0
```

Prints the file name, dimensions, and `origin`. Add `--inline` to get base64 in the response instead of a path. Prefer the path: a full-screen PNG is megabytes, and pasting it into your context costs far more than reading the file when you need it.

`--full` captures the primary display unless you name another with `--monitor`.

```
computer-use parse --hwnd 0x0001A2B --session s-...
computer-use parse --image C:\path\to\shot.png --session s-...
```

Runs an element detector over an image and writes a markdown file, returning its path and an element count. It does not return the elements themselves, on purpose: a busy screen produces hundreds of them, and you should read the file only if you need it.

`--ai` additionally asks a configured vision model to correct the descriptions. It costs a network round trip and only helps when element captions are wrong. Plain `parse` is the default for a reason.

If the detector is not installed, `parse` fails with `omni_not_installed` and a setup hint. Everything else keeps working, because the detector is an optional component.

### The vision endpoint behind `--ai`

`--ai` is the only feature that needs a multimodal endpoint, and nothing is configured by default. Point it at one with the config commands:

```
computer-use config show
computer-use config set vlm.base_url https://api.example.com/v1
computer-use config set vlm.api_key sk-YOUR-KEY
computer-use config set vlm.model_name gpt-4o-mini
computer-use config show --json
```

`config show` prints every setting except `vlm.api_key`, which is masked (`sk-a2b1…fbd2`) in both text and `--json` output so the key never reaches an agent's context. `config set <key> <value>` writes one key and persists it to `config.json`, so the next `parse --ai` picks it up without a restart. The three keys describe an OpenAI-compatible endpoint: `vlm.base_url` is its root (up to and including the `/v1`), `vlm.api_key` is the bearer token, and `vlm.model_name` is the model id to call. `config set` accepts only the keys that `config show` lists; anything else fails with `invalid_params` and names the allowed set.

The api key rests in `config.json` in plain text, so treat that file as a secret and keep the value out of logs and records. Plain `parse` never touches this endpoint.

## Write commands

Every write command must carry `--describe "<what and why>"`. Omitting it fails with `describe_required` before anything touches the desktop. The description is not decoration: it is what makes the record replayable. A log of coordinates alone cannot tell a reader what the operator was trying to do.

```
computer-use click 850 420 --session s-... --describe "open the font size field"
computer-use click 850 420 --count 2 --session s-... --describe "double-click to select the word"
computer-use move 400 300 --session s-... --describe "hover the toolbar to reveal labels"
computer-use drag 100 200 700 600 --session s-... --describe "resize the panel by its corner"
computer-use scroll 0 -3 --at 850 420 --session s-... --describe "scroll down in the document"
computer-use type "hello world" --session s-... --describe "fill the search box"
computer-use key ctrl+s --session s-... --describe "save the file"
```

Coordinates are absolute screen coordinates. `--hwnd` is optional and does not change what the numbers mean; it asks the tool to verify the target first and raise that window before sending input. Supply it when you have a specific window in mind, because it is what catches a recycled handle or a minimized window before the click lands somewhere unintended.

For `scroll`, a positive `dy` scrolls up. That is the opposite of what many APIs do, so check the sign when direction matters.

### Tell the tool when you are continuing

Between two write commands the tool keeps the screen marked and the physical keyboard and mouse blocked, so the person watching knows you are still working. It decides how long to keep that up by watching the clock, and the clock is a guess: it does not know whether your next command is coming in a second or in thirty.

You know. Say so:

```
computer-use click 400 300 --session s-... --continue --describe "focus the address bar"
computer-use type "https://example.com" --session s-... --end --describe "navigate"
```

`--continue` means another command is coming. `--end` means that was the last one. Neither is required, and if you omit both the tool waits a while and then stops on its own.

Use `--continue` when you are mid-task. Without it, a pause longer than the built-in window makes the tool block the keyboard and mouse again before your next command, which the person watching feels as their input being cut off repeatedly.

`type` handles non-ASCII text through the Unicode input path, so typing Chinese does not require a clipboard round trip.

When that Unicode path fails, `type` falls back to the clipboard: it reads your current clipboard, writes the text to the clipboard, sends `Ctrl+V`, then tries to put the old contents back. The restore can fail, and the result says so — `detail.clipboard_restored` is `false` when it does — and in that case your clipboard is left holding the text the tool typed. The write still reports `ok`; read the detail if the clipboard matters to you.

### Writes do not retry, and you must not either

A write command never retries, and a timeout is reported as a failure rather than silently repeated. The rule is: do not retry a failed write yourself. A click that timed out may already have landed. Sending it again clicks twice, and the second click lands on whatever the first one opened.

When a write fails, read `ops.md` to see whether it executed, then decide. The log is the account of record, not the command's return value.

### Some key combinations are blocked

`win+l`, `ctrl+alt+del`, and similar combinations are refused with `dangerous_key_blocked`. This is deliberate: they lock the session, and a tool that locks you out is worse than one that refuses. `--force` bypasses the check. Use it only when locking the session is genuinely what you want, and tell the user first.

## The user can stop you, and you must respect it

While the tool is sending input, it shows an overlay on screen and blocks the physical keyboard and mouse. This is not a bug and not something to work around; it exists so that a person watching can interrupt.

Pressing physical Esc aborts the current operation. Your own `key esc` command is not affected, because the tool distinguishes synthetic input from a real key press.

When you receive `aborted_by_user`, stop. Do not resume the task, and do not re-issue the command that was interrupted. Confirm with the user first.

The overlay appears for a moment before the first write of a run and stays while writes continue in quick succession. It leaves on its own when they stop.

## Errors

Every failure carries an `error_code` from a closed set, plus a human-readable message and a hint naming the next action. Branch on the code.

| `error_code` | What it means for you |
|---|---|
| `invalid_params` | Fix the command. |
| `describe_required` | Add `--describe`. |
| `session_not_found` | The session is gone. Start a new one. |
| `lock_timeout` | Another session holds the write lock. The detail names it and how long it has held. |
| `window_not_found` | Enumerate again. |
| `window_stale` | The handle was recycled to a different process. Enumerate again; do not reuse the handle. |
| `window_minimized` | Ask the user to restore it, or pick another window. |
| `elevated_window` | The tool cannot operate it. Ask the user to do this step by hand. |
| `capture_failed` | Every capture path failed. Check the window still exists. |
| `capture_black` | The frame came back black. Retry; if it persists, the surface may be protected. |
| `omni_not_installed` | The element detector is absent. Install it, or work from the image yourself. |
| `dangerous_key_blocked` | The combination is on the blocklist. |
| `aborted_by_user` | Stop and confirm with the user. |
| `internal_error` | Report it; the detail names the log. |

Exit codes group these for a shell: `2` arguments, `3` session or lock, `4` target, `5` capture or parse, `6` aborted. Prefer `error_code`; the exit code is the coarse version of the same fact.

Add `--json` to any command for a machine-readable envelope with `ok`, `result`, or `error` carrying `code`, `message`, `hint`, and `detail`.

## Sessions and cleanup

```
computer-use session list
computer-use session end --session s-...
```

Ending a session releases the write lock and runs storage cleanup. Cleanup evicts oldest first and never touches an active session. It removes images and their parsed output before it removes an operation log, because the log is the record of what happened and it is small.

The daemon starts on demand and exits when idle. You do not manage it. Two commands exist for diagnosis:

```
computer-use daemon status
computer-use lock status
```

`lock status` is worth reading before you conclude that a write is hanging. It reports who holds the lock and for how long, which separates "the tool is stuck" from "another session is working".

## Data on disk

Everything the tool keeps lives under `~/.computer-use/` (set `COMPUTER_USE_HOME` to move it). It is a normal directory you can read, copy, or delete — the tool does not encrypt it and does not upload it anywhere. This is what you will find there:

- `config.json` — every setting, including `vlm.api_key`, which is stored **in plain text**. If a key ends up here, treat the file as the secret.
- `sessions/` — one directory per session, named by session id. Inside it are the screenshot PNGs, `session.json` (session metadata), `ops.md` (the append-only command log), and the structured markdown that `parse` writes.
- `logs/daemon.log` — the daemon's own log (see below).
- `venv/` — the base Python environment the wrapper builds on first run.
- `venv-omni/`, `models/`, `OmniParser/` — the detector's separate environment, weights, and upstream source. These exist only after you run `computer-use setup omni`.

Your screenshots stay on this machine as files. They are not a transient buffer: a `sessions/` directory holds them until cleanup removes them, and sessions share a default quota of 1 GiB. The quota is enforced only when a session ends, and it evicts oldest first and never deletes an active session. If you need a screenshot gone sooner than that, delete it yourself.

### The daemon log

When the daemon hits something it cannot explain, it returns `internal_error` with `detail.log` naming the log path, and the full account goes to `~/.computer-use/logs/daemon.log`. That file is the place to look when an error code is not enough; unlike `ops.md`, which records only your commands, it records what the daemon itself was doing.

`daemon_log_level` sets how much is written. It takes `info`, `warning`, or `error`, and defaults to `info` (write everything). Raise it to `warning` or `error` to quiet a noisy log:

```
computer-use config set daemon_log_level warning
computer-use daemon status --json
```

The log has a 500 MB ceiling (`daemon_log_limit_bytes`). Past it, the oldest part is cut and the newest is kept, so a recent failure stays readable even on a machine that has been running for a while.

## Checking that it works

```
computer-use windows
```

If this lists windows, the tool is reachable and the desktop layer is working. If it fails, the error code tells you whether the daemon is down, the session is missing, or the desktop refused.

A useful sanity check before a long run: screenshot the target, read `origin`, and click a harmless point using the origin arithmetic. If the click lands where you aimed, your coordinate handling is right, and everything after it will be too.
