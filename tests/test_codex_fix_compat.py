"""Regression tests for the restore failure "Model provider `ccs` not found".

After Codex / cc-switch updates two things could leave restored threads unable
to load: config.toml sets model_provider = "ccs" but has no matching
[model_providers.ccs] block, and the rollout_migration_skipped_rollouts skip
list now lives in ~/.codex/sqlite/*.db instead of the legacy state_5.sqlite.

All tests use isolated temp homes and never touch the real ~/.codex or
~/.cc-switch.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_history_sync import codex_fix, core, paths


class CodexConfigBlockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        core.CODEX_HOME = self.home
        core.BACKUP_ROOT = self.home / "history-sync-backups"
        core.CC_SWITCH_DB = self.home / "missing.db"
        core.CC_SWITCH_SETTINGS = self.home / "missing.json"
        core.OFFICIAL_PROVIDER_IDS = None

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_target_block_is_added(self):
        text = (
            'model_provider = "ccs"\n'
            'model = "deepseek-v4-flash"\n'
            '\n'
            '[model_providers.custom]\n'
            'base_url = "http://127.0.0.1:15721/v1"\n'
            'wire_api = "responses"\n'
        )
        new = core.normalize_codex_config(text, target_provider="ccs")
        self.assertIn("[model_providers.ccs]", new)
        self.assertIn('model_provider = "ccs"', new)
        self.assertIn('base_url = "http://127.0.0.1:15721/v1"', new)

    def test_block_creation_is_idempotent(self):
        text = (
            'model_provider = "ccs"\n'
            '[model_providers.custom]\n'
            'base_url = "http://127.0.0.1:1/v1"\n'
        )
        once = core.normalize_codex_config(text, target_provider="ccs")
        twice = core.normalize_codex_config(once, target_provider="ccs")
        self.assertEqual(once, twice)
        self.assertEqual(once.count("[model_providers.ccs]"), 1)

    def test_active_block_renamed_when_differs(self):
        text = (
            'model_provider = "custom"\n'
            '[model_providers.custom]\n'
            'base_url = "http://127.0.0.1:15721/v1"\n'
        )
        new = core.normalize_codex_config(text, target_provider="ccs")
        self.assertIn("[model_providers.ccs]", new)
        self.assertNotIn("[model_providers.custom]", new)
        self.assertIn('model_provider = "ccs"', new)

    def test_official_target_gets_no_block(self):
        text = (
            'model_provider = "openai"\n'
            '[model_providers.custom]\n'
            'base_url = "http://127.0.0.1:15721/v1"\n'
        )
        new = core.normalize_codex_config(text, target_provider="openai")
        self.assertNotIn("[model_providers.openai]", new)

    def test_no_source_block_is_not_fabricated(self):
        text = 'model_provider = "ccs"\nmodel = "x"\n'
        new = core.normalize_codex_config(text, target_provider="ccs")
        self.assertNotIn("[model_providers.ccs]", new)


class CheckConfigTomlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        core.CODEX_HOME = self.home
        core.CC_SWITCH_DB = self.home / "missing.db"
        core.CC_SWITCH_SETTINGS = self.home / "missing.json"
        core.OFFICIAL_PROVIDER_IDS = None

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_block_reported_and_fixed(self):
        (self.home / "config.toml").write_text(
            'model_provider = "ccs"\n'
            'model = "deepseek-v4-flash"\n'
            '\n'
            '[model_providers.custom]\n'
            'base_url = "http://127.0.0.1:15721/v1"\n'
            'wire_api = "responses"\n',
            encoding="utf-8",
        )
        section, _changed = codex_fix._check_config_toml(self.home, "ccs")
        text = (self.home / "config.toml").read_text(encoding="utf-8")
        self.assertIn("[model_providers.ccs]", text)
        labels = [i["label"] for i in section["items"]]
        self.assertIn("目标 provider 块", labels)
        item = next(i for i in section["items"] if i["label"] == "目标 provider 块")
        self.assertTrue(item["fixed"])


class ClearSkippedRolloutsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _mk_db(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path))
        con.execute("create table threads (id text)")
        con.execute(
            "create table rollout_migration_skipped_rollouts "
            "(rollout_path text, skip_reason text)"
        )
        con.executemany(
            "insert into rollout_migration_skipped_rollouts values (?,?)", rows
        )
        con.commit()
        con.close()

    def _count(self, path):
        con = sqlite3.connect(str(path))
        n = con.execute(
            "select count(*) from rollout_migration_skipped_rollouts"
        ).fetchone()[0]
        con.close()
        return n

    def test_sqlite_dir_db_cleared(self):
        db = self.home / "sqlite" / "codex-dev.db"
        self._mk_db(db, [("sessions/x.jsonl", "malformed_session_meta")])
        section, changed = codex_fix._clear_skipped_rollouts(self.home)
        self.assertTrue(changed)
        self.assertEqual(self._count(db), 0)
        self.assertTrue(any("codex-dev.db" in i["label"] for i in section["items"]))

    def test_legacy_and_multi_db_cleared_backup_preserved(self):
        legacy = self.home / "state_5.sqlite"
        dev = self.home / "sqlite" / "codex-dev.db"
        backup = self.home / "history-sync-backups" / "20261009" / "state_5.sqlite"
        self._mk_db(legacy, [("a.jsonl", "x")])
        self._mk_db(dev, [("b.jsonl", "x")])
        self._mk_db(backup, [("keep.jsonl", "x")])
        _section, changed = codex_fix._clear_skipped_rollouts(self.home)
        self.assertTrue(changed)
        self.assertEqual(self._count(legacy), 0)
        self.assertEqual(self._count(dev), 0)
        self.assertEqual(self._count(backup), 1)


class ComprehensiveFixE2ETest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["CODEX_HOME"] = str(self.home)
        self._orig_db = paths.cc_switch_db
        self._orig_settings = paths.cc_switch_settings
        paths.cc_switch_db = lambda: self.home / "cc-switch.db"
        paths.cc_switch_settings = lambda: self.home / "settings.json"

    def tearDown(self):
        paths.cc_switch_db = self._orig_db
        paths.cc_switch_settings = self._orig_settings
        os.environ.pop("CODEX_HOME", None)
        self.tmp.cleanup()

    def test_full_fix_repairs_config_and_skiplist(self):
        (self.home / "config.toml").write_text(
            'model_provider = "ccs"\n'
            'model = "deepseek-v4-flash"\n'
            '\n'
            '[model_providers.custom]\n'
            'base_url = "http://127.0.0.1:15721/v1"\n'
            'wire_api = "responses"\n',
            encoding="utf-8",
        )
        db = self.home / "sqlite" / "codex-dev.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db))
        con.execute(
            "create table threads (id text, rollout_path text, created_at integer, "
            "updated_at integer, model_provider text, title text, cwd text, "
            "has_user_event integer)"
        )
        con.execute(
            "create table rollout_migration_skipped_rollouts "
            "(rollout_path text, skip_reason text)"
        )
        con.execute(
            "insert into rollout_migration_skipped_rollouts "
            "values ('sessions/x.jsonl','malformed_session_meta')"
        )
        con.commit()
        con.close()

        report = codex_fix.run_comprehensive_fix(home=self.home)

        text = (self.home / "config.toml").read_text(encoding="utf-8")
        self.assertIn("[model_providers.ccs]", text)
        self.assertIn('model_provider = "ccs"', text)
        con = sqlite3.connect(str(db))
        remaining = con.execute(
            "select count(*) from rollout_migration_skipped_rollouts"
        ).fetchone()[0]
        con.close()
        self.assertEqual(remaining, 0)
        cfg = next(s for s in report["sections"] if s["title"] == "config.toml")
        self.assertIn("目标 provider 块", [i["label"] for i in cfg["items"]])


if __name__ == "__main__":
    unittest.main()
