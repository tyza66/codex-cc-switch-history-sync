"""Comprehensive Codex local-state check and auto-fix.

Runs the same full repair pipeline as ``core.run`` plus a pre-flight diagnosis,
and returns a structured, human-readable report that the GUI can render.
Covers:

- ``config.toml`` (model_provider / model / sandbox / history settings)
- ``cc-switch`` provider database config
- rollout JSONL session_meta provider + id
- state databases (threads provider / cwd / has_user_event)
- sidebar (local_thread_catalog)
- global state normalization
- model window-size suffixes (threads + logs)
- session_index rebuild + ghost cleanup
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import core, doctor, paths, repair


def _bind_core(home):
    home = Path(home)
    core.CODEX_HOME = home
    core.CC_SWITCH_DB = paths.cc_switch_db()
    core.CC_SWITCH_SETTINGS = paths.cc_switch_settings()
    core.BACKUP_ROOT = home / "history-sync-backups"
    core.OFFICIAL_PROVIDER_IDS = None


def _section(title):
    return {"title": title, "items": [], "fixed": 0}


def _add(section, label, before, after=None, fixed=False):
    section["items"].append({
        "label": label,
        "before": before,
        "after": after,
        "fixed": fixed,
    })
    if fixed:
        section["fixed"] += 1


def _check_config_toml(home, target_provider):
    """Check and normalize ``config.toml``."""
    section = _section("config.toml")
    path = home / "config.toml"
    if not path.exists():
        _add(section, "文件不存在", "—")
        return section, False

    old = path.read_text(encoding="utf-8")
    old_provider = core.active_provider_name(old)
    old_disable = "disable_response_storage" in old
    old_save = "history_save" in old

    new = core.normalize_codex_config(old, target_provider=target_provider)
    new = core.sync_model_defaults(new, core.current_model_defaults())
    changed = new != old
    if changed:
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, path)

    new_provider = core.active_provider_name(new)
    _add(section, "model_provider", old_provider or "(未设置)",
         new_provider or "(未设置)", fixed=(old_provider != new_provider))
    _add(section, "disable_response_storage",
         "存在(将移除)" if old_disable else "无",
         "已移除" if (old_disable and "disable_response_storage" not in new) else "无",
         fixed=old_disable and "disable_response_storage" not in new)
    _add(section, "history_save",
         "未配置" if not old_save else "已配置",
         "已确保" if "history_save" in new else "未配置",
         fixed=not old_save)
    return section, changed


def _check_cc_switch_db(home, current_provider_id):
    """Check and normalize cc-switch provider config DB."""
    section = _section("cc-switch 数据库")
    db = paths.cc_switch_db()
    if not db.exists():
        _add(section, "cc-switch.db 不存在", "—")
        return section, False

    result = core.normalize_cc_switch_db(current_provider_id, core.current_model_defaults())
    _add(section, "provider 配置数", str(result["providers"]))
    _add(section, "已修改配置", str(result["changed"]),
         fixed=result["changed"] > 0)
    return section, result["changed"] > 0


def _check_rollouts(home, target_provider, rewrite_provider):
    """Check and normalize rollout JSONL metadata."""
    section = _section("rollout 元数据")
    mismatches = repair._scan_rollout_provider_mismatches(home, target_provider)
    _add(section, "provider 不一致", str(mismatches),
         fixed=mismatches > 0)

    backup_dir = core.make_backup()
    result = repair.rewrite_rollout_metadata(
        home, target_provider, rewrite_provider, dry_run=False
    )
    _add(section, "首行已重写", str(result["changed"]),
         fixed=result["changed"] > 0)
    if result.get("skipped_locked"):
        _add(section, "被占用跳过", str(result["skipped_locked"]))
    return section, result["changed"] > 0


def _check_state_and_index(home, target_provider, rewrite_provider):
    """Check and repair state DBs + session index."""
    section = _section("state 数据库 / 会话索引")
    index_titles = core.load_current_index_titles()

    db_paths = repair.all_db_paths(home)
    excluded = repair._subagent_thread_ids(db_paths) | repair.load_projectless_thread_ids(home)
    _add(section, "排除子代理线程", str(len(excluded)))

    before_provider = repair._count_threads_provider(home, target_provider, excluded)
    _add(section, "threads provider 不一致", str(before_provider),
         fixed=before_provider > 0)

    state = core.repair_state_db(target_provider, rewrite_provider, index_titles)
    _add(section, "provider 已更新", str(state.get("updated", 0)),
         fixed=state.get("updated", 0) > 0)
    _add(section, "标题已更新", str(state.get("title_updated", 0)),
         fixed=state.get("title_updated", 0) > 0)
    _add(section, "新插入线程", str(state.get("inserted", 0)),
         fixed=state.get("inserted", 0) > 0)

    index = core.rebuild_session_index(index_titles)
    _add(section, "session_index 条目", str(index["index_entries"]),
         fixed=index["index_entries"] > 0)

    cwd_by_id, user_event_ids = repair._rollout_thread_metadata(home)
    meta = repair.update_threads_metadata(home, cwd_by_id, user_event_ids, excluded)
    _add(section, "cwd 已修复", str(meta["cwd_rows"]),
         fixed=meta["cwd_rows"] > 0)
    _add(section, "has_user_event 已修复", str(meta["has_user_event_rows"]),
         fixed=meta["has_user_event_rows"] > 0)

    return section, True


def _check_catalog_and_global(home, target_provider):
    """Check sidebar catalogue + global state."""
    section = _section("侧边栏目录 / 全局状态")
    db_paths = repair.all_db_paths(home)
    excluded = repair._subagent_thread_ids(db_paths) | repair.load_projectless_thread_ids(home)

    cat_ins, cat_rem = repair._count_catalog_repairs(home, target_provider, excluded)
    cat = repair.repair_local_thread_catalog(home, target_provider, excluded)
    _add(section, "目录已插入", str(cat["inserted"]),
         fixed=cat["inserted"] > 0)
    _add(section, "子代理已移除", str(cat["removed"]),
         fixed=cat["removed"] > 0)

    g_changed = repair.global_state_changed_keys(home)
    gs = repair.sync_global_state(home)
    _add(section, "global-state 待归一 key", str(g_changed),
         fixed=gs > 0)
    return section, cat["inserted"] + cat["removed"] + gs > 0


def _check_model_suffixes(home):
    """Check and clean model window-size suffixes."""
    section = _section("模型窗口后缀")
    scanned, to_fix = repair._scan_thread_model_suffixes(home)
    suffix = repair.sanitize_thread_model_suffixes(home)
    logs = repair.sanitize_logs_model_suffixes(home)
    _add(section, "threads 后缀共", f"{scanned} 处 / 待清理 {to_fix}",
         fixed=suffix["updated"] > 0)
    _add(section, "threads 已清理", str(suffix["updated"]),
         fixed=suffix["updated"] > 0)
    _add(section, "logs 已清理", str(logs),
         fixed=logs > 0)
    return section, suffix["updated"] + logs > 0


def _check_rollout_first_lines(home):
    """Ensure every rollout JSONL starts with a session_meta line."""
    section = _section("rollout 首行检查")
    result = core.repair_rollout_first_lines()
    _add(section, "扫描文件", str(result["scanned"]))
    _add(section, "首行修复", str(result["repaired"]),
         fixed=result["repaired"] > 0)
    _add(section, "首行补全", str(result["prepended"]),
         fixed=result["prepended"] > 0)
    if result["errors"]:
        _add(section, "处理失败", str(result["errors"]))
    return section, result["repaired"] + result["prepended"] > 0


def _clear_skipped_rollouts(home):
    """Clear the rollout_migration_skipped_rollouts table so Codex Desktop
    will re-read repaired rollout files."""
    section = _section("跳过列表清理")
    state_path = home / "state_5.sqlite"
    if not state_path.exists():
        _add(section, "state_5.sqlite 不存在", "—")
        return section, False
    import sqlite3
    con = sqlite3.connect(str(state_path), timeout=10)
    con.execute("PRAGMA busy_timeout=10000")
    try:
        tables = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table'").fetchall()]
        if "rollout_migration_skipped_rollouts" not in tables:
            _add(section, "跳过列表表不存在", "—")
            return section, False
        before = con.execute(
            "select count(*) from rollout_migration_skipped_rollouts").fetchone()[0]
        _add(section, "跳过条目", str(before))
        if before > 0:
            con.execute("delete from rollout_migration_skipped_rollouts")
            con.commit()
            after = con.execute(
                "select count(*) from rollout_migration_skipped_rollouts").fetchone()[0]
            _add(section, "已清除", f"{before} → {after}", fixed=True)
        else:
            _add(section, "无需清除", "0")
        return section, before > 0
    except Exception as exc:
        _add(section, "操作失败", str(exc))
        return section, False
    finally:
        con.close()


def run_comprehensive_fix(home=None, progress_cb=None):
    """Run a comprehensive check-and-fix of the whole local Codex state.

    Returns a structured report with a per-section breakdown. Each section has
    a ``title``, a list of ``items`` (label / before / after / fixed), and a
    ``fixed`` count. The top-level report also carries ``summary`` (a short
    one-line conclusion) and ``anything_fixed``.
    """
    home = Path(home or paths.codex_home())
    _bind_core(home)

    report = {
        "home": str(home),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sections": [],
        "anything_fixed": False,
        "summary": "",
    }

    def step(weight, msg):
        if progress_cb:
            try:
                progress_cb(weight, msg)
            except Exception:
                pass

    step(0.02, "检测 provider")
    current_provider_id = core.current_codex_provider_id()
    target_provider = core.target_provider_for_provider_id(current_provider_id)
    report["current_provider_id"] = current_provider_id
    report["target_model_provider"] = target_provider
    report["is_official"] = target_provider == core.OFFICIAL_MODEL_PROVIDER

    step(0.1, "检查 config.toml")
    sec, _ = _check_config_toml(home, target_provider)
    report["sections"].append(sec)

    step(0.2, "检查 cc-switch 数据库")
    sec, _ = _check_cc_switch_db(home, current_provider_id)
    report["sections"].append(sec)

    step(0.35, "修复 rollout 元数据")
    rewrite = target_provider == core.TRANSIT_MODEL_PROVIDER
    sec, _ = _check_rollouts(home, target_provider, rewrite)
    report["sections"].append(sec)

    step(0.55, "修复 state 数据库 / 会话索引")
    sec, _ = _check_state_and_index(home, target_provider, rewrite)
    report["sections"].append(sec)

    step(0.65, "检查 rollout 首行")
    sec, _ = _check_rollout_first_lines(home)
    report["sections"].append(sec)

    step(0.72, "清理跳过列表")
    sec, _ = _clear_skipped_rollouts(home)
    report["sections"].append(sec)

    step(0.80, "修复侧边栏 / 全局状态")
    sec, _ = _check_catalog_and_global(home, target_provider)
    report["sections"].append(sec)

    step(0.90, "清理模型后缀")
    sec, _ = _check_model_suffixes(home)
    report["sections"].append(sec)

    step(1.0, "完成")

    total_fixed = sum(s["fixed"] for s in report["sections"])
    report["anything_fixed"] = total_fixed > 0
    report["total_fixed"] = total_fixed
    if total_fixed == 0:
        report["summary"] = "✅ 全方位检查完成，一切正常，无需修复。"
    else:
        report["summary"] = f"✅ 全方位检查完成，共自动修复 {total_fixed} 处。"

    return report


def format_report_text(report):
    """Render a comprehensive-fix report to plain text."""
    lines = [f"Codex 全方位检查与修复 — {report['home']}"]
    lines.append(f"时间: {report['started_at']}")
    lines.append(f"目标 provider: {report.get('target_model_provider', '?')}  "
                 f"{'(官方)' if report.get('is_official') else '(中转)'}")
    lines.append("")

    for sec in report["sections"]:
        lines.append(f"【{sec['title']}】  修复 {sec['fixed']} 项")
        for item in sec["items"]:
            mark = "  ✓" if item["fixed"] else "   "
            line = f"{mark} {item['label']}: {item['before']}"
            if item["after"] is not None and item["after"] != item["before"]:
                line += f"  →  {item['after']}"
            lines.append(line)
        lines.append("")

    lines.append(report["summary"])
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    report = run_comprehensive_fix()
    print(format_report_text(report))
    sys.exit(0)
