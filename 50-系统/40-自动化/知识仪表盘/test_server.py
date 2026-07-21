from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SERVER_PATH = Path(__file__).with_name("server.py")
SPEC = importlib.util.spec_from_file_location("rays_dashboard_server", SERVER_PATH)
dashboard = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(dashboard)


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name)
        self.review_dir = self.vault / "10-创作/10-灵感/10-待评估/剪藏复核"
        self.review_dir.mkdir(parents=True)
        self.inbox = self.vault / "10-创作/10-灵感/inbox.md"
        self.inbox.parent.mkdir(parents=True, exist_ok=True)
        self.inbox.write_text("# Inbox\n", encoding="utf-8")
        self.card = self.review_dir / "test.md"
        self.card.write_text(
            """---
kind: capture-review
status: 待审核
recommendation: 保留
relevance_score: 96
source_url: "https://example.com"
---

# 一张测试卡

## 摘要

这是摘要。

## 人工审核

- [ ] 批准进入长期知识库
- [ ] 仅保留为写作素材
- [ ] 暂缓
- [ ] 标记为可恢复的待清理项
""",
            encoding="utf-8",
        )
        self.patchers = [
            mock.patch.object(dashboard, "VAULT", self.vault),
            mock.patch.object(dashboard, "REVIEW_DIR", self.review_dir),
            mock.patch.object(dashboard, "INBOX_FILE", self.inbox),
            mock.patch.object(dashboard, "STATE_HOME", self.vault / ".state"),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def test_review_action_is_single_and_reversible(self) -> None:
        result = dashboard.choose_review_action(
            "10-创作/10-灵感/10-待评估/剪藏复核/test.md", "writing"
        )
        self.assertTrue(result["ok"])
        text = self.card.read_text(encoding="utf-8")
        self.assertEqual(text.count("- [x]"), 1)
        self.assertIn("- [x] 仅保留为写作素材", text)
        dashboard.choose_review_action(
            "10-创作/10-灵感/10-待评估/剪藏复核/test.md", None
        )
        self.assertNotIn("- [x]", self.card.read_text(encoding="utf-8"))

    def test_review_path_cannot_escape_queue(self) -> None:
        with self.assertRaisesRegex(ValueError, "找不到"):
            dashboard.choose_review_action("README.md", "knowledge")

    def test_capture_appends_without_replacing_existing_content(self) -> None:
        dashboard.append_capture("第一行\n第二行")
        text = self.inbox.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Inbox"))
        self.assertIn("第一行", text)
        self.assertIn("  第二行", text)

    def test_review_card_extracts_real_fields(self) -> None:
        card = dashboard.review_card(self.card)
        self.assertEqual(card["title"], "一张测试卡")
        self.assertEqual(card["score"], 96)
        self.assertEqual(card["summary"], "这是摘要。")

    def test_dashboard_skips_unreadable_files(self) -> None:
        (self.review_dir / "broken.md").symlink_to(self.vault / "missing.md")
        knowledge = self.vault / "20-知识/10-概念"
        knowledge.mkdir(parents=True)
        (knowledge / "概念.md").write_text(
            "---\nkind: concept\n---\n\n# 一个概念\n", encoding="utf-8"
        )
        (knowledge / "broken.md").symlink_to(self.vault / "missing-too.md")
        payload = dashboard.dashboard_payload()
        self.assertEqual(payload["counts"]["decision_pending"], 1)
        self.assertEqual(payload["knowledge_kinds"], {"concept": 1})
        self.assertEqual(len(dashboard.search_notes("一个概念")), 1)

    def test_dashboard_lists_drafts_and_published(self) -> None:
        draft_dir = self.vault / "10-创作/20-草稿"
        draft_dir.mkdir(parents=True)
        (draft_dir / "草稿一.md").write_text("# 一篇草稿\n", encoding="utf-8")
        published_dir = self.vault / "40-发布/10-公众号"
        published_dir.mkdir(parents=True)
        (published_dir / "成稿.md").write_text("# 一篇成稿\n", encoding="utf-8")
        payload = dashboard.dashboard_payload()
        self.assertEqual([row["title"] for row in payload["drafts"]], ["一篇草稿"])
        self.assertEqual(payload["published"][0]["title"], "一篇成稿")
        self.assertEqual(payload["published"][0]["platform"], "公众号")

    def test_layout_overrides_from_config_file(self) -> None:
        config = self.vault / "layout.json"
        config.write_text(json.dumps({"knowledge_dir": "knowledge"}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"RAYS_BRAIN_CONFIG": str(config)}):
            layout = dashboard.load_layout()
        self.assertEqual(layout["knowledge_dir"], "knowledge")
        self.assertEqual(layout["review_dir"], dashboard.DEFAULT_LAYOUT["review_dir"])

    def test_layout_rejects_unknown_keys_and_escaping_paths(self) -> None:
        config = self.vault / "layout.json"
        for bad in ({"nope": "x"}, {"knowledge_dir": "../outside"}, {"inbox_file": "/etc/passwd"}):
            config.write_text(json.dumps(bad), encoding="utf-8")
            with mock.patch.dict(os.environ, {"RAYS_BRAIN_CONFIG": str(config)}):
                with self.assertRaises(SystemExit):
                    dashboard.load_layout()

    def test_bootstrap_builds_workable_vault(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "rays_bootstrap", SERVER_PATH.with_name("bootstrap.py")
        )
        bootstrap = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(bootstrap)
        target = self.vault / "demo-vault"
        layout = dict(dashboard.DEFAULT_LAYOUT)
        created = bootstrap.build_vault(target, layout, demo=True)
        self.assertTrue(created)
        with mock.patch.object(dashboard, "VAULT", target), \
             mock.patch.object(dashboard, "REVIEW_DIR", target / layout["review_dir"]), \
             mock.patch.object(dashboard, "INBOX_FILE", target / layout["inbox_file"]):
            payload = dashboard.dashboard_payload()
        self.assertEqual(payload["counts"]["decision_pending"], 2)
        self.assertEqual(payload["knowledge_kinds"].get("concept"), 1)
        self.assertEqual(len(payload["drafts"]), 1)
        self.assertEqual(payload["published"][0]["platform"], "公众号")
        self.assertEqual(bootstrap.build_vault(target, layout, demo=True), [])

    def test_watch_signature_changes_when_review_dir_changes(self) -> None:
        before = dashboard.watch_signature()
        self.assertTrue(before)
        (self.review_dir / "新卡.md").write_text("# 新卡\n", encoding="utf-8")
        self.assertNotEqual(before, dashboard.watch_signature())

    def test_search_excerpt_hides_frontmatter(self) -> None:
        results = dashboard.search_notes("测试卡")
        self.assertEqual(len(results), 1)
        self.assertNotIn("capture-review", results[0]["excerpt"])
        self.assertNotIn("relevance_score", results[0]["excerpt"])


if __name__ == "__main__":
    unittest.main()
