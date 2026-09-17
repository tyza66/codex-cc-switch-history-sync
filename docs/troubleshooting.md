# Troubleshooting

## The watcher does not start

Run:

```bash
python3 main.py install
```

Then check that a watcher process is running:

- Windows: look for a hidden `pythonw.exe` running `main.py watch`.
- macOS: `launchctl list | grep codex.history-sync`
- Linux: `pgrep -af "main.py watch"`

Or start it manually in the foreground to see any error:

```bash
python3 main.py watch
```

## Switching provider does not open the progress window

Make sure cc-switch updates one of these files:

- `~/.cc-switch/settings.json`
- `~/.cc-switch/cc-switch.db`

The watcher only triggers when the active Codex provider actually changes.

## The progress window does not appear (console fallback)

The tool prefers a tkinter dialog and falls back to a console progress bar when no
graphical environment is available or Python lacks tkinter. To install tkinter:

- macOS Homebrew: `brew install python-tk@3.13` (adjust to your Python version)
- Debian/Ubuntu: `sudo apt install python3-tk`
- Windows: the official python.org installer bundles tkinter

## Codex history is still missing

Run a manual sync:

```bash
python3 main.py sync
```

or with the UI:

```bash
python3 main.py run
```

If `CODEX_HOME` is not set, the default is `~/.codex`. Use `--codex-home <dir>` to override.

## The model list still does not show GPT-5.6

For API-key / transit-provider routes, Codex Desktop may show a custom model label
instead of a first-class GPT-5.6 preset. The sync script preserves the active top-level
`model` and `model_reasoning_effort` from `~/.codex/config.toml`, and propagates them
into legacy cc-switch provider configs that still contain older values such as
`gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, or `gpt-5.2`.

Check the active config:

```bash
head -8 ~/.codex/config.toml
```

Then run:

```bash
python3 main.py sync
```

## Codex Desktop reports `token_expired`

If Codex Desktop crashes or reports:

```text
failed to refresh available models ... 401 Unauthorized ... token_expired
```

the cached ChatGPT web token inside the Codex Desktop Chromium profile is stale.
Automatic cc-switch provider changes now close Codex, clear the stale web cache for
transit providers, and restart Codex.

The cleanup also removes a user-level `CODEX_API_KEY` environment override, because
that variable can silently override `~/.codex/auth.json`.

Manual UI sync uses a confirmation dialog. If you answer **No**, the web cache cleanup
is skipped until Codex is closed and the sync is run again.

## I want to undo everything

Disable the tool:

```bash
python3 main.py uninstall
```

Restore latest install backup (asks for `RESTORE` confirmation):

```bash
python3 main.py restore --restore-latest-backup
```
