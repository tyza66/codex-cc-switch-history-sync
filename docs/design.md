# Design

## Core idea

Codex local history is not a single file. The visible session list depends on:

- rollout JSONL files under `~/.codex/sessions` and `~/.codex/archived_sessions`
- `~/.codex/state_5.sqlite`
- `~/.codex/session_index.jsonl`

When cc-switch changes the active Codex provider, the effective `model_provider` may change. This can make sessions appear split by provider. The tool scans cc-switch's current `providers` table at runtime, keeps official Codex providers as `openai`, and normalizes all non-official Codex providers to `ccs`.

Some cc-switch provider configs can also lag behind the current Codex config. For example, a provider may still contain an older top-level `model = "gpt-5.5"` after the user has moved to `gpt-5.6-*`. The sync step treats older known model IDs as legacy values and replaces them with the active defaults from `~/.codex/config.toml`, but only for providers in the same class as the current provider: transit defaults update transit configs, official defaults update official configs. Runtime defaults such as sandbox and approval mode are inherited only when they already exist in the active config; the tool does not hard-code permissive settings.

## Components (Python package `codex_history_sync/`)

- `core.py`: the portable sync engine — backup, config normalization, rollout metadata repair, SQLite repair, and session index rebuild. Ported from the original embedded Python.
- `repair.py`: the complete local history-repair toolchain (ported from CodexPlusPlus provider-sync) — discovers the newer `~/.codex/sqlite/*.db` layout, repairs `threads` / `local_thread_catalog`, normalizes `.codex-global-state.json`, strips model window-size suffixes, and rebuilds `session_index.jsonl`. Auto-detects the target `model_provider` from cc-switch and supports `--dry-run`.
- `doctor.py`: read-only diagnosis of the local history state (what a repair would change).
- `paths.py`: cross-platform path resolution and Codex Desktop / web-cache detection.
- `processes.py`: stdlib-only process enumeration and termination (Windows Toolhelp32 via ctypes; macOS/Linux `ps`).
- `watcher.py`: polls `~/.cc-switch` for provider changes and spawns the UI wrapper in automatic mode. Uses a cross-platform `flock`/`msvcrt` single-instance lock.
- `ui.py`: tkinter progress dialog with a console progress fallback; owns the run mutex and the close → clear → sync → relaunch flow.
- `actions.py`: launch Codex, clear `CODEX_API_KEY` overrides, clear stale web-login cache.
- `autostart.py`: per-platform autostart (Windows `.vbs`, macOS LaunchAgent, Linux XDG autostart).
- `backup_restore.py`: install-time backup and latest-backup restore.
- `cli.py`: the `install` / `uninstall` / `restore` / `watch` / `run` / `sync` entry points.

## Safety model

The first sync of each day creates a small backup under `~/.codex/history-sync-backups`.

When stale Codex Desktop web auth cache is cleared, the removed cache entries are copied to `~/.codex/web-cache-backups/history-sync-token-clear-*` first. Only the most recent five automatic token-cache backups are kept.

Install creates a separate backup under `~/.codex/history-sync-tool-backups`.

Restore is conservative by default: it disables the tool but does not overwrite Codex or cc-switch state unless `--restore-latest-backup` is provided and confirmed.

The tool does not copy `auth.json`, API keys, or login tokens into the repository or any cloud location. It may remove a user-level `CODEX_API_KEY` environment variable because Codex gives that override priority over `auth.json` (persistent removal is done on Windows via the registry; on macOS/Linux the current-process override is cleared and the user should remove any shell rc export manually).

## Concurrency

- The watcher holds `~/.codex/.history-sync-watcher.lock` for its lifetime (one watcher at a time).
- The UI wrapper holds `~/.codex/.history-sync-run.lock` while a sync runs (one sync at a time).
- Both locks use OS file locks, so stale processes do not leave behind a deadlock: the lock dies with the process.
