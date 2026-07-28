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
knowledge_value_score: 88
writing_value_score: 94
timeliness: 高
source_url: "https://example.com"
---

# 一张测试卡

## 摘要

这是摘要。

## 人工审核

- [ ] 同时沉淀知识并加入候选选题
- [ ] 只沉淀为长期知识
- [ ] 只加入候选选题
- [ ] 暂缓
- [ ] 标记为可恢复的待清理项
""",
            encoding="utf-8",
        )
        self.link_inbox = self.vault / "30-资料/00-待抓取/链接收件箱.md"
        self.link_inbox.parent.mkdir(parents=True, exist_ok=True)
        self.link_inbox.write_text("# 链接收件箱\n\n## 待处理\n", encoding="utf-8")
        self.patchers = [
            mock.patch.object(dashboard, "VAULT", self.vault),
            mock.patch.object(dashboard, "REVIEW_DIR", self.review_dir),
            mock.patch.object(dashboard, "INBOX_FILE", self.inbox),
            mock.patch.object(dashboard, "LINK_INBOX_FILE", self.link_inbox),
            mock.patch.object(
                dashboard, "INTENT_QUEUE_FILE", self.vault / "50-系统/40-自动化/AI任务队列.md"
            ),
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
            "10-创作/10-灵感/10-待评估/剪藏复核/test.md", "both"
        )
        self.assertTrue(result["ok"])
        text = self.card.read_text(encoding="utf-8")
        self.assertEqual(text.count("- [x]"), 1)
        self.assertIn("- [x] 同时沉淀知识并加入候选选题", text)
        self.assertEqual(len(list(dashboard.ACTION_PATTERN.finditer(text))), 5)
        dashboard.choose_review_action(
            "10-创作/10-灵感/10-待评估/剪藏复核/test.md", None
        )
        self.assertNotIn("- [x]", self.card.read_text(encoding="utf-8"))

    def test_old_writing_action_and_card_are_migrated_to_topic(self) -> None:
        text = self.card.read_text(encoding="utf-8")
        text = text.replace(
            "- [ ] 同时沉淀知识并加入候选选题\n"
            "- [ ] 只沉淀为长期知识\n"
            "- [ ] 只加入候选选题",
            "- [ ] 同时进入长期知识库和候选选题\n"
            "- [ ] 批准进入长期知识库\n"
            "- [ ] 仅保留为写作素材",
        )
        self.card.write_text(text, encoding="utf-8")
        result = dashboard.choose_review_action(
            "10-创作/10-灵感/10-待评估/剪藏复核/test.md", "writing"
        )
        self.assertEqual(result["action"], "topic")
        updated = self.card.read_text(encoding="utf-8")
        self.assertIn("- [x] 只加入候选选题", updated)
        self.assertIn("- [ ] 同时沉淀知识并加入候选选题", updated)
        self.assertEqual(len(list(dashboard.ACTION_PATTERN.finditer(updated))), 5)

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
        self.assertEqual(card["knowledge_value"], "88")
        self.assertEqual(card["writing_value"], "94")
        self.assertEqual(card["timeliness"], "高")

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

    def test_dashboard_reads_real_creation_stages_and_feedback(self) -> None:
        topics_dir = self.vault / dashboard.LAYOUT["topics_dir"]
        topics_dir.mkdir(parents=True)
        (topics_dir / "候选低.md").write_text(
            "---\nkind: topic-candidate\nstatus: candidate\npriority_score: 96\n"
            "writing_value_score: 99\nknowledge_value_score: 72\ntimeliness: 中\n"
            "source_published_at: 2099-01-01\nfresh_until: 2099-12-31\n"
            "freshness_status: fresh\n---\n\n# 候选低\n",
            encoding="utf-8",
        )
        (topics_dir / "候选高.md").write_text(
            "---\nkind: topic-candidate\nstatus: candidate\npriority_score: 96\n"
            "writing_value_score: 80\nknowledge_value_score: 90\ntimeliness: 高\n"
            "source_published_at: 2099-01-01\nfresh_until: 2099-12-31\n"
            "freshness_status: fresh\n---\n\n# 候选高\n",
            encoding="utf-8",
        )
        (topics_dir / "候选次优.md").write_text(
            "---\nkind: topic-candidate\nstatus: candidate\npriority_score: 82\n"
            "writing_value_score: 100\nknowledge_value_score: 75\ntimeliness: 中\n"
            "source_published_at: 2099-01-01\nfresh_until: 2099-12-31\n"
            "freshness_status: fresh\n---\n\n# 候选次优\n",
            encoding="utf-8",
        )
        (topics_dir / "不是候选.md").write_text(
            "---\nkind: topic-candidate\nstatus: parked\npriority_score: 100\n---\n\n# 不应出现\n",
            encoding="utf-8",
        )
        (topics_dir / "可续写.md").write_text(
            "---\nkind: topic-candidate\nstatus: partially-published\npriority_score: 88\n"
            "writing_value_score: 91\nsource_published_at: 2099-01-01\n"
            "fresh_until: 2099-12-31\n"
            "freshness_status: fresh\n---\n\n# 一个可续写角度\n",
            encoding="utf-8",
        )
        (topics_dir / "过期续写.md").write_text(
            "---\nkind: topic-candidate\nstatus: partially-published\npriority_score: 100\n"
            "writing_value_score: 100\nsource_published_at: 2019-12-01\n"
            "fresh_until: 2020-01-01\n"
            "freshness_status: stale\n---\n\n# 不应出现的续写角度\n",
            encoding="utf-8",
        )
        task_dir = self.vault / dashboard.LAYOUT["writing_tasks_dir"]
        task_dir.mkdir(parents=True)
        (task_dir / "任务.md").write_text(
            "---\nkind: content-pack\nstatus: active\npriority_score: 91\n"
            "writing_value_score: 95\ntimeliness: 高\n---\n\n# 写作任务一\n",
            encoding="utf-8",
        )
        (task_dir / "已完成.md").write_text(
            "---\nkind: writing-task\nstatus: completed\n---\n\n# 已完成任务\n",
            encoding="utf-8",
        )
        draft_dir = self.vault / "10-创作/30-文章草稿"
        draft_dir.mkdir(parents=True)
        (draft_dir / "草稿一.md").write_text(
            "---\nkind: draft\nstatus: drafting\nwriting_value_score: 90\n---\n\n# 一篇草稿\n",
            encoding="utf-8",
        )
        (draft_dir / "备选稿.md").write_text(
            "---\nkind: draft\nstatus: alternative-draft\n---\n\n# 不应混入母稿\n",
            encoding="utf-8",
        )
        # 从长文改出来的口播稿跟着母稿走，不在母稿看板上单独占位。
        (draft_dir / "口播稿.md").write_text(
            "---\nkind: oral-script\nstatus: ready\n---\n\n# 不应混入母稿\n",
            encoding="utf-8",
        )
        # 以口播起稿、没有图文母稿的内容，它自己就是母稿，必须出现。
        oral_dir = self.vault / "10-创作/20-口播草稿"
        oral_dir.mkdir(parents=True)
        (oral_dir / "口播母稿.md").write_text(
            "---\nkind: oral-script\nstatus: draft\n---\n\n# 一篇口播母稿\n",
            encoding="utf-8",
        )
        (oral_dir / "已发口播.md").write_text(
            "---\nkind: oral-script\nstatus: published\n---\n\n# 不应混入母稿\n",
            encoding="utf-8",
        )
        published_dir = self.vault / "40-发布/10-公众号"
        published_dir.mkdir(parents=True)
        (published_dir / "成稿.md").write_text(
            "---\nkind: article-published\nstatus: published\n---\n\n# 一篇成稿\n",
            encoding="utf-8",
        )
        feedback_dir = self.vault / dashboard.LAYOUT["feedback_dir"]
        feedback_dir.mkdir(parents=True)
        (feedback_dir / "待反馈.md").write_text(
            "---\nkind: content-feedback\nstatus: pending\ndue_date: 2026-08-01\n---\n\n# 待反馈\n",
            encoding="utf-8",
        )
        (feedback_dir / "已反馈.md").write_text(
            "---\nkind: content-feedback\nstatus: reviewed\n---\n\n# 已反馈\n",
            encoding="utf-8",
        )
        processed = self.review_dir / "旧可写卡.md"
        processed.write_text(
            self.card.read_text(encoding="utf-8").replace("status: 待审核", "status: 可写作"),
            encoding="utf-8",
        )
        payload = dashboard.dashboard_payload()
        self.assertEqual(payload["schema_version"], 5)
        self.assertTrue(payload["server_started_at"])
        self.assertIn("topic-candidate", payload["board_protocol"])
        self.assertIn("content-feedback", payload["board_protocol"])
        self.assertIn("last_activity", payload["pipeline"])
        self.assertEqual(payload["pipeline"]["unresolved_errors"], 0)
        self.assertEqual(payload["reports"], [])
        self.assertEqual(
            [(item["shortcut"], item["key"]) for item in payload["review_actions"]],
            [
                (1, "knowledge"),
                (2, "topic"),
                (3, "both"),
                (4, "paused"),
                (5, "cleanup"),
            ],
        )
        self.assertEqual(
            [row["title"] for row in payload["topic_candidates"]],
            ["候选低", "候选高", "候选次优"],
        )
        self.assertEqual(
            [row["title"] for row in payload["topic_continuations"]],
            ["一个可续写角度"],
        )
        self.assertEqual([row["title"] for row in payload["writing_tasks"]], ["写作任务一"])
        self.assertEqual(
            sorted(row["title"] for row in payload["drafts"]),
            ["一篇口播母稿", "一篇草稿"],
        )
        self.assertEqual(payload["published"][0]["title"], "一篇成稿")
        self.assertEqual(payload["published"][0]["platform"], "公众号")
        self.assertNotIn("旧可写卡", [row["title"] for row in payload["topic_candidates"]])
        self.assertEqual(payload["counts"]["topic_candidates"], 3)
        self.assertEqual(payload["counts"]["topic_continuations"], 1)
        self.assertEqual(payload["counts"]["writing_tasks"], 1)
        self.assertEqual(payload["counts"]["drafts"], 2)
        self.assertEqual(payload["counts"]["published"], 1)
        self.assertEqual(payload["counts"]["feedback"], 2)
        self.assertEqual(payload["counts"]["feedback_pending"], 1)

    def test_dashboard_hides_expired_or_unknown_candidates(self) -> None:
        topics_dir = self.vault / dashboard.LAYOUT["topics_dir"]
        topics_dir.mkdir(parents=True)
        fixtures = {
            "新鲜.md": (
                "source_published_at: 2099-01-01\n"
                "fresh_until: 2099-12-31\nfreshness_status: fresh"
            ),
            "过期.md": (
                "source_published_at: 2019-12-01\n"
                "fresh_until: 2020-01-01\nfreshness_status: stale"
            ),
            "未知.md": (
                'source_published_at: ""\nfresh_until: ""\nfreshness_status: unknown'
            ),
            "无来源依据.md": "fresh_until: 2099-12-31\nfreshness_status: fresh",
        }
        for name, freshness in fixtures.items():
            (topics_dir / name).write_text(
                "---\nkind: topic-candidate\nstatus: candidate\npriority_score: 80\n"
                f"writing_value_score: 80\n{freshness}\n---\n\n# {Path(name).stem}\n",
                encoding="utf-8",
            )
        rows = dashboard.load_topic_candidates(dashboard.markdown_files())
        self.assertEqual([row["title"] for row in rows], ["新鲜"])

    def test_dashboard_surfaces_latest_health_reasons(self) -> None:
        snapshots = self.vault / ".state/health-snapshots"
        snapshots.mkdir(parents=True)
        (snapshots / "2026-07-23.json").write_text(
            json.dumps(
                {
                    "date": "2026-07-23",
                    "health": "yellow",
                    "generated_at": "2026-07-23T12:03:02-07:00",
                    "red_reasons": [],
                    "yellow_reasons": [
                        "有 2 条到期反馈尚未复盘",
                        "有 4 篇已发布内容缺少平台公开信息",
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        payload = dashboard.dashboard_payload()
        self.assertEqual(payload["health"], "yellow")
        self.assertEqual(
            payload["health_reasons"],
            [
                "有 2 条到期反馈尚未复盘",
                "有 4 篇已发布内容缺少平台公开信息",
            ],
        )

    def test_layout_overrides_from_config_file(self) -> None:
        config = self.vault / "layout.json"
        config.write_text(json.dumps({"knowledge_dir": "knowledge"}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"RAYS_BRAIN_CONFIG": str(config)}):
            layout = dashboard.load_layout()
        self.assertEqual(layout["knowledge_dir"], "knowledge")
        self.assertEqual(layout["review_dir"], dashboard.DEFAULT_LAYOUT["review_dir"])
        self.assertEqual(layout["topics_dir"], "10-创作/10-灵感/20-候选选题")
        self.assertEqual(layout["topic_reserve_dir"], "10-创作/10-灵感/90-选题储备")
        self.assertEqual(layout["writing_tasks_dir"], "10-创作/20-写作任务")
        self.assertEqual(layout["feedback_dir"], "40-发布/00-内容反馈")

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
        self.assertEqual(payload["counts"]["topic_candidates"], 1)
        self.assertEqual(payload["counts"]["writing_tasks"], 1)
        self.assertEqual(payload["counts"]["feedback_pending"], 1)
        self.assertEqual(payload["knowledge_kinds"].get("concept"), 1)
        self.assertEqual(len(payload["drafts"]), 1)
        self.assertEqual(payload["published"][0]["platform"], "公众号")
        self.assertEqual(bootstrap.build_vault(target, layout, demo=True), [])

    def test_watch_signature_changes_when_review_dir_changes(self) -> None:
        before = dashboard.watch_signature()
        self.assertTrue(before)
        (self.review_dir / "新卡.md").write_text("# 新卡\n", encoding="utf-8")
        self.assertNotEqual(before, dashboard.watch_signature())

    def test_watch_signature_tracks_candidates_and_writing_tasks(self) -> None:
        topics = self.vault / dashboard.LAYOUT["topics_dir"]
        tasks = self.vault / dashboard.LAYOUT["writing_tasks_dir"]
        topics.mkdir(parents=True)
        tasks.mkdir(parents=True)
        before = dashboard.watch_signature()
        (topics / "新候选.md").write_text("# 新候选\n", encoding="utf-8")
        after_topic = dashboard.watch_signature()
        self.assertNotEqual(before, after_topic)
        (tasks / "新任务.md").write_text("# 新任务\n", encoding="utf-8")
        self.assertNotEqual(after_topic, dashboard.watch_signature())

    def test_search_excerpt_hides_frontmatter(self) -> None:
        results = dashboard.search_notes("测试卡")
        self.assertEqual(len(results), 1)
        self.assertNotIn("capture-review", results[0]["excerpt"])
        self.assertNotIn("relevance_score", results[0]["excerpt"])

    # ---- 阅读层 ----

    def test_note_payload_returns_body_and_mtime(self) -> None:
        note = dashboard.note_payload("10-创作/10-灵感/10-待评估/剪藏复核/test.md")
        self.assertEqual(note["title"], "一张测试卡")
        self.assertEqual(note["status"], "待审核")
        self.assertNotIn("---", note["body"].split("\n", 1)[0])
        self.assertIn("## 摘要", note["body"])
        self.assertGreater(int(note["mtime_ns"]), 0)  # 字符串透传，避免 JS 数字精度丢失
        self.assertEqual(note["frontmatter"]["kind"], "capture-review")

    def test_note_payload_rejects_escaping_paths(self) -> None:
        for bad in ("../outside.md", "/etc/passwd", "README.txt", ".obsidian/app.json"):
            with self.assertRaises(ValueError):
                dashboard.note_payload(bad)

    def test_wikilink_resolves_by_stem_and_path(self) -> None:
        knowledge = self.vault / "20-知识/10-概念"
        knowledge.mkdir(parents=True)
        (knowledge / "复利效应.md").write_text("# 复利效应\n", encoding="utf-8")
        by_stem = dashboard.resolve_wikilink("复利效应")
        self.assertIsNotNone(by_stem)
        self.assertEqual(by_stem.name, "复利效应.md")
        by_path = dashboard.resolve_wikilink("20-知识/10-概念/复利效应")
        self.assertEqual(by_path, by_stem)
        with_heading = dashboard.resolve_wikilink("复利效应#定义|别名")
        self.assertEqual(with_heading, by_stem)
        self.assertIsNone(dashboard.resolve_wikilink("不存在的笔记"))

    def test_asset_lookup_by_name_and_rejects_bad_paths(self) -> None:
        assets = self.vault / "60-素材/图片"
        assets.mkdir(parents=True)
        (assets / "架构图.png").write_bytes(b"\x89PNG\r\n")
        found = dashboard.find_asset(link="架构图.png")
        self.assertEqual(found.name, "架构图.png")
        found_direct = dashboard.find_asset(rel_path="60-素材/图片/架构图.png")
        self.assertEqual(found_direct, found)
        for bad in ({"rel_path": "../x.png"}, {"link": "no.png"}, {"rel_path": "README.md"}):
            with self.assertRaises(ValueError):
                dashboard.find_asset(**bad)

    # ---- 状态流转 ----

    def _write_topic(self, name: str = "候选.md", status: str = "candidate") -> Path:
        topics_dir = self.vault / dashboard.LAYOUT["topics_dir"]
        topics_dir.mkdir(parents=True, exist_ok=True)
        path = topics_dir / name
        path.write_text(
            f"---\nkind: topic-candidate\nstatus: {status}\npriority_score: 90\n---\n\n# 一个候选\n\n正文。\n",
            encoding="utf-8",
        )
        return path

    def test_update_frontmatter_text_touches_only_named_keys(self) -> None:
        text = "---\nkind: topic-candidate\nstatus: candidate\npriority_score: 90\n---\n\n# 标题\n"
        updated = dashboard.update_frontmatter_text(text, {"status": "parked", "updated_at": "2026-07-27 10:00"})
        self.assertIn("status: parked", updated)
        self.assertIn("kind: topic-candidate", updated)
        self.assertIn("priority_score: 90", updated)
        self.assertIn('updated_at: "2026-07-27 10:00"', updated)
        self.assertTrue(updated.endswith("# 标题\n"))
        with self.assertRaises(ValueError):
            dashboard.update_frontmatter_text("# 没有属性\n", {"status": "x"})

    def test_transition_follows_protocol_and_stamps_fields(self) -> None:
        path = self._write_topic()
        rel = "10-创作/10-灵感/20-候选选题/候选.md"
        result = dashboard.apply_transition(rel, "parked", path.stat().st_mtime_ns)
        self.assertTrue(result["ok"])
        text = path.read_text(encoding="utf-8")
        self.assertIn("status: parked", text)
        self.assertIn("updated_at:", text)
        back = dashboard.apply_transition(rel, "candidate", result["mtime_ns"])
        self.assertIn("status: candidate", path.read_text(encoding="utf-8"))
        self.assertTrue(back["ok"])

    def test_transition_rejects_undeclared_or_conflicting_changes(self) -> None:
        path = self._write_topic()
        rel = "10-创作/10-灵感/20-候选选题/候选.md"
        with self.assertRaisesRegex(ValueError, "不支持"):
            dashboard.apply_transition(rel, "published", path.stat().st_mtime_ns)
        with self.assertRaisesRegex(ValueError, "修改过"):
            dashboard.apply_transition(rel, "parked", path.stat().st_mtime_ns + 1)
        review_rel = "10-创作/10-灵感/10-待评估/剪藏复核/test.md"
        with self.assertRaisesRegex(ValueError, "不支持"):
            dashboard.apply_transition(review_rel, "parked", None)

    def test_transition_rejects_wrong_directory(self) -> None:
        stray = self.vault / "20-知识/伪装候选.md"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text(
            "---\nkind: topic-candidate\nstatus: candidate\n---\n\n# 伪装\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "工作目录"):
            dashboard.apply_transition("20-知识/伪装候选.md", "parked", None)

    def test_feedback_review_roundtrip_manages_reviewed_at(self) -> None:
        feedback_dir = self.vault / dashboard.LAYOUT["feedback_dir"]
        feedback_dir.mkdir(parents=True)
        path = feedback_dir / "反馈.md"
        path.write_text(
            "---\nkind: content-feedback\nstatus: pending\n---\n\n# 反馈\n", encoding="utf-8"
        )
        rel = dashboard.relative(path)
        dashboard.apply_transition(rel, "reviewed", None)
        text = path.read_text(encoding="utf-8")
        self.assertIn("status: reviewed", text)
        self.assertRegex(text, r"reviewed_at: \d{4}-\d{2}-\d{2}")
        dashboard.apply_transition(rel, "pending", None)
        text = path.read_text(encoding="utf-8")
        self.assertIn("status: pending", text)
        self.assertIn('reviewed_at: ""', text)
        payload = dashboard.dashboard_payload()
        self.assertEqual(payload["counts"]["feedback_pending"], 1)

    def test_transition_writes_operation_log(self) -> None:
        path = self._write_topic()
        dashboard.apply_transition(
            "10-创作/10-灵感/20-候选选题/候选.md", "parked", path.stat().st_mtime_ns
        )
        log_file = self.vault / ".state/logs/dashboard-actions.jsonl"
        self.assertTrue(log_file.exists())
        entry = json.loads(log_file.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(entry["op"], "transition")
        self.assertEqual(entry["to"], "parked")

    # ---- 立项 ----

    def test_promote_validates_before_running_pipeline(self) -> None:
        self._write_topic()
        rel = "10-创作/10-灵感/20-候选选题/候选.md"
        with self.assertRaisesRegex(ValueError, "角度"):
            dashboard.promote_candidate(rel, "   ")
        with self.assertRaisesRegex(ValueError, "120"):
            dashboard.promote_candidate(rel, "长" * 121)
        with self.assertRaisesRegex(ValueError, "候选选题"):
            dashboard.promote_candidate(
                "10-创作/10-灵感/10-待评估/剪藏复核/test.md", "一个角度"
            )

    def test_promote_runs_ingest_and_relays_result(self) -> None:
        self._write_topic()
        rel = "10-创作/10-灵感/20-候选选题/候选.md"
        output = json.dumps(
            {"promoted": rel, "writing_task": "10-创作/20-写作任务/新任务.md"},
            ensure_ascii=False,
        )
        fake = mock.Mock(returncode=0, stdout=output, stderr="")
        with mock.patch.object(dashboard.subprocess, "run", return_value=fake) as run:
            result = dashboard.promote_candidate(rel, "  从 FDE 视角  拆解 ")
        self.assertTrue(result["ok"])
        self.assertEqual(result["writing_task"], "10-创作/20-写作任务/新任务.md")
        self.assertEqual(result["angle"], "从 FDE 视角 拆解")
        command = run.call_args.args[0]
        self.assertIn("promote", command)
        self.assertIn(rel, command)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["RAYS_BRAIN"], str(self.vault))

    # ---- 阶段 3：编辑与速记分流 ----

    def test_capture_with_url_goes_to_link_inbox(self) -> None:
        result = dashboard.append_capture("https://example.com/post 一条备注")
        self.assertEqual(result["target"], "link-inbox")
        text = self.link_inbox.read_text(encoding="utf-8")
        self.assertIn("- [ ] https://example.com/post 一条备注", text)
        self.assertNotIn("example.com", self.inbox.read_text(encoding="utf-8"))
        plain = dashboard.append_capture("一条普通灵感")
        self.assertEqual(plain["target"], "inbox")
        self.assertIn("一条普通灵感", self.inbox.read_text(encoding="utf-8"))

    def test_save_note_body_keeps_frontmatter_and_detects_conflict(self) -> None:
        rel = "10-创作/10-灵感/10-待评估/剪藏复核/test.md"
        note = dashboard.note_payload(rel)
        result = dashboard.save_note_body(rel, "\n# 一张测试卡\n\n改写后的正文。", note["mtime_ns"])
        self.assertTrue(result["ok"])
        text = self.card.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nkind: capture-review\n"))
        self.assertIn("改写后的正文。", text)
        self.assertNotIn("这是摘要", text)
        with self.assertRaisesRegex(ValueError, "修改过"):
            dashboard.save_note_body(rel, "again", note["mtime_ns"])
        with self.assertRaisesRegex(ValueError, "请求格式"):
            dashboard.save_note_body(rel, "again", None)

    # ---- 阶段 4：管线与意图队列 ----

    def test_queue_intent_appends_once_per_task(self) -> None:
        task_dir = self.vault / dashboard.LAYOUT["writing_tasks_dir"]
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "任务.md").write_text(
            "---\nkind: content-pack\nstatus: active\n---\n\n# 任务\n", encoding="utf-8"
        )
        rel = "10-创作/20-写作任务/任务.md"
        result = dashboard.queue_intent(rel, "draft")
        self.assertTrue(result["ok"])
        queue_text = dashboard.INTENT_QUEUE_FILE.read_text(encoding="utf-8")
        self.assertIn("kind: ai-task-queue", queue_text)
        self.assertIn("起草 · [[10-创作/20-写作任务/任务]]", queue_text)
        self.assertEqual(len(dashboard.pending_intents()), 1)
        with self.assertRaisesRegex(ValueError, "队列里"):
            dashboard.queue_intent(rel, "draft")
        with self.assertRaisesRegex(ValueError, "写作任务"):
            dashboard.queue_intent(
                "10-创作/10-灵感/10-待评估/剪藏复核/test.md", "draft"
            )
        with self.assertRaisesRegex(ValueError, "不支持"):
            dashboard.queue_intent(rel, "publish")

    def test_resolve_pipeline_error_marks_state(self) -> None:
        state_dir = self.vault / ".state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "state.json").write_text(
            json.dumps(
                {
                    "errors": [
                        {"at": "2026-07-28T10:00:00", "message": "x sync failed"},
                        {"at": "2026-07-28T11:00:00", "message": "other", "resolved_at": "done"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        result = dashboard.resolve_pipeline_error("2026-07-28T10:00:00", "x sync failed")
        self.assertTrue(result["ok"])
        data = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        self.assertTrue(data["errors"][0]["resolved_at"])
        self.assertEqual(data["errors"][0]["resolved_by"], "dashboard")
        status = dashboard.pipeline_status()
        self.assertEqual(status["errors"], [])
        with self.assertRaisesRegex(ValueError, "处理过"):
            dashboard.resolve_pipeline_error("2026-07-28T10:00:00", "x sync failed")

    def test_manual_run_refuses_reentry(self) -> None:
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        with mock.patch.dict(dashboard._MANUAL_RUN, {"proc": fake_proc, "started_at": "x"}):
            self.assertTrue(dashboard.manual_run_running())
            with self.assertRaisesRegex(ValueError, "进行中"):
                dashboard.start_manual_run()

    def test_promote_surfaces_pipeline_error(self) -> None:
        self._write_topic()
        fake = mock.Mock(
            returncode=1,
            stdout="",
            stderr='Traceback ...\nValueError: 原始资料已过时或日期不明\n',
        )
        with mock.patch.object(dashboard.subprocess, "run", return_value=fake):
            with self.assertRaisesRegex(ValueError, "原始资料已过时"):
                dashboard.promote_candidate(
                    "10-创作/10-灵感/20-候选选题/候选.md", "一个角度"
                )


class DegradedProtocolTests(unittest.TestCase):
    """协议文件缺失时的降级：服务照常启动，审核/流转功能关闭。"""

    def _load_from(self, dashboard_dir: Path):
        spec = importlib.util.spec_from_file_location(
            "rays_dashboard_degraded", dashboard_dir / "server.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module

    def test_missing_protocols_degrade_instead_of_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dashboard_dir = Path(temp) / "知识仪表盘"
            dashboard_dir.mkdir()
            (dashboard_dir / "server.py").write_bytes(SERVER_PATH.read_bytes())
            module = self._load_from(dashboard_dir)
            self.assertEqual(module.ACTION_LABELS, {})
            self.assertEqual(module.UI_ACTIONS, [])
            self.assertEqual(module.BOARD_GROUPS, [])
            # 勾选解析永不匹配：旧卡片上的勾选被视作未选择，而不是误改
            self.assertIsNone(module.selected_action("- [x] 只沉淀为长期知识\n"))
            with self.assertRaisesRegex(ValueError, "审核功能未启用"):
                module.choose_review_action(
                    "10-创作/10-灵感/10-待评估/剪藏复核/x.md", "knowledge"
                )

    def test_corrupt_review_protocol_still_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dashboard_dir = Path(temp) / "知识仪表盘"
            dashboard_dir.mkdir()
            (dashboard_dir / "server.py").write_bytes(SERVER_PATH.read_bytes())
            (Path(temp) / "review_protocol.json").write_text("{", encoding="utf-8")
            with self.assertRaises(SystemExit):
                self._load_from(dashboard_dir)


if __name__ == "__main__":
    unittest.main()
