"""Progress UI and the end-to-end sync flow.

Primary UI is a small tkinter dialog (standard library); on headless systems it
falls back to a console progress bar. The heavy sync work runs in a worker
thread while the UI thread pumps events.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import actions, core, paths, processes, watcher


# --------------------------------------------------------------------------- #
# UI backends
# --------------------------------------------------------------------------- #

class TkProgressUI:
    def __init__(self):
        import tkinter as tk
        from tkinter import messagebox, ttk
        self._tk = tk
        self._messagebox = messagebox
        self.root = tk.Tk()
        self.root.title("Codex 会话同步中")
        self.root.resizable(False, False)
        try:
            self.root.attributes("-topmost", True)
        except Exception:
            pass
        self._status = tk.StringVar(value="正在同步历史会话")
        self._percent = tk.DoubleVar(value=0)

        frame = tk.Frame(self.root, padx=18, pady=14)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, textvariable=self._status, anchor="w").pack(fill="x")
        ttk.Progressbar(frame, variable=self._percent, maximum=100, length=340,
                        mode="determinate").pack(fill="x", pady=(10, 0))

        # Center on screen.
        self.root.update_idletasks()
        w, h = 380, 120
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.update()

    def set(self, percent, status=""):
        try:
            self._percent.set(int(percent))
            if status:
                self._status.set(status)
            self.root.update()
        except Exception:
            pass

    def ask_yes_no(self, title, message):
        try:
            return self._messagebox.askyesno(title, message)
        except Exception:
            return False

    def show_error(self, message):
        try:
            self._messagebox.showerror("同步失败", message)
        except Exception:
            pass

    def close(self):
        try:
            self.root.destroy()
        except Exception:
            pass


class ConsoleProgressUI:
    def __init__(self):
        self._last = -1

    def set(self, percent, status=""):
        percent = int(percent)
        if percent == self._last and not status:
            return
        self._last = percent
        width = 30
        filled = int(width * percent / 100)
        bar = "[" + "=" * filled + ">" + " " * (width - filled - 1) + "]"
        line = f"\r{bar} {percent:3d}%"
        if status:
            line += "  " + status
        print(line, end="", flush=True)
        if percent >= 100:
            print()

    def ask_yes_no(self, title, message):
        print(f"\n[{title}] {message}")
        try:
            answer = input("是否关闭 Codex？(y/N): ").strip().lower()
        except EOFError:
            return False
        return answer in ("y", "yes")

    def show_error(self, message):
        print(f"\n同步失败: {message}", flush=True)

    def close(self):
        pass


def make_ui():
    try:
        return TkProgressUI()
    except Exception:
        return ConsoleProgressUI()


# --------------------------------------------------------------------------- #
# Sync flow
# --------------------------------------------------------------------------- #

def _run_core_with_progress(ui):
    result = {}
    errors = []

    def work():
        try:
            result["value"] = core.run()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    percent = 12
    while thread.is_alive():
        percent = min(95, percent + 2)
        ui.set(percent)
        time.sleep(0.12)
    thread.join()
    if errors:
        raise errors[0]
    return result.get("value")


def run_sync_flow(automatic=False, provider_id=None, codex_home=None, ui=None):
    """Orchestrate close -> clear cache -> sync -> relaunch. Returns exit code."""
    # Resolve and export paths so core.run() sees the right locations.
    home = Path(codex_home) if codex_home else paths.codex_home()
    os.environ["CODEX_HOME"] = str(home)
    os.environ["CC_SWITCH_DB"] = str(paths.cc_switch_db())
    os.environ["CC_SWITCH_SETTINGS"] = str(paths.cc_switch_settings())
    # The flow consumes core.run()'s return value directly; suppress its stdout.
    os.environ["CODEX_HISTORY_SYNC_QUIET"] = "1"

    ui = ui or make_ui()

    lock = watcher.acquire_lock(paths.run_lock_path())
    if lock is None:
        ui.close()
        return 0  # another sync already running

    try:
        ui.set(0, "准备同步")

        should_clear = actions.should_clear_auth_cache(provider_id)
        if processes.find_codex_pids():
            if automatic:
                ui.set(3, "正在关闭 Codex")
                processes.close_codex_processes()
                ui.set(5)
            elif ui.ask_yes_no(
                "关闭并清理 Codex？",
                "检测到 Codex 正在运行。切换中转需要关闭并清理过期登录缓存，"
                "避免 token_expired 和旧 provider 状态残留。是否关闭 Codex？",
            ):
                processes.close_codex_processes()
                ui.set(5)
            elif should_clear:
                should_clear = False
                actions.clear_auth_overrides()
                ui.set(8, "已跳过缓存清理；Codex 重启后才会完全生效")

        if should_clear:
            ui.set(8, "正在清理过期登录缓存")
            cleared = actions.clear_codex_auth_cache()["cleared"]
            ui.set(12, f"已清理 {cleared} 项登录缓存" if cleared else "未发现需要清理的登录缓存")

        # Remove any CODEX_API_KEY override before running the sync (matches the
        # original sync script's behavior).
        auth_cleared = actions.clear_auth_overrides()
        os.environ["CODEX_AUTH_OVERRIDES_CLEARED"] = "1" if auth_cleared else "0"

        result = _run_core_with_progress(ui)

        ui.set(100, "正在启动 Codex")
        time.sleep(0.35)
        actions.launch_codex()
        time.sleep(0.25)
        return 0
    except Exception as exc:  # noqa: BLE001
        ui.set(100)
        ui.show_error(str(exc))
        return 1
    finally:
        watcher.release_lock(lock)
        ui.close()
