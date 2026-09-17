"""Directory watcher that triggers a sync when the cc-switch provider changes.

Zero-dependency: watches ``~/.cc-switch`` by polling ``settings.json`` and
``cc-switch.db`` for mtime/size changes, then spawns the UI wrapper in automatic
mode.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import paths


def acquire_lock(lock_path):
    """Acquire an exclusive, non-blocking process lock. Returns an fd or None."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return None
    if paths.is_windows():
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            return None
    else:
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None
    return fd


def release_lock(fd):
    if fd is None:
        return
    try:
        if paths.is_windows():
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def get_settings_provider():
    settings = paths.cc_switch_settings()
    if not settings.exists():
        return None
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except Exception:
        return None
    value = data.get("currentProviderCodex")
    return value if isinstance(value, str) and value else None


def get_db_provider():
    db = paths.cc_switch_db()
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            row = con.execute(
                "select id from providers where app_type='codex' and is_current=1 limit 1"
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    return row[0] if row else None


def get_current_provider():
    return get_settings_provider() or get_db_provider()


def _signature():
    """Return a tuple capturing the mtime/size of the two watched files."""
    parts = []
    for p in (paths.cc_switch_settings(), paths.cc_switch_db()):
        try:
            st = p.stat()
            parts.append((st.st_mtime, st.st_size))
        except OSError:
            parts.append(None)
    return tuple(parts)


def spawn_ui(provider_id, automatic=True):
    """Launch the UI wrapper in a fresh, detached process."""
    here = Path(__file__).resolve().parent.parent  # package root
    entry = here / "main.py"
    args = [str(entry), "run"]
    if automatic:
        args.append("--automatic")
    if provider_id:
        args += ["--provider-id", str(provider_id)]

    if paths.is_windows():
        pythonw = _pythonw_path()
        exe = pythonw or sys.executable
        creationflags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen([exe, *args], creationflags=creationflags, close_fds=True)
        except Exception:
            pass
    else:
        try:
            subprocess.Popen(
                [sys.executable, *args],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except Exception:
            pass


def _pythonw_path():
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        return exe[:-10] + "pythonw.exe"
    return None


def watch():
    """Run the watcher loop. Returns an exit code."""
    pid_path = paths.watcher_pid_path()
    try:
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

    lock = acquire_lock(paths.watcher_lock_path())
    if lock is None:
        return 0  # another watcher is already running

    try:
        cc_home = paths.cc_switch_home()
        if not cc_home.exists():
            return 1

        last_provider = get_current_provider()
        last_sig = _signature()

        while True:
            time.sleep(1.0)
            sig = _signature()
            if sig == last_sig:
                continue
            last_sig = sig
            time.sleep(1.5)  # debounce cc-switch's write burst

            current = get_current_provider()
            if not current or current == last_provider:
                continue

            last_provider = current
            spawn_ui(provider_id=current, automatic=True)
    finally:
        release_lock(lock)
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
    return 0
