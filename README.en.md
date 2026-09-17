# Codex Session Sync for CC Switch

A cross-platform (Windows / macOS / Linux) helper for **Codex chat-history sync**, **Codex session-history sync**, and keeping the same set of local Codex conversations visible when switching official / relay providers with cc-switch.

> 中文：[README.md](README.md) — This is the English version; the default `README.md` is in Chinese.

## What this update does

This update replaces the original Windows-only PowerShell implementation with a **pure Python standard-library** cross-platform implementation (one codebase for Windows / macOS / Linux):

- Fixes the sync window occasionally not actually running, which left history unsynced.
- Prevents stale provider configs from downgrading the current GPT-5.6 model back to GPT-5.5 / GPT-5.4.
- Fixes Codex Desktop `401 Unauthorized` / `token_expired` caused by an expired login cache.
- Distinguishes official vs relay providers so model config and login state don't overwrite each other.
- Handles the process names and launch methods of Codex Desktop / Codex CLI on every platform.

The automated flow is now: detect provider change → close Codex → clear necessary caches by provider type → sync config and history → relaunch Codex.

## Keywords

If you searched for any of these, this tool may apply:

- Codex chat history sync / conversations sync / sessions sync
- Codex history lost after switching to a relay in cc-switch
- Codex sessions invisible after switching providers
- Codex local session recovery / `session_index` repair
- Repairing `~/.codex/sessions`, `state_5.sqlite`, `session_index.jsonl`

## What problem it solves

Codex local history depends on rollout files, `state_5.sqlite`, and `session_index.jsonl` at the same time. After switching Codex providers with cc-switch, history can split or become invisible because the `model_provider` values no longer match.

This tool will:

- Watch cc-switch's current Codex provider for changes.
- Scan the Codex providers present in cc-switch at runtime: official OpenAI stays `openai`, all relays are written uniformly as `ccs`.
- Repair the Codex local history index and SQLite state.
- Support the newer Codex multi-database layout: `~/.codex/sqlite/*.db` (`threads` / `local_thread_catalog` / `messages` / `sessions`, …), not just the legacy `state_5.sqlite`.
- Repair the `local_thread_catalog` sidebar catalogue (insert missing rows, delete subagent rows).
- Normalize `.codex-global-state.json` (workspace roots / thread IDs / path dedup).
- Strip model window-size suffixes `gpt-5.5[1M]` → `gpt-5.5` (`threads.model` and `logs_2.sqlite`).
- Preserve the current Codex top-level model default, so stale cc-switch provider configs don't roll `gpt-5.6-*` back to an older model.
- Clear the expired Codex Desktop web login cache that causes `token_expired` when switching to a relay.
- Remove the user-level `CODEX_API_KEY` environment variable that would overwrite `auth.json`.
- Pop up a clean progress window after a switch (tkinter; falls back to a console progress bar in headless environments).
- Auto-launch Codex after a successful sync.
- Auto-backup critical state before the first sync of each day.

## Supported

- Windows / macOS / Linux
- OpenAI Codex Desktop / Codex CLI
- cc-switch
- Python 3.8+ (standard library only; the progress popup optionally uses tkinter and degrades gracefully when absent)

## Install

From the repository root:

```bash
python3 main.py install
```

The installer will:

- Back up critical state to `~/.codex/history-sync-tool-backups/install_<timestamp>` before the first sync of each day.
- Register autostart:
  - Windows: `Codex History Sync.vbs` in the `Startup` folder (launched hidden via `pythonw.exe`)
  - macOS: `~/Library/LaunchAgents/com.codex.history-sync.plist`
  - Linux: `~/.config/autostart/codex-history-sync.desktop`
- Start the background watcher immediately.

> Alternatively, `pip install -e .` and use the `codex-history-sync` command instead of `python3 main.py`.

## Usage

After installing, use cc-switch normally. When you click "Enable" on a Codex provider:

1. The watcher detects the provider change.
2. A sync progress window pops up.
3. Any running Codex is closed automatically to avoid stale provider / token state.
4. If the target is a relay, the expired Codex Desktop web login cache is cleared.
5. Session history is synced automatically.
6. Codex is launched automatically once the sync finishes.

Manual sync (with progress / confirmation UI):

```bash
python3 main.py run
```

Manual headless sync:

```bash
python3 main.py sync
```

Repair the local history index only (does not touch cc-switch provider config; auto-detects official vs relay):

```bash
python3 main.py repair            # full repair (auto-detects the target provider from cc-switch)
python3 main.py repair --dry-run  # preview changes without writing
```

Read-only diagnosis of the current history state (writes nothing):

```bash
python3 main.py doctor
```

Run the background watcher manually:

```bash
python3 main.py watch
```

## Packaging & release (GitHub Actions)

Pushing a `v*` version tag builds a single-file executable on all three platforms and uploads them to the matching GitHub Release:

```bash
git tag v1.0.20260917
git push origin v1.0.20260917
```

Artifacts (`.github/workflows/release.yml`):

| Platform | Artifact |
|---|---|
| Windows | `codex-history-sync-v*-windows-x64.zip` (contains `.exe`) |
| macOS | `codex-history-sync-v*-macos-arm64.tar.gz` (Apple Silicon, ad-hoc signed) |
| Linux | `codex-history-sync-v*-linux-x64.tar.gz` |

- `-rc` / `-beta` / `-alpha` tags are marked as pre-releases automatically.
- The macOS/Linux binaries need `chmod +x` after extraction.
- Binaries are unsigned: on macOS, right-click "Open" or `xattr -dr com.apple.quarantine <binary>` if Gatekeeper blocks the first run; on Windows, SmartScreen may appear — click "Run anyway".

All commands accept `--codex-home <dir>` to override the Codex directory (default `~/.codex`).

## Restore / uninstall

Stop the tool without restoring the database:

```bash
python3 main.py uninstall
```

or equivalently:

```bash
python3 main.py restore
```

Restore the most recent install backup (prompts for `RESTORE` to confirm):

```bash
python3 main.py restore --restore-latest-backup
```

## Risks

This tool modifies local Codex state indexes, including:

- `config.toml`
- `session_index.jsonl`
- `state_5.sqlite`
- cc-switch Codex provider configs
- Codex Desktop's Chromium web login cache (only during automatic relay switches)
- the user-level `CODEX_API_KEY` environment variable override

It uploads nothing and never syncs your sessions to the cloud. Everything runs locally.

Notes:

- Relay history is uniformly marked as `ccs`; the original per-relay name is not preserved. The relay list comes from a runtime scan of cc-switch, not a hard-coded vendor list.
- Switching providers automatically closes Codex; in-flight tasks may be interrupted.
- The tool does not back up `auth.json` and never copies API keys or login tokens; only local backups are made before web-cache cleanup.
- Logging in directly through the official route may cause official sessions to be lost.
- Do not commit your `.codex`, `.cc-switch`, backups, `auth.json`, or SQLite databases to GitHub.

## Platform differences

- **Progress window**: prefers a tkinter popup; falls back to a console progress bar in headless environments (or when Python lacks tkinter). Homebrew `python@3.13` on macOS ships without tkinter by default — `brew install python-tk@3.13` to enable the popup.
- **Autostart**: Windows uses a Startup-folder `.vbs`, macOS uses a LaunchAgent, Linux uses an XDG autostart `.desktop`.
- **Codex Desktop cache clearing**: candidate paths differ per platform and are best-effort scans; missing directories are skipped safely.
- **Legacy Windows PowerShell scripts**: kept under `scripts/` for reference only; the new implementation no longer depends on them.

## What it does not do

- Does not modify cc-switch itself.
- Does not provide health-check scripts.
- Does not clean Codex `logs_2.sqlite` runtime logs.
- Does not handle cross-device sync.

## License

[MIT](LICENSE)
