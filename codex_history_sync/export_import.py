"""Export and import Codex chat history as a zip archive.

The zip preserves the Codex home layout (``sessions/``, ``archived_sessions/``,
``session_index.jsonl``, ``state_5.sqlite``, ``config.toml``) so it can be
loaded back later. Import validates the internal folder structure before
writing anything.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import zipfile
from pathlib import Path

from . import core, paths

# Top-level entries we recognise inside an export zip. Anything else is
# rejected by :func:`validate_zip`.
_KNOWN_TOP_LEVEL = {
    "sessions",
    "archived_sessions",
    "session_index.jsonl",
    "state_5.sqlite",
    "config.toml",
    "manifest.json",
}


def _default_export_name():
    return f"codex-history-{time.strftime('%Y%m%d-%H%M%S')}.zip"


def _manifest(metadata):
    return {
        "format": "codex-history-export",
        "version": 1,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "codex_home": str(paths.codex_home()),
        **metadata,
    }


def _iter_session_rollouts(home):
    """Yield ``(archive_rel_path, absolute_path)`` for every rollout jsonl."""
    roots = [
        (home / "sessions", "sessions"),
        (home / "archived_sessions", "archived_sessions"),
    ]
    for root, prefix in roots:
        if not root.exists():
            continue
        for path in root.rglob("rollout-*.jsonl"):
            rel = f"{prefix}/{path.relative_to(root)}"
            yield rel, path


def _add_file_to_zip(zf, arcname, src):
    """Add a single file, preserving mtime inside the zip."""
    src = Path(src)
    if not src.exists():
        return False
    info = zipfile.ZipInfo.from_file(str(src), arcname=arcname)
    with open(src, "rb") as f:
        zf.writestr(info, f.read())
    return True


def export_zip(dest_path, home=None, include=None, progress=None):
    """Export Codex history to a zip file.

    ``dest_path`` is the destination zip path. ``home`` defaults to the
    current Codex home. ``include`` is an optional set of top-level entries to
    export (``{"sessions", "archived_sessions", "session_index.jsonl",
    "state_5.sqlite", "config.toml"}``); when ``None`` everything available is
    exported. ``progress`` is an optional callback ``(message, done, total)``.
    """
    home = Path(home) if home else paths.codex_home()
    dest_path = Path(dest_path)
    include = _normalize_include(include)

    if progress is None:
        progress = lambda msg, done, total: None  # noqa: E731

    files_to_add = []

    if "sessions" in include or "archived_sessions" in include:
        for rel, abs_path in _iter_session_rollouts(home):
            prefix = rel.split("/")[0]
            if prefix in include:
                files_to_add.append((rel, abs_path))

    for name in ("session_index.jsonl", "state_5.sqlite", "config.toml"):
        if name in include:
            p = home / name
            if p.exists():
                files_to_add.append((name, p))

    total = len(files_to_add) + 1
    progress("开始导出…", 0, total)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")

    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, (rel, abs_path) in enumerate(files_to_add, 1):
                progress(f"打包 {rel}", i, total)
                _add_file_to_zip(zf, rel, abs_path)

            manifest = _manifest({
                "files": len(files_to_add),
                "include": sorted(include),
            })
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        os.replace(tmp_path, dest_path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    progress("导出完成", total, total)
    return {"path": str(dest_path), "files": len(files_to_add)}


def _normalize_include(include):
    if include is None:
        return {"sessions", "archived_sessions", "session_index.jsonl",
                "state_5.sqlite", "config.toml"}
    allowed = {"sessions", "archived_sessions", "session_index.jsonl",
               "state_5.sqlite", "config.toml"}
    out = set(include) & allowed
    return out or allowed


def validate_zip(zip_path):
    """Validate that a zip has a legal Codex history export structure.

    Returns ``(valid, errors, warnings, manifest)``. When ``valid`` is
    ``False`` the import must be aborted.
    """
    zip_path = Path(zip_path)
    errors = []
    warnings: list[str] = []
    manifest = None

    if not zip_path.exists():
        return False, [f"文件不存在: {zip_path}"], [], None

    try:
        zf = zipfile.ZipFile(str(zip_path), "r")
    except zipfile.BadZipFile as exc:
        return False, [f"不是有效的 zip 文件: {exc}"], [], None

    try:
        names = zf.namelist()
        if not names:
            errors.append("zip 文件为空")
            return False, errors, warnings, None

        top_entries = set()
        for name in names:
            parts = name.split("/")
            top_entries.add(parts[0])

        unknown = top_entries - _KNOWN_TOP_LEVEL
        if unknown:
            errors.append(f"包含未知顶层条目: {', '.join(sorted(unknown))}")

        if "manifest.json" in names:
            try:
                manifest = json.loads(zf.read("manifest.json"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                warnings.append(f"manifest.json 解析失败: {exc}")
            else:
                fmt = manifest.get("format")
                if fmt != "codex-history-export":
                    warnings.append(f"manifest.format 不是预期的 'codex-history-export': {fmt!r}")

        has_sessions = "sessions" in top_entries
        has_archived = "archived_sessions" in top_entries
        if not has_sessions and not has_archived:
            warnings.append("zip 中不包含 sessions/ 或 archived_sessions/ 目录")

        bad_rollouts = []
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            parts = name.split("/")
            if parts[0] not in ("sessions", "archived_sessions"):
                continue
            # Accept either sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl or a direct
            # rollout-*.jsonl under sessions/ or archived_sessions/. The filename
            # itself is the authoritative check.
            if len(parts) < 2:
                bad_rollouts.append(name)
                continue
            fname = parts[-1]
            if not fname.startswith("rollout-"):
                bad_rollouts.append(name)
                continue
            stem = fname[:-6] if fname.endswith(".jsonl") else fname
            if not core._ROLLOUT_UUID_RE.search(stem):
                bad_rollouts.append(name)
        if bad_rollouts:
            shown = ", ".join(bad_rollouts[:5])
            suffix = " …" if len(bad_rollouts) > 5 else ""
            errors.append(
                f"rollout 路径不符合 sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl 结构: {shown}{suffix}"
            )

        if "state_5.sqlite" in top_entries:
            try:
                data = zf.read("state_5.sqlite")
                if data[:16] != b"SQLite format 3\x00":
                    errors.append("state_5.sqlite 不是有效的 SQLite 文件")
            except Exception as exc:
                errors.append(f"无法读取 state_5.sqlite: {exc}")

        if "session_index.jsonl" in top_entries:
            bad_lines = 0
            try:
                with zf.open("session_index.jsonl") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            bad_lines += 1
                            continue
                        if not isinstance(obj.get("id"), str) or not obj["id"]:
                            bad_lines += 1
            except Exception as exc:
                errors.append(f"无法读取 session_index.jsonl: {exc}")
            if bad_lines:
                warnings.append(f"session_index.jsonl 中有 {bad_lines} 行无效记录")

    finally:
        zf.close()

    valid = not errors
    return valid, errors, warnings, manifest


def import_zip(zip_path, home=None, include=None, progress=None, backup=True):
    """Import Codex history from a zip file.

    Validates the zip first and raises :class:`ZipValidationError` if the
    structure is illegal. When ``backup`` is ``True`` (default) the current
    Codex home is backed up before any writes.
    """
    valid, errors, warnings, manifest = validate_zip(zip_path)
    if not valid:
        raise ZipValidationError(errors)

    home = Path(home) if home else paths.codex_home()
    include = _normalize_include(include)
    if progress is None:
        progress = lambda msg, done, total: None  # noqa: E731

    backup_dir = None
    if backup:
        backup_dir = paths.backup_root() / f"pre-import-{time.strftime('%Y%m%d-%H%M%S')}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        progress(f"备份当前状态到 {backup_dir}", 0, 0)
        _backup_home(home, backup_dir, include)

    zf = zipfile.ZipFile(str(zip_path), "r")
    try:
        names = zf.namelist()
        targets = []
        for name in names:
            if name == "manifest.json":
                continue
            top = name.split("/")[0]
            if top in include:
                targets.append(name)

        total = len(targets)
        progress("开始导入…", 0, total)

        for i, name in enumerate(targets, 1):
            progress(f"解压 {name}", i, total)
            top = name.split("/")[0]
            if top in ("sessions", "archived_sessions"):
                _extract_rollout(zf, name, home)
            else:
                _extract_file(zf, name, home)
    finally:
        zf.close()

    progress("导入完成", total, total)
    return {
        "path": str(zip_path),
        "files": len(targets),
        "backup_dir": str(backup_dir) if backup_dir else None,
        "warnings": warnings,
        "manifest": manifest,
    }


class ZipValidationError(Exception):
    """Raised when an import zip fails validation."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("zip 校验失败: " + "; ".join(errors))


def _backup_home(home, backup_dir, include):
    for name in include:
        if name in ("sessions", "archived_sessions"):
            src = home / name
            if src.exists():
                shutil.copytree(src, backup_dir / name, dirs_exist_ok=True)
        else:
            src = home / name
            if src.exists():
                shutil.copy2(src, backup_dir / name)


def _extract_rollout(zf, name, home):
    dest = home / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = zf.read(name)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def _extract_file(zf, name, home):
    dest = home / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = zf.read(name)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
