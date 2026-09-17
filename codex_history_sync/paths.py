"""Platform path resolution and Codex app detection.

Everything here is best-effort and read-only: the callers guard every use of a
returned path with an existence check, so returning a path that happens not to
exist on a given platform is harmless.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def home() -> Path:
    return Path.home()


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def cc_switch_home() -> Path:
    return Path.home() / ".cc-switch"


def cc_switch_db() -> Path:
    return cc_switch_home() / "cc-switch.db"


def cc_switch_settings() -> Path:
    return cc_switch_home() / "settings.json"


def lock_dir() -> Path:
    return codex_home()


def watcher_lock_path() -> Path:
    return lock_dir() / ".history-sync-watcher.lock"


def run_lock_path() -> Path:
    return lock_dir() / ".history-sync-run.lock"


def watcher_pid_path() -> Path:
    return lock_dir() / ".history-sync-watcher.pid"


def backup_root() -> Path:
    return codex_home() / "history-sync-backups"


def install_backup_root() -> Path:
    return codex_home() / "history-sync-tool-backups"


# --------------------------------------------------------------------------- #
# Codex launch candidates
# --------------------------------------------------------------------------- #

def _windows_launch_candidates():
    """Return [(executable_path, argv), ...] ordered by preference."""
    candidates = []

    # MSIX desktop app: %ProgramFiles%\WindowsApps\OpenAI.Codex_*\app\*.exe
    windows_apps = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
    try:
        packages = sorted(
            windows_apps.glob("OpenAI.Codex_*"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )
        for pkg in packages:
            for rel in ("app/ChatGPT.exe", "app/Codex.exe"):
                exe = pkg / rel
                if exe.exists():
                    candidates.append((exe, []))
    except OSError:
        pass

    # Codex CLI / app binary.
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    if str(local):
        cli = local / "OpenAI" / "Codex" / "bin" / "codex.exe"
        if cli.exists():
            candidates.append((cli, ["app"]))

    return candidates


def _macos_launch_candidates():
    candidates = []
    for root in (Path("/Applications"), Path.home() / "Applications"):
        app = root / "Codex.app"
        if app.exists():
            candidates.append(("open", ["-a", str(app)]))
    # Homebrew/npm CLI.
    for exe in (Path.home() / ".codex" / "bin" / "codex",):
        if exe.exists():
            candidates.append((exe, ["app"]))
    which = shutil.which("codex")
    if which:
        candidates.append((which, ["app"]))
    return candidates


def _linux_launch_candidates():
    candidates = []
    for exe in (
        Path.home() / ".local" / "bin" / "codex",
        Path("/usr/local/bin/codex"),
        Path("/usr/bin/codex"),
    ):
        if exe.exists():
            candidates.append((exe, ["app"]))
    which = shutil.which("codex")
    if which:
        candidates.append((which, ["app"]))
    # Flatpak / desktop entry.
    desktop = Path("/var/lib/flatpak/app/com.openai.Codex") if False else None  # placeholder
    return candidates


def codex_launch_candidates():
    """Ordered list of ``(executable_or_command, argv)`` used to start Codex."""
    if is_windows():
        return _windows_launch_candidates()
    if is_macos():
        return _macos_launch_candidates()
    return _linux_launch_candidates()


# --------------------------------------------------------------------------- #
# Codex web (Chromium) cache roots — used to clear stale transit auth.
# --------------------------------------------------------------------------- #

_CHROMIUM_CACHE_ENTRIES = (
    "Default/Network/Cookies",
    "Default/Network/Cookies-journal",
    "Default/Local Storage",
    "Default/Session Storage",
    "Default/IndexedDB",
    "Default/Service Worker",
    "Default/GCM Store",
    "Default/Sync Data",
    "Default/Safe Browsing Network",
    "Default/WebStorage",
)


def chromium_cache_entries():
    return list(_CHROMIUM_CACHE_ENTRIES)


def codex_web_cache_roots():
    """Best-effort list of directories that contain a Chromium ``Default`` dir."""
    roots = []
    if is_windows():
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        if str(local):
            pkg_root = local / "Packages"
            try:
                for pkg in pkg_root.glob("OpenAI.Codex_*"):
                    c = pkg / "LocalCache" / "Roaming" / "Codex" / "web" / "Codex"
                    if c.exists():
                        roots.append(c)
            except OSError:
                pass
        for c in (
            Path(os.environ.get("APPDATA", "")) / "Codex" / "web" / "Codex",
            local / "Codex" / "web" / "Codex",
        ):
            if str(c.parent) and c.exists():
                roots.append(c)
    elif is_macos():
        for c in (
            Path.home() / "Library" / "Application Support" / "Codex" / "web" / "Codex",
            Path.home() / "Library" / "Application Support" / "Codex",
            Path.home() / "Library" / "Caches" / "Codex",
        ):
            if c.exists():
                roots.append(c)
    else:
        for c in (
            Path.home() / ".config" / "Codex" / "web" / "Codex",
            Path.home() / ".config" / "Codex",
            Path.home() / ".local" / "share" / "Codex",
        ):
            if c.exists():
                roots.append(c)

    # De-duplicate while preserving order.
    seen = set()
    out = []
    for r in roots:
        key = str(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out
