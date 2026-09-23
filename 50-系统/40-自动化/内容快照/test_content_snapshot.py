from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("content_snapshot.py")
SPEC = importlib.util.spec_from_file_location("content_snapshot", MODULE_PATH)
assert SPEC and SPEC.loader
snapshot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snapshot)


class ContentSnapshotLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.originals = {
            "STATE_DIR": snapshot.STATE_DIR,
            "LOCK_DIR": snapshot.LOCK_DIR,
            "LOCK_INFO_FILE": snapshot.LOCK_INFO_FILE,
            "SUCCESS_FILE": snapshot.SUCCESS_FILE,
        }
        snapshot.STATE_DIR = self.root
        snapshot.LOCK_DIR = self.root / "content-snapshot.lock"
        snapshot.LOCK_INFO_FILE = snapshot.LOCK_DIR / "owner.json"
        snapshot.SUCCESS_FILE = self.root / "content-snapshot-success.json"

    def tearDown(self) -> None:
        for name, value in self.originals.items():
            setattr(snapshot, name, value)
        self.temp.cleanup()

    def test_live_owner_is_not_replaced(self) -> None:
        snapshot.LOCK_DIR.mkdir()
        snapshot.LOCK_INFO_FILE.write_text(
            json.dumps({"pid": os.getpid(), "started_at": snapshot.now_text()}),
            encoding="utf-8",
        )

        self.assertFalse(snapshot.acquire_lock())
        self.assertTrue(snapshot.LOCK_DIR.exists())

    def test_orphaned_lock_is_recovered(self) -> None:
        snapshot.LOCK_DIR.mkdir()
        snapshot.LOCK_INFO_FILE.write_text(
            json.dumps({"pid": 99999999, "started_at": "2026-07-22T18:05:00-07:00"}),
            encoding="utf-8",
        )

        self.assertTrue(snapshot.acquire_lock())
        owner = json.loads(snapshot.LOCK_INFO_FILE.read_text(encoding="utf-8"))
        self.assertEqual(owner["pid"], os.getpid())
        snapshot.release_lock()
        self.assertFalse(snapshot.LOCK_DIR.exists())

    def test_old_empty_lock_is_recovered(self) -> None:
        snapshot.LOCK_DIR.mkdir()
        old = time.time() - snapshot.LOCK_GRACE_SECONDS - 5
        os.utime(snapshot.LOCK_DIR, (old, old))

        self.assertEqual(snapshot.inspect_lock()["state"], "stale")
        self.assertTrue(snapshot.acquire_lock())
        snapshot.release_lock()


class ContentSnapshotRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.vault.mkdir()
        for item in snapshot.CONTENT_PATHS:
            path = self.vault / item
            if path.suffix:
                path.write_text(f"# {path.stem}\n", encoding="utf-8")
            else:
                path.mkdir(parents=True)
                (path / ".keep").write_text("", encoding="utf-8")
        self.sync_script = (
            self.vault / "50-系统/40-自动化/知识采集/weread_sync.py"
        )
        self.sync_script.parent.mkdir(parents=True)
        self.sync_script.write_text("raise SystemExit(0)\n", encoding="utf-8")
        self.run_git("init", "-q")
        self.run_git("config", "user.name", "Snapshot Test")
        self.run_git("config", "user.email", "snapshot@example.com")
        self.run_git("add", "-A")
        self.run_git("commit", "-q", "-m", "baseline")
        self.originals = {
            "VAULT": snapshot.VAULT,
            "STATE_DIR": snapshot.STATE_DIR,
            "LOCK_DIR": snapshot.LOCK_DIR,
            "LOCK_INFO_FILE": snapshot.LOCK_INFO_FILE,
            "SUCCESS_FILE": snapshot.SUCCESS_FILE,
            "WEREAD_SYNC": snapshot.WEREAD_SYNC,
        }
        snapshot.VAULT = self.vault
        snapshot.STATE_DIR = self.state
        snapshot.LOCK_DIR = self.state / "content-snapshot.lock"
        snapshot.LOCK_INFO_FILE = snapshot.LOCK_DIR / "owner.json"
        snapshot.SUCCESS_FILE = self.state / "content-snapshot-success.json"
        snapshot.WEREAD_SYNC = self.sync_script

    def tearDown(self) -> None:
        for name, value in self.originals.items():
            setattr(snapshot, name, value)
        self.temp.cleanup()

    def run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.vault,
            text=True,
            capture_output=True,
            check=True,
        )

    def test_snapshot_commits_chinese_content_and_leaves_system_files_unstaged(self) -> None:
        note = self.vault / "20-知识/新笔记.md"
        note.write_text("# 新笔记\n", encoding="utf-8")
        system_note = self.vault / "50-系统/不应提交.md"
        system_note.write_text("# 系统改动\n", encoding="utf-8")

        with patch.object(snapshot, "weread_key", return_value=""):
            self.assertEqual(snapshot.run_snapshot(), 0)

        committed = self.run_git(
            "-c", "core.quotepath=false", "show", "--name-only", "--format="
        ).stdout.splitlines()
        self.assertEqual(committed, ["20-知识/新笔记.md"])
        status = self.run_git(
            "-c", "core.quotepath=false", "status", "--short"
        ).stdout
        self.assertIn("?? 50-系统/不应提交.md", status)
        success = json.loads(snapshot.SUCCESS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(success["status"], "committed")
        self.assertFalse(snapshot.LOCK_DIR.exists())

    def test_ignored_media_directory_does_not_block_snapshot(self) -> None:
        (self.vault / ".gitignore").write_text("60-素材/\n", encoding="utf-8")
        media = self.vault / "60-素材/封面.png"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"not-a-real-png")
        note = self.vault / "20-知识/可提交.md"
        note.write_text("# 可提交\n", encoding="utf-8")

        with patch.object(snapshot, "weread_key", return_value=""):
            self.assertEqual(snapshot.run_snapshot(), 0)

        committed = self.run_git(
            "-c", "core.quotepath=false", "show", "--name-only", "--format="
        ).stdout.splitlines()
        self.assertEqual(committed, ["20-知识/可提交.md"])
        self.assertTrue(media.is_file())

    def test_partial_staging_is_rolled_back_when_git_add_fails(self) -> None:
        note = self.vault / "20-知识/不能留下半成品.md"
        note.write_text("# 不能留下半成品\n", encoding="utf-8")
        original_git = snapshot.git

        def failing_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
            if args[:3] == ("add", "-A", "--"):
                original_git("add", "--", "20-知识/不能留下半成品.md")
                raise subprocess.CalledProcessError(
                    1,
                    ["git", *args],
                    stderr="simulated add failure",
                )
            return original_git(*args, check=check)

        with (
            patch.object(snapshot, "weread_key", return_value=""),
            patch.object(snapshot, "git", side_effect=failing_git),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated add failure"):
                snapshot.run_snapshot()

        self.assertEqual(self.run_git("diff", "--cached", "--name-only").stdout, "")
        self.assertTrue(note.is_file())
        self.assertFalse(snapshot.LOCK_DIR.exists())


if __name__ == "__main__":
    unittest.main()
