from __future__ import annotations

import importlib.util
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
                "CONTENT_FEEDBACK_DIR",
                "REVIEW_DIR",
                "KNOWLEDGE_DIR",
            )
        }
        health.VAULT = Path(self.temp.name)
        health.CANDIDATE_DIR = health.VAULT / "10-创作/10-灵感/20-候选选题"
        health.TOPIC_RESERVE_DIR = health.VAULT / "10-创作/10-灵感/90-选题储备"
        health.WRITING_TASK_DIR = health.VAULT / "10-创作/20-写作任务"
        health.CONTENT_FEEDBACK_DIR = health.VAULT / "40-发布/00-内容反馈"
        health.REVIEW_DIR = health.VAULT / "10-创作/10-灵感/10-待评估/剪藏复核"
        health.KNOWLEDGE_DIR = health.VAULT / "20-知识"

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
review_after: 2026-08-01
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
review_after: 2026-08-01
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
review_after: 2026-07-20
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
        self.assertEqual(result["review_overdue_candidate_count"], 1)
        self.assertEqual(result["stale_candidate_count"], 1)
        self.assertEqual(result["overdue_candidate_count"], 3)
        self.assertEqual(result["active_writing_task_count"], 1)
        self.assertEqual(result["pending_feedback_count"], 2)
        self.assertEqual(result["overdue_feedback_count"], 1)
        self.assertEqual(result["processed_review_in_active_count"], 1)
        self.assertEqual(result["underlinked_knowledge_count"], 2)

    def test_creation_alert_thresholds_and_compatibility(self) -> None:
        metrics = {
            "active_candidate_count": 13,
            "stale_candidate_count": 2,
            "active_writing_task_count": 5,
            "overdue_feedback_count": 1,
            "processed_review_in_active_count": 3,
        }

        reasons = health.creation_alerts(metrics)

        self.assertEqual(len(reasons), 5)
        self.assertTrue(any("超过 12 条" in reason for reason in reasons))
        self.assertTrue(any("超过 14 天" in reason for reason in reasons))
        self.assertTrue(any("超过 4 个" in reason for reason in reasons))
        self.assertTrue(any("到期反馈" in reason for reason in reasons))
        self.assertTrue(any("已处理卡" in reason for reason in reasons))

        at_limits = {
            "active_candidate_count": 12,
            "stale_candidate_count": 0,
            "active_writing_task_count": 4,
            "overdue_feedback_count": 0,
            "processed_review_in_active_count": 0,
        }
        self.assertEqual(health.creation_alerts(at_limits), [])

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
            self.note("kind: map\nstatus: evergreen", "[[已覆盖]]"),
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
        publication = health.published_metadata_issues()
        self.assertEqual(len(publication), 1)
        self.assertEqual(
            publication[0]["missing"],
            ["公众号准确发布时间", "公众号公开链接"],
        )

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
            self.note("kind: map\nstatus: evergreen", "[[AI Agent]]"),
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
        }

        report = health.daily_report(legacy_metrics)

        self.assertIn("## 创作健康", report)
        self.assertIn("| 今日可选 | 0 |", report)
        self.assertIn("## 运行健康", report)


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


class PublishedMetadataTests(unittest.TestCase):
    """发布信息待补要两个平台一视同仁：只在 X 上发的文章原来一篇也点不出来。"""

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

    def missing_fields(self) -> list[str]:
        issues = health.published_metadata_issues()
        self.assertLessEqual(len(issues), 1)
        return issues[0]["missing"] if issues else []

    def test_x_article_without_a_public_link_is_flagged(self) -> None:
        self.write_article(
            "缺链接.md",
            """
kind: article-published
x_article_status: published
x_article_published_at: 2026-07-19T23:30:50-07:00
""",
        )
        self.assertEqual(self.missing_fields(), ["X Article 公开链接"])

    def test_x_article_without_a_publish_time_is_flagged(self) -> None:
        self.write_article(
            "缺时间.md",
            f"""
kind: article-published
x_article_status: published
x_article_url: "{self.PUBLIC_URL}"
""",
        )
        self.assertEqual(self.missing_fields(), ["X Article 准确发布时间"])

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
            self.missing_fields(), ["X Article 公开链接（当前填的是草稿编辑器地址）"]
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

    def test_both_platforms_are_reported_on_the_same_article(self) -> None:
        self.write_article(
            "两边都缺.md",
            """
kind: article-published
x_article_status: published
wechat_status: published
""",
        )
        self.assertEqual(
            self.missing_fields(),
            [
                "X Article 准确发布时间",
                "X Article 公开链接",
                "公众号准确发布时间",
                "公众号公开链接",
            ],
        )

    def test_an_empty_string_counts_as_missing(self) -> None:
        """库里真实存在的形状：字段建了但值是 ""，不能当成已填。"""
        self.write_article(
            "空字符串.md",
            """
kind: article-published
wechat_status: published
wechat_public_url: ""
wechat_published_at: ""
""",
        )
        self.assertEqual(
            self.missing_fields(), ["公众号准确发布时间", "公众号公开链接"]
        )


if __name__ == "__main__":
    unittest.main()
