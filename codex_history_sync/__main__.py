"""Allow ``python -m codex_history_sync`` to run the CLI."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
