# Computer-Use

[![CI](https://github.com/arsonist-g/computer-use/actions/workflows/ci.yml/badge.svg)](https://github.com/arsonist-g/computer-use/actions/workflows/ci.yml)

Windows desktop control for AI agents: screens, windows, mouse, keyboard, and structured UI parsing, as a command-line surface an agent can call.

Each command is its own process talking to a resident daemon that owns the screen, the input queue, and the session record. The tool turns your coordinates and your text into input that really lands, and every write must carry a natural-language description of what it does.

Nothing here is undoable. A click that has already landed cannot be taken back, and a write reports that input was dispatched, not that the application responded.

## Requirements

- Windows 10 or 11
- Node.js 22 or newer
- [`uv`](https://docs.astral.sh/uv/) on `PATH` - the first run uses it to build the tool's own Python environment

## Install

```
npm install -g @arsonist-g/computer-use
```

The first call builds a private Python environment under `~/.computer-use/` (it prints progress on stderr and can take a minute). Nothing is installed into your project, and no system Python is touched.

## Quick start

```
computer-use --version
computer-use windows                    # if this lists windows, the desktop layer works
computer-use begin --agent-hint my-agent
computer-use screenshot --hwnd 0x1A2B --session s-...
computer-use click 850 420 --hwnd 0x1A2B --session s-... --describe "open the font size field"
computer-use session end --session s-...
```

Look, act, look again: screenshot the target, read the image, add `origin`, send one write, then screenshot again rather than assuming what the write opened.

## Coordinates

Everything is physical pixels with the origin at the top left of the primary display and y increasing downward. A screenshot's `origin` is the screen position of the image's pixel `(0, 0)`:

```
screen_x = origin_x + image_x
screen_y = origin_y + image_y
```

Never pass an image coordinate to a write command without adding `origin` first. `--hwnd` does not change the meaning of coordinates.

## Commands

| Group | Commands |
|---|---|
| Sessions | `begin`, `session list`, `session info`, `session end` |
| Reads | `windows`, `screenshot`, `parse` |
| Writes | `click`, `move`, `drag`, `scroll`, `type`, `key` |
| Write lock | `lock status`, `lock unlock` |

Arguments are positional and flags start with `--`. `computer-use --help` lists every command, and `computer-use <command> --help` prints that command's arguments and the values each one accepts, so nothing has to be guessed.

| Flag | Applies to | Meaning |
|---|---|---|
| `--session <id>` | `screenshot`, `parse`, `session end`, every write | Session id; also read from `COMPUTER_USE_SESSION`. |
| `--describe "<text>"` | every write | What the operation does and why. A write without it fails before anything reaches the desktop. |
| `--hwnd <h>` | `screenshot`, `parse`, every write but `scroll` | Target window, brought to the foreground for `click`, `drag`, `type`, and `key`. |
| `--json` | everywhere | Machine-readable envelope on stdout. |
| `--continue` / `--end` | writes | Another command follows, or this was the last one. |
| `--verbose` | everywhere | Lower-level detail, such as which capture layer served the request. |

`type` inserts text and nothing else: a newline in the text lands as a literal line break through the clipboard, never as an Enter keypress. Press Enter with `key enter`.

## Sessions and the write lock

A session owns the write lock, the on-disk record, and the parser reference count. Desktop input is serialized by a single global write lock, so only one session writes at a time; a loser waits `lock_wait_seconds` and then fails with `lock_timeout`. `lock status` shows who holds it, and `lock unlock --force --reason "..."` takes it away.

While input is in flight the overlay marks the screen and blocks the physical keyboard and mouse. The physical Esc key aborts the operation and refuses one write; calling again afterwards succeeds. Read `ops.md` in the session directory, rather than your own memory, to learn whether an action actually executed.

`session end` releases the lock and runs storage cleanup.

## Safety

- Input to elevated windows is blocked by the OS. Ask the person at the machine to do that step.
- Dangerous combinations (`win+l`, `ctrl+alt+del`) are refused unless the write carries `--force`.
- The physical keyboard and mouse are blocked only while the overlay is up, and the physical Esc key always gets through.
- The person at the machine is in charge: after `aborted_by_user`, stop and confirm before continuing.

## Structured UI parsing (optional)

`screenshot` and every input command work with no extra dependency. `parse` additionally turns an image into elements carrying `type`, `bbox`, `interactivity`, and `content`, and needs the parser environment and its weights:

```
computer-use setup omni
computer-use parse --hwnd 0x1A2B --session s-...
```

`setup omni` downloads about 1.4 GB of weights. Without it, `parse` reports `omni_not_installed`.

## Agent skill

The package carries the agent-facing skill and can copy it into the skill directory of every agent family present on this machine:

```
computer-use skill install       # writes SKILL.md and references/ to ~/.claude, ~/.codex, ~/.agents
computer-use skill uninstall
```

An agent family is targeted only when its root directory already exists, so nothing is created for an agent that is not installed; when no family is found, the install falls back to the Claude Code path. Set `COMPUTER_USE_SKILL_DIR` to install into exactly one directory instead.

## Configuration

```
computer-use config show
computer-use config set overlay_continue_seconds 60
```

Settings live in `~/.computer-use/config.json`, and `config show` prints every key with its live value. `skill/computer-use/references/` holds the full command reference and the closed set of error codes.

## Development

```
cd core
uv venv
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check src tests
```

CI runs the unit suite on `windows-latest`. The machine-dependent checks are in `core/tests/native/`, and the manual acceptance list is `core/tests/acceptance.md`.

## License

MIT
