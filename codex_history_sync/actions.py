"""Higher-level actions: launch Codex, clear auth overrides and stale web cache."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import paths


# --------------------------------------------------------------------------- #
# Auth override clearing
# --------------------------------------------------------------------------- #

def clear_auth_overrides():
    """Remove ``CODEX_API_KEY`` from the current process and (on Windows) the
    user environment, because it silently overrides ``auth.json``. Returns True
    if anything was cleared."""
    cleared = False
    if "CODEX_API_KEY" in os.environ:
        del os.environ["CODEX_API_KEY"]
        cleared = True
    if paths.is_windows():
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE
            )
            try:
                try:
                    winreg.QueryValueEx(key, "CODEX_API_KEY")
                except FileNotFoundError:
                    pass
                else:
                    winreg.DeleteValue(key, "CODEX_API_KEY")
                    cleared = True
            finally:
                winreg.CloseKey(key)
        except Exception:
            pass
    return cleared


# --------------------------------------------------------------------------- #
# Provider classification
# --------------------------------------------------------------------------- #

def is_official_provider(provider_id):
    if not provider_id:
        return False
    if provider_id == "codex-official":
        return True
    db = paths.cc_switch_db()
    if not db.exists():
        return False
    try:
        import sqlite3
        con = sqlite3.connect(str(db), timeout=5)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "select id, name, category from providers where app_type='codex' and id=? limit 1",
                (provider_id,),
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return False
    if not row:
        return False
    provider_id = (row["id"] or "").strip().lower()
    name = (row["name"] or "").strip().lower()
    category = (row["category"] or "").strip().lower()
    return (
        provider_id == "codex-official"
        or category == "official"
        or (provider_id == "openai" and "official" in name)
    )


def should_clear_auth_cache(provider_id):
    if provider_id:
        return not is_official_provider(provider_id)
    config = paths.codex_home() / "config.toml"
    if not config.exists():
        return False
    try:
        text = config.read_text(encoding="utf-8")
    except Exception:
        return False
    import re
    m = re.search(r'(?m)^\s*model_provider\s*=\s*"([^"]+)"\s*$', text)
    return bool(m) and m.group(1) == "ccs"


# --------------------------------------------------------------------------- #
# Stale web-cache clearing
# --------------------------------------------------------------------------- #

def clear_codex_auth_cache():
    """Back up and remove Chromium web-login cache entries for transit providers."""
    web_roots = paths.codex_web_cache_roots()
    entries = paths.chromium_cache_entries()
    if not web_roots:
        return {"cleared": 0, "backup_root": None}

    backup_root = paths.codex_home() / "web-cache-backups" / (
        "history-sync-token-clear-" + time.strftime("%Y%m%d-%H%M%S")
    )
    cleared = 0
    for web_root in web_roots:
        default_root = web_root / "Default"
        if not default_root.exists():
            continue
        resolved_root = web_root.resolve()
        for rel in entries:
            target = web_root / rel
            if not target.exists():
                continue
            try:
                resolved_target = target.resolve()
            except OSError:
                continue
            # Safety: never touch anything outside the detected web root.
            try:
                resolved_target.relative_to(resolved_root)
            except ValueError:
                continue

            backup_path = backup_root / web_root.name / rel
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copytree(target, backup_path, dirs_exist_ok=True)
            except Exception:
                pass
            try:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            except Exception:
                pass
            cleared += 1

    # Keep only the five most recent token-clear backups.
    web_cache_backup_root = paths.codex_home() / "web-cache-backups"
    if web_cache_backup_root.exists():
        try:
            dirs = sorted(
                [p for p in web_cache_backup_root.iterdir()
                 if p.is_dir() and p.name.startswith("history-sync-token-clear-")],
                key=lambda p: p.name,
                reverse=True,
            )
            for old in dirs[5:]:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass

    return {
        "cleared": cleared,
        "backup_root": str(backup_root) if cleared else None,
    }


# --------------------------------------------------------------------------- #
# Launch Codex
# --------------------------------------------------------------------------- #

def launch_codex():
    for exe, argv in paths.codex_launch_candidates():
        try:
            if exe == "open":
                subprocess.Popen(["open", *argv], start_new_session=True)
            elif paths.is_windows():
                subprocess.Popen(
                    [str(exe), *argv],
                    cwd=str(Path.home()),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.Popen(
                    [str(exe), *argv],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            return True
        except Exception:
            continue
    return False
