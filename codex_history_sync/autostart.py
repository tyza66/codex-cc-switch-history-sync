"""Per-platform autostart registration (Windows / macOS / Linux), stdlib only."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import paths


def entry_point() -> Path:
    return Path(__file__).resolve().parent.parent / "main.py"


def _interpreter(for_windows_hidden=False):
    exe = sys.executable
    if for_windows_hidden and paths.is_windows():
        cand = exe[:-len("python.exe")] + "pythonw.exe" if exe.lower().endswith("python.exe") else None
        if cand and Path(cand).exists():
            return cand
    return exe


def _windows_startup_dir():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        appdata = str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _windows_vbs_path():
    return _windows_startup_dir() / "Codex History Sync.vbs"


def _macos_plist_path():
    return Path.home() / "Library" / "LaunchAgents" / "com.codex.history-sync.plist"


def _linux_desktop_path():
    return Path.home() / ".config" / "autostart" / "codex-history-sync.desktop"


def _install_windows():
    vbs = _windows_vbs_path()
    vbs.parent.mkdir(parents=True, exist_ok=True)
    pythonw = _interpreter(for_windows_hidden=True)
    content = (
        "Set WshShell = CreateObject(\"WScript.Shell\")\n"
        f"WshShell.Run \"\"\"{pythonw}\"\" \"\"{entry_point()}\"\" watch\", 0, False\n"
    )
    vbs.write_text(content, encoding="utf-8")


def _install_macos():
    plist = _macos_plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    log_out = str(paths.codex_home() / "history-sync-watcher.log")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.codex.history-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>{_interpreter()}</string>
        <string>{entry_point()}</string>
        <string>watch</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_out}</string>
    <key>StandardErrorPath</key>
    <string>{log_out}</string>
</dict>
</plist>
"""
    plist.write_text(xml, encoding="utf-8")


def _install_linux():
    desktop = _linux_desktop_path()
    desktop.parent.mkdir(parents=True, exist_ok=True)
    exec_cmd = f'"{_interpreter()}" "{entry_point()}" watch'
    content = f"""[Desktop Entry]
Type=Application
Name=Codex History Sync
Comment=Watch cc-switch provider changes and sync Codex history
Exec={exec_cmd}
Terminal=false
X-GNOME-Autostart-enabled=true
"""
    desktop.write_text(content, encoding="utf-8")


def install():
    paths.codex_home().mkdir(parents=True, exist_ok=True)
    if paths.is_windows():
        _install_windows()
    elif paths.is_macos():
        _install_macos()
    else:
        _install_linux()


def uninstall():
    if paths.is_windows():
        _windows_vbs_path().unlink(missing_ok=True)
    elif paths.is_macos():
        plist = _macos_plist_path()
        if plist.exists():
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
            plist.unlink(missing_ok=True)
    else:
        _linux_desktop_path().unlink(missing_ok=True)


def start_now():
    """Start the watcher immediately (detached), mirroring the autostart entry."""
    pythonw = _interpreter(for_windows_hidden=True) if paths.is_windows() else _interpreter()
    args = [pythonw, str(entry_point()), "watch"]
    if paths.is_windows():
        creationflags = 0x00000008 | 0x00000200
        subprocess.Popen(args, creationflags=creationflags, close_fds=True)
    else:
        subprocess.Popen(args, start_new_session=True, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
