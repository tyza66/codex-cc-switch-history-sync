"""Progress UI and the end-to-end sync flow.

Primary UI is a small tkinter dialog (standard library); on headless systems it
falls back to a console progress bar. The heavy sync work runs in a worker
thread while the UI thread pumps events.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from . import actions, autostart, backup_restore, core, doctor, paths, processes, repair, watcher


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


# --------------------------------------------------------------------------- #
# GUI dashboard (launched when the app is opened without a subcommand)
# --------------------------------------------------------------------------- #

def _gui_status(home):
    """Return (provider_id, target_provider, kind_label)."""
    try:
        target, provider_id, is_official = repair.detect_target_provider(home)
    except Exception:
        target, provider_id, is_official = core.TRANSIT_MODEL_PROVIDER, None, False
    kind = "官方 (openai)" if is_official else "中转 (ccs)"
    return provider_id or "(未检测到)", target, kind


class TkMainWindow:
    """A small dashboard window: sync / diagnose / install autostart."""

    def __init__(self):
        import tkinter as tk
        from tkinter import scrolledtext
        self._tk = tk
        self.root = tk.Tk()
        self.root.title("Codex History Sync")
        self.root.geometry("580x420")
        self.root.minsize(480, 320)
        self.home = paths.codex_home()
        # Keep core.run()'s JSON off stdout while in GUI mode.
        os.environ["CODEX_HISTORY_SYNC_QUIET"] = "1"

        header = tk.Frame(self.root, padx=14, pady=12)
        header.pack(fill="x")
        tk.Label(header, text="Codex History Sync", font=("Helvetica", 16, "bold")).pack(anchor="w")
        self._provider_var = tk.StringVar(value="")
        self._target_var = tk.StringVar(value="")
        tk.Label(header, textvariable=self._provider_var, anchor="w").pack(fill="x", pady=(8, 0))
        tk.Label(header, textvariable=self._target_var, anchor="w", fg="#555555").pack(fill="x")

        buttons = tk.Frame(self.root, padx=14)
        buttons.pack(fill="x")
        self._sync_btn = tk.Button(buttons, text="立即同步", command=self._on_sync)
        self._sync_btn.pack(side="left", padx=(0, 8))
        self._diag_btn = tk.Button(buttons, text="诊断", command=self._on_diagnose)
        self._diag_btn.pack(side="left", padx=(0, 8))
        self._install_btn = tk.Button(buttons, text="安装自启动", command=self._on_install)
        self._install_btn.pack(side="left", padx=(0, 8))
        tk.Button(buttons, text="退出", command=self.root.destroy).pack(side="right")

        self._log_box = scrolledtext.ScrolledText(
            self.root, state="disabled", font=("Menlo", 11), padx=8, pady=8, wrap="word"
        )
        self._log_box.pack(fill="both", expand=True, padx=14, pady=(8, 14))

        self._refresh_status()
        self._log("就绪。点击「立即同步」同步历史，或「诊断」查看当前状态。")

    # -- helpers --------------------------------------------------------------
    def _log(self, text):
        self._log_box.configure(state="normal")
        self._log_box.insert("end", str(text) + "\n")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for btn in (self._sync_btn, self._diag_btn, self._install_btn):
            btn.configure(state=state)

    def _refresh_status(self):
        provider_id, target, kind = _gui_status(self.home)
        self._provider_var.set(f"当前 cc-switch provider: {provider_id}")
        self._target_var.set(f"目标 model_provider: {target}  [{kind}]")

    def _run_async(self, work, done, busy_msg):
        self._set_busy(True)
        self._log(busy_msg)

        def runner():
            value = error = None
            try:
                value = work()
            except Exception as exc:  # noqa: BLE001
                error = exc
            self.root.after(0, lambda: done(value, error))

        threading.Thread(target=runner, daemon=True).start()

    # -- actions --------------------------------------------------------------
    def _on_sync(self):
        self._run_async(core.run, self._sync_done, "开始同步历史…")

    def _sync_done(self, result, error):
        self._set_busy(False)
        self._refresh_status()
        if error:
            self._log(f"同步失败: {error}")
            return
        hist = result.get("history_repair") or {}
        index = result.get("index") or {}
        self._log("同步完成。")
        self._log(f"  目标 provider: {result.get('target_model_provider')}")
        self._log(f"  rollout 元数据更新: {result.get('rollout_meta_changed', 0)}")
        self._log(f"  threads provider 更新: {hist.get('threads_provider_rows', 0)}")
        self._log(f"  侧边栏目录 插入: {hist.get('catalog_inserted', 0)} / 删除: {hist.get('catalog_removed', 0)}")
        self._log(f"  会话索引: {index.get('index_entries', 0)} 条")
        self._log(f"  备份目录: {result.get('backup_dir')}")

    def _on_diagnose(self):
        self._run_async(lambda: doctor.run_diagnosis(self.home), self._diag_done, "诊断中…")

    def _diag_done(self, report, error):
        self._set_busy(False)
        if error:
            self._log(f"诊断失败: {error}")
            return
        for line in doctor.format_report(report).splitlines():
            self._log("  " + line)

    def _on_install(self):
        def work():
            backup_dir = backup_restore.make_install_backup()
            autostart.install()
            autostart.start_now()
            return backup_dir

        self._run_async(work, self._install_done, "安装自启动并启动 watcher…")

    def _install_done(self, backup_dir, error):
        self._set_busy(False)
        if error:
            self._log(f"安装失败: {error}")
            return
        self._log(f"已安装自启动并启动后台 watcher。备份: {backup_dir}")


def run_gui(parser=None):
    """Launch the dashboard window; fall back to help/console when headless."""
    try:
        window = TkMainWindow()
    except Exception as exc:  # noqa: BLE001
        if parser is not None:
            parser.print_help(sys.stderr)
        print(f"\n图形界面不可用（tkinter）: {exc}", file=sys.stderr)
        return 1
    window.root.mainloop()
    return 0
