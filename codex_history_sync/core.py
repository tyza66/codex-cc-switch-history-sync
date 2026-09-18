"""Portable sync core.

Ported verbatim from the original ``sync-codex-history.ps1`` embedded Python,
with the only change being that paths are resolved at call time (inside
:func:`run`) rather than at import time, so it works in any process and with
overridden ``CODEX_HOME`` / ``CC_SWITCH_*`` environment variables.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

CODEX_HOME = None
CC_SWITCH_DB = None
CC_SWITCH_SETTINGS = None
BACKUP_ROOT = None
KEEP_BACKUPS = 5
OFFICIAL_MODEL_PROVIDER = "openai"
TRANSIT_MODEL_PROVIDER = "ccs"
OFFICIAL_PROVIDER_IDS = None
LEGACY_MODEL_VALUES = {
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.2",
}


def _log(msg):
    if os.environ.get("CODEX_HISTORY_SYNC_QUIET") != "1":
        print(msg)


def utc_iso_from_epoch(seconds):
    return _dt.datetime.fromtimestamp(float(seconds), _dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(ts):
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def backup_sqlite(src, dst):
    src = Path(src)
    if not src.exists():
        return False
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return True


def make_backup():
    stamp = time.strftime("%Y%m%d")
    backup_dir = BACKUP_ROOT / stamp
    already_exists = backup_dir.exists()
    backup_dir.mkdir(parents=True, exist_ok=True)
    if not already_exists:
        for name in ("config.toml", "session_index.jsonl"):
            src = CODEX_HOME / name
            if src.exists():
                shutil.copy2(src, backup_dir / name)
        if CC_SWITCH_DB.exists():
            backup_sqlite(CC_SWITCH_DB, backup_dir / "cc-switch.db")
        if CC_SWITCH_SETTINGS.exists():
            shutil.copy2(CC_SWITCH_SETTINGS, backup_dir / "cc-switch-settings.json")
        state = CODEX_HOME / "state_5.sqlite"
        if state.exists():
            backup_sqlite(state, backup_dir / "state_5.sqlite")
    dirs = sorted([p for p in BACKUP_ROOT.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)
    for old in dirs[KEEP_BACKUPS:]:
        shutil.rmtree(old, ignore_errors=True)
    return backup_dir


def strip_disable_storage(text):
    return re.sub(r"(?m)^\s*disable_response_storage\s*=\s*true\s*\r?\n?", "", text or "")


def ensure_history_save_all(text):
    text = text or ""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "[history]":
            start = i
            break
    if start is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + "\n[history]\npersistence = \"save-all\"\n"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^\s*\[.+\]\s*$", lines[j]):
            end = j
            break
    found = False
    for j in range(start + 1, end):
        if re.match(r"^\s*persistence\s*=", lines[j]):
            lines[j] = 'persistence = "save-all"'
            found = True
            break
    if not found:
        lines.insert(start + 1, 'persistence = "save-all"')
    return "\n".join(lines) + "\n"


def ensure_top_level_setting(text, key, value):
    text = text or ""
    lines = text.splitlines()
    pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    first_table = len(lines)
    for i, line in enumerate(lines):
        if re.match(r"^\s*\[.+\]\s*$", line):
            first_table = i
            break
    for i in range(first_table):
        if pattern.match(lines[i]):
            lines[i] = f'{key} = "{value}"'
            return "\n".join(lines) + "\n"
    insert_at = first_table
    while insert_at > 0 and lines[insert_at - 1].strip() == "":
        insert_at -= 1
    lines.insert(insert_at, f'{key} = "{value}"')
    return "\n".join(lines) + "\n"


def top_level_setting(text, key):
    lines = (text or "").splitlines()
    pattern = re.compile(r"^\s*" + re.escape(key) + r'\s*=\s*"([^"]*)"\s*$')
    for line in lines:
        if re.match(r"^\s*\[.+\]\s*$", line):
            break
        m = pattern.match(line)
        if m:
            return m.group(1)
    return None


def current_model_defaults():
    path = CODEX_HOME / "config.toml"
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    return {
        "model": top_level_setting(text, "model"),
        "model_reasoning_effort": top_level_setting(text, "model_reasoning_effort"),
        "sandbox_mode": top_level_setting(text, "sandbox_mode"),
        "approval_policy": top_level_setting(text, "approval_policy"),
    }


def sync_model_defaults(text, defaults):
    text = text or ""
    model = (defaults or {}).get("model")
    if model:
        current_model = top_level_setting(text, "model")
        if not current_model or current_model in LEGACY_MODEL_VALUES:
            text = ensure_top_level_setting(text, "model", model)
    effort = (defaults or {}).get("model_reasoning_effort")
    if effort:
        current_effort = top_level_setting(text, "model_reasoning_effort")
        if not current_effort:
            text = ensure_top_level_setting(text, "model_reasoning_effort", effort)
    for key in ("sandbox_mode", "approval_policy"):
        value = (defaults or {}).get(key)
        if value and not top_level_setting(text, key):
            text = ensure_top_level_setting(text, key, value)
    return text


def active_provider_name(text):
    m = re.search(r'(?m)^\s*model_provider\s*=\s*"([^"]+)"\s*$', text or "")
    return m.group(1) if m else None


def current_codex_provider_id():
    if not CC_SWITCH_SETTINGS.exists():
        return None
    try:
        data = json.loads(CC_SWITCH_SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return None
    value = data.get("currentProviderCodex")
    return value if isinstance(value, str) and value else None


def is_official_provider_row(row):
    provider_id = (row["id"] or "").strip().lower()
    name = (row["name"] or "").strip().lower()
    keys = row.keys() if hasattr(row, "keys") else []
    category = ((row["category"] if "category" in keys else "") or "").strip().lower()
    return (
        provider_id == "codex-official"
        or category == "official"
        or (provider_id == "openai" and "official" in name)
    )


def official_provider_ids():
    global OFFICIAL_PROVIDER_IDS
    if OFFICIAL_PROVIDER_IDS is not None:
        return OFFICIAL_PROVIDER_IDS
    ids = set()
    if CC_SWITCH_DB.exists():
        try:
            con = sqlite3.connect(f"file:{CC_SWITCH_DB}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                for row in con.execute("select id, name, category from providers where app_type='codex'"):
                    if is_official_provider_row(row):
                        ids.add(row["id"])
            finally:
                con.close()
        except Exception:
            pass
    ids.add("codex-official")
    OFFICIAL_PROVIDER_IDS = ids
    return OFFICIAL_PROVIDER_IDS


def target_provider_for_provider_id(provider_id):
    if provider_id in official_provider_ids():
        return OFFICIAL_MODEL_PROVIDER
    return TRANSIT_MODEL_PROVIDER


def target_provider_for_provider_row(row):
    if is_official_provider_row(row):
        return OFFICIAL_MODEL_PROVIDER
    return TRANSIT_MODEL_PROVIDER


def ensure_model_provider(text, provider):
    text = text or ""
    if re.search(r"(?m)^\s*model_provider\s*=", text):
        return re.sub(r'(?m)^\s*model_provider\s*=\s*"[^"]+"\s*$', f'model_provider = "{provider}"', text, count=1)
    return f'model_provider = "{provider}"\n' + text


def normalize_active_model_provider_block(text, target_provider):
    text = text or ""
    provider = active_provider_name(text)
    if not provider or provider == target_provider:
        return text
    pattern = r"(?m)^\[model_providers\." + re.escape(provider) + r"\]\s*$"
    return re.sub(pattern, f"[model_providers.{target_provider}]", text, count=1)


def normalize_codex_config(text, target_provider=None):
    text = strip_disable_storage(text)
    text = ensure_history_save_all(text)
    if target_provider:
        text = normalize_active_model_provider_block(text, target_provider)
        text = ensure_model_provider(text, target_provider)
    return text


def table_blocks(text):
    lines = (text or "").splitlines()
    starts = []
    for i, line in enumerate(lines):
        if re.match(r"^\s*\[[^\]]+\]\s*$", line):
            starts.append(i)
    blocks = {}
    prefixes = ("[projects.", "[plugins.", "[mcp_servers.", "[marketplaces.")
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        header = lines[start].strip()
        if header.startswith(prefixes):
            blocks.setdefault(header, "\n".join(lines[start:end]).rstrip() + "\n")
    return blocks


def append_missing_blocks(text, union_blocks):
    existing = set(table_blocks(text).keys())
    add = [block for header, block in union_blocks.items() if header not in existing]
    if not add:
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + "\n" + "\n".join(add)


def normalize_cc_switch_db(current_provider_id, model_defaults=None):
    if not CC_SWITCH_DB.exists():
        return {"providers": 0, "changed": 0}
    current_target_provider = target_provider_for_provider_id(current_provider_id)
    con = sqlite3.connect(str(CC_SWITCH_DB), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    rows = list(con.execute("select id, name, category, settings_config from providers where app_type='codex'"))
    union = {}
    parsed = []
    for row in rows:
        cfg = json.loads(row["settings_config"] or "{}")
        text = cfg.get("config", "")
        union.update(table_blocks(text))
        parsed.append((row, cfg, text))
    changed = 0
    for row, cfg, old_text in parsed:
        target_provider = target_provider_for_provider_row(row)
        text = normalize_codex_config(old_text, target_provider=target_provider)
        if target_provider == current_target_provider:
            text = sync_model_defaults(text, model_defaults)
        text = append_missing_blocks(text, union)
        if text != old_text:
            cfg["config"] = text
            con.execute("update providers set settings_config=? where id=?", (json.dumps(cfg, ensure_ascii=False), row["id"]))
            changed += 1
    common = con.execute("select value from settings where key='common_config_codex'").fetchone()
    if common:
        new_common = normalize_codex_config(common["value"], target_provider=None)
        if new_common != common["value"]:
            con.execute("update settings set value=? where key='common_config_codex'", (new_common,))
            changed += 1
    con.commit()
    con.close()
    return {
        "providers": len(rows),
        "changed": changed,
        "current_provider_id": current_provider_id,
        "current_target_model_provider": current_target_provider,
    }


def normalize_current_config(current_provider_id, model_defaults=None):
    path = CODEX_HOME / "config.toml"
    if not path.exists():
        return False
    old = path.read_text(encoding="utf-8")
    new = normalize_codex_config(old, target_provider=target_provider_for_provider_id(current_provider_id))
    new = sync_model_defaults(new, model_defaults)
    if new != old:
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, path)
        return True
    return False


_ROLLOUT_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def is_rollout_uuid(value):
    return isinstance(value, str) and _ROLLOUT_UUID_RE.fullmatch(value) is not None


def rollout_id_from_name(path):
    stem = path.stem
    m = _ROLLOUT_UUID_RE.search(stem)
    return m.group(0) if m else stem


def repair_rollout_first_lines(progress=None):
    """Ensure the first line of every rollout JSONL is a ``session_meta`` entry.

    Codex Desktop requires ``session_meta`` to be the very first line. When a
    file starts with any other entry the session fails to restore with the
    "does not start with session metadata" error.  This function:

    1. Finds the ``session_meta`` line (scanning up to *max_search* lines).
    2. If it is not at index 0, rewrites the file so that line comes first
       while preserving the order of all other lines.
    3. If no ``session_meta`` line exists at all, constructs one from the
       available metadata and prepends it.

    Returns a dict with ``scanned``, ``repaired``, ``prepended``, ``errors``.
    """
    max_search = 20
    scanned = repaired = prepended = errors = 0

    for path, _archived in iter_rollouts():
        scanned += 1
        try:
            with path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            errors += 1
            continue

        if not lines:
            errors += 1
            continue

        # Check if the first line is already session_meta.
        try:
            first_obj = json.loads(lines[0])
        except Exception:
            first_obj = None
        if first_obj and first_obj.get("type") == "session_meta":
            continue

        # Find the session_meta line within the first max_search lines.
        meta_idx = None
        for i, line in enumerate(lines[:max_search]):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") == "session_meta":
                meta_idx = i
                break

        if meta_idx is not None and meta_idx > 0:
            # Move the session_meta line to the front.
            meta_line = lines.pop(meta_idx)
            lines.insert(0, meta_line)
            try:
                tmp = path.with_suffix(path.suffix + ".tmp")
                with tmp.open("w", encoding="utf-8", newline="") as f:
                    f.writelines(lines)
                os.replace(tmp, path)
                repaired += 1
            except Exception:
                errors += 1
        elif meta_idx is None:
            # No session_meta line found — construct one from data in the file.
            meta_payload = {}
            for line in lines[:max_search]:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                payload = obj.get("payload") or {}
                if obj.get("type") == "session_meta":
                    meta_payload.update(payload)
                elif "model" in payload and "model" not in meta_payload:
                    meta_payload["model"] = payload["model"]
                if not meta_payload.get("session_id"):
                    sid = payload.get("session_id")
                    if sid:
                        meta_payload["session_id"] = sid

            filename_id = rollout_id_from_name(path)
            meta_payload.setdefault("id", filename_id)
            meta_payload.setdefault("session_id", filename_id)
            meta_payload.setdefault("model", "gpt-5.5")
            new_line = json.dumps(
                {"type": "session_meta", "payload": meta_payload},
                ensure_ascii=False, separators=(",", ":"),
            ) + "\n"
            try:
                tmp = path.with_suffix(path.suffix + ".tmp")
                with tmp.open("w", encoding="utf-8", newline="") as f:
                    f.write(new_line)
                    f.writelines(lines)
                os.replace(tmp, path)
                prepended += 1
            except Exception:
                errors += 1

        if progress and (repaired + prepended) % 10 == 0:
            try:
                progress(scanned, repaired, prepended)
            except Exception:
                pass

    return {
        "scanned": scanned,
        "repaired": repaired,
        "prepended": prepended,
        "errors": errors,
    }


def rollout_thread_id(path, meta=None):
    """Return the authoritative thread id for a rollout.

    ``session_meta.payload.id`` is authoritative when it is a canonical UUID.
    When a previous buggy ``rollout_id_from_name`` corrupted ``payload.id`` with
    an underscore-joined suffix, fall back to ``session_id`` and finally the
    first UUID in the filename.
    """
    payload = meta or {}
    pid = payload.get("id")
    if is_rollout_uuid(pid):
        return pid
    sid = payload.get("session_id")
    if is_rollout_uuid(sid):
        return sid
    return rollout_id_from_name(path)


def iter_rollouts():
    roots = [(CODEX_HOME / "sessions", 0), (CODEX_HOME / "archived_sessions", 1)]
    for root, archived in roots:
        if not root.exists():
            continue
        for path in root.rglob("rollout-*.jsonl"):
            yield path, archived


def read_session_meta_line(path, max_lines=20):
    try:
        with path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") == "session_meta":
                    return i, line, obj
    except Exception:
        return None
    return None


def replace_line_streaming(path, target_index, new_line):
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with path.open("r", encoding="utf-8") as src, tmp.open("w", encoding="utf-8", newline="") as dst:
            for i, line in enumerate(src):
                dst.write(new_line if i == target_index else line)
        os.replace(tmp, path)
        return True
    except PermissionError:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def normalize_rollout_metadata(backup_dir, target_provider, rewrite_provider):
    backup_path = backup_dir / "rollout_session_meta_backup.jsonl"
    changed = 0
    skipped_locked = 0
    with backup_path.open("a", encoding="utf-8", newline="\n") as backup:
        for path, _archived in iter_rollouts():
            try:
                original_stat = path.stat()
            except Exception:
                continue
            meta_line = read_session_meta_line(path)
            if not meta_line:
                continue
            i, line, obj = meta_line
            payload = obj.get("payload") or {}
            filename_id = rollout_thread_id(path, payload)
            provider_changed = rewrite_provider and payload.get("model_provider") != target_provider
            id_changed = payload.get("id") != filename_id
            if not provider_changed and not id_changed:
                continue
            backup.write(json.dumps({
                "path": str(path),
                "line_index": i,
                "original_line": line.rstrip("\r\n"),
            }, ensure_ascii=False) + "\n")
            payload["id"] = filename_id
            if rewrite_provider:
                payload["model_provider"] = target_provider
            obj["payload"] = payload
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            new_line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + newline
            try:
                if replace_line_streaming(path, i, new_line):
                    os.utime(path, (original_stat.st_atime, original_stat.st_mtime))
                    changed += 1
            except PermissionError:
                skipped_locked += 1
    if changed == 0 and backup_path.exists() and backup_path.stat().st_size == 0:
        backup_path.unlink()
    return {
        "changed": changed,
        "skipped_locked": skipped_locked,
        "target_provider": target_provider if rewrite_provider else None,
    }


def text_from_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                val = item.get("text") or item.get("content")
                if isinstance(val, str):
                    parts.append(val)
        return "\n".join(parts)
    return ""


def clean_title(text):
    text = (text or "").strip()
    if text.startswith("# AGENTS.md instructions"):
        return ""
    if text.startswith("<environment_context>"):
        return ""
    text = re.sub(r"\s+", " ", text)
    if len(text) > 140:
        text = text[:140].rstrip() + "..."
    return text or ""


def parse_rollout(path):
    meta = {}
    title = ""
    last_ts = None
    first_ts = None
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                ts = parse_iso(obj.get("timestamp"))
                if ts is not None:
                    first_ts = ts if first_ts is None else min(first_ts, ts)
                    last_ts = ts if last_ts is None else max(last_ts, ts)
                typ = obj.get("type")
                payload = obj.get("payload") or {}
                if typ == "session_meta":
                    meta.update(payload)
                    mts = parse_iso(payload.get("timestamp"))
                    if mts is not None:
                        first_ts = mts if first_ts is None else min(first_ts, mts)
                elif typ == "event_msg" and payload.get("type") == "user_message" and not title:
                    title = clean_title(payload.get("message", ""))
                elif typ == "response_item" and payload.get("role") == "user" and not title:
                    title = clean_title(text_from_content(payload.get("content")))
    except Exception:
        pass
    stat = path.stat()
    rid = rollout_thread_id(path, meta)
    created = first_ts or parse_iso(meta.get("timestamp")) or stat.st_ctime
    updated = max([x for x in (last_ts, stat.st_mtime) if x is not None])
    if not title:
        title = "Untitled session"
    return {
        "id": rid,
        "title": title,
        "created_at": int(created),
        "updated_at": int(updated),
        "cwd": meta.get("cwd"),
        "source": meta.get("source") or "vscode",
        "thread_source": meta.get("thread_source"),
        "model": meta.get("model") or "gpt-5.5",
        "reasoning_effort": meta.get("reasoning_effort"),
    }


def load_current_index_titles():
    index_path = CODEX_HOME / "session_index.jsonl"
    titles = {}
    if not index_path.exists():
        return titles
    try:
        with index_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                rid = obj.get("id")
                title = (obj.get("thread_name") or "").strip()
                if rid and title:
                    titles[rid] = title
    except Exception:
        pass
    return titles


def repair_state_db(target_provider, rewrite_provider, index_titles=None):
    index_titles = index_titles or {}
    path = CODEX_HOME / "state_5.sqlite"
    if not path.exists():
        return {"updated": 0, "title_updated": 0, "inserted": 0, "integrity": "missing"}
    con = sqlite3.connect(str(path), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    updated = 0
    if rewrite_provider:
        before = con.total_changes
        con.execute(
            "update threads set model_provider=? where model_provider is null or model_provider <> ?",
            (target_provider, target_provider),
        )
        updated = con.total_changes - before
    existing = {r["id"]: r for r in con.execute("select id, title from threads")}
    title_updated = 0
    for rid, title in index_titles.items():
        row = existing.get(rid)
        if row is None or (row["title"] or "") == title:
            continue
        before = con.total_changes
        con.execute(
            "update threads set title=?, first_user_message=?, preview=? where id=?",
            (title, title, title, rid),
        )
        title_updated += con.total_changes - before
        existing[rid] = {"id": rid, "title": title}
    inserted = 0
    for rollout, archived in iter_rollouts():
        try:
            _ml = read_session_meta_line(rollout)
            _pl = (_ml[2].get("payload") or {}) if _ml else {}
        except Exception:
            _pl = {}
        rid = rollout_thread_id(rollout, _pl)
        if rid in existing:
            continue
        info = parse_rollout(rollout)
        rollout_path = str(rollout)
        con.execute(
            """
            insert or ignore into threads
            (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
             sandbox_policy, approval_mode, tokens_used, has_user_event, archived, archived_at,
             cli_version, first_user_message, model, reasoning_effort, created_at_ms,
             updated_at_ms, thread_source, preview)
            values (?, ?, ?, ?, ?, ?, ?, ?, '', '', 0, 1, ?, ?, '', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                info["id"], rollout_path, info["created_at"], info["updated_at"], info["source"], target_provider,
                info["cwd"], info["title"], archived, info["updated_at"] if archived else None,
                info["title"], info["model"], info["reasoning_effort"], info["created_at"] * 1000,
                info["updated_at"] * 1000, info["thread_source"], info["title"],
            ),
        )
        inserted += 1
    con.commit()
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    return {
        "updated": updated,
        "title_updated": title_updated,
        "inserted": inserted,
        "integrity": integrity,
        "target_provider": target_provider,
        "rewrite_provider": rewrite_provider,
    }


def rebuild_session_index(index_titles=None):
    index_titles = index_titles or {}
    state_rows = {}
    state_path = CODEX_HOME / "state_5.sqlite"
    if state_path.exists():
        con = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            for r in con.execute("select id, title, updated_at from threads"):
                state_rows[r["id"]] = {
                    "id": r["id"],
                    "title": r["title"] or "Untitled session",
                    "updated_at": int(r["updated_at"] or 0),
                }
        finally:
            con.close()
    rows = []
    seen = set()
    for rollout, archived in iter_rollouts():
        try:
            _ml = read_session_meta_line(rollout)
            _pl = (_ml[2].get("payload") or {}) if _ml else {}
        except Exception:
            _pl = {}
        rid = rollout_thread_id(rollout, _pl)
        if rid in seen:
            continue
        seen.add(rid)
        if rid in state_rows:
            row = dict(state_rows[rid])
            if rid in index_titles:
                row["title"] = index_titles[rid]
            rows.append(row)
            continue
        info = parse_rollout(rollout)
        rows.append({
            "id": info["id"],
            "title": index_titles.get(info["id"]) or info["title"] or "Untitled session",
            "updated_at": int(info["updated_at"]),
        })
    rows.sort(key=lambda r: (r["updated_at"], r["id"]))
    index_path = CODEX_HOME / "session_index.jsonl"
    tmp = index_path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps({
                "id": r["id"],
                "thread_name": r["title"] or "Untitled session",
                "updated_at": utc_iso_from_epoch(r["updated_at"]),
            }, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(tmp, index_path)
    return {"index_entries": len(rows)}


def run():
    """Run the full sync and return a JSON-serializable result dict."""
    global CODEX_HOME, CC_SWITCH_DB, CC_SWITCH_SETTINGS, BACKUP_ROOT, OFFICIAL_PROVIDER_IDS
    CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    CC_SWITCH_DB = Path(os.environ.get("CC_SWITCH_DB") or (Path.home() / ".cc-switch" / "cc-switch.db"))
    CC_SWITCH_SETTINGS = Path(os.environ.get("CC_SWITCH_SETTINGS") or (Path.home() / ".cc-switch" / "settings.json"))
    BACKUP_ROOT = CODEX_HOME / "history-sync-backups"
    OFFICIAL_PROVIDER_IDS = None

    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    current_provider_id = current_codex_provider_id()
    target_provider = target_provider_for_provider_id(current_provider_id)
    rewrite_history_provider = target_provider == TRANSIT_MODEL_PROVIDER
    backup_dir = make_backup()
    model_defaults = current_model_defaults()
    index_titles = load_current_index_titles()
    rollout_meta_changed = normalize_rollout_metadata(backup_dir, target_provider, rewrite_history_provider)
    cc = normalize_cc_switch_db(current_provider_id, model_defaults)
    config_changed = normalize_current_config(current_provider_id, model_defaults)
    state = repair_state_db(target_provider, rewrite_history_provider, index_titles)
    index = rebuild_session_index(index_titles)

    # Complete history-repair toolchain (newer Codex sqlite layout, sidebar
    # catalogue, global state, model suffixes). Rollout rewrite and session
    # index rebuild are already done above, so skip them here.
    from . import repair
    history_repair = repair.run_history_repair(CODEX_HOME, target_provider, do_rollout=False, do_index=False)

    result = {
        "backup_dir": str(backup_dir),
        "auth_overrides_cleared": os.environ.get("CODEX_AUTH_OVERRIDES_CLEARED") == "1",
        "current_provider_id": current_provider_id,
        "target_model_provider": target_provider,
        "model_defaults": model_defaults,
        "rewrite_history_provider": rewrite_history_provider,
        "rollout_meta_changed": rollout_meta_changed,
        "cc_switch": cc,
        "config_changed": config_changed,
        "state": state,
        "index": index,
        "history_repair": history_repair,
    }
    _log(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    run()
