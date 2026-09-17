#!/usr/bin/env python3
"""Entry point so the tool can run without installation: ``python main.py ...``."""

import os
import sys

# Make the package importable when running straight from a checkout.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from codex_history_sync.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
