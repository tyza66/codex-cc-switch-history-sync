"""Complete local history-repair toolchain, coupled to cc-switch.

Ported from CodexPlusPlus's provider-sync pipeline plus the original cc-switch
coupling. Repairs the full local Codex history state:

- ``~/.codex/sqlite/*.db`` + legacy ``state_5.sqlite`` databases
- ``threads.model_provider`` / ``cwd`` / ``has_user_event`` columns
- ``local_thread_catalog`` sidebar catalogue rows
- rollout JSONL ``session_meta`` provider/id
- ``session_index.jsonl`` (rebuild + ghost cleanup)
- ``.codex-global-state.json`` workspace/thread state
- model window-size suffixes (``gpt-5.5[1M]`` -> ``gpt-5.5``)

Every step is defensive and idempotent. ``run_history_repair`` supports a
``dry_run`` mode that reports what would change without writing.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

from . import core, paths

# Tables that identify a session database (newer Codex layout).
_SESSION_TABLES = ("threads", "automation_runs", "inbox_items")
# Tables that identify a thread-reference database (superset).
_REFERENCE_TABLES = (
    "threads",
    "local_thread_catalog",
    "automation_runs",
    "inbox_items",
    "sessions",
    "messages",
    "thread_dynamic_tools",
    "thread_goals",
    "thread_spawn_edges",
    "stage1_outputs",
    "agent_job_items",
)

_DB_EXTENSIONS = (".db", ".sqlite", ".sqlite3")


# --------------------------------------------------------------------------- #
# Provider detection (cc-switch coupled)
# --------------------------------------------------------------------------- #

def current_cc_switch_provider_id():
    settings = paths.cc_switch_settings()
    if settings.exists():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
            value = data.get("currentProviderCodex")
            if isinstance(value, str) and value:
                return value
        except Exception:
            pass
    db = paths.cc_switch_db()
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            try:
                row = con.execute(
                    "select id from providers where app_type='codex' and is_current=1 limit 1"
                ).fetchone()
            finally:
                con.close()
            if row and row[0]:
                return str(row[0])
        except Exception:
            pass
    return None


def is_official_provider_id(provider_id):
    from . import actions
    return actions.is_official_provider(provider_id)


def detect_target_provider(home=None):
    """Resolve the target ``model_provider`` from cc-switch, falling back to
    ``config.toml`` and finally ``ccs``. Returns ``(target_provider, provider_id,
    is_official)``."""
    provider_id = current_cc_switch_provider_id()
    if provider_id:
        if is_official_provider_id(provider_id):
            return core.OFFICIAL_MODEL_PROVIDER, provider_id, True
        return core.TRANSIT_MODEL_PROVIDER, provider_id, False

    config = (home or paths.codex_home()) / "config.toml"
    if config.exists():
        try:
            text = config.read_text(encoding="utf-8")
            m = re.search(r'(?m)^\s*model_provider\s*=\s*"([^"]+)"\s*$', text)
            if m and m.group(1):
                return m.group(1), None, m.group(1) == core.OFFICIAL_MODEL_PROVIDER
        except Exception:
            pass
    return core.TRANSIT_MODEL_PROVIDER, None, False


# --------------------------------------------------------------------------- #
# Database discovery
# --------------------------------------------------------------------------- #

def _sqlite_dir(home):
    return home / "sqlite"


def _db_candidates(home):
    sqlite_dir = _sqlite_dir(home)
    out = []
    if sqlite_dir.is_dir():
        try:
            for entry in sqlite_dir.iterdir():
                if entry.is_file() and entry.suffix.lower() in _DB_EXTENSIONS:
                    out.append(entry)
        except OSError:
            pass
    out.sort(key=lambda p: (p.name != "codex-dev.db", p.name))
    return out


def has_table(db_path, table):
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        try:
            row = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
            ).fetchone()
        finally:
            con.close()
        return row is not None
    except Exception:
        return False


def table_columns(db_path, table):
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        try:
            rows = con.execute(
                f'PRAGMA table_info("{table.replace(chr(34), chr(34) + chr(34))}")'
            ).fetchall()
        finally:
            con.close()
        return {r[1] for r in rows}
    except Exception:
        return None


def _has_any_table(db_path, tables):
    return any(has_table(db_path, t) for t in tables)


def session_db_paths(home):
    paths_list = [p for p in _db_candidates(home) if _has_any_table(p, _SESSION_TABLES)]
    legacy = home / "state_5.sqlite"
    if legacy.exists() and legacy not in paths_list:
        paths_list.append(legacy)
    return paths_list


def reference_db_paths(home):
    paths_list = [p for p in _db_candidates(home) if _has_any_table(p, _REFERENCE_TABLES)]
    legacy = home / "state_5.sqlite"
    if legacy.exists() and legacy not in paths_list:
        paths_list.append(legacy)
    return paths_list


def all_db_paths(home):
    seen = set()
    out = []
    for p in session_db_paths(home) + reference_db_paths(home):
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
# Model suffix sanitization
# --------------------------------------------------------------------------- #

def parse_window_token(token):
    token = (token or "").strip()
    if not token:
        return None
    last = token[-1]
    if last in "Kk":
        num, mult = token[:-1], 1000
    elif last in "Mm":
        num, mult = token[:-1], 1000000
    else:
        num, mult = token, 1
    num = num.strip()
    if not num.isdigit():
        return None
    value = int(num) * mult
    return value if value > 0 else None


def parse_model_suffix(raw):
    raw = (raw or "").strip()
    close = raw.rfind("]")
    if close == len(raw) - 1:
        open_ = raw[:close].rfind("[")
        if open_ != -1:
            inner = raw[open_ + 1:close].strip()
            slug = raw[:open_].strip()
            window = parse_window_token(inner)
            if slug and window is not None:
                return slug, window
    return raw, None


def _scan_thread_model_suffixes(home):
    scanned = 0
    update = 0
    for db_path in session_db_paths(home):
        if not db_path.exists():
            continue
        columns = table_columns(db_path, "threads")
        if not columns or "model" not in columns:
            continue
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
            try:
                rows = con.execute("SELECT id, model FROM threads WHERE model LIKE '%[%'").fetchall()
            finally:
                con.close()
        except Exception:
            continue
        scanned += len(rows)
        for _tid, model in rows:
            if isinstance(model, str):
                slug, window = parse_model_suffix(model)
                if window is not None and slug != model:
                    update += 1
    return scanned, update


def sanitize_thread_model_suffixes(home, dry_run=False):
    scanned = 0
    updated = 0
    for db_path in session_db_paths(home):
        if not db_path.exists():
            continue
        columns = table_columns(db_path, "threads")
        if not columns or "model" not in columns:
            continue
        try:
            con = sqlite3.connect(str(db_path), timeout=10)
            con.execute("PRAGMA busy_timeout=10000")
            try:
                rows = con.execute("SELECT id, model FROM threads WHERE model LIKE '%[%'").fetchall()
                scanned += len(rows)
                for thread_id, model in rows:
                    if not isinstance(model, str):
                        continue
                    slug, window = parse_model_suffix(model)
                    if window is not None and slug != model:
                        if not dry_run:
                            con.execute("UPDATE threads SET model=? WHERE id=?", (slug, thread_id))
                        updated += 1
                if not dry_run:
                    con.commit()
            finally:
                con.close()
        except Exception:
            continue
    return {"scanned": scanned, "updated": updated}


def _sanitize_text_suffixes(text):
    pattern = re.compile(r"([\w./:_-]+)\[\d+[KkMm]\]")
    return pattern.sub(r"\1", text or "")


def sanitize_logs_model_suffixes(home, dry_run=False):
    db_path = home / "logs_2.sqlite"
    if not db_path.exists():
        return 0
    columns = table_columns(db_path, "logs")
    if not columns or "feedback_log_body" not in columns:
        return 0
    updated = 0
    try:
        con = sqlite3.connect(str(db_path), timeout=10)
        try:
            rows = con.execute(
                "SELECT rowid, feedback_log_body FROM logs WHERE feedback_log_body LIKE '%[%'"
            ).fetchall()
            for rowid, body in rows:
                if not isinstance(body, str):
                    continue
                sanitized = _sanitize_text_suffixes(body)
                if sanitized != body:
                    if not dry_run:
                        con.execute(
                            "UPDATE logs SET feedback_log_body=? WHERE rowid=?",
                            (sanitized, rowid),
                        )
                    updated += 1
            if not dry_run:
                con.commit()
        finally:
            con.close()
    except Exception:
        pass
    return updated


# --------------------------------------------------------------------------- #
# Global state normalization
# --------------------------------------------------------------------------- #

def _norm_path(value):
    value = value.strip()
    if not value:
        return None
    return os.path.normpath(value)


def _dedupe_paths(values):
    seen = set()
    out = []
    for value in values:
        norm = _norm_path(value)
        if norm is None:
            continue
        key = os.path.normcase(norm) if paths.is_windows() else norm
        if key not in seen:
            seen.add(key)
            out.append(norm)
    return out


def _path_array(value):
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    if isinstance(value, str):
        return [value]
    return []


def _normalized_global_state(state):
    next_state = {}
    for key in ("electron-saved-workspace-roots", "project-order"):
        if key in state:
            next_state[key] = _dedupe_paths(_path_array(state.get(key)))
    if "active-workspace-roots" in state:
        value = state["active-workspace-roots"]
        normalized = _dedupe_paths(_path_array(value))
        next_state["active-workspace-roots"] = normalized if isinstance(value, list) else (normalized[0] if normalized else value)
    if "electron-workspace-root-labels" in state and isinstance(state.get("electron-workspace-root-labels"), dict):
        next_state["electron-workspace-root-labels"] = {
            _norm_path(k) or k: v for k, v in state["electron-workspace-root-labels"].items()
        }
    if "open-in-target-preferences" in state and isinstance(state.get("open-in-target-preferences"), dict):
        prefs = dict(state["open-in-target-preferences"])
        per_path = prefs.get("perPath")
        if isinstance(per_path, dict):
            prefs["perPath"] = {_norm_path(k) or k: v for k, v in per_path.items()}
        next_state["open-in-target-preferences"] = prefs
    return next_state


def global_state_changed_keys(home):
    path = home / ".codex-global-state.json"
    if not path.exists():
        return 0
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(state, dict):
        return 0
    next_state = _normalized_global_state(state)
    return sum(1 for k, v in next_state.items() if state.get(k) != v)


def sync_global_state(home, dry_run=False):
    path = home / ".codex-global-state.json"
    if not path.exists():
        return 0
    try:
        original_text = path.read_text(encoding="utf-8")
        state = json.loads(original_text)
    except Exception:
        return 0
    if not isinstance(state, dict):
        return 0

    next_state = _normalized_global_state(state)
    changed = [k for k, v in next_state.items() if state.get(k) != v]
    if not changed or dry_run:
        return len(changed)

    state.update(next_state)
    new_text = json.dumps(state, ensure_ascii=False, indent=2)
    try:
        if path.read_text(encoding="utf-8") != original_text:
            return 0
    except Exception:
        return 0
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    try:
        (home / ".codex-global-state.json.bak").write_text(new_text, encoding="utf-8")
    except Exception:
        pass
    return len(changed)


def load_projectless_thread_ids(home):
    path = home / ".codex-global-state.json"
    ids = set()
    if not path.exists():
        return ids
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ids
    items = state.get("projectless-thread-ids") if isinstance(state, dict) else None
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str) and item.strip():
                ids.add(item.strip())
    return ids


# --------------------------------------------------------------------------- #
# threads table repair
# --------------------------------------------------------------------------- #

def _subagent_thread_ids(db_paths):
    ids = set()
    for db_path in db_paths:
        if not db_path.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
            try:
                cols = table_columns(db_path, "thread_spawn_edges") or set()
                if "child_thread_id" in cols:
                    ids.update(r[0] for r in con.execute(
                        "SELECT child_thread_id FROM thread_spawn_edges WHERE COALESCE(child_thread_id,'') <> ''"
                    ).fetchall())
                cols = table_columns(db_path, "agent_job_items") or set()
                if "assigned_thread_id" in cols:
                    ids.update(r[0] for r in con.execute(
                        "SELECT assigned_thread_id FROM agent_job_items WHERE COALESCE(assigned_thread_id,'') <> ''"
                    ).fetchall())
            finally:
                con.close()
        except Exception:
            continue
    return ids


def _count_threads_provider(home, target_provider, excluded):
    total = 0
    for db_path in session_db_paths(home):
        if not db_path.exists():
            continue
        columns = table_columns(db_path, "threads")
        if not columns or "id" not in columns or "model_provider" not in columns:
            continue
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
            try:
                rows = con.execute(
                    "SELECT id FROM threads WHERE COALESCE(id,'') <> '' AND COALESCE(model_provider,'') <> ?",
                    (target_provider,),
                ).fetchall()
            finally:
                con.close()
        except Exception:
            continue
        total += sum(1 for (tid,) in rows if tid not in excluded)
    return total


def update_threads_provider(home, target_provider, excluded_thread_ids=None, dry_run=False):
    excluded = excluded_thread_ids or set()
    total = 0
    for db_path in session_db_paths(home):
        if not db_path.exists():
            continue
        columns = table_columns(db_path, "threads")
        if not columns or "id" not in columns:
            continue
        try:
            con = sqlite3.connect(str(db_path), timeout=10)
            con.execute("PRAGMA busy_timeout=10000")
            try:
                if "model_provider" in columns:
                    rows = con.execute(
                        "SELECT id FROM threads WHERE COALESCE(id,'') <> '' AND COALESCE(model_provider,'') <> ?",
                        (target_provider,),
                    ).fetchall()
                    for (thread_id,) in rows:
                        if thread_id in excluded:
                            continue
                        if not dry_run:
                            cur = con.execute(
                                "UPDATE threads SET model_provider=? WHERE id=? AND COALESCE(model_provider,'') <> ?",
                                (target_provider, thread_id, target_provider),
                            )
                            total += cur.rowcount
                        else:
                            total += 1
                catalog_cols = table_columns(db_path, "local_thread_catalog")
                if catalog_cols and "thread_id" in catalog_cols and "model_provider" in catalog_cols:
                    rows = con.execute(
                        "SELECT thread_id FROM local_thread_catalog WHERE COALESCE(thread_id,'') <> '' AND COALESCE(model_provider,'') <> ?",
                        (target_provider,),
                    ).fetchall()
                    for (thread_id,) in rows:
                        if thread_id in excluded:
                            continue
                        if not dry_run:
                            cur = con.execute(
                                "UPDATE local_thread_catalog SET model_provider=? WHERE thread_id=? AND COALESCE(model_provider,'') <> ?",
                                (target_provider, thread_id, target_provider),
                            )
                            total += cur.rowcount
                        else:
                            total += 1
                if not dry_run:
                    con.commit()
            finally:
                con.close()
        except Exception:
            continue
    return total


def _rollout_thread_metadata(home):
    """Scan rollouts for per-thread cwd and whether a user event exists.

    The thread id is taken from ``session_meta.payload.id`` (the authoritative
    id that matches ``threads.id``), falling back to the filename-derived id.
    """
    cwd_by_id = {}
    user_event_ids = set()
    try:
        core.CODEX_HOME = home
        for path, _archived in core.iter_rollouts():
            meta_line = core.read_session_meta_line(path)
            payload = (meta_line[2].get("payload") or {}) if meta_line else {}
            info = core.parse_rollout(path)
            cwd = info.get("cwd") or payload.get("cwd")
            has_user_event = (
                isinstance(info.get("title"), str)
                and info["title"]
                and info["title"] != "Untitled session"
            )
            # Key on both the payload id and the filename-derived id: they are
            # normally identical, but after a prior repair the payload id may
            # already have been rewritten to the filename form.
            ids = set()
            pid = payload.get("id")
            if core.is_rollout_uuid(pid):
                ids.add(pid)
            fid = core.rollout_id_from_name(path)
            if isinstance(fid, str) and fid:
                ids.add(fid)
            for rid in ids:
                if isinstance(cwd, str) and cwd:
                    cwd_by_id[rid] = cwd
                if has_user_event:
                    user_event_ids.add(rid)
    except Exception:
        pass
    return cwd_by_id, user_event_ids


def update_threads_metadata(home, cwd_by_id, user_event_ids, excluded_thread_ids=None, dry_run=False):
    """Repair ``threads.cwd`` and ``threads.has_user_event`` from rollout data."""
    excluded = excluded_thread_ids or set()
    cwd_rows = 0
    event_rows = 0
    for db_path in session_db_paths(home):
        if not db_path.exists():
            continue
        columns = table_columns(db_path, "threads")
        if not columns or "id" not in columns:
            continue
        try:
            con = sqlite3.connect(str(db_path), timeout=10)
            con.execute("PRAGMA busy_timeout=10000")
            try:
                if "cwd" in columns:
                    for rid, cwd in cwd_by_id.items():
                        if rid in excluded:
                            continue
                        if not dry_run:
                            cur = con.execute(
                                "UPDATE threads SET cwd=? WHERE id=? AND COALESCE(cwd,'') <> ?",
                                (cwd, rid, cwd),
                            )
                            cwd_rows += cur.rowcount
                        else:
                            exists = con.execute(
                                "SELECT 1 FROM threads WHERE id=? AND COALESCE(cwd,'') <> ? LIMIT 1",
                                (rid, cwd),
                            ).fetchone()
                            if exists:
                                cwd_rows += 1
                if "has_user_event" in columns:
                    for rid in user_event_ids:
                        if rid in excluded:
                            continue
                        if not dry_run:
                            cur = con.execute(
                                "UPDATE threads SET has_user_event=1 WHERE id=? AND COALESCE(has_user_event,0) <> 1",
                                (rid,),
                            )
                            event_rows += cur.rowcount
                        else:
                            exists = con.execute(
                                "SELECT 1 FROM threads WHERE id=? AND COALESCE(has_user_event,0) <> 1 LIMIT 1",
                                (rid,),
                            ).fetchone()
                            if exists:
                                event_rows += 1
                if not dry_run:
                    con.commit()
            finally:
                con.close()
        except Exception:
            continue
    return {"cwd_rows": cwd_rows, "has_user_event_rows": event_rows}


def _coalesce_expr(columns, names, fallback):
    picks = [n for n in names if n in columns]
    if not picks:
        return fallback
    return "COALESCE(" + ", ".join(picks) + ", " + fallback + ")"


def _catalog_host_id(con, db_path):
    cols = table_columns(db_path, "local_thread_catalog_hosts")
    if not cols or "host_id" not in cols:
        return "local"
    query = (
        "SELECT host_id FROM local_thread_catalog_hosts WHERE LOWER(COALESCE(host_kind,''))='local' ORDER BY host_id LIMIT 1"
        if "host_kind" in cols
        else "SELECT host_id FROM local_thread_catalog_hosts WHERE host_id='local' LIMIT 1"
    )
    row = con.execute(query).fetchone()
    if row and str(row[0]).strip():
        return str(row[0]).strip()
    return None


def _count_catalog_repairs(home, target_provider, excluded):
    inserted = 0
    removed = 0
    for db_path in reference_db_paths(home):
        if not db_path.exists():
            continue
        columns = table_columns(db_path, "local_thread_catalog")
        if not columns or not {
            "host_id", "thread_id", "display_title", "source_created_at",
            "source_updated_at", "cwd", "source_kind", "model_provider",
            "observation_sequence",
        }.issubset(columns):
            continue
        thread_cols = table_columns(db_path, "threads")
        if not thread_cols or "id" not in thread_cols:
            continue
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
            try:
                host_id = _catalog_host_id(con, db_path)
                if host_id is None:
                    continue
                existing = set(r[0] for r in con.execute(
                    "SELECT thread_id FROM local_thread_catalog WHERE host_id=?", (host_id,)
                ).fetchall())
                for tid in excluded:
                    if tid in existing:
                        removed += 1
                thread_ids = set(r[0] for r in con.execute(
                    "SELECT id FROM threads WHERE COALESCE(id,'') <> ''"
                ).fetchall())
                for tid in thread_ids:
                    if tid in excluded:
                        continue
                    if tid not in existing:
                        inserted += 1
            finally:
                con.close()
        except Exception:
            continue
    return inserted, removed


def repair_local_thread_catalog(home, target_provider, excluded_thread_ids=None, dry_run=False):
    excluded = excluded_thread_ids or set()
    inserted = 0
    removed = 0
    for db_path in reference_db_paths(home):
        if not db_path.exists():
            continue
        columns = table_columns(db_path, "local_thread_catalog")
        if not columns or not {
            "host_id", "thread_id", "display_title", "source_created_at",
            "source_updated_at", "cwd", "source_kind", "model_provider",
            "observation_sequence",
        }.issubset(columns):
            continue
        thread_cols = table_columns(db_path, "threads")
        if not thread_cols or "id" not in thread_cols:
            continue
        try:
            con = sqlite3.connect(str(db_path), timeout=10)
            con.execute("PRAGMA busy_timeout=10000")
            try:
                host_id = _catalog_host_id(con, db_path)
                if host_id is None:
                    continue
                obs_row = con.execute(
                    "SELECT COALESCE(MAX(observation_sequence),0) FROM local_thread_catalog WHERE host_id=?",
                    (host_id,),
                ).fetchone()
                obs = int(obs_row[0]) if obs_row else 0

                for thread_id in sorted(excluded):
                    if dry_run:
                        exists = con.execute(
                            "SELECT 1 FROM local_thread_catalog WHERE host_id=? AND thread_id=? LIMIT 1",
                            (host_id, thread_id),
                        ).fetchone()
                        if exists:
                            removed += 1
                    else:
                        removed += con.execute(
                            "DELETE FROM local_thread_catalog WHERE host_id=? AND thread_id=?",
                            (host_id, thread_id),
                        ).rowcount

                title_expr = _coalesce_expr(thread_cols, ("name", "title", "preview", "first_user_message"), "id")
                created_expr = "created_at_ms" if "created_at_ms" in thread_cols else ("created_at" if "created_at" in thread_cols else "0")
                updated_expr = "updated_at_ms" if "updated_at_ms" in thread_cols else ("updated_at" if "updated_at" in thread_cols else "0")
                cwd_expr = "cwd" if "cwd" in thread_cols else "''"
                source_kind_expr = "source" if "source" in thread_cols else "'cli'"
                source_detail_expr = "rollout_path" if "rollout_path" in thread_cols else "''"

                rows = con.execute(
                    f"SELECT id, {title_expr}, {created_expr}, {updated_expr}, {cwd_expr}, "
                    f"{source_kind_expr}, {source_detail_expr} FROM threads WHERE COALESCE(id,'') <> ''"
                ).fetchall()

                insert_cols = ["host_id", "thread_id", "display_title", "source_created_at",
                               "source_updated_at", "cwd", "source_kind", "model_provider",
                               "observation_sequence"]
                for opt in ("source_detail", "missing_candidate", "git_branch", "thread_source"):
                    if opt in columns:
                        insert_cols.append(opt)
                placeholders = ", ".join("?" for _ in insert_cols)
                insert_sql = (
                    f"INSERT OR IGNORE INTO local_thread_catalog ({', '.join(insert_cols)}) "
                    f"VALUES ({placeholders})"
                )

                for row in rows:
                    thread_id, title, created, updated, cwd, source_kind, source_detail = row
                    if thread_id in excluded:
                        continue
                    obs += 1
                    values = {
                        "host_id": host_id,
                        "thread_id": thread_id,
                        "display_title": title if isinstance(title, str) else str(thread_id),
                        "source_created_at": float(created or 0),
                        "source_updated_at": float(updated or 0),
                        "cwd": cwd if isinstance(cwd, str) else "",
                        "source_kind": source_kind if isinstance(source_kind, str) else "cli",
                        "source_detail": source_detail if isinstance(source_detail, str) else "",
                        "model_provider": target_provider,
                        "git_branch": None,
                        "thread_source": None,
                        "observation_sequence": obs,
                        "missing_candidate": 0,
                    }
                    params = [values[c] for c in insert_cols]
                    if dry_run:
                        cur = con.execute(
                            "SELECT 1 FROM local_thread_catalog WHERE host_id=? AND thread_id=? LIMIT 1",
                            (host_id, thread_id),
                        )
                        if not cur.fetchone():
                            inserted += 1
                    else:
                        inserted += con.execute(insert_sql, params).rowcount

                if not dry_run:
                    con.commit()
            finally:
                con.close()
        except Exception:
            continue
    return {"inserted": inserted, "removed": removed}


# --------------------------------------------------------------------------- #
# Rollout + session index repair (reuse core helpers)
# --------------------------------------------------------------------------- #

def _bind_core(home):
    core.CODEX_HOME = home
    core.BACKUP_ROOT = home / "history-sync-backups"
    core.CC_SWITCH_DB = paths.cc_switch_db()
    core.CC_SWITCH_SETTINGS = paths.cc_switch_settings()
    core.OFFICIAL_PROVIDER_IDS = None


def _scan_rollout_provider_mismatches(home, target_provider):
    _bind_core(home)
    changed = 0
    for path, _archived in core.iter_rollouts():
        meta_line = core.read_session_meta_line(path)
        if not meta_line:
            continue
        _i, _line, obj = meta_line
        payload = obj.get("payload") or {}
        if payload.get("model_provider") != target_provider:
            changed += 1
    return changed


def rewrite_rollout_metadata(home, target_provider, rewrite_provider, dry_run=False):
    _bind_core(home)
    backup_dir = core.BACKUP_ROOT / time.strftime("%Y%m%d")
    backup_dir.mkdir(parents=True, exist_ok=True)
    if dry_run:
        return {"changed": _scan_rollout_provider_mismatches(home, target_provider), "skipped_locked": 0}
    return core.normalize_rollout_metadata(backup_dir, target_provider, rewrite_provider)


def rebuild_session_index(home, dry_run=False):
    _bind_core(home)
    if dry_run:
        return {"index_entries": 0}
    return core.rebuild_session_index({})


def cleanup_session_index_ghosts(home, live_thread_ids, dry_run=False):
    """Remove ``session_index.jsonl`` entries whose thread id has no live thread
    and no rollout. Returns the number of removed entries."""
    index_path = home / "session_index.jsonl"
    if not index_path.exists():
        return 0
    try:
        lines = index_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 0
    kept = []
    removed = 0
    for line in lines:
        line = line.strip()
        if not line:
            kept.append(line)
            continue
        try:
            obj = json.loads(line)
        except Exception:
            kept.append(line)
            continue
        rid = obj.get("id")
        if rid and rid not in live_thread_ids:
            removed += 1
        else:
            kept.append(line)
    if removed and not dry_run:
        tmp = index_path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, index_path)
    return removed


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def run_history_repair(home=None, target_provider=None, progress_cb=None, dry_run=False,
                       do_rollout=True, do_index=True):
    """Run the complete history-repair pipeline. ``target_provider`` defaults to
    auto-detection from cc-switch. ``do_rollout`` / ``do_index`` let callers that
    already performed those steps (``core.run``) skip them."""
    home = Path(home or paths.codex_home())
    if target_provider is None:
        target_provider, provider_id, is_official = detect_target_provider(home)
    else:
        provider_id, is_official = None, target_provider == core.OFFICIAL_MODEL_PROVIDER
    rewrite_history_provider = target_provider == core.TRANSIT_MODEL_PROVIDER

    db_paths = all_db_paths(home)
    excluded = _subagent_thread_ids(db_paths) | load_projectless_thread_ids(home)

    def report(fraction, message=""):
        if progress_cb:
            try:
                progress_cb(min(1.0, max(0.0, fraction)), message)
            except Exception:
                pass

    report(0.05, "扫描会话数据库")
    suffix = sanitize_thread_model_suffixes(home, dry_run=dry_run)

    if do_rollout:
        report(0.2, "重写 rollout 元数据")
        rollout = rewrite_rollout_metadata(home, target_provider, rewrite_history_provider, dry_run=dry_run)
    else:
        rollout = {"changed": 0, "skipped_locked": 0}

    report(0.35, "更新线程 provider")
    provider_rows = update_threads_provider(home, target_provider, excluded, dry_run=dry_run)

    report(0.5, "修复线程 cwd / 用户事件")
    cwd_by_id, user_event_ids = _rollout_thread_metadata(home)
    metadata = update_threads_metadata(home, cwd_by_id, user_event_ids, excluded, dry_run=dry_run)

    report(0.65, "修复侧边栏目录")
    catalog = repair_local_thread_catalog(home, target_provider, excluded, dry_run=dry_run)

    report(0.78, "同步全局状态")
    global_state_changed = sync_global_state(home, dry_run=dry_run)

    report(0.86, "清理模型后缀")
    logs_suffix = sanitize_logs_model_suffixes(home, dry_run=dry_run)

    if do_index:
        report(0.93, "重建会话索引")
        index = rebuild_session_index(home, dry_run=dry_run)
    else:
        index = {"index_entries": 0}

    report(1.0, "完成")
    return {
        "dry_run": dry_run,
        "target_model_provider": target_provider,
        "cc_switch_provider_id": provider_id,
        "is_official": is_official,
        "databases": len(db_paths),
        "excluded_thread_ids": len(excluded),
        "rollout_meta_changed": rollout["changed"],
        "threads_provider_rows": provider_rows,
        "cwd_rows": metadata["cwd_rows"],
        "has_user_event_rows": metadata["has_user_event_rows"],
        "catalog_inserted": catalog["inserted"],
        "catalog_removed": catalog["removed"],
        "global_state_changed": global_state_changed,
        "model_suffix_scanned": suffix["scanned"],
        "model_suffix_updated": suffix["updated"],
        "logs_suffix_updated": logs_suffix,
        "index_entries": index.get("index_entries", 0),
    }


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    print(json.dumps(run_history_repair(dry_run=dry), ensure_ascii=False, indent=2))
