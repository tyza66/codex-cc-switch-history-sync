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

from . import actions, autostart, backup_restore, core, codex_fix, doctor, export_import, paths, processes, repair, watcher


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
    """A small dashboard window: sync / diagnose / install autostart / export / import / Codex修复."""

    def __init__(self):
        import tkinter as tk
        from tkinter import scrolledtext
        self._tk = tk
        self.root = tk.Tk()
        self.root.title("Codex History Sync")
        self.root.geometry("580x440")
        self.root.minsize(480, 340)
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
        self._export_btn = tk.Button(buttons, text="导出记录", command=self._on_export)
        self._export_btn.pack(side="left", padx=(0, 8))
        self._import_btn = tk.Button(buttons, text="导入记录", command=self._on_import)
        self._import_btn.pack(side="left", padx=(0, 8))
        self._fix_btn = tk.Button(buttons, text="Codex修复", command=self._on_fix)
        self._fix_btn.pack(side="left", padx=(0, 8))
        tk.Button(buttons, text="退出", command=self.root.destroy).pack(side="right")

        self._log_box = scrolledtext.ScrolledText(
            self.root, state="disabled", font=("Menlo", 11), padx=8, pady=8, wrap="word"
        )
        self._log_box.pack(fill="both", expand=True, padx=14, pady=(8, 14))

        self._refresh_status()
        self._log("就绪。点击「立即同步」同步历史，或「诊断」查看当前状态。")
        self._log("「导出记录」/「导入记录」可备份与恢复聊天历史。")

    # -- helpers --------------------------------------------------------------
    def _log(self, text):
        self._log_box.configure(state="normal")
        self._log_box.insert("end", str(text) + "\n")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for btn in (self._sync_btn, self._diag_btn, self._install_btn, self._export_btn, self._import_btn, self._fix_btn):
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

    def _on_export(self):
        from tkinter import filedialog, messagebox
        default_name = export_import._default_export_name()
        dest = filedialog.asksaveasfilename(
            title="导出聊天记录到 zip",
            initialdir=str(paths.home() / "Desktop") if paths.home().joinpath("Desktop").exists() else str(paths.home()),
            initialfile=default_name,
            defaultextension=".zip",
            filetypes=[("ZIP archive", "*.zip"), ("All files", "*.*")],
        )
        if not dest:
            return

        include = self._ask_include_dialog("选择要导出的内容")
        if include is None:
            return

        def work():
            return export_import.export_zip(
                dest, home=self.home, include=include,
                progress=lambda msg, done, total: self.root.after(0, lambda m=msg: self._log(m)),
            )

        self._run_async(work, self._export_done, f"开始导出到 {dest}…")

    def _export_done(self, result, error):
        self._set_busy(False)
        if error:
            self._log(f"导出失败: {error}")
            return
        self._log(f"导出完成: {result.get('path')}")
        self._log(f"  打包文件数: {result.get('files')}")

    def _on_import(self):
        from tkinter import filedialog, messagebox
        src = filedialog.askopenfilename(
            title="选择要导入的 zip 文件",
            initialdir=str(paths.home() / "Desktop") if paths.home().joinpath("Desktop").exists() else str(paths.home()),
            filetypes=[("ZIP archive", "*.zip"), ("All files", "*.*")],
        )
        if not src:
            return

        valid, errors, warnings, manifest = export_import.validate_zip(src)
        if manifest:
            self._log(f"zip 信息: format={manifest.get('format')} version={manifest.get('version')} "
                      f"exported_at={manifest.get('exported_at')} files={manifest.get('files')}")
        for w in warnings:
            self._log(f"  警告: {w}")
        if not valid:
            for e in errors:
                self._log(f"  错误: {e}")
            messagebox.showerror("导入失败", "zip 结构校验失败，已拒绝导入。\n" + "\n".join(errors))
            return

        include = self._ask_include_dialog("选择要导入的内容")
        if include is None:
            return

        if not messagebox.askyesno("确认导入",
                                   "导入前会自动备份当前 Codex 状态。\n是否继续？"):
            return

        def work():
            return export_import.import_zip(
                src, home=self.home, include=include,
                progress=lambda msg, done, total: self.root.after(0, lambda m=msg: self._log(m)),
            )

        self._run_async(work, self._import_done, f"开始导入 {src}…")

    def _import_done(self, result, error):
        self._set_busy(False)
        if error:
            self._log(f"导入失败: {error}")
            return
        self._log(f"导入完成: {result.get('path')}")
        self._log(f"  解压文件数: {result.get('files')}")
        if result.get("backup_dir"):
            self._log(f"  备份目录: {result.get('backup_dir')}")
        for w in result.get("warnings") or []:
            self._log(f"  警告: {w}")

    def _on_fix(self):
        try:
            from tkinter import messagebox
        except Exception:
            messagebox = None
        if messagebox is not None:
            ok = messagebox.askyesno(
                "Codex 全方位检查与修复",
                "将检查并自动修复以下内容：\n"
                "• config.toml (model_provider / 历史保存设置)\n"
                "• cc-switch provider 数据库\n"
                "• rollout 元数据\n"
                "• state 数据库 (threads provider / cwd / 标题)\n"
                "• 侧边栏目录 / 全局状态\n"
                "• 模型窗口后缀\n"
                "• session_index 重建\n\n"
                "操作前会自动备份。是否继续？"
            )
            if not ok:
                return

        def work():
            def progress(weight, msg):
                self.root.after(0, lambda m=msg: self._log(f"  [{int(weight*100):3d}%] {m}"))
            return codex_fix.run_comprehensive_fix(self.home, progress_cb=progress)

        self._run_async(work, self._fix_done, "开始全方位检查与修复…")

    def _fix_done(self, report, error):
        self._set_busy(False)
        if error:
            self._log(f"修复失败: {error}")
            return
        for line in codex_fix.format_report_text(report).splitlines():
            self._log(line)

    def _ask_include_dialog(self, title):
        """Open a small checkbox dialog for choosing export/import entries.

        Returns a set of selected entries, or None if cancelled.
        """
        from tkinter import messagebox
        popup = self._tk.Toplevel(self.root)
        popup.title(title)
        popup.transient(self.root)
        popup.grab_set()
        popup.resizable(False, False)

        entries = [
            ("sessions", "sessions/ (会话文件)"),
            ("archived_sessions", "archived_sessions/ (归档会话)"),
            ("session_index.jsonl", "session_index.jsonl (会话索引)"),
            ("state_5.sqlite", "state_5.sqlite (状态数据库)"),
            ("config.toml", "config.toml (配置文件)"),
        ]
        vars_ = {}
        for key, label in entries:
            var = self._tk.BooleanVar(value=True)
            vars_[key] = var
            self._tk.Checkbutton(popup, text=label, variable=var, anchor="w").pack(
                fill="x", padx=14, pady=2
            )

        result = {"value": None}

        def on_ok():
            result["value"] = {k for k, v in vars_.items() if v.get()}
            popup.destroy()

        def on_cancel():
            popup.destroy()

        btns = self._tk.Frame(popup)
        btns.pack(fill="x", padx=14, pady=(8, 10))
        self._tk.Button(btns, text="确定", command=on_ok).pack(side="right", padx=(8, 0))
        self._tk.Button(btns, text="取消", command=on_cancel).pack(side="right")

        popup.update_idletasks()
        w, h = 360, 240
        x = (popup.winfo_screenwidth() - w) // 2
        y = (popup.winfo_screenheight() - h) // 2
        popup.geometry(f"{w}x{h}+{x}+{y}")

        self.root.wait_window(popup)
        return result["value"]


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
