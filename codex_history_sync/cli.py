"""Command-line interface.

Commands (run ``python main.py --help``):

    install      install autostart, back up state, start the watcher
    uninstall    stop the watcher and remove autostart (no data restored)
    restore      stop the tool; with --restore-latest-backup also restore data
    watch        run the watcher loop (used by autostart)
    run          manual sync with a progress/confirmation UI
    sync         headless sync only (no UI, no process management)
    export       export chat history to a zip file
    import       import chat history from a zip file (validates structure first)
    repair       repair local history indexes only (no config change)
    fix          comprehensive check-and-fix of the whole local Codex state
    doctor       read-only diagnosis of the local history state
    gui          open the GUI dashboard window (also the default with no args)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import (
    actions,
    autostart,
    backup_restore,
    core,
    doctor,
    codex_fix,
    export_import,
    paths,
    processes,
    repair,
    ui,
    watcher,
)


def _set_codex_home_env(codex_home):
    if codex_home:
        os.environ["CODEX_HOME"] = str(codex_home)


def stop_watcher():
    """Stop a running watcher, if any."""
    pids = set()
    pid_path = paths.watcher_pid_path()
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            if pid:
                pids.add(pid)
        except (ValueError, OSError):
            pass

    # POSIX fallback: a process whose command line references our own entry
    # point / module name *and* runs "watch".
    entry = str(autostart.entry_point())
    watch_pids = set(processes.find_pids_by_cmdline(("watch",)))
    own_pids = set(processes.find_pids_by_cmdline((entry, "codex-history-sync", "codex_history_sync")))
    pids.update(watch_pids & own_pids)

    for pid in pids:
        processes.stop_process(pid, graceful=False)
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass


def cmd_install(args):
    paths.codex_home().mkdir(parents=True, exist_ok=True)
    backup_dir = backup_restore.make_install_backup()
    autostart.install()
    autostart.start_now()
    print("Installed Codex cc-switch history sync.")
    print(f"Backup: {backup_dir}")
    return 0


def cmd_uninstall(args):
    stop_watcher()
    autostart.uninstall()
    print("Tool disabled. No Codex or cc-switch data was restored.")
    return 0


def cmd_restore(args):
    stop_watcher()
    autostart.uninstall()
    if not args.restore_latest_backup:
        print("Tool disabled. No Codex or cc-switch data was restored.")
        print("Run with --restore-latest-backup to restore the latest install backup.")
        return 0

    def confirm(latest):
        print(f"Restore backup '{latest}'? Close Codex and cc-switch first.")
        try:
            answer = input("This may overwrite local state. Type RESTORE to continue: ").strip()
        except EOFError:
            return False
        return answer == "RESTORE"

    try:
        restored, latest = backup_restore.restore_latest_backup(confirm_fn=confirm)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not restored:
        print("Restore cancelled.")
        return 0
    print(f"Restored latest install backup: {latest}")
    return 0


def cmd_watch(args):
    _set_codex_home_env(args.codex_home)
    return watcher.watch()


def cmd_run(args):
    _set_codex_home_env(args.codex_home)
    return ui.run_sync_flow(
        automatic=args.automatic,
        provider_id=args.provider_id,
        codex_home=args.codex_home,
    )


def cmd_sync(args):
    _set_codex_home_env(args.codex_home)
    if args.quiet:
        os.environ["CODEX_HISTORY_SYNC_QUIET"] = "1"
    else:
        os.environ.pop("CODEX_HISTORY_SYNC_QUIET", None)
    auth_cleared = actions.clear_auth_overrides()
    os.environ["CODEX_AUTH_OVERRIDES_CLEARED"] = "1" if auth_cleared else "0"
    core.run()
    return 0


def cmd_export(args):
    """Export chat history to a zip file."""
    _set_codex_home_env(args.codex_home)
    home = paths.codex_home()
    include = _parse_include(args.include)

    if args.output:
        dest = Path(args.output)
    else:
        dest = paths.home() / "Desktop" / export_import._default_export_name()
        if not paths.home().joinpath("Desktop").exists():
            dest = paths.home() / export_import._default_export_name()

    dest = dest.expanduser().resolve()

    def _progress(msg, done, total):
        if os.environ.get("CODEX_HISTORY_SYNC_QUIET") != "1":
            print(f"[{done}/{total}] {msg}")

    result = export_import.export_zip(dest, home=home, include=include, progress=_progress)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_import(args):
    """Import chat history from a zip file (validates structure first)."""
    _set_codex_home_env(args.codex_home)
    home = paths.codex_home()
    include = _parse_include(args.include)
    zip_path = Path(args.file).expanduser().resolve()

    valid, errors, warnings, manifest = export_import.validate_zip(zip_path)
    print(f"校验结果: {'通过' if valid else '失败'}")
    if manifest:
        print(f"  manifest format: {manifest.get('format')}  version: {manifest.get('version')}")
        print(f"  exported_at: {manifest.get('exported_at')}")
        print(f"  files: {manifest.get('files')}")
    for w in warnings:
        print(f"  警告: {w}")
    if not valid:
        for e in errors:
            print(f"  错误: {e}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("dry-run: 跳过实际导入。")
        return 0

    def _progress(msg, done, total):
        if os.environ.get("CODEX_HISTORY_SYNC_QUIET") != "1":
            print(f"[{done}/{total}] {msg}")

    result = export_import.import_zip(
        zip_path, home=home, include=include, progress=_progress, backup=args.backup
    )
    for w in result.get("warnings") or []:
        print(f"  警告: {w}")
    print(json.dumps({k: v for k, v in result.items() if k != "manifest"}, ensure_ascii=False, indent=2))
    return 0


def _parse_include(value):
    if not value:
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return set(parts) if parts else None


def cmd_repair(args):
    """Standalone history repair: rebuild the local history indexes without
    touching cc-switch provider config. Auto-detects the target provider from
    cc-switch unless --target-provider is given."""
    _set_codex_home_env(args.codex_home)
    home = paths.codex_home()
    result = repair.run_history_repair(
        home, target_provider=args.target_provider, dry_run=args.dry_run
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_fix(args):
    """Comprehensive check-and-fix of the whole local Codex state."""
    _set_codex_home_env(args.codex_home)
    home = paths.codex_home()

    def _progress(weight, msg):
        if os.environ.get("CODEX_HISTORY_SYNC_QUIET") != "1":
            print(f"[{int(weight*100):3d}%] {msg}")

    report = codex_fix.run_comprehensive_fix(home, progress_cb=_progress)
    print(codex_fix.format_report_text(report))
    return 0


def cmd_doctor(args):
    """Read-only diagnosis of the local history state (no writes)."""
    _set_codex_home_env(args.codex_home)
    report = doctor.run_diagnosis(paths.codex_home())
    print(doctor.format_report(report))
    return 0


def cmd_strip_tools(args):
    """Strip function_call/tool_result entries from a rollout jsonl."""
    _set_codex_home_env(args.codex_home)
    result = core.strip_tool_entries_from_rollout(Path(args.rollout).expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_gui(args):
    """Open the GUI dashboard window."""
    _set_codex_home_env(args.codex_home)
    return ui.run_gui()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="codex-history-sync",
        description="Cross-platform Codex session history sync for cc-switch provider switches.",
    )
    sub = parser.add_subparsers(dest="command")

    def add_home(p):
        p.add_argument("--codex-home", default=None, help="Codex home directory (default ~/.codex)")

    p = sub.add_parser("install", help="install autostart and start the watcher")
    add_home(p)
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="stop the watcher and remove autostart")
    add_home(p)
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("restore", help="disable the tool; optionally restore backup")
    add_home(p)
    p.add_argument("--restore-latest-backup", action="store_true",
                   help="restore the latest install backup after confirmation")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("watch", help="run the provider watcher loop")
    add_home(p)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("run", help="run a sync with progress/confirmation UI")
    add_home(p)
    p.add_argument("--automatic", action="store_true",
                   help="auto-close Codex (used by the watcher)")
    p.add_argument("--provider-id", default=None, help="target cc-switch provider id")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("sync", help="run the headless sync only")
    add_home(p)
    p.add_argument("--quiet", action="store_true", help="suppress the JSON result output")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("export", help="export chat history to a zip file")
    add_home(p)
    p.add_argument("-o", "--output", default=None,
                   help="output zip path (default: ~/Desktop/codex-history-<timestamp>.zip)")
    p.add_argument("--include", default=None,
                   help="comma-separated entries to export: sessions,archived_sessions,session_index.jsonl,state_5.sqlite,config.toml (default: all)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", help="import chat history from a zip file (validates structure first)")
    add_home(p)
    p.add_argument("file", help="zip file to import")
    p.add_argument("--include", default=None,
                   help="comma-separated entries to import (default: all present in zip)")
    p.add_argument("--dry-run", action="store_true",
                   help="validate only, do not write")
    p.add_argument("--no-backup", dest="backup", action="store_false",
                   help="skip backing up the current Codex home before import")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("repair", help="repair local history indexes only (no config change)")
    add_home(p)
    p.add_argument("--target-provider", default=None,
                   help="target model_provider to write (default: auto-detect from cc-switch)")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would change without writing")
    p.set_defaults(func=cmd_repair)

    p = sub.add_parser("fix", help="comprehensive check-and-fix of the whole local Codex state")
    add_home(p)
    p.set_defaults(func=cmd_fix)

    p = sub.add_parser("strip-tools", help="strip function_call/tool_result entries from a rollout to fix infinite retry")
    add_home(p)
    p.add_argument("rollout", help="path to the rollout .jsonl file")
    p.set_defaults(func=cmd_strip_tools)

    p = sub.add_parser("doctor", help="read-only diagnosis of the local history state")
    add_home(p)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("gui", help="open the GUI dashboard window")
    add_home(p)
    p.set_defaults(func=cmd_gui)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        return ui.run_gui(parser)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
