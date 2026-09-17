"""Install-time backup and restore of Codex / cc-switch state."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from . import core, paths


def _copy_if_exists(src, dst):
    src = Path(src)
    if not src.exists():
        return
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    except Exception:
        pass


def make_install_backup():
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = paths.install_backup_root() / f"install_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    codex_home = paths.codex_home()
    for name in ("config.toml", "session_index.jsonl"):
        _copy_if_exists(codex_home / name, backup_dir / name)

    state = codex_home / "state_5.sqlite"
    if state.exists():
        core.backup_sqlite(state, backup_dir / "state_5.sqlite")

    db = paths.cc_switch_db()
    if db.exists():
        core.backup_sqlite(db, backup_dir / "cc-switch.db")

    _copy_if_exists(paths.cc_switch_settings(), backup_dir / "cc-switch-settings.json")
    return backup_dir


def latest_install_backup():
    root = paths.install_backup_root()
    if not root.exists():
        return None
    dirs = [p for p in root.iterdir() if p.is_dir() and p.name.startswith("install_")]
    if not dirs:
        return None
    return sorted(dirs, key=lambda p: p.name, reverse=True)[0]


def restore_latest_backup(confirm_fn=None):
    """Restore the latest install backup. Returns (restored, backup_dir)."""
    latest = latest_install_backup()
    if latest is None:
        raise FileNotFoundError(f"No install backup found under {paths.install_backup_root()}")

    if confirm_fn is not None and not confirm_fn(latest):
        return False, latest

    codex_home = paths.codex_home()
    for name in ("config.toml", "session_index.jsonl", "state_5.sqlite"):
        _copy_if_exists(latest / name, codex_home / name)

    _copy_if_exists(latest / "cc-switch.db", paths.cc_switch_db())
    _copy_if_exists(latest / "cc-switch-settings.json", paths.cc_switch_settings())
    return True, latest
