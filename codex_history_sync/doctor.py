"""Read-only diagnosis of the local Codex history state (coupled to cc-switch).

Reports what a repair would change without writing anything. Used by the
``doctor`` command to inspect the current state before running a repair.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import core, paths, repair


def _threads_total(db_paths):
    total = 0
    for db_path in db_paths:
        if not db_path.exists():
            continue
        cols = repair.table_columns(db_path, "threads")
        if not cols or "id" not in cols:
            continue
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
            try:
                total += con.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
            finally:
                con.close()
        except Exception:
            continue
    return total


def _live_thread_ids(home):
    """Thread ids that exist in the DBs or in rollout files."""
    ids = set()
    for db_path in repair.all_db_paths(home):
        if not db_path.exists():
            continue
        cols = repair.table_columns(db_path, "threads")
        if not cols or "id" not in cols:
            continue
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
            try:
                ids.update(r[0] for r in con.execute("SELECT id FROM threads WHERE COALESCE(id,'') <> ''").fetchall())
            finally:
                con.close()
        except Exception:
            continue
    repair._bind_core(home)
    try:
        for path, _archived in core.iter_rollouts():
            ids.add(core.rollout_id_from_name(path))
    except Exception:
        pass
    return ids


def _rollout_total(home):
    repair._bind_core(home)
    try:
        return sum(1 for _ in core.iter_rollouts())
    except Exception:
        return 0


def _index_stats(home, live_ids):
    index_path = home / "session_index.jsonl"
    if not index_path.exists():
        return 0, 0
    try:
        lines = index_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 0, 0
    total = 0
    ghosts = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            total += 1
            continue
        total += 1
        rid = obj.get("id")
        if rid and rid not in live_ids:
            ghosts += 1
    return total, ghosts


def run_diagnosis(home=None):
    home = Path(home or paths.codex_home())
    target_provider, provider_id, is_official = repair.detect_target_provider(home)
    db_paths = repair.all_db_paths(home)
    excluded = repair._subagent_thread_ids(db_paths) | repair.load_projectless_thread_ids(home)

    scanned_suffix, update_suffix = repair._scan_thread_model_suffixes(home)
    rollout_total = _rollout_total(home)
    rollout_mismatch = repair._scan_rollout_provider_mismatches(home, target_provider)
    threads_total = _threads_total(db_paths)
    provider_rows = repair._count_threads_provider(home, target_provider, excluded)
    catalog_inserted, catalog_removed = repair._count_catalog_repairs(home, target_provider, excluded)
    global_changed = repair.global_state_changed_keys(home)
    live_ids = _live_thread_ids(home)
    index_total, index_ghosts = _index_stats(home, live_ids)

    return {
        "codex_home": str(home),
        "cc_switch_provider_id": provider_id,
        "target_model_provider": target_provider,
        "is_official": is_official,
        "databases": len(db_paths),
        "threads_total": threads_total,
        "threads_wrong_provider": provider_rows,
        "rollout_files": rollout_total,
        "rollout_wrong_provider": rollout_mismatch,
        "catalog_missing_rows": catalog_inserted,
        "catalog_subagent_rows": catalog_removed,
        "session_index_entries": index_total,
        "session_index_ghosts": index_ghosts,
        "global_state_changed_keys": global_changed,
        "model_suffix_rows": scanned_suffix,
        "model_suffix_to_fix": update_suffix,
        "excluded_thread_ids": len(excluded),
    }


def format_report(d):
    provider_name = "官方 (openai)" if d["is_official"] else "中转 (ccs)"
    lines = [
        f"Codex home:      {d['codex_home']}",
        f"cc-switch 当前 provider: {d['cc_switch_provider_id'] or '(未检测到)'}",
        f"目标 model_provider:     {d['target_model_provider']}  [{provider_name}]",
        f"会话数据库数量:          {d['databases']}",
        f"线程总数:                {d['threads_total']}",
        f"  ├─ provider 不一致:    {d['threads_wrong_provider']}",
        f"  └─ 排除的子代理线程:   {d['excluded_thread_ids']}",
        f"rollout 文件:            {d['rollout_files']}",
        f"  └─ provider 不一致:    {d['rollout_wrong_provider']}",
        f"侧边栏目录:              缺 {d['catalog_missing_rows']} 行 / 待删子代理 {d['catalog_subagent_rows']} 行",
        f"session_index:          {d['session_index_entries']} 条",
        f"  └─ 幽灵条目(无来源):   {d['session_index_ghosts']}",
        f"global-state 待归一 key: {d['global_state_changed_keys']}",
        f"模型窗口后缀:            共 {d['model_suffix_rows']} 处 / 待清理 {d['model_suffix_to_fix']}",
    ]
    needs_repair = (
        d["threads_wrong_provider"]
        or d["rollout_wrong_provider"]
        or d["catalog_missing_rows"]
        or d["catalog_subagent_rows"]
        or d["session_index_ghosts"]
        or d["global_state_changed_keys"]
        or d["model_suffix_to_fix"]
    )
    lines.append("")
    lines.append("结论: 需要修复" if needs_repair else "结论: 无需修复（已一致）")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_report(run_diagnosis()))
