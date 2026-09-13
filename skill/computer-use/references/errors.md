# Error codes

Every failure carries an `error_code` from a closed set, plus a message and a hint naming the next action. Read the code, not the prose: the tables below say what each one means for you and what to do about it.

## Error codes

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
| `omni_failed` | The detector process failed. Retry once; if it keeps failing, read the image yourself instead. |
| `vlm_failed` | The multimodal endpoint failed. Check `vlm.base_url` and `vlm.api_key`; the plain structured data is still there. |
| `dangerous_key_blocked` | The combination is on the blocklist. Add `--force` only if you truly mean it, and tell the user first. |
| `aborted_by_user` | The user pressed physical Esc. Stop and confirm with the user. |
| `internal_error` | The tool itself failed, and your command was not the cause. Tell the user; do not retry the same command. |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | Unexpected internal error. |
| `2` | Argument error. Fix the command. |
| `3` | Session or lock problem. Read the `error_code` to decide between a new session and waiting. |
| `4` | Target problem: the window is missing, recycled, minimized, or elevated. Enumerate again or give up on that target. |
| `5` | Capture or parse failure. Reads may be retried; writes must not be. |
| `6` | Aborted by the user. Stop and confirm. |
