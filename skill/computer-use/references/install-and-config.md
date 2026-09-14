# Installing and configuring Computer-Use

This file covers what you do when you are setting the tool up rather than driving the desktop: building its environment on first run, installing this skill into an agent, the local commands, and the configuration keys.

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

The package can copy this skill into the skill directory of the calling agent:

```
computer-use skill install       # writes SKILL.md and references/ to ~/.claude/skills/computer-use/
computer-use skill uninstall
```

Set `COMPUTER_USE_SKILL_DIR` to install somewhere else. `computer-use --launcher-help` lists the launcher's own commands, which are the only ones the launcher handles itself; everything else goes to the Python client. Only the English files in force are installed; the `*-zh.md` proofreading translations stay in the package.

## Local commands

| Command | Arguments | Result |
|---|---|---|
| `config show` | | Every setting as JSON. `vlm.api_key` is masked in the output, in both the text and the `--json` form. |
| `config set <key> <value>` | | Writes one setting and persists it immediately. Only the keys `config show` lists are accepted. |
| `daemon status` | | PID, start time, pipe name, active sessions, parser reference count, idle time, resident memory, protocol version, and the overlay and input-blocking state. |
| `daemon stop` | | Stops the daemon. It restarts on demand. |
| `setup omni` | `--force` (reinstall the dependencies, to repair a broken environment), `--skip-weights` (build the environment without the weights) | Builds the parser environment and downloads its weights (about 1.4 GB). Needed only for `parse`. |
| `env sync` | | Launcher command. Rebuilds the Python environment. |
| `skill install` / `skill uninstall` | | Launcher commands. Copies this skill (SKILL.md and references/) into the calling agent's skill directory, or removes it. |

## Configuration keys

`config show` prints these with their live values, and `config set` writes them by the same dotted names.

| Key | Default | Meaning |
|---|---|---|
| `data_dir` | empty | Empty means `~/.computer-use`. |
| `storage_limit_bytes` | 1 GiB | Total size cap for `sessions/`, enforced at `session end`, oldest first. |
| `image_format` | `png` | Default screenshot format; `--format` overrides it per call. |
| `lock_wait_seconds` | 10 | How long a write waits for the write lock before `lock_timeout`. |
| `overlay_arm_ms` | 1500 | Delay before a write sequence starts, giving the person time to take their hands off the keyboard. Input is already blocked during it. |
| `overlay_continue_seconds` | 30 | How long `--continue` keeps the overlay and input blocking while it waits for your next command. A write with no flag ends the sequence when it finishes. |
| `overlay_exit_hold_ms` | 500 | Input stays blocked this long after the overlay leaves, so a physical keystroke in that instant is not swallowed. |
| `daemon_idle_exit_seconds` | 600 | The daemon exits after this much idle time. It never exits while the overlay or the write lock is present. |
| `daemon_log_level` | `info` | `info`, `warning`, or `error`. Raise it to quiet a noisy log. |
| `daemon_log_limit_bytes` | 500 MiB | Log cap; the oldest part is trimmed and the newest kept. |
| `danger_keys` | `win+l`, `ctrl+alt+del` | Combinations refused unless a write carries `--force`. |
| `mouse_step_ms` | 10 | Pacing of the synthetic cursor movement. |
| `mouse_max_points` | 30 | Maximum number of points in that movement. |
| `vlm.base_url`, `vlm.api_key`, `vlm.model_name`, `vlm.user_agent` | empty | An OpenAI-compatible endpoint for `parse --ai`. The key is masked whenever it is printed, and stored in plain text in `config.json`. |
| `omni.env_path`, `omni.weights_dir`, `omni.mirror` | empty | Parser environment, weights, and HuggingFace mirror. Empty means derived from `data_dir`. |
