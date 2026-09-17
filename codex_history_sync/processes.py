"""Cross-platform process enumeration and termination (stdlib only).

Windows uses the Toolhelp32 snapshot API via ``ctypes`` (no PowerShell / wmic
dependency); macOS and Linux use ``ps``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

from . import paths

# Process names that clearly identify a Codex process on Windows.
_WINDOWS_CODEX_NAMES = {"chatgpt.exe", "codex.exe"}

# Command-line fragments that identify a Codex process on POSIX.
_POSIX_CODEX_MARKERS = (
    "/codex.app/contents/macos",
    "/.codex/",
    "codex/web/codex",
    ".local/bin/codex",
    "/codex ",
    " openai/codex",
)


def _windows_processes():
    """Return list of ``(pid, name)`` for every running process."""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.CHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32First.restype = wintypes.BOOL
    kernel32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    kernel32.Process32Next.restype = wintypes.BOOL
    kernel32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []

    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    out = []
    try:
        if kernel32.Process32First(snapshot, ctypes.byref(entry)):
            while True:
                name = entry.szExeFile.decode("mbcs", errors="ignore")
                out.append((int(entry.th32ProcessID), name))
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    return out


def _posix_processes():
    """Return list of ``(pid, comm, args)`` for every running process."""
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,comm=,args="],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = proc.stdout.splitlines()
    except Exception:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        comm = parts[1] if len(parts) > 1 else ""
        args = parts[2] if len(parts) > 2 else ""
        out.append((pid, comm, args))
    return out


def _is_codex_process(pid, name, cmdline):
    """Conservative matcher: require both a plausible name and a marker."""
    if paths.is_windows():
        return name.lower() in _WINDOWS_CODEX_NAMES
    low_name = name.lower()
    low_cmd = cmdline.lower()
    if "codex" not in low_name and "codex" not in low_cmd:
        return False
    return any(marker in low_cmd for marker in _POSIX_CODEX_MARKERS) or low_name == "codex"


def find_codex_pids():
    """Return the list of PIDs that look like a running Codex process."""
    pids = []
    if paths.is_windows():
        for pid, name in _windows_processes():
            if name.lower() in _WINDOWS_CODEX_NAMES:
                pids.append(pid)
    else:
        for pid, comm, args in _posix_processes():
            if _is_codex_process(pid, comm, args):
                pids.append(pid)
    return pids


def find_pids_by_cmdline(substrings):
    """Return PIDs whose command line contains any of the given substrings.

    On Windows the command line is not enumerated (only the process name), so
    this returns an empty list there and callers should fall back to a pid file.
    """
    if paths.is_windows():
        return []
    out = []
    for pid, _comm, args in _posix_processes():
        low = args.lower()
        if any(s.lower() in low for s in substrings):
            out.append(pid)
    return out


def _terminate_windows(pid, force):
    flags = ["/F"] if force else []
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), *flags],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def _terminate_posix(pid, force):
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(pid, sig)
    except (OSError, ProcessLookupError):
        pass


def stop_process(pid, graceful=True):
    if pid == os.getpid():
        return
    if paths.is_windows():
        _terminate_windows(pid, force=not graceful)
    else:
        _terminate_posix(pid, force=not graceful)


def close_codex_processes(grace_ms=2000):
    """Gracefully close, then force-kill any remaining Codex process."""
    pids = find_codex_pids()
    if not pids:
        return
    for pid in pids:
        stop_process(pid, graceful=True)
    time.sleep(grace_ms / 1000.0)
    for pid in find_codex_pids():
        stop_process(pid, graceful=False)
    time.sleep(0.3)
