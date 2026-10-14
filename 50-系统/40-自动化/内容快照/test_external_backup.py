from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("external_backup.py")
SPEC = importlib.util.spec_from_file_location("external_backup_test", MODULE_PATH)
assert SPEC and SPEC.loader
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class ExternalBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "vault"
        self.destination = root / "external" / "RaysBrain-Recovery"
        self.source.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.source, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.source, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.source, check=True)
        (self.source / "20-知识").mkdir()
        (self.source / "20-知识/观点.md").write_text("知识。\n", encoding="utf-8")
        (self.source / "60-素材").mkdir()
        (self.source / "60-素材/图片.bin").write_bytes(b"asset")
        cache = self.source / "50-系统/40-自动化/对标拆解/media"
        cache.mkdir(parents=True)
        (cache / "temp.mp4").write_bytes(b"temporary")
        subprocess.run(["git", "add", "20-知识/观点.md"], cwd=self.source, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.source, check=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_snapshot_includes_assets_excludes_cache_and_verifies(self) -> None:
        result = backup.run_backup(
            self.source,
            self.destination,
            keep=2,
            require_separate_device=False,
        )
        snapshot = Path(str(result["snapshot"]))
        self.assertTrue(result["verified"])
        self.assertTrue((snapshot / "vault/20-知识/观点.md").is_file())
        self.assertTrue((snapshot / "vault/60-素材/图片.bin").is_file())
        self.assertFalse((snapshot / "vault/50-系统/40-自动化/对标拆解/media").exists())
        self.assertTrue((snapshot / "history.bundle").is_file())
        self.assertEqual((self.destination / "latest").resolve(), snapshot.resolve())

    def test_nested_obsidian_and_claude_worktrees_do_not_break_verification(self) -> None:
        nested = self.source / ".claude/worktrees/old-session/.obsidian"
        nested.mkdir(parents=True)
        (nested / "app.json").write_text("{}", encoding="utf-8")
        (self.source / ".claude/worktrees/old-session/笔记.md").write_text("副本\n", encoding="utf-8")
        other = self.source / "30-资料/某项目/.obsidian"
        other.mkdir(parents=True)
        (other / "workspace.json").write_text("{}", encoding="utf-8")

        result = backup.run_backup(
            self.source,
            self.destination,
            keep=2,
            require_separate_device=False,
        )
        snapshot = Path(str(result["snapshot"]))
        self.assertTrue(result["verified"])
        self.assertFalse((snapshot / "vault/.claude/worktrees").exists())
        self.assertFalse((snapshot / "vault/30-资料/某项目/.obsidian").exists())


if __name__ == "__main__":
    unittest.main()
