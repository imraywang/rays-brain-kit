from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("knowledge_health.py")
SPEC = importlib.util.spec_from_file_location("knowledge_health", MODULE_PATH)
assert SPEC and SPEC.loader
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)


class CreationHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.original_paths = {
            name: getattr(health, name)
            for name in (
                "VAULT",
                "CANDIDATE_DIR",
                "TOPIC_RESERVE_DIR",
                "WRITING_TASK_DIR",
                "ARTICLE_DRAFT_DIR",
                "ORAL_DRAFT_DIR",
                "CONTENT_FEEDBACK_DIR",
                "REVIEW_DIR",
                "KNOWLEDGE_DIR",
                "TOPIC_MAP_CANDIDATE_STATE",
                "TOPIC_MAP_CANDIDATE_NOTE",
                "CONFIG_FILE",
            )
        }
        health.VAULT = Path(self.temp.name)
        health.CANDIDATE_DIR = health.VAULT / "10-创作/10-灵感/20-候选选题"
        health.TOPIC_RESERVE_DIR = health.VAULT / "10-创作/10-灵感/90-选题储备"
        health.WRITING_TASK_DIR = health.VAULT / "10-创作/20-写作任务"
        health.ARTICLE_DRAFT_DIR = health.VAULT / "10-创作/30-文章草稿"
        health.ORAL_DRAFT_DIR = health.VAULT / "10-创作/25-口播草稿"
        health.CONTENT_FEEDBACK_DIR = health.VAULT / "40-发布/00-内容反馈"
        health.REVIEW_DIR = health.VAULT / "10-创作/10-灵感/10-待评估/剪藏复核"
        health.KNOWLEDGE_DIR = health.VAULT / "20-知识"
        health.TOPIC_MAP_CANDIDATE_STATE = health.VAULT / ".state/topic-map-candidates.json"
        health.TOPIC_MAP_CANDIDATE_NOTE = health.VAULT / "00-入口/10-工作台/主题地图候选.md"
        health.CONFIG_FILE = health.VAULT / ".state/config.json"

    def tearDown(self) -> None:
        for name, value in self.original_paths.items():
            setattr(health, name, value)
        self.temp.cleanup()

    @staticmethod
    def note(frontmatter: str, body: str = "") -> str:
        return f"---\n{frontmatter.strip()}\n---\n\n{body}\n"

    def write(self, relative: str, text: str) -> None:
        path = health.VAULT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def seed_representative_notes(self) -> None:
        candidate_root = "10-创作/10-灵感/20-候选选题"
        self.write(
            f"{candidate_root}/新候选.md",
            self.note(
                """
kind: topic-candidate
status: candidate
last_progress_at: 2026-07-22T10:00:00-07:00
attention_entered_at: 2026-07-22T10:00:00-07:00
attention_until: 2026-07-29
attention_status: fresh
review_after: 2026-07-29
source_published_at: 2026-07-22
fresh_until: 2026-08-01
freshness_status: fresh
"""
            ),
        )
        self.write(
            f"{candidate_root}/十四天未推进.md",
            self.note(
                """
kind: topic-candidate
status: candidate
last_progress_at: 2026-07-01T10:00:00-07:00
attention_entered_at: 2026-07-01T10:00:00-07:00
attention_until: 2026-07-08
attention_status: expired
review_after: 2026-07-08
source_published_at: 2026-07-22
fresh_until: 2026-08-01
freshness_status: fresh
"""
            ),
        )
        self.write(
            f"{candidate_root}/到复查日.md",
            self.note(
                """
kind: topic-candidate
status: candidate
last_progress_at: 2026-07-22T10:00:00-07:00
attention_entered_at: 2026-07-22T10:00:00-07:00
attention_until: 2026-07-29
attention_status: fresh
review_after: 2026-07-29
source_published_at: 2026-07-19
fresh_until: 2026-08-01
freshness_status: fresh
"""
            ),
        )
        self.write(
            f"{candidate_root}/已过期.md",
            self.note(
                """
kind: topic-candidate
status: candidate
last_progress_at: 2026-07-22T10:00:00-07:00
attention_entered_at: 2026-07-22T10:00:00-07:00
attention_until: 2026-07-29
attention_status: fresh
valid_until: 2026-07-10
source_published_at: 2026-06-26
fresh_until: 2026-07-10
freshness_status: stale
"""
            ),
        )
        self.write(
            f"{candidate_root}/可续写.md",
            self.note(
                "kind: topic-candidate\nstatus: partially-published\n"
                "source_published_at: 2026-07-22\nfresh_until: 2026-08-01\n"
                "freshness_status: fresh"
            ),
        )
        self.write(
            f"{candidate_root}/过期续写.md",
            self.note(
                "kind: topic-candidate\nstatus: partially-published\n"
                "source_published_at: 2026-05-01\nfresh_until: 2026-05-15\n"
                "freshness_status: stale"
            ),
        )
        self.write(
            "10-创作/10-灵感/90-选题储备/储备.md",
            self.note(
                "kind: topic-candidate\nstatus: parked\n"
                "source_published_at: 2026-05-01\nfresh_until: 2026-05-15\n"
                "freshness_status: stale"
            ),
        )
        self.write(
            "10-创作/20-写作任务/正在写.md",
            self.note("kind: writing-task\nstatus: active"),
        )
        self.write(
            "10-创作/20-写作任务/写完了.md",
            self.note("kind: writing-task\nstatus: completed"),
        )
        self.write(
            "40-发布/00-内容反馈/未来复盘.md",
            self.note("kind: content-feedback\nstatus: pending\ndue_date: 2026-07-30"),
        )
        self.write(
            "40-发布/00-内容反馈/到期复盘.md",
            self.note("kind: content-feedback\nstatus: pending\ndue_date: 2026-07-20"),
        )
        self.write(
            "40-发布/00-内容反馈/已复盘.md",
            self.note(
                "kind: content-feedback\nstatus: reviewed\nreviewed_at: 2026-07-22"
            ),
        )
        self.write(
            "10-创作/10-灵感/10-待评估/剪藏复核/已处理.md",
            self.note("status: 已沉淀\naction_processed_at: 2026-07-22T10:00:00-07:00"),
        )
        self.write(
            "10-创作/10-灵感/10-待评估/剪藏复核/待处理.md",
            self.note("status: 待审核"),
        )
        self.write(
            "20-知识/A.md",
            self.note("kind: concept\nstatus: evergreen", "[[B]]\n\n[[C]]"),
        )
        self.write(
            "20-知识/B.md",
            self.note("kind: concept\nstatus: evergreen", "[[A]]"),
        )
        self.write(
            "20-知识/C.md",
            self.note("kind: concept\nstatus: evergreen", "没有内部链接。"),
        )

    def test_collects_creation_health_metrics(self) -> None:
        self.seed_representative_notes()
        now = datetime(
            2026, 7, 23, 12, 0, tzinfo=timezone(timedelta(hours=-7))
        )

        result = health.collect_creation_metrics(now)

        self.assertEqual(result["active_candidate_count"], 3)
        self.assertEqual(result["fresh_candidate_count"], 3)
        self.assertEqual(result["continuation_count"], 1)
        self.assertEqual(result["reserve_candidate_count"], 1)
        self.assertEqual(result["needs_refresh_candidate_count"], 2)
        self.assertTrue(
            any(
                item["file"].endswith("过期续写.md")
                for item in result["needs_refresh_candidates"]
            )
        )
        self.assertEqual(result["expired_candidate_count"], 1)
        self.assertEqual(result["source_stale_candidate_count"], 1)
        self.assertEqual(result["source_date_missing_candidate_count"], 0)
        self.assertEqual(result["review_overdue_candidate_count"], 0)
        self.assertEqual(result["stale_candidate_count"], 0)
        self.assertEqual(result["overdue_candidate_count"], 1)
        self.assertEqual(result["active_writing_task_count"], 1)
        self.assertEqual(result["pending_feedback_count"], 2)
        self.assertEqual(result["overdue_feedback_count"], 1)
        self.assertEqual(result["processed_review_in_active_count"], 1)
        self.assertEqual(result["underlinked_knowledge_count"], 2)

    def test_creation_alert_thresholds_and_compatibility(self) -> None:
        metrics = {
            "active_candidate_count": 8,
            "stale_candidate_count": 2,
            "active_writing_task_count": 5,
            "overdue_feedback_count": 1,
            "processed_review_in_active_count": 3,
        }

        reasons = health.creation_alerts(metrics)

        self.assertEqual(len(reasons), 5)
        self.assertTrue(any("超过 7 条" in reason for reason in reasons))
        self.assertTrue(any("超过 7 天" in reason for reason in reasons))
        self.assertTrue(any("超过 4 个" in reason for reason in reasons))
        self.assertTrue(any("到期反馈" in reason for reason in reasons))
        self.assertTrue(any("已处理卡" in reason for reason in reasons))

        at_limits = {
            "active_candidate_count": 7,
            "stale_candidate_count": 0,
            "active_writing_task_count": 4,
            "overdue_feedback_count": 0,
            "processed_review_in_active_count": 0,
        }
        self.assertEqual(health.creation_alerts(at_limits), [])

    def test_published_files_left_in_active_dirs_are_reported(self) -> None:
        self.write(
            "40-发布/10-X长文/成稿.md",
            self.note(
                'kind: article-published\nstatus: published\n'
                'draft_source: "[[10-创作/30-文章草稿/母稿]]"\n'
                'content_pack: "[[10-创作/20-写作任务/任务]]"',
                "成稿",
            ),
        )
        self.write(
            "10-创作/30-文章草稿/母稿.md",
            self.note("kind: article-draft\nstatus: published", "母稿"),
        )
        self.write(
            "10-创作/20-写作任务/任务.md",
            self.note("kind: content-pack\nstatus: published", "任务"),
        )
        self.write(
            "10-创作/25-口播草稿/旧口播.md",
            self.note("kind: oral-script\nstatus: recorded", "口播"),
        )
        self.write(
            "40-发布/50-口播视频/旧视频.md",
            self.note(
                'kind: video-published\nstatus: published\n'
                'oral_script: "[[10-创作/25-口播草稿/旧口播]]"',
                "正式视频逐字稿",
            ),
        )
        self.write(
            "10-创作/25-口播草稿/已录未发.md",
            self.note("kind: oral-script\nstatus: recorded", "不应收尾"),
        )

        issues = health.published_active_residue_issues()

        self.assertEqual(len(issues), 3)
        files = {item["file"] for item in issues}
        self.assertIn("10-创作/30-文章草稿/母稿.md", files)
        self.assertIn("10-创作/20-写作任务/任务.md", files)
        self.assertIn("10-创作/25-口播草稿/旧口播.md", files)
        self.assertNotIn("10-创作/25-口播草稿/已录未发.md", files)
        reasons = health.creation_alerts({
            "active_candidate_count": 0,
            "stale_candidate_count": 0,
            "active_writing_task_count": 0,
            "overdue_feedback_count": 0,
            "processed_review_in_active_count": 0,
            "published_active_residue_count": 3,
        })
        self.assertTrue(any("已发布文件仍留在" in reason for reason in reasons))

    def test_creation_alerts_flag_stale_or_missing_source_dates(self) -> None:
        metrics = {
            "active_candidate_count": 2,
            "stale_candidate_count": 0,
            "source_stale_candidate_count": 1,
            "source_date_missing_candidate_count": 1,
            "active_writing_task_count": 0,
            "overdue_feedback_count": 0,
            "processed_review_in_active_count": 0,
        }

        reasons = health.creation_alerts(metrics)

        self.assertEqual(len(reasons), 2)
        self.assertTrue(any("原始资料已过时" in reason for reason in reasons))
        self.assertTrue(any("缺少原始资料日期" in reason for reason in reasons))

    def test_checks_provenance_backfill_and_writing_handoff(self) -> None:
        self.write(
            "30-资料/source.md",
            self.note("kind: source\nstatus: captured", "原始资料"),
        )
        self.write(
            "20-知识/有效知识.md",
            self.note(
                'kind: viewpoint\nstatus: evergreen\norigin: capture\n'
                'created_at: 2026-07-23\nupdated_at: 2026-07-23\n'
                'source_file: "30-资料/source.md"',
                "[[另一知识]]\n\n"
                "[[40-发布/10-X长文/已发布]]",
            ),
        )
        self.write(
            "20-知识/另一知识.md",
            self.note(
                'kind: concept\nstatus: evergreen\norigin: capture\n'
                'created_at: 2026-07-23\nupdated_at: 2026-07-23\n'
                'source_file: "30-资料/missing.md"',
                "[[有效知识]]",
            ),
        )
        self.write(
            "40-发布/10-X长文/已发布.md",
            self.note(
                "kind: article-published\nstatus: published\n"
                "knowledge_backfill_status: complete"
            ),
        )
        self.write(
            "10-创作/10-灵感/20-候选选题/候选.md",
            self.note(
                "kind: topic-candidate\nstatus: active",
                "## 可调用知识\n\n- [[有效知识]]",
            ),
        )
        self.write(
            "10-创作/20-写作任务/任务.md",
            self.note(
                'kind: content-pack\nstatus: active\n'
                'candidate: "[[10-创作/10-灵感/20-候选选题/候选]]"',
                "尚未继承知识。",
            ),
        )

        provenance = health.knowledge_provenance_issues()
        self.assertEqual(len(provenance), 1)
        self.assertIn("原始资料不存在", provenance[0]["issue"])
        self.assertEqual(health.knowledge_backfill_issues(), [])
        handoff = health.writing_knowledge_handoff_issues()
        self.assertEqual(len(handoff), 1)
        self.assertEqual(handoff[0]["missing"], ["有效知识"])

    def test_review_source_issue_suggests_corpus_file_by_url(self) -> None:
        self.write(
            "30-资料/70-人物语料/Dan Koe/youtube/2026-09-20-原文.md",
            self.note('title: "原文"\nsource_url: "https://youtube.com/watch?v=abc"'),
        )
        self.write(
            "30-资料/40-网页剪藏/2026-09-21-在的剪藏.md",
            self.note('title: "在的"\nsource_url: "https://example.com/ok"'),
        )
        self.write(
            "10-创作/10-灵感/10-待评估/剪藏复核/2026-09-21-来源被移走-aaaa1111.md",
            self.note(
                "kind: capture-review\nstatus: 待审核\n"
                'source_file: "30-资料/40-网页剪藏/2026-09-20-原文-aaaa1111.md"\n'
                'source_url: "https://youtube.com/watch?v=abc"'
            ),
        )
        self.write(
            "10-创作/10-灵感/10-待评估/剪藏复核/2026-09-21-来源健在-bbbb2222.md",
            self.note(
                "kind: capture-review\nstatus: 待审核\n"
                'source_file: "30-资料/40-网页剪藏/2026-09-21-在的剪藏.md"\n'
                'source_url: "https://example.com/ok"'
            ),
        )
        issues = health.review_source_issues()
        self.assertEqual(len(issues), 1)
        self.assertIn("来源被移走", issues[0]["file"])
        self.assertEqual(
            issues[0]["suggested_source"],
            "30-资料/70-人物语料/Dan Koe/youtube/2026-09-20-原文.md",
        )

    def test_provenance_accepts_escaped_quotes_in_source_filename(self) -> None:
        source = '30-资料/Sam Altman "Startup".md'
        self.write(source, self.note("kind: source\nstatus: captured", "原始资料"))
        self.write(
            "20-知识/带引号来源.md",
            self.note(
                "kind: viewpoint\nstatus: evergreen\n"
                f"source_file: {json.dumps(source, ensure_ascii=False)}",
                "正文",
            ),
        )

        self.assertEqual(health.knowledge_provenance_issues(), [])

    def test_flags_links_that_only_resolve_to_cleanup(self) -> None:
        self.write(
            "10-创作/候选.md",
            self.note("kind: topic-candidate\nstatus: parked", "[[旧知识]]"),
        )
        self.write(
            "90-归档/90-待清理/2026-07-23/20-知识/旧知识.md",
            self.note("kind: concept\nstatus: cleanup", "旧内容"),
        )

        checked, broken, cleanup_only = health.resolve_wikilinks()

        self.assertEqual(checked, 1)
        self.assertEqual(broken, [])
        self.assertEqual(cleanup_only, [
            {"source": "10-创作/候选.md", "target": "旧知识"}
        ])

    def test_skips_hidden_directories_when_checking_links(self) -> None:
        self.write(".claude/worktrees/副本/20-知识/笔记.md", "[[不存在]]")
        self.write(".obsidian/workspace.json", '{"x": "[[不存在]]"}')
        self.write("50-系统/30-模板/模板.md", "[[YYYY-MM-DD-占位]]")
        self.write("30-资料/70-人物语料/_模板/风格画像.md", "![[XX-style.png]]")
        self.write("20-知识/10-概念/笔记.md", "[[另一篇]]")
        self.write("20-知识/10-概念/另一篇.md", "正文")

        checked, broken, cleanup_only = health.resolve_wikilinks()

        self.assertEqual(checked, 1)
        self.assertEqual(broken, [])
        self.assertEqual(cleanup_only, [])

    def test_resolves_table_aliases_attachments_and_folders(self) -> None:
        self.write(
            "30-资料/70-人物语料/甲/索引.md",
            "| 期 | 链接 |\n|---|---|\n"
            "| 1 | [[30-资料/70-人物语料/甲/podcast/第一期\\|第一期]] |\n"
            "[[^g1]] 指向同一篇里的块\n"
            "![[卡片.png]]\n"
            "[[60-素材/10-图片/10-封面/某期封面]]\n"
            "[[60-素材/10-图片/10-封面/某期封面/]]\n",
        )
        self.write("30-资料/70-人物语料/甲/podcast/第一期.md", "正文")
        self.write("30-资料/70-人物语料/甲/_cards/卡片.png", "png")
        self.write("60-素材/10-图片/10-封面/某期封面/cover.png", "png")
        self.write(
            "30-资料/70-人物语料/甲/留存.json",
            '{"link": "[[30-资料/40-网页剪藏/Sam \\"Startup\\"]]",'
            ' "row": "| [[30-资料/70-人物语料/甲/podcast/第一期\\\\|第一期]] |"}',
        )
        self.write('30-资料/40-网页剪藏/Sam "Startup".md', "正文")

        checked, broken, cleanup_only = health.resolve_wikilinks()

        self.assertEqual(checked, 7)
        self.assertEqual(broken, [])
        self.assertEqual(cleanup_only, [])

    def test_still_flags_missing_attachment_and_table_link(self) -> None:
        self.write(
            "20-知识/10-概念/笔记.md",
            "| [[不存在的笔记\\|别名]] |\n![[不存在.png]]\n",
        )

        checked, broken, cleanup_only = health.resolve_wikilinks()

        self.assertEqual(checked, 2)
        self.assertEqual(broken, [
            {"source": "20-知识/10-概念/笔记.md", "target": "不存在的笔记\\|别名"},
            {"source": "20-知识/10-概念/笔记.md", "target": "不存在.png"},
        ])
        self.assertEqual(cleanup_only, [])

    def write_config(self, **values: object) -> None:
        self.write(".state/config.json", json.dumps(values, ensure_ascii=False))

    def test_closeout_flags_open_candidate_sibling_and_stale_drafts(self) -> None:
        now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
        self.write(
            "40-发布/50-口播视频/正式稿.md",
            self.note('kind: video-published\nstatus: published\n'
                      'topic_candidate: "[[10-创作/10-灵感/20-候选选题/候选]]"'),
        )
        self.write(
            "10-创作/10-灵感/20-候选选题/候选.md",
            self.note("kind: topic-candidate\nstatus: active"),
        )
        self.write(
            "10-创作/25-口播草稿/另一版.md",
            self.note('kind: oral-script\nstatus: draft\nupdated_at: "2026-10-26"\n'
                      'supersedes: "[[40-发布/50-口播视频/正式稿]]"'),
        )
        self.write(
            "10-创作/25-口播草稿/老稿.md",
            self.note('kind: oral-script\nstatus: draft\nupdated_at: "2026-10-10"'),
        )
        self.write(
            "10-创作/25-口播草稿/录完待发.md",
            self.note('kind: oral-script\nstatus: draft\nrecording_status: recorded\n'
                      'updated_at: "2026-10-10"'),
        )

        issues = health.publication_closeout_issues(now)

        self.assertEqual(
            [item["file"] for item in issues["open_candidates"]],
            ["10-创作/10-灵感/20-候选选题/候选.md"],
        )
        self.assertEqual(
            [item["file"] for item in issues["sibling_drafts"]],
            ["10-创作/25-口播草稿/另一版.md"],
        )
        self.assertEqual(
            [item["file"] for item in issues["stale_drafts"]],
            ["10-创作/25-口播草稿/老稿.md"],
        )

    def test_new_oral_drafts_need_knowledge_precheck(self) -> None:
        now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
        self.write_config(knowledge_precheck_since="2026-10-28")
        template_text = "### 知识库预检\n\n调研之前先跑 knowledge_search.py，替换掉这两行。\n\n### 调研与出处\n"
        self.write("10-创作/25-口播草稿/没查.md", self.note(
            'kind: oral-script\nstatus: draft\ncreated_at: "2026-10-28"', template_text))
        self.write("10-创作/25-口播草稿/查过无相关.md", self.note(
            'kind: oral-script\nstatus: draft\ncreated_at: "2026-10-28"',
            "### 知识库预检（2026-10-28）\n\n- 无相关\n\n### 调研与出处\n"))
        self.write("10-创作/25-口播草稿/查过有命中.md", self.note(
            'kind: oral-script\nstatus: draft\ncreated_at: "2026-10-29"',
            "### 知识库预检\n\n- [[20-知识/10-概念/某概念|某概念]]\n"))
        self.write("10-创作/25-口播草稿/旧稿.md", self.note(
            'kind: oral-script\nstatus: draft\ncreated_at: "2026-10-20"', "没有预检"))

        issues = health.publication_closeout_issues(now)["missing_precheck"]

        self.assertEqual([item["file"] for item in issues], ["10-创作/25-口播草稿/没查.md"])

    def test_platform_fields_need_known_status_and_douyin_account(self) -> None:
        self.write_config(douyin_accounts=["解说号", "口播号"])
        self.write(
            "40-发布/50-口播视频/缺账号.md",
            self.note("kind: video-published\nstatus: published\ndouyin_status: published"),
        )
        self.write(
            "40-发布/50-口播视频/错取值.md",
            self.note("kind: video-published\nstatus: published\n"
                      "douyin_status: published\ndouyin_account: 小号\nbilibili_status: done"),
        )
        self.write(
            "40-发布/50-口播视频/正常.md",
            self.note("kind: video-published\nstatus: published\n"
                      "douyin_status: private\ndouyin_account: 解说号\n"
                      "xiaohongshu_status: not-needed"),
        )

        issues = {item["file"]: item["issue"] for item in health.platform_field_issues()}

        self.assertEqual(sorted(issues), [
            "40-发布/50-口播视频/缺账号.md", "40-发布/50-口播视频/错取值.md",
        ])
        self.assertIn("douyin_account", issues["40-发布/50-口播视频/缺账号.md"])
        self.assertIn("bilibili_status", issues["40-发布/50-口播视频/错取值.md"])
        self.assertIn("小号", issues["40-发布/50-口播视频/错取值.md"])

    def test_local_paths_resolve_by_project_name_and_skip_unmounted(self) -> None:
        media = health.VAULT / ".media/ray-explainer"
        (media / "2026-10-25-china-ai-chain").mkdir(parents=True)
        self.write_config(media_roots=[str(media)])
        (health.VAULT / "60-素材/10-图片/10-封面/有").mkdir(parents=True)
        self.write(
            "40-发布/50-口播视频/项目名.md",
            self.note('kind: video-published\nstatus: published\n'
                      'video_project: 2026-10-25-china-ai-chain\n'
                      'video_file: "/Volumes/没接的盘/x/out.mp4"\n'
                      'cover_dir: "[[60-素材/10-图片/10-封面/有]]"'),
        )
        self.write(
            "40-发布/50-口播视频/搬家.md",
            self.note('kind: video-published\nstatus: published\n'
                      f'video_project: "{health.VAULT}/old/2026-10-25-china-ai-chain"\n'
                      'cover_dir: "[[60-素材/10-图片/10-封面/没有]]"'),
        )

        issues, unmounted = health.local_path_issues()

        self.assertEqual(unmounted, 1)
        self.assertEqual([item["file"] for item in issues], ["40-发布/50-口播视频/搬家.md"])
        self.assertIn(str(media / "2026-10-25-china-ai-chain"), issues[0]["issue"])
        self.assertIn("cover_dir", issues[0]["issue"])

    def test_backfill_pending_only_counts_after_cutoff(self) -> None:
        now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
        self.write_config(backfill_check_since="2026-10-27")
        self.write(
            "40-发布/50-口播视频/旧.md",
            self.note('kind: video-published\nstatus: published\ncreated_at: "2026-10-20"\n'
                      "knowledge_backfill_status: pending"),
        )
        self.write(
            "40-发布/50-口播视频/新.md",
            self.note('kind: video-published\nstatus: published\ncreated_at: "2026-10-28"\n'
                      "knowledge_backfill_status: pending\nremaining_angles_status: not-needed"),
        )
        self.write(
            "40-发布/50-口播视频/刚发.md",
            self.note('kind: video-published\nstatus: published\ncreated_at: "2026-11-04"\n'
                      "knowledge_backfill_status: pending"),
        )

        issues = health.backfill_pending_issues(now)

        self.assertEqual([item["file"] for item in issues], ["40-发布/50-口播视频/新.md"])
        self.assertNotIn("剩余角度", issues[0]["issue"])

    def test_data_snapshots_are_not_pending_feedback(self) -> None:
        now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
        self.write(
            "40-发布/00-内容反馈/2026-10-24-口播视频数据快照.md",
            self.note('kind: data-snapshot\nsnapshot_at: "2026-10-24T19:30:00-07:00"'),
        )

        metrics = health.collect_creation_metrics(now)

        self.assertEqual(metrics["pending_feedback_count"], 0)
        self.assertEqual(metrics["feedback_schedule_issue_count"], 0)

    def test_backflow_needs_all_three_answers(self) -> None:
        now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
        self.write_config(backfill_check_since="2026-10-27")
        done = ("## 回流\n\n- 机制与类比 → 长期知识：[[20-知识/10-概念/技术扩散的中介窗口]]\n"
                "- 带日期的事实 → 对象档案时间线：无\n- 讲法与反馈 → 留在本篇：渗流网格开场留存高\n")
        half = "## 回流\n\n- 机制与类比 → 长期知识：无\n- 带日期的事实 → 对象档案时间线：\n"
        meta = ('kind: video-published\nstatus: published\ncreated_at: "2026-10-28"\n'
                "knowledge_backfill_status: complete\nremaining_angles_status: not-needed")
        self.write("40-发布/50-口播视频/答完.md", self.note(meta, done))
        self.write("40-发布/50-口播视频/没答完.md", self.note(meta, half))

        issues = {item["file"]: item["issue"] for item in health.backfill_pending_issues(now)}

        self.assertEqual(list(issues), ["40-发布/50-口播视频/没答完.md"])
        self.assertIn("带日期的事实、讲法与反馈", issues["40-发布/50-口播视频/没答完.md"])

    def test_chronic_backlog_only_alerts_when_it_grows(self) -> None:
        metrics = {
            "active_candidate_count": 0, "stale_candidate_count": 0,
            "active_writing_task_count": 0, "overdue_feedback_count": 0,
            "processed_review_in_active_count": 0,
            "underlinked_knowledge_count": 34, "knowledge_provenance_issue_count": 41,
        }
        previous = {"underlinked_knowledge_count": 34, "knowledge_provenance_issue_count": 40}

        reasons = health.creation_alerts(metrics, previous)

        self.assertEqual(reasons, ["有 41 篇长期知识来源缺失或失效（较上次 +1）"])
        self.assertEqual(
            health.chronic_backlog(metrics, previous),
            ["34 篇长期知识少于 2 条有效连接（未增加）"],
        )
        self.assertEqual(len(health.creation_alerts(metrics)), 2)

    def test_flags_unresolved_frontmatter_author_link(self) -> None:
        self.write(
            "30-资料/40-网页剪藏/剪藏.md",
            self.note('kind: source\nauthor: "[[@missing_author]]"', "正文"),
        )

        checked, broken, cleanup_only = health.resolve_wikilinks()

        self.assertEqual(checked, 1)
        self.assertEqual(broken, [
            {
                "source": "30-资料/40-网页剪藏/剪藏.md",
                "target": "@missing_author",
            }
        ])
        self.assertEqual(cleanup_only, [])

    def test_checks_candidate_knowledge_map_and_publication_metadata(self) -> None:
        self.write(
            "10-创作/10-灵感/20-候选选题/候选.md",
            self.note(
                "kind: topic-candidate\nstatus: candidate\n"
                "knowledge_status: extracted\nknowledge_file: 20-知识/不存在.md"
            ),
        )
        self.write(
            "20-知识/10-概念/已覆盖.md",
            self.note("kind: concept\nstatus: seed", "[[未覆盖]]\n[[地图]]"),
        )
        self.write(
            "20-知识/10-概念/未覆盖.md",
            self.note("kind: concept\nstatus: seed", "[[已覆盖]]\n[[地图]]"),
        )
        self.write(
            "20-知识/70-知识地图/地图.md",
            self.note(
                "kind: map\nmap_role: theme\nstatus: evergreen",
                "# 主题地图：地图\n\n[[已覆盖]]",
            ),
        )
        self.write(
            "40-发布/10-X长文/文章.md",
            self.note(
                "kind: article-published\nstatus: published\n"
                "wechat_status: published\nwechat_published_at: \"\"\n"
                "wechat_public_url: \"\""
            ),
        )

        self.assertEqual(len(health.topic_knowledge_reference_issues()), 1)
        coverage = health.knowledge_map_coverage_issues()
        self.assertEqual([item["file"] for item in coverage], [
            "20-知识/10-概念/未覆盖.md"
        ])
        self.assertEqual(health.published_metadata_issues(), [])

    def test_candidate_knowledge_must_be_active_and_map_links_match_exactly(self) -> None:
        self.write(
            "README.md",
            self.note("kind: guide\nstatus: active", "不是长期知识目录"),
        )
        self.write(
            "10-创作/10-灵感/20-候选选题/错误去向.md",
            self.note(
                "kind: topic-candidate\nstatus: candidate\n"
                "knowledge_status: extracted\nknowledge_file: README.md"
            ),
        )
        self.write(
            "20-知识/10-概念/AI.md",
            self.note("kind: concept\nstatus: seed", "[[AI Agent]]\n[[地图]]"),
        )
        self.write(
            "20-知识/10-概念/AI Agent.md",
            self.note("kind: concept\nstatus: seed", "[[AI]]\n[[地图]]"),
        )
        self.write(
            "20-知识/70-知识地图/地图.md",
            self.note(
                "kind: map\nmap_role: theme\nstatus: evergreen",
                "# 主题地图：地图\n\n[[AI Agent]]",
            ),
        )

        references = health.topic_knowledge_reference_issues()
        self.assertEqual(len(references), 1)
        self.assertIn("不在活跃长期知识中", references[0]["issue"])
        coverage = health.knowledge_map_coverage_issues()
        self.assertEqual(
            [item["file"] for item in coverage],
            ["20-知识/10-概念/AI.md"],
        )

    def test_candidate_knowledge_must_not_point_to_archived_note(self) -> None:
        self.write(
            "10-创作/10-灵感/20-候选选题/错误去向.md",
            self.note(
                "kind: topic-candidate\nstatus: candidate\n"
                "knowledge_status: extracted\n"
                "knowledge_file: 20-知识/10-概念/已归档.md"
            ),
        )
        self.write(
            "20-知识/10-概念/已归档.md",
            self.note("kind: concept\nstatus: archived", "旧知识"),
        )

        references = health.topic_knowledge_reference_issues()
        self.assertEqual(len(references), 1)
        self.assertIn("指向非活跃知识", references[0]["issue"])

    def test_candidate_checks_every_extracted_knowledge_unit(self) -> None:
        self.write(
            "10-创作/10-灵感/20-候选选题/多知识.md",
            self.note(
                "kind: topic-candidate\nstatus: candidate\n"
                "knowledge_status: extracted\n"
                'knowledge_file: "20-知识/10-概念/存在.md"\n'
                'knowledge_files_json: "[\\"20-知识/10-概念/存在.md\\",'
                '\\"20-知识/30-问题/不存在.md\\"]"'
            ),
        )
        self.write(
            "20-知识/10-概念/存在.md",
            self.note("kind: concept\nstatus: seed", "内容"),
        )

        references = health.topic_knowledge_reference_issues()

        self.assertEqual(len(references), 1)
        self.assertIn("20-知识/30-问题/不存在.md", references[0]["issue"])

    def test_repeated_source_failure_becomes_red_instead_of_hidden(self) -> None:
        state = {
            "source_health": {
                "mobile": {
                    "label": "手机分享",
                    "status": "degraded",
                    "consecutive_failures": 3,
                    "last_failure_at": "2026-07-30T00:00:00-07:00",
                    "message": "Resource deadlock avoided",
                }
            }
        }

        degraded, red, yellow = health.source_health_findings(state)

        self.assertEqual(len(degraded), 1)
        self.assertEqual(yellow, [])
        self.assertEqual(red, ["手机分享已连续 3 轮读取失败"])

    def test_daily_report_accepts_legacy_metrics_without_new_fields(self) -> None:
        legacy_metrics = {
            "health": "green",
            "captures_today": {},
            "knowledge_added": [],
            "knowledge_baseline_established": True,
            "red_reasons": [],
            "yellow_reasons": [],
            "date": "2026-07-23",
            "generated_at": "2026-07-23T12:00:00-07:00",
            "captured_today_total": 0,
            "reviewed_today": 0,
            "writable_cards": 0,
            "pipeline_pending": 0,
            "human_pending": 0,
            "stale_reviews": [],
            "invalid_review_actions": [],
            "legacy_waiting": [],
            "legacy_errors": [],
            "launch": {"loaded": True, "last_exit_code": 0},
            "state_updated_at": "2026-07-23T12:00:00-07:00",
            "icloud": "caught_up",
            "failed_articles": [],
            "failed_web": [],
            "unresolved_errors": [],
            "links_checked": 0,
            "broken_links": [],
            "duplicate_x_ids": {},
            "schema_issue_count": 0,
            "overdue_cleanup": [],
            "knowledge_total": 0,
            "top_pending": [],
            "cleanup_retention_days": 90,
            "entity_identity_issues": [
                {
                    "file": "20-知识/25-产品与模型/Kimi K3.md；20-知识/25-产品与模型/Kimi.md",
                    "issue": "同类对象的名称或别名冲突",
                }
            ],
        }

        report = health.daily_report(legacy_metrics)

        self.assertIn("## 创作健康", report)
        self.assertIn("| 今日可选 | 0 |", report)
        self.assertIn("## 运行健康", report)
        self.assertIn(
            "[[20-知识/25-产品与模型/Kimi K3]]；[[20-知识/25-产品与模型/Kimi]]",
            report,
        )
        self.assertNotIn("Kimi K3.md；20-知识", report)


class ContentSnapshotHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patchers = [
            mock.patch.object(
                health,
                "CONTENT_SNAPSHOT_SUCCESS_FILE",
                self.root / "content-snapshot-success.json",
            ),
            mock.patch.object(
                health,
                "CONTENT_SNAPSHOT_LOCK_DIR",
                self.root / "content-snapshot.lock",
            ),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def test_missing_first_snapshot_is_scheduled_before_daily_deadline(self) -> None:
        now = datetime(
            2026, 7, 23, 12, 0, tzinfo=timezone(timedelta(hours=-7))
        )

        result = health.content_snapshot_runtime(now)

        self.assertEqual(result["snapshot_state"], "scheduled")
        self.assertEqual(result["runtime_error"], "")

    def test_missing_snapshot_is_overdue_after_daily_deadline(self) -> None:
        now = datetime(
            2026, 7, 23, 19, 0, tzinfo=timezone(timedelta(hours=-7))
        )

        result = health.content_snapshot_runtime(now)

        self.assertEqual(result["snapshot_state"], "overdue")
        self.assertIn("尚无成功记录", result["runtime_error"])

    def test_today_success_and_orphaned_lock_are_distinguished(self) -> None:
        now = datetime(
            2026, 7, 23, 19, 0, tzinfo=timezone(timedelta(hours=-7))
        )
        health.atomic_json(
            health.CONTENT_SNAPSHOT_SUCCESS_FILE,
            {
                "status": "no-change",
                "completed_at": "2026-07-23T18:06:00-07:00",
            },
        )
        self.assertEqual(
            health.content_snapshot_runtime(now)["snapshot_state"],
            "ok",
        )

        health.CONTENT_SNAPSHOT_LOCK_DIR.mkdir()
        health.atomic_json(
            health.CONTENT_SNAPSHOT_LOCK_DIR / "owner.json",
            {"pid": 99999999, "started_at": "2026-07-22T18:05:00-07:00"},
        )
        locked = health.content_snapshot_runtime(now)
        self.assertEqual(locked["snapshot_state"], "stale-lock")
        self.assertIn("失效", locked["runtime_error"])


class ReportRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.original_paths = {
            name: getattr(health, name)
            for name in (
                "VAULT",
                "CONFIG_FILE",
                "SNAPSHOT_DIR",
                "DAILY_DIR",
                "WEEKLY_DIR",
                "REPORT_ARCHIVE_DIR",
            )
        }
        health.VAULT = Path(self.temp.name)
        health.CONFIG_FILE = health.VAULT / "config.json"
        health.SNAPSHOT_DIR = health.VAULT / ".state/health-snapshots"
        health.DAILY_DIR = health.VAULT / "00-入口/20-日报/知识库健康日报"
        health.WEEKLY_DIR = health.VAULT / "00-入口/20-日报/知识库周报"
        health.REPORT_ARCHIVE_DIR = health.VAULT / "90-归档/40-系统报告"
        health.atomic_json(
            health.CONFIG_FILE,
            {
                "daily_report_keep_days": 7,
                "weekly_report_keep_weeks": 8,
            },
        )

    def tearDown(self) -> None:
        for name, value in self.original_paths.items():
            setattr(health, name, value)
        self.temp.cleanup()

    @staticmethod
    def write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_archives_expired_reports_and_keeps_recent_and_latest(self) -> None:
        for name in ("2026-07-16.md", "2026-07-17.md", "2026-07-23.md"):
            self.write(health.DAILY_DIR / name, name)
        self.write(health.DAILY_DIR / "最新健康日报.md", "latest daily")
        for name in ("2026-W22.md", "2026-W23.md", "2026-W30.md"):
            self.write(health.WEEKLY_DIR / name, name)
        self.write(health.WEEKLY_DIR / "最新知识库周报.md", "latest weekly")
        map_digest_dir = health.VAULT / health.MAP_DIGEST_WEEKLY_DIR_REL
        for name in ("2026-W22.md", "2026-W30.md"):
            self.write(map_digest_dir / name, name)
        self.write(map_digest_dir / "最新知识地图整理.md", "latest digest")

        result = health.archive_report_history(reference_date=date(2026, 7, 23))

        self.assertEqual(result["daily_keep_days"], 7)
        self.assertEqual(result["weekly_keep_weeks"], 8)
        self.assertFalse((health.DAILY_DIR / "2026-07-16.md").exists())
        self.assertTrue((health.DAILY_DIR / "2026-07-17.md").exists())
        self.assertTrue((health.DAILY_DIR / "2026-07-23.md").exists())
        self.assertTrue((health.DAILY_DIR / "最新健康日报.md").exists())
        self.assertTrue(
            (
                health.REPORT_ARCHIVE_DIR
                / "知识库健康日报/2026-07/2026-07-16.md"
            ).exists()
        )
        self.assertFalse((health.WEEKLY_DIR / "2026-W22.md").exists())
        self.assertTrue((health.WEEKLY_DIR / "2026-W23.md").exists())
        self.assertTrue((health.WEEKLY_DIR / "2026-W30.md").exists())
        self.assertTrue((health.WEEKLY_DIR / "最新知识库周报.md").exists())
        self.assertTrue(
            (
                health.REPORT_ARCHIVE_DIR
                / "知识库周报/2026/2026-W22.md"
            ).exists()
        )
        self.assertFalse((map_digest_dir / "2026-W22.md").exists())
        self.assertTrue((map_digest_dir / "2026-W30.md").exists())
        self.assertTrue((map_digest_dir / "最新知识地图整理.md").exists())
        self.assertTrue(
            (
                health.REPORT_ARCHIVE_DIR
                / "知识地图整理/2026/2026-W22.md"
            ).exists()
        )

    def test_existing_archive_is_not_overwritten(self) -> None:
        source = health.DAILY_DIR / "2026-07-16.md"
        existing = (
            health.REPORT_ARCHIVE_DIR
            / "知识库健康日报/2026-07/2026-07-16.md"
        )
        self.write(source, "new copy")
        self.write(existing, "existing copy")

        result = health.archive_report_history(reference_date=date(2026, 7, 23))

        self.assertEqual(existing.read_text(encoding="utf-8"), "existing copy")
        duplicate = existing.with_name("2026-07-16-2.md")
        self.assertEqual(duplicate.read_text(encoding="utf-8"), "new copy")
        self.assertEqual(result["daily_archived"], [
            "90-归档/40-系统报告/知识库健康日报/2026-07/2026-07-16-2.md"
        ])

    def test_archiving_report_updates_existing_wikilinks(self) -> None:
        source = health.DAILY_DIR / "2026-07-16.md"
        backlink = health.VAULT / "20-知识/40-观点/引用日报.md"
        self.write(source, "old report")
        self.write(
            backlink,
            "[[00-入口/20-日报/知识库健康日报/2026-07-16|旧日报]]\n",
        )

        health.archive_report_history(reference_date=date(2026, 7, 23))

        self.assertIn(
            "[[90-归档/40-系统报告/知识库健康日报/2026-07/2026-07-16|旧日报]]",
            backlink.read_text(encoding="utf-8"),
        )

    def test_daily_generation_triggers_retention(self) -> None:
        metrics = {
            "date": "2026-07-23",
            "health": "green",
            "captured_today_total": 0,
            "reviewed_today": 0,
            "pipeline_pending": 0,
            "human_pending": 0,
            "active_candidate_count": 0,
            "active_writing_task_count": 0,
            "pending_feedback_count": 0,
        }
        retention = {
            "daily_keep_days": 7,
            "weekly_keep_weeks": 8,
            "daily_archived": [],
            "weekly_archived": [],
        }
        with (
            mock.patch.object(health, "collect_metrics", return_value=metrics),
            mock.patch.object(health, "daily_report", return_value="# daily\n"),
            mock.patch.object(
                health, "archive_report_history", return_value=retention
            ) as archive,
        ):
            report = health.write_daily()

        archive.assert_called_once_with(reference_date=date(2026, 7, 23))
        self.assertEqual(report, health.DAILY_DIR / "2026-07-23.md")
        self.assertTrue(report.exists())

    def test_weekly_generation_triggers_retention(self) -> None:
        now = datetime(
            2026, 7, 23, 12, 0, tzinfo=timezone(timedelta(hours=-7))
        )
        context = {"days": [{"date": "2026-07-23"}]}
        retention = {
            "daily_keep_days": 7,
            "weekly_keep_weeks": 8,
            "daily_archived": [],
            "weekly_archived": [],
        }
        with (
            mock.patch.object(health, "now_local", return_value=now),
            mock.patch.object(
                health,
                "refresh_weekly_map_digest",
                return_value={
                    "summary": {
                        "maps_with_new_knowledge": 2,
                        "new_knowledge_count": 8,
                        "pending_action_count": 6,
                    }
                },
            ) as map_digest,
            mock.patch.object(health, "weekly_context", return_value=context),
            mock.patch.object(
                health, "generate_weekly_with_codex", return_value="# weekly\n"
            ),
            mock.patch.object(
                health, "archive_report_history", return_value=retention
            ) as archive,
        ):
            report = health.write_weekly()

        archive.assert_called_once_with(reference_date=date(2026, 7, 23))
        self.assertEqual(report, health.WEEKLY_DIR / "2026-W30.md")
        self.assertTrue(report.exists())
        self.assertIn(
            "本周有 2 张地图需要整理",
            report.read_text(encoding="utf-8"),
        )
        map_digest.assert_called_once()


class PublishedMetadataTests(unittest.TestCase):
    """链接和时间可选；只拦截已经填写却明显错误的平台地址。"""

    EDITOR_URL = "https://x.com/compose/articles/edit/2079080041305276416"
    PUBLIC_URL = "https://x.com/wangray/status/2079091643693863273"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.original_paths = {
            name: getattr(health, name) for name in ("VAULT", "CONTENT_FEEDBACK_DIR")
        }
        health.VAULT = Path(self.temp.name)
        health.CONTENT_FEEDBACK_DIR = health.VAULT / "40-发布/00-内容反馈"

    def tearDown(self) -> None:
        for name, value in self.original_paths.items():
            setattr(health, name, value)
        self.temp.cleanup()

    def write_article(self, name: str, frontmatter: str) -> None:
        path = health.VAULT / "40-发布/10-X长文" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\n{frontmatter.strip()}\n---\n\n正文。\n", encoding="utf-8"
        )

    def issue_fields(self) -> list[str]:
        issues = health.published_metadata_issues()
        self.assertLessEqual(len(issues), 1)
        return issues[0]["issues"] if issues else []

    def test_x_article_without_a_public_link_is_clean(self) -> None:
        self.write_article(
            "缺链接.md",
            """
kind: article-published
x_article_status: published
x_article_published_at: 2026-07-19T23:30:50-07:00
""",
        )
        self.assertEqual(health.published_metadata_issues(), [])

    def test_x_article_without_a_publish_time_is_clean(self) -> None:
        self.write_article(
            "缺时间.md",
            f"""
kind: article-published
x_article_status: published
x_article_url: "{self.PUBLIC_URL}"
""",
        )
        self.assertEqual(health.published_metadata_issues(), [])

    def test_a_draft_editor_url_does_not_pass_as_a_public_link(self) -> None:
        """填了草稿编辑器地址就不算空，不拦下来这篇就永远显得信息齐全。"""
        self.write_article(
            "草稿地址.md",
            f"""
kind: article-published
x_article_status: published
x_article_url: "{self.EDITOR_URL}"
x_article_published_at: 2026-07-19T23:30:50-07:00
""",
        )
        self.assertEqual(
            self.issue_fields(), ["X Article 链接当前填的是草稿编辑器地址"]
        )

    def test_a_complete_x_article_is_clean(self) -> None:
        self.write_article(
            "齐全.md",
            f"""
kind: article-published
x_article_status: published
x_article_url: "{self.PUBLIC_URL}"
x_article_published_at: 2026-07-19T23:30:50-07:00
x_article_editor_url: "{self.EDITOR_URL}"
""",
        )
        self.assertEqual(health.published_metadata_issues(), [])

    def test_a_draft_is_not_checked_at_all(self) -> None:
        self.write_article(
            "草稿.md",
            """
kind: article-published
x_article_status: draft
""",
        )
        self.assertEqual(health.published_metadata_issues(), [])

    def test_both_platforms_may_omit_optional_metadata(self) -> None:
        self.write_article(
            "两边都缺.md",
            """
kind: article-published
x_article_status: published
wechat_status: published
""",
        )
        self.assertEqual(health.published_metadata_issues(), [])

    def test_empty_strings_are_valid_optional_metadata(self) -> None:
        self.write_article(
            "空字符串.md",
            """
kind: article-published
wechat_status: published
wechat_public_url: ""
wechat_published_at: ""
""",
        )
        self.assertEqual(health.published_metadata_issues(), [])

    def test_creator_backend_url_is_still_flagged(self) -> None:
        self.write_article(
            "后台地址.md",
            """
kind: video-published
douyin_status: published
douyin_url: "https://creator.douyin.com/creator-micro/content/manage"
""",
        )
        self.assertEqual(
            self.issue_fields(), ["抖音链接当前填的是创作者后台地址"]
        )

    def test_published_item_without_a_platform_is_flagged(self) -> None:
        self.write_article(
            "平台未知.md",
            """
kind: video-published
status: published
""",
        )
        self.assertEqual(self.issue_fields(), ["尚未确认具体发布平台"])


class EntityIdentityHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_vault = health.VAULT
        self.old_knowledge = health.KNOWLEDGE_DIR
        health.VAULT = Path(self.temp.name)
        health.KNOWLEDGE_DIR = health.VAULT / "20-知识"

    def tearDown(self) -> None:
        health.VAULT = self.old_vault
        health.KNOWLEDGE_DIR = self.old_knowledge
        self.temp.cleanup()

    def write_entity(
        self,
        folder: str,
        name: str,
        entity_type: str,
        entity_id: str,
        aliases: str = "[]",
    ) -> None:
        path = health.KNOWLEDGE_DIR / folder / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"""---
kind: entity
entity_type: {entity_type}
entity_id: {entity_id}
aliases: {aliases}
---

# {name}
""",
            encoding="utf-8",
        )

    def test_reports_duplicate_identity_wrong_directory_and_missing_id(self) -> None:
        self.write_entity(
            "20-人物与组织",
            "埃隆·马斯克",
            "人物",
            "entity-musk",
            '["Elon Musk"]',
        )
        self.write_entity(
            "20-人物与组织",
            "Elon Musk",
            "人物",
            "",
            '["Musk"]',
        )
        self.write_entity(
            "20-人物与组织",
            "Fable 5",
            "模型",
            "entity-fable",
        )

        issues = health.entity_identity_issues()
        messages = [item["issue"] for item in issues]

        self.assertIn("缺少稳定身份", messages)
        self.assertTrue(any("目录" in message for message in messages))
        self.assertIn("同类对象的名称或别名冲突", messages)

    def test_cross_type_alias_is_not_treated_as_duplicate(self) -> None:
        self.write_entity(
            "20-人物与组织",
            "Moonshot AI",
            "公司",
            "entity-company",
            '["Kimi"]',
        )
        self.write_entity(
            "25-产品与模型",
            "Kimi",
            "产品",
            "entity-product",
        )

        self.assertEqual(health.entity_identity_issues(), [])


if __name__ == "__main__":
    unittest.main()
