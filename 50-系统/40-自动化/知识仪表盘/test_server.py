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
knowledge_unit_count: 3
entity_count: 1
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
        # 立项前会确认采集脚本真实存在；给个占位文件让测试不依赖部署位置
        # （skill 资产只带仪表盘目录，没有知识采集），真正的调用已被 mock。
        self.ingest_script = self.vault / "知识采集/knowledge_ingest.py"
        self.ingest_script.parent.mkdir(parents=True, exist_ok=True)
        self.ingest_script.write_text("# 测试占位\n", encoding="utf-8")
        # 登记发布同理：脚本存在性检查发生在 mock 之前
        self.record_script = self.vault / "发布归档/record_published.py"
        self.record_script.parent.mkdir(parents=True, exist_ok=True)
        self.record_script.write_text("# 测试占位\n", encoding="utf-8")
        template_root = self.vault / "50-系统/30-模板"
        template_root.mkdir(parents=True, exist_ok=True)
        # 模板由发布归档脚本读取；skill 资产只带仪表盘目录，没有上游模板时用最小占位
        real_template_root = SERVER_PATH.parents[2] / "30-模板"
        for name in ("内容反馈.md", "口播视频反馈.md"):
            source = real_template_root / name
            content = (
                source.read_text(encoding="utf-8")
                if source.exists()
                else "---\nkind: content-feedback\nstatus: pending\narticle: \"\"\n"
                "platform: \"\"\nfeedback_stage: 24h\ndue_at: \"\"\n"
                "feedback_24h_status: pending\nfeedback_7d_status: pending\n---\n\n# {{title}}\n"
            )
            (template_root / name).write_text(content, encoding="utf-8")
        self.patchers = [
            mock.patch.object(dashboard, "VAULT", self.vault),
            mock.patch.object(dashboard, "REVIEW_DIR", self.review_dir),
            mock.patch.object(dashboard, "INBOX_FILE", self.inbox),
            mock.patch.object(dashboard, "LINK_INBOX_FILE", self.link_inbox),
            mock.patch.object(
                dashboard, "INTENT_QUEUE_FILE", self.vault / "50-系统/40-自动化/AI任务队列.md"
            ),
            mock.patch.object(dashboard, "INGEST_SCRIPT", self.ingest_script),
            mock.patch.object(dashboard, "RECORD_SCRIPT", self.record_script),
            mock.patch.object(dashboard, "STATE_HOME", self.vault / ".state"),
            mock.patch.object(
                dashboard,
                "TOPIC_MAP_CANDIDATE_STATE",
                self.vault / ".state/topic-map-candidates.json",
            ),
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

    def test_auto_knowledge_card_turns_topic_choice_into_both(self) -> None:
        text = self.card.read_text(encoding="utf-8")
        self.card.write_text(
            text.replace(
                "knowledge_value_score: 88",
                'knowledge_value_score: 94\nknowledge_auto_at: "2026-08-11T08:00:00-07:00"',
            ),
            encoding="utf-8",
        )

        result = dashboard.choose_review_action(
            "10-创作/10-灵感/10-待评估/剪藏复核/test.md", "topic"
        )

        self.assertEqual(result["action"], "both")
        updated = self.card.read_text(encoding="utf-8")
        self.assertIn("- [x] 同时沉淀知识并加入候选选题", updated)
        self.assertIn("- [ ] 只加入候选选题", updated)

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

    def test_dashboard_loads_topic_map_candidates(self) -> None:
        dashboard.TOPIC_MAP_CANDIDATE_STATE.parent.mkdir(parents=True)
        dashboard.TOPIC_MAP_CANDIDATE_STATE.write_text(
            json.dumps(
                {
                    "candidates": [
                        {
                            "id": "topic-context",
                            "name": "上下文工程",
                            "proposal": "拆分",
                            "note_count": 8,
                            "source_count": 4,
                            "reason": "现有地图已经过宽。",
                            "question": "上下文如何稳定进入任务？",
                            "supporting_notes": [],
                        }
                    ],
                    "observing": [{"id": "topic-memory", "name": "长期记忆"}],
                    "summary": {
                        "theme_map_count": 5,
                        "true_unmapped_count": 2,
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        payload = dashboard.dashboard_payload()

        self.assertEqual(payload["counts"]["topic_map_candidates"], 1)
        self.assertEqual(payload["topic_map_candidates"][0]["name"], "上下文工程")
        self.assertEqual(payload["topic_map_observing"][0]["name"], "长期记忆")
        self.assertEqual(payload["topic_map_summary"]["theme_map_count"], 5)

    def test_dashboard_lists_weekly_map_digest_when_available(self) -> None:
        note = self.vault / dashboard.MAP_DIGEST_NOTE_REL
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("# 知识地图整理\n", encoding="utf-8")

        payload = dashboard.dashboard_payload()

        self.assertIn(
            {
                "title": "知识地图整理",
                "path": dashboard.MAP_DIGEST_NOTE_REL,
            },
            payload["reports"],
        )

    def test_topic_map_action_is_forwarded_and_logged(self) -> None:
        returned = {
            "ok": True,
            "message": "已进入 14 天观察期",
            "state": {"candidates": [], "observing": []},
        }
        with (
            mock.patch.object(
                dashboard,
                "apply_topic_map_candidate_action",
                return_value=returned,
            ) as apply_action,
            mock.patch.object(dashboard, "log_operation") as log_operation,
        ):
            result = dashboard.choose_topic_map_candidate(
                "topic-context",
                "watch",
            )

        self.assertEqual(result, returned)
        apply_action.assert_called_once_with(
            self.vault,
            dashboard.TOPIC_MAP_CANDIDATE_STATE,
            "topic-context",
            "watch",
            self.vault / dashboard.TOPIC_MAP_CANDIDATE_NOTE_REL,
        )
        log_operation.assert_called_once()

    def test_dashboard_reads_real_creation_stages_and_feedback(self) -> None:
        topics_dir = self.vault / dashboard.LAYOUT["topics_dir"]
        topics_dir.mkdir(parents=True)
        (topics_dir / "候选低.md").write_text(
            "---\nkind: topic-candidate\nstatus: candidate\npriority_score: 96\n"
            "writing_value_score: 99\nknowledge_value_score: 72\ntimeliness: 中\n"
            "attention_entered_at: 2099-01-01\nattention_until: 2099-01-08\n"
            "attention_status: fresh\n"
            "source_published_at: 2099-01-01\nfresh_until: 2099-12-31\n"
            "freshness_status: fresh\n---\n\n# 候选低\n",
            encoding="utf-8",
        )
        (topics_dir / "候选高.md").write_text(
            "---\nkind: topic-candidate\nstatus: candidate\npriority_score: 96\n"
            "writing_value_score: 80\nknowledge_value_score: 90\ntimeliness: 高\n"
            "attention_entered_at: 2099-01-01\nattention_until: 2099-01-08\n"
            "attention_status: fresh\n"
            "source_published_at: 2099-01-01\nfresh_until: 2099-12-31\n"
            "freshness_status: fresh\n---\n\n# 候选高\n",
            encoding="utf-8",
        )
        (topics_dir / "候选次优.md").write_text(
            "---\nkind: topic-candidate\nstatus: candidate\npriority_score: 82\n"
            "writing_value_score: 100\nknowledge_value_score: 75\ntimeliness: 中\n"
            "attention_entered_at: 2099-01-01\nattention_until: 2099-01-08\n"
            "attention_status: fresh\n"
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
        oral_dir = self.vault / "10-创作/25-口播草稿"
        oral_dir.mkdir(parents=True)
        (oral_dir / "口播母稿.md").write_text(
            "---\nkind: oral-script\nstatus: draft\n---\n\n# 一篇口播母稿\n",
            encoding="utf-8",
        )
        (oral_dir / "已发口播.md").write_text(
            "---\nkind: oral-script\nstatus: published\n---\n\n# 不应混入母稿\n",
            encoding="utf-8",
        )
        # ray-kb 写的 koubo-draft 与 oral-script 同义，也必须出现。
        (oral_dir / "快速口播.md").write_text(
            "---\nkind: koubo-draft\nstatus: draft\n---\n\n# 一篇快速口播\n",
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
        self.assertEqual(payload["schema_version"], 8)
        self.assertTrue(payload["server_started_at"])
        self.assertIn("topic-candidate", payload["board_protocol"])
        self.assertIn("content-feedback", payload["board_protocol"])
        self.assertIn("last_activity", payload["pipeline"])
        self.assertEqual(payload["pipeline"]["unresolved_errors"], 0)
        self.assertEqual(payload["pipeline"]["degraded_sources"], 0)
        self.assertEqual(payload["reviews"][0]["knowledge_unit_count"], 3)
        self.assertEqual(payload["reviews"][0]["entity_count"], 1)
        self.assertEqual(payload["reviews"][0]["knowledge_auto_at"], "")
        self.assertEqual(payload["reviews"][0]["personal_context_status"], "")
        self.assertEqual(payload["review_daily_limit"], 3)
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
            ["一篇口播母稿", "一篇快速口播", "一篇草稿"],
        )
        self.assertEqual(payload["published"][0]["title"], "一篇成稿")
        self.assertEqual(payload["published"][0]["platform"], "公众号")
        self.assertNotIn("旧可写卡", [row["title"] for row in payload["topic_candidates"]])
        self.assertEqual(payload["counts"]["topic_candidates"], 3)
        self.assertEqual(payload["counts"]["topic_continuations"], 1)
        self.assertEqual(payload["counts"]["writing_tasks"], 1)
        self.assertEqual(payload["counts"]["drafts"], 3)
        # 已到期的复盘排在稿子和审核前面。
        self.assertEqual(payload["focus"], "先复盘 1 篇已到期的发布内容")
        self.assertEqual((payload["focus_count"], payload["focus_target"]), (1, "feedback"))
        self.assertEqual(payload["counts"]["published"], 1)
        self.assertEqual(payload["counts"]["feedback"], 2)
        self.assertEqual(payload["counts"]["feedback_pending"], 1)

    def test_dashboard_uses_attention_window_instead_of_source_freshness(self) -> None:
        topics_dir = self.vault / dashboard.LAYOUT["topics_dir"]
        topics_dir.mkdir(parents=True)
        fixtures = {
            "新鲜.md": (90, (
                "source_published_at: 2099-01-01\n"
                "fresh_until: 2099-12-31\nfreshness_status: fresh\n"
                "attention_entered_at: 2099-01-01\n"
                "attention_until: 2099-01-08\nattention_status: fresh"
            )),
            "资料过期但刚进候选.md": (80, (
                "source_published_at: 2019-12-01\n"
                "fresh_until: 2020-01-01\nfreshness_status: stale\n"
                "attention_entered_at: 2099-01-01\n"
                "attention_until: 2099-01-08\nattention_status: fresh"
            )),
            "来源日期未知.md": (70, (
                'source_published_at: ""\nfresh_until: ""\nfreshness_status: unknown\n'
                "attention_entered_at: 2099-01-01\n"
                "attention_until: 2099-01-08\nattention_status: fresh"
            )),
            "候选期已结束.md": (100, (
                "source_published_at: 2099-01-01\nfresh_until: 2099-12-31\n"
                "freshness_status: fresh\nattention_entered_at: 2020-01-01\n"
                "attention_until: 2020-01-08\nattention_status: expired"
            )),
        }
        for name, (priority, fields) in fixtures.items():
            (topics_dir / name).write_text(
                f"---\nkind: topic-candidate\nstatus: candidate\npriority_score: {priority}\n"
                f"writing_value_score: 80\n{fields}\n---\n\n# {Path(name).stem}\n",
                encoding="utf-8",
            )
        rows = dashboard.load_topic_candidates(dashboard.markdown_files())
        self.assertEqual(
            [row["title"] for row in rows],
            ["新鲜", "资料过期但刚进候选", "来源日期未知"],
        )

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
                        "有 4 篇已发布内容的平台资料填写不正确",
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
                "有 4 篇已发布内容的平台资料填写不正确",
            ],
        )

    def test_focus_prefers_drafts_over_review_and_review_over_backlog(self) -> None:
        self.assertEqual(dashboard.dashboard_payload()["focus_target"], "review")
        oral_dir = self.vault / "10-创作/25-口播草稿"
        oral_dir.mkdir(parents=True)
        (oral_dir / "快速口播.md").write_text(
            "---\nkind: koubo-draft\nstatus: draft\n---\n\n# 一篇快速口播\n",
            encoding="utf-8",
        )
        payload = dashboard.dashboard_payload()
        self.assertEqual(payload["focus"], "推进 1 篇手上的稿子")
        self.assertEqual(payload["focus_target"], "drafts")

    def test_feedback_overdue_respects_due_date(self) -> None:
        self.assertTrue(dashboard.is_past_due("2026-01-01"))
        self.assertTrue(dashboard.is_past_due("2026-01-01T08:00:00+00:00"))
        self.assertFalse(dashboard.is_past_due("2999-01-01"))
        self.assertFalse(dashboard.is_past_due(""))
        self.assertFalse(dashboard.is_past_due("下周"))

    def test_live_runtime_supersedes_stale_snapshot_errors(self) -> None:
        # 快照记下的错误随后已在采集状态里标记解决，工作台不应继续报红。
        snapshots = self.vault / ".state/health-snapshots"
        snapshots.mkdir(parents=True)
        (snapshots / "2026-07-23.json").write_text(
            json.dumps(
                {
                    "date": "2026-07-23",
                    "health": "red",
                    "red_reasons": ["存在 19 个未解决错误", "X 书签已连续 3 轮读取失败"],
                    "yellow_reasons": ["有 2 条到期反馈尚未复盘"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        payload = dashboard.dashboard_payload()
        self.assertEqual(payload["health"], "yellow")
        self.assertEqual(payload["health_reasons"], ["有 2 条到期反馈尚未复盘"])

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
            f"---\nkind: topic-candidate\nstatus: {status}\npriority_score: 90\n"
            "attention_entered_at: 2099-01-01\nattention_until: 2099-01-08\n"
            "attention_status: fresh\n---\n\n# 一个候选\n\n正文。\n",
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
        self.assertIn("attention_status: inactive", text)
        back = dashboard.apply_transition(rel, "candidate", result["mtime_ns"])
        reopened = path.read_text(encoding="utf-8")
        self.assertIn("status: candidate", reopened)
        self.assertIn("attention_status: fresh", reopened)
        self.assertRegex(reopened, r"attention_until: \d{4}-\d{2}-\d{2}")
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

    def test_feedback_review_roundtrip_manages_completed_at(self) -> None:
        feedback_dir = self.vault / dashboard.LAYOUT["feedback_dir"]
        feedback_dir.mkdir(parents=True)
        path = feedback_dir / "反馈.md"
        path.write_text(
            "---\nkind: content-feedback\nstatus: pending\n---\n\n# 反馈\n", encoding="utf-8"
        )
        rel = dashboard.relative(path)
        dashboard.apply_transition(rel, "complete", None)
        text = path.read_text(encoding="utf-8")
        self.assertIn("status: complete", text)
        self.assertRegex(text, r'feedback_completed_at: "\d{4}-\d{2}-\d{2} \d{2}:\d{2}"')
        dashboard.apply_transition(rel, "pending", None)
        text = path.read_text(encoding="utf-8")
        self.assertIn("status: pending", text)
        self.assertIn('feedback_completed_at: ""', text)
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

    def test_pipeline_status_exposes_degraded_source(self) -> None:
        state_dir = self.vault / ".state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "state.json").write_text(
            json.dumps(
                {
                    "errors": [],
                    "source_health": {
                        "mobile": {
                            "label": "手机分享",
                            "status": "degraded",
                            "consecutive_failures": 4,
                            "last_failure_at": "2026-07-30T00:00:00-07:00",
                            "message": "Resource deadlock avoided",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        status = dashboard.pipeline_status()

        self.assertEqual(len(status["degraded_sources"]), 1)
        self.assertEqual(
            status["degraded_sources"][0]["consecutive_failures"],
            4,
        )

    def test_start_draft_run_validates_target_and_reentry(self) -> None:
        task_dir = self.vault / dashboard.LAYOUT["writing_tasks_dir"]
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "任务.md").write_text(
            "---\nkind: content-pack\nstatus: active\n---\n\n# 任务\n", encoding="utf-8"
        )
        (task_dir / "已发.md").write_text(
            "---\nkind: content-pack\nstatus: published\n---\n\n# 已发\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "写作任务"):
            dashboard.start_draft_run("10-创作/10-灵感/10-待评估/剪藏复核/test.md")
        with self.assertRaisesRegex(ValueError, "不需要起草"):
            dashboard.start_draft_run("10-创作/20-写作任务/已发.md")
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        with mock.patch.dict(
            dashboard._DRAFT_RUN, {"proc": fake_proc, "started_at": "x", "target": "y"}
        ):
            self.assertTrue(dashboard.draft_run_running())
            with self.assertRaisesRegex(ValueError, "起草中"):
                dashboard.start_draft_run("10-创作/20-写作任务/任务.md")
        status = dashboard.pipeline_status()
        self.assertFalse(status["draft_run_running"])

    def test_manual_run_refuses_reentry(self) -> None:
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        with mock.patch.dict(dashboard._MANUAL_RUN, {"proc": fake_proc, "started_at": "x"}):
            self.assertTrue(dashboard.manual_run_running())
            with self.assertRaisesRegex(ValueError, "进行中"):
                dashboard.start_manual_run()

    # ---- 发布回流 ----

    def _write_publish_chain(self) -> tuple[Path, Path]:
        task_dir = self.vault / dashboard.LAYOUT["writing_tasks_dir"]
        task_dir.mkdir(parents=True, exist_ok=True)
        pack = task_dir / "任务.md"
        pack.write_text(
            "---\nkind: content-pack\nstatus: active\ncontent_id: article-abc\n---\n\n# 成稿包\n",
            encoding="utf-8",
        )
        draft_dir = self.vault / "10-创作/30-文章草稿"
        draft_dir.mkdir(parents=True, exist_ok=True)
        draft = draft_dir / "草稿.md"
        draft.write_text(
            "---\nkind: draft\nstatus: active-draft\ncontent_id: article-abc\n"
            "content_pack: \"[[10-创作/20-写作任务/任务]]\"\n---\n\n# 一篇草稿\n\n正文。\n",
            encoding="utf-8",
        )
        return draft, pack

    def test_record_publication_archives_and_backfills(self) -> None:
        draft, pack = self._write_publish_chain()
        real_script = Path(__file__).resolve().parent.parent / "发布归档/record_published.py"
        if not real_script.exists():
            self.skipTest("需要上游 发布归档/record_published.py，skill 资产不包含")
        with mock.patch.object(dashboard, "RECORD_SCRIPT", real_script):
            result = dashboard.record_publication(
                "10-创作/30-文章草稿/草稿.md",
                "x",
                "https://x.com/wangray/status/2079091643693863273",
                "2026-07-28T10:00:00-07:00",
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["created_article"])
        article = self.vault / "40-发布/10-X长文/草稿.md"
        self.assertTrue(article.exists())
        article_text = article.read_text(encoding="utf-8")
        self.assertIn("kind: article-published", article_text)
        self.assertIn("status: published", article_text)
        self.assertIn("90-归档/30-创作记录/旧文章草稿/2026-07/草稿", article_text)
        self.assertIn("90-归档/30-创作记录/旧成稿包/2026-07/任务", article_text)
        self.assertFalse(draft.exists())
        self.assertFalse(pack.exists())
        archived_draft = self.vault / "90-归档/30-创作记录/旧文章草稿/2026-07/草稿.md"
        archived_pack = self.vault / "90-归档/30-创作记录/旧成稿包/2026-07/任务.md"
        self.assertTrue(archived_draft.exists())
        self.assertTrue(archived_pack.exists())
        self.assertIn("status: archived", archived_draft.read_text(encoding="utf-8"))
        self.assertIn("status: archived", archived_pack.read_text(encoding="utf-8"))
        self.assertIn("发布后收尾", "\n".join(result["report"]))

    def test_record_publication_reuses_existing_article(self) -> None:
        draft, _ = self._write_publish_chain()
        existing_dir = self.vault / "40-发布/10-X长文"
        existing_dir.mkdir(parents=True, exist_ok=True)
        existing = existing_dir / "已归档.md"
        existing.write_text(
            "---\nkind: article-published\nstatus: published\ncontent_id: article-abc\n---\n\n# 已归档\n",
            encoding="utf-8",
        )
        real_script = Path(__file__).resolve().parent.parent / "发布归档/record_published.py"
        if not real_script.exists():
            self.skipTest("需要上游 发布归档/record_published.py，skill 资产不包含")
        with mock.patch.object(dashboard, "RECORD_SCRIPT", real_script):
            result = dashboard.record_publication(
                "10-创作/30-文章草稿/草稿.md",
                "wechat",
                "https://mp.weixin.qq.com/s/AbCdEfGh12345",
                "2026-07-28T10:00:00-07:00",
            )
        self.assertFalse(result["created_article"])
        self.assertEqual(result["article"], "40-发布/10-X长文/已归档.md")
        self.assertFalse((self.vault / "40-发布/10-X长文/草稿.md").exists())
        self.assertFalse(draft.exists())
        self.assertTrue(
            (self.vault / "90-归档/30-创作记录/旧文章草稿/2026-07/草稿.md").exists()
        )
        existing_text = existing.read_text(encoding="utf-8")
        self.assertIn("90-归档/30-创作记录/旧文章草稿/2026-07/草稿", existing_text)
        self.assertIn("90-归档/30-创作记录/旧成稿包/2026-07/任务", existing_text)

    def test_record_publication_validates_input(self) -> None:
        self._write_publish_chain()
        with self.assertRaisesRegex(ValueError, "平台"):
            dashboard.record_publication("10-创作/30-文章草稿/草稿.md", "weibo", "https://x", "2026-07-28T10:00:00-07:00")
        with self.assertRaisesRegex(ValueError, "只能对草稿"):
            dashboard.record_publication(
                "10-创作/10-灵感/10-待评估/剪藏复核/test.md", "x", "https://x.com/1", "2026-07-28T10:00:00-07:00"
            )

    def test_record_publication_accepts_optional_url_and_time(self) -> None:
        draft, pack = self._write_publish_chain()
        real_script = Path(__file__).resolve().parent.parent / "发布归档/record_published.py"
        if not real_script.exists():
            self.skipTest("需要上游 发布归档/record_published.py，skill 资产不包含")
        with mock.patch.object(dashboard, "RECORD_SCRIPT", real_script):
            result = dashboard.record_publication(
                "10-创作/30-文章草稿/草稿.md", "x", "", ""
            )
        self.assertTrue(result["ok"])
        article = self.vault / "40-发布/10-X长文/草稿.md"
        meta = dashboard.parse_frontmatter(article.read_text(encoding="utf-8"))
        self.assertEqual(meta["x_article_status"], "published")
        self.assertEqual(meta["x_article_url"], "")
        self.assertEqual(meta["x_article_published_at"], "")
        self.assertFalse(draft.exists())
        self.assertFalse(pack.exists())

    def test_note_payload_flags_publishable_notes(self) -> None:
        draft, _ = self._write_publish_chain()
        self.assertTrue(dashboard.note_payload("10-创作/30-文章草稿/草稿.md")["can_record_publish"])
        self.assertFalse(
            dashboard.note_payload("10-创作/10-灵感/10-待评估/剪藏复核/test.md")["can_record_publish"]
        )

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


    # ---- 嵌入、查询、正文勾选、发布面板、网页复盘 ----

    def write_note(self, rel: str, text: str) -> Path:
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_embed_renders_base_view_and_heading_section(self) -> None:
        self.write_note("50-系统/50-视图/候选.base", """filters:
  and:
    - file.inFolder("10-创作/候选")
    - kind == "topic-candidate"
properties:
  file.name:
    displayName: 选题
views:
  - type: table
    name: 待选择
    filters:
      and:
        - status == "candidate"
        - attention_until > today()
    order:
      - file.name
      - priority_score
    sort:
      - property: priority_score
        direction: DESC
""")
        self.write_note("10-创作/候选/甲.md", "---\nkind: topic-candidate\nstatus: candidate\npriority_score: 70\nattention_until: 2999-01-01\n---\n# 甲\n")
        self.write_note("10-创作/候选/乙.md", "---\nkind: topic-candidate\nstatus: candidate\npriority_score: 90\nattention_until: 2999-01-01\n---\n# 乙\n")
        self.write_note("10-创作/候选/过期.md", "---\nkind: topic-candidate\nstatus: candidate\npriority_score: 99\nattention_until: 2000-01-01\n---\n# 过期\n")
        self.write_note("10-创作/候选/关闭.md", "---\nkind: topic-candidate\nstatus: closed\n---\n# 关闭\n")
        result = dashboard.embed_payload("50-系统/50-视图/候选.base#待选择")
        self.assertEqual(result["type"], "base")
        self.assertEqual([row["title"] for row in result["rows"]], ["乙", "甲"])
        self.assertEqual(result["columns"][0]["label"], "选题")
        self.assertEqual(result["unsupported"], "")

        self.write_note("00-入口/周报.md", "# 周报\n\n## 本周总览\n\n总览内容\n\n### 细节\n\n细节内容\n\n## 下一节\n\n不该出现\n")
        section = dashboard.embed_payload("00-入口/周报#本周总览")
        self.assertEqual(section["heading"], "本周总览")
        self.assertIn("细节内容", section["body"])
        self.assertNotIn("不该出现", section["body"])

    def test_query_block_supports_path_regex_or_and_negation(self) -> None:
        self.write_note("10-创作/草稿/甲.md", "---\nkind: draft\nstatus: draft\n---\n# 甲\n")
        self.write_note("10-创作/草稿/乙.md", "---\nkind: koubo-draft\nstatus: draft\n---\n# 乙\n")
        self.write_note("10-创作/草稿/丙.md", "---\nkind: draft\nstatus: published\n---\n# 丙\n")
        self.write_note("10-创作/别处/丁.md", "---\nkind: draft\nstatus: draft\n---\n# 丁\n")
        result = dashboard.query_payload(
            'path:"10-创作/草稿" (/^kind: draft$/ OR /^kind: koubo-draft$/) -/^status: published$/'
        )
        self.assertEqual(sorted(row["title"] for row in result["rows"]), ["乙", "甲"])
        with self.assertRaisesRegex(ValueError, "还不支持"):
            dashboard.query_payload("(path:x")

    def test_toggle_task_checks_line_and_converts_plain_bullet(self) -> None:
        rel = "10-创作/25-口播草稿/稿.md"
        path = self.write_note(rel, "---\nkind: koubo-draft\nstatus: draft\n---\n# 稿\n\n## 待确认\n\n- [ ] 第一条\n- 第二条\n")
        note = dashboard.note_payload(rel)
        body_lines = note["body"].split("\n")
        first, second = body_lines.index("- [ ] 第一条"), body_lines.index("- 第二条")
        result = dashboard.toggle_task(rel, first, True, note["mtime_ns"])
        result = dashboard.toggle_task(rel, second, True, result["mtime_ns"])
        text = path.read_text(encoding="utf-8")
        self.assertIn("- [x] 第一条", text)
        self.assertIn("- [x] 第二条", text)
        self.assertTrue(text.startswith("---\nkind: koubo-draft"))
        with self.assertRaisesRegex(ValueError, "刚被其他程序修改"):
            dashboard.toggle_task(rel, first, False, note["mtime_ns"])
        with self.assertRaisesRegex(ValueError, "不是勾选项"):
            dashboard.toggle_task(rel, 0, True, result["mtime_ns"])

    def test_publish_panel_collects_platforms_links_and_local_video(self) -> None:
        video = self.vault / "out.mp4"
        video.write_bytes(b"\x00" * 64)
        self.write_note("40-发布/00-内容反馈/反馈.md", "---\nkind: content-feedback\nstatus: pending\n---\n# 反馈\n")
        rel = "40-发布/50-口播视频/视频.md"
        self.write_note(rel, f"""---
kind: video-published
status: published
platform: 视频号；抖音
video_file: "{video}"
douyin_status: published
douyin_url: "https://www.douyin.com/video/1"
x_article_draft_url: "javascript:alert(1)"
feedback_file: "[[40-发布/00-内容反馈/反馈]]"
---
# 视频
""")
        panel = dashboard.note_payload(rel)["publish"]
        self.assertTrue(panel["video"])
        rows = {row["label"]: row for row in panel["platforms"]}
        self.assertEqual(set(rows), {"视频号", "抖音"})
        self.assertEqual(rows["抖音"]["url"], "https://www.douyin.com/video/1")
        self.assertEqual(panel["links"], [{"label": "反馈卡", "path": "40-发布/00-内容反馈/反馈.md"}])
        self.assertEqual(dashboard.media_file(rel, "video_file"), video)
        with self.assertRaisesRegex(ValueError, "不支持"):
            dashboard.media_file(rel, "source_file")

    def test_feedback_metrics_and_verdict_from_dashboard(self) -> None:
        if dashboard.feedback_snapshot is None:
            self.skipTest("反馈复盘脚本不在")
        rel = "40-发布/00-内容反馈/2026-10-01-测试.md"
        path = self.write_note(rel, """---
kind: content-feedback
status: pending
platform: 抖音
feedback_stage: 24h
due_at: "2026-10-02T00:00:00-07:00"
feedback_24h_status: pending
feedback_7d_status: pending
---

# 口播视频反馈：测试

## 24 小时事实快照

### 抖音

- 播放：
- 点赞：
- 抓取时间：

## 24 小时初步信号

- 评论区在追问什么：
""")
        note = dashboard.note_payload(rel)
        fields = note["feedback"]["fields"]
        self.assertEqual([(f["platform"], f["label"]) for f in fields], [("抖音", "播放"), ("抖音", "点赞")])
        result = dashboard.save_feedback_metrics(
            rel, {str(fields[0]["line"]): "1200", str(fields[1]["line"]): "30"}, note["mtime_ns"]
        )
        self.assertEqual(result["changed"], 2)
        self.assertIn("- 播放：1200", path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "结构刚变过"):
            dashboard.save_feedback_metrics(rel, {"0": "x"}, result["mtime_ns"])
        verdict = dashboard.record_feedback_verdict(rel, "换角度重写", "开头没留住人")
        self.assertTrue(verdict["ok"])
        text = path.read_text(encoding="utf-8")
        self.assertIn("status: complete", text)
        self.assertIn("- 结论：换角度重写", text)
        self.assertTrue((self.vault / dashboard.feedback_snapshot.WORKBENCH_REL).exists())
        with self.assertRaisesRegex(ValueError, "不认识"):
            dashboard.record_feedback_verdict(rel, "随便", "")

    def test_performance_groups_by_series_and_issues_come_from_snapshot(self) -> None:
        self.write_note("40-发布/10-X长文/文章.md", "---\nkind: article-published\nseries: 出海美卡\n---\n# 一篇文章\n")
        self.write_note("40-发布/00-内容反馈/文章.md", """---
kind: content-feedback
status: complete
article: "[[40-发布/10-X长文/文章]]"
published_at: "2026-09-01T00:00:00+00:00"
---
# 反馈

## 24 小时

- 曝光：100
- 收藏：1

## 7 天

- 曝光：2000
- 收藏：40
""")
        rows = dashboard.load_performance(dashboard.markdown_files())
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["title"], rows[0]["series"], rows[0]["views"], rows[0]["bookmarks"]), ("一篇文章", "出海美卡", 2000, 40))
        self.assertEqual(rows[0]["bookmark_rate"], 2.0)
        snapshots = self.vault / ".state/health-snapshots"
        snapshots.mkdir(parents=True)
        (snapshots / "2026-10-08.json").write_text(json.dumps({
            "broken_links": [{"source": "40-发布/10-X长文/文章.md", "target": "不存在"}],
            "underlinked_knowledge": [],
        }, ensure_ascii=False), encoding="utf-8")
        groups = dashboard.health_issue_groups()
        self.assertEqual([g["label"] for g in groups], ["无法解析的链接"])
        self.assertTrue(groups[0]["items"][0]["openable"])

    def test_review_card_points_to_local_source(self) -> None:
        self.write_note("30-资料/30-X书签/原帖.md", "# 原帖\n")
        text = self.card.read_text(encoding="utf-8")
        self.card.write_text(text.replace('source_url:', 'source_file: "30-资料/30-X书签/原帖.md"\nsource_url:'), encoding="utf-8")
        card = dashboard.load_reviews()[0]
        self.assertEqual(card["source_note"], "30-资料/30-X书签/原帖.md")


class ObsidianViewsTests(unittest.TestCase):
    views = dashboard.obsidian_views

    def test_yaml_subset_handles_next_line_scalars_and_nested_lists(self) -> None:
        spec = self.views.parse_yaml("""# 注释
views:
  - type: table
    name: 待复盘
    filters:
      status == "pending"
  - name: 可续写
    filters:
      and:
        - fresh_until >= today()
        - or:
            - source_published_at != null
            - and:
                - refreshed_at != null
    sort:
      - property: priority_score
        direction: DESC
""")
        first, second = spec["views"]
        self.assertEqual(first["filters"], 'status == "pending"')
        self.assertEqual(second["filters"]["and"][1]["or"][1], {"and": ["refreshed_at != null"]})
        self.assertEqual(second["sort"], [{"property": "priority_score", "direction": "DESC"}])

    def test_filters_compare_numbers_dates_and_null(self) -> None:
        note = ("a/b.md", "b", {"score": "9", "until": "2026-10-15", "empty": ""})
        evaluate = lambda expr: self.views.eval_filter(expr, note, "2026-10-08")
        self.assertTrue(evaluate("score > 10") is False)
        self.assertTrue(evaluate("until > today()"))
        self.assertTrue(evaluate("empty == null"))
        self.assertTrue(evaluate('file.inFolder("a")'))
        self.assertTrue(evaluate({"not": ["score == 9"]}) is False)
        with self.assertRaises(self.views.Unsupported):
            evaluate("score.contains(1)")


class TailnetAccessTests(unittest.TestCase):
    @staticmethod
    def _handler(headers: dict[str, str]):
        handler = dashboard.DashboardHandler.__new__(dashboard.DashboardHandler)
        handler.headers = headers
        return handler

    def test_request_host_handles_ports_and_ipv6_literals(self) -> None:
        self.assertEqual(dashboard.request_host("127.0.0.1:8765"), "127.0.0.1")
        self.assertEqual(dashboard.request_host("[::1]:8765"), "::1")
        self.assertEqual(dashboard.request_host("Creator-Mac.Example.TS.NET:8765"), "creator-mac.example.ts.net")
        self.assertEqual(dashboard.request_host(""), "")

    def test_is_tailnet_address_only_matches_tailscale_ranges(self) -> None:
        self.assertTrue(dashboard.is_tailnet_address("100.64.0.42"))
        self.assertTrue(dashboard.is_tailnet_address("fd7a:115c:a1e0::b001:e69b"))
        self.assertFalse(dashboard.is_tailnet_address("192.168.1.10"))
        self.assertFalse(dashboard.is_tailnet_address("8.8.8.8"))
        self.assertFalse(dashboard.is_tailnet_address("not-an-ip"))

    def test_tailnet_host_and_origin_require_optin(self) -> None:
        # 默认（未开 --tailnet）：tailnet 主机名一律拒绝，行为与旧版一致
        self.assertFalse(self._handler({"Host": "100.64.0.42:8765"}).allowed_host())
        self.assertTrue(self._handler({"Host": "127.0.0.1:8765"}).allowed_host())
        extra = {"100.64.0.42", "creator-mac.example.ts.net"}
        with mock.patch.object(dashboard, "EXTRA_ALLOWED_HOSTS", extra):
            self.assertTrue(self._handler({"Host": "100.64.0.42:8765"}).allowed_host())
            self.assertTrue(self._handler({"Host": "creator-mac.example.ts.net:8765"}).allowed_host())
            # DNS rebinding：Host 不在白名单仍然拒绝
            self.assertFalse(self._handler({"Host": "evil.example.com:8765"}).allowed_host())
            self.assertTrue(self._handler({"Origin": "http://creator-mac.example.ts.net:8765"}).local_origin())
            self.assertTrue(self._handler({"Origin": "https://creator-mac.example.ts.net"}).local_origin())
            self.assertFalse(self._handler({"Origin": "http://evil.example.com"}).local_origin())
        self.assertFalse(self._handler({"Origin": "http://creator-mac.example.ts.net:8765"}).local_origin())

    def test_ponte_hosts_allowed_without_optin(self) -> None:
        # Surge Ponte 在本机把 *.sgponte 解析到 127.0.0.1，视同本机访问，无需开关
        self.assertTrue(self._handler({"Host": "creator-mac.sgponte:8765"}).allowed_host())
        self.assertTrue(self._handler({"Origin": "http://creator-mac.sgponte:8765"}).local_origin())
        # 不是该后缀的域名不受影响
        self.assertFalse(self._handler({"Host": "sgponte.evil.com:8765"}).allowed_host())
        self.assertFalse(self._handler({"Host": "evil-sgponte.com:8765"}).allowed_host())


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
