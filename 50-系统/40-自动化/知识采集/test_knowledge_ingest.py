from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("knowledge_ingest.py")
SPEC = importlib.util.spec_from_file_location("knowledge_ingest", MODULE_PATH)
assert SPEC and SPEC.loader
ingest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ingest)


class XBookmarkCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_x_dir = ingest.X_DIR
        self.old_x_cleanup_dir = ingest.X_CLEANUP_DIR
        ingest.X_DIR = Path(self.temp.name)
        ingest.X_CLEANUP_DIR = Path(self.temp.name) / "cleanup"

    def tearDown(self) -> None:
        ingest.X_DIR = self.old_x_dir
        ingest.X_CLEANUP_DIR = self.old_x_cleanup_dir
        self.temp.cleanup()

    @staticmethod
    def tweet() -> dict:
        return {
            "id": "2040000000000000001",
            "author_id": "42",
            "created_at": "2026-07-17T01:02:03.000Z",
            "text": "This is truncated… https://t.co/a",
            "note_tweet": {
                "text": "This is the complete long post with every important sentence.",
                "entities": {"urls": [{"expanded_url": "https://example.com/full"}]},
            },
            "attachments": {"media_keys": ["3_a", "3_b", "13_c"]},
            "public_metrics": {"like_count": 2, "retweet_count": 1},
        }

    def test_full_text_prefers_note_tweet(self) -> None:
        self.assertEqual(
            ingest.x_full_text(self.tweet()),
            "This is the complete long post with every important sentence.",
        )

    def test_reconciles_reviewed_source_moved_to_cleanup(self) -> None:
        vault = Path(self.temp.name)
        old_vault = ingest.VAULT
        ingest.VAULT = vault
        try:
            archived = (
                ingest.X_CLEANUP_DIR
                / "2026-07-23/30-资料/30-X书签/2026-07/source.md"
            )
            archived.parent.mkdir(parents=True, exist_ok=True)
            archived.write_text("# source\n", encoding="utf-8")
            state = {
                "reviewed_sources": {
                    "x:1": {"source": "30-资料/30-X书签/2026-07/source.md"}
                }
            }

            repaired = ingest.reconcile_reviewed_source_paths(state)

            self.assertEqual(repaired, 1)
            self.assertEqual(
                state["reviewed_sources"]["x:1"]["source"],
                "cleanup/2026-07-23/30-资料/30-X书签/2026-07/source.md",
            )
        finally:
            ingest.VAULT = old_vault

    def test_writes_all_attachments_and_is_current_afterward(self) -> None:
        tweet = self.tweet()
        media = {
            "3_a": {"media_key": "3_a", "type": "photo", "url": "https://img/1.jpg"},
            "3_b": {"media_key": "3_b", "type": "photo", "url": "https://img/2.jpg"},
            "13_c": {
                "media_key": "13_c",
                "type": "video",
                "preview_image_url": "https://img/video.jpg",
            },
        }
        path = ingest.write_x_bookmark(tweet, "tester", None, media_by_key=media)
        text = path.read_text(encoding="utf-8")
        self.assertIn(ingest.x_full_text(tweet), text)
        self.assertIn('source_url: "https://x.com/tester/status/2040000000000000001"', text)
        self.assertIn('published_at: "2026-07-17T01:02:03.000Z"', text)
        self.assertIn("created_at:", text)
        self.assertNotIn("\nsource:", text)
        self.assertNotIn("\npublished:", text)
        self.assertNotIn("\ncreated:", text)
        self.assertIn("attachment_count: 3", text)
        self.assertIn("/photo/1", text)
        self.assertIn("/photo/2", text)
        self.assertIn("/video/1", text)
        self.assertFalse(ingest.x_bookmark_needs_refresh(text, tweet, None, media))

    def test_preserves_legacy_summary_while_replacing_old_source(self) -> None:
        old = """---
title: "Legacy"
tweet_id: "2040000000000000001"
---

# Legacy

**Author:** Tester
**Source:** https://x.com/tester/status/2040000000000000001

## 摘要

这是我手写的总结。

## 原文

旧的、不完整的正文。
"""
        path = ingest.X_DIR / "2026-07" / "legacy.md"
        path.parent.mkdir(parents=True)
        path.write_text(old, encoding="utf-8")
        ingest.write_x_bookmark(self.tweet(), "tester", None)
        text = path.read_text(encoding="utf-8")
        self.assertIn("## 个人笔记", text)
        self.assertIn("这是我手写的总结。", text)
        self.assertIn(ingest.x_full_text(self.tweet()), text)
        self.assertNotIn("旧的、不完整的正文。", text)
        ingest.write_x_bookmark(self.tweet(), "tester", None)
        self.assertIn("这是我手写的总结。", path.read_text(encoding="utf-8"))

    def test_writes_inline_article_body(self) -> None:
        tweet = self.tweet()
        tweet["article"] = {
            "title": "Complete Article",
            "plain_text": "Full article body\n\nWith a second paragraph.",
        }
        path = ingest.write_x_bookmark(tweet, "tester", tweet["article"])
        text = path.read_text(encoding="utf-8")
        self.assertIn("article_fetch_status: success", text)
        self.assertIn("Full article body", text)
        self.assertIn("With a second paragraph.", text)

    def test_archived_bookmark_ids_still_participate_in_deduplication(self) -> None:
        archived = (
            ingest.X_CLEANUP_DIR
            / "2026-07-23/30-资料/30-X书签/2026-03/archived.md"
        )
        archived.parent.mkdir(parents=True)
        archived.write_text(
            """---
tweet_id: "2040000000000000001"
capture_id: "x:2040000000000000001"
---
""",
            encoding="utf-8",
        )

        self.assertEqual(
            ingest.known_local_x_ids(),
            {"2040000000000000001"},
        )


class XArticleImageTests(unittest.TestCase):
    """A1：Article 的封面和插图必须随卡片留下来，不能只剩正文。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_x_dir = ingest.X_DIR
        self.old_x_cleanup_dir = ingest.X_CLEANUP_DIR
        ingest.X_DIR = Path(self.temp.name)
        ingest.X_CLEANUP_DIR = Path(self.temp.name) / "cleanup"

    def tearDown(self) -> None:
        ingest.X_DIR = self.old_x_dir
        ingest.X_CLEANUP_DIR = self.old_x_cleanup_dir
        self.temp.cleanup()

    @staticmethod
    def tweet() -> dict:
        return {
            "id": "2050000000000000002",
            "author_id": "42",
            "created_at": "2026-07-18T01:02:03.000Z",
            "text": "长文来了 https://t.co/b",
            "article": {"title": "带图的长文"},
            "entities": {
                "urls": [{"expanded_url": "https://x.com/i/article/2050000000000000002"}]
            },
            "public_metrics": {"like_count": 9},
        }

    @staticmethod
    def payload() -> dict:
        return {
            "data": {
                "article": {
                    "title": "带图的长文",
                    "plain_text": "开头一段讲清楚问题。\n\n第二段给出证据。\n\n第三段收尾。",
                    "cover_media": {"media_key": "3_cover"},
                    "media_entities": [
                        {"media_key": "3_body1", "start": 11},
                        {"media_key": "3_body2", "start": 21},
                    ],
                }
            },
            "includes": {
                "media": [
                    {
                        "media_key": "3_cover",
                        "type": "photo",
                        "url": "https://img/cover.jpg",
                        "alt_text": "封面说明",
                    },
                    {"media_key": "3_body1", "type": "photo", "url": "https://img/b1.jpg"},
                    {
                        "media_key": "3_body2",
                        "type": "photo",
                        "preview_image_url": "https://img/b2.jpg",
                    },
                ]
            },
        }

    def fetch(self, payload: dict | None = None) -> tuple[dict, list[str]]:
        endpoints: list[str] = []

        def fake_request(config: dict, args: list[str]) -> dict:
            endpoints.append(args[0])
            return payload if payload is not None else self.payload()

        with patch.object(ingest, "run_xurl_request", fake_request):
            article = ingest.fetch_x_article("2050000000000000002", {})
        return article, endpoints

    def test_article_request_declares_media_fields(self) -> None:
        article, endpoints = self.fetch()
        self.assertIn("expansions=article.cover_media,article.media_entities", endpoints[0])
        self.assertIn("media.fields=", endpoints[0])
        self.assertIn("url", endpoints[0].split("media.fields=")[1])
        self.assertTrue(article["media_resolved"])
        self.assertEqual(len(article["media_by_key"]), 3)

    def test_images_carry_role_address_and_position(self) -> None:
        article, _ = self.fetch()
        images = ingest.x_article_images(article)

        self.assertEqual([item["role"] for item in images], ["cover", "body", "body"])
        self.assertEqual(images[0]["url"], "https://img/cover.jpg")
        self.assertEqual(images[0]["alt_text"], "封面说明")
        self.assertNotIn("anchor", images[0])
        self.assertEqual(images[1]["anchor"], "开头一段讲清楚问题。")
        self.assertEqual(images[2]["anchor"], "开头一段讲清楚问题。 第二段给出证据。")

    def test_card_lists_every_image_with_its_position(self) -> None:
        article, _ = self.fetch()
        path = ingest.write_x_bookmark(self.tweet(), "tester", article)
        text = path.read_text(encoding="utf-8")

        self.assertIn("article_image_count: 3", text)
        self.assertIn("## Article 配图", text)
        self.assertIn("- 封面：https://img/cover.jpg", text)
        self.assertIn("  - 说明：封面说明", text)
        self.assertIn("- 插图 1：https://img/b1.jpg", text)
        self.assertIn("  - 位置：接在「开头一段讲清楚问题。」之后", text)
        self.assertIn("- 插图 2：https://img/b2.jpg", text)
        self.assertFalse(
            ingest.x_bookmark_needs_refresh(text, self.tweet(), article, None)
        )

    def test_missing_address_still_records_the_media_key(self) -> None:
        payload = self.payload()
        payload["includes"]["media"] = []
        article, _ = self.fetch(payload)
        path = ingest.write_x_bookmark(self.tweet(), "tester", article)
        text = path.read_text(encoding="utf-8")

        self.assertIn("article_image_count: 3", text)
        self.assertIn("- 封面：地址未返回（media_key 3_cover）", text)

    def test_article_without_images_settles_at_zero(self) -> None:
        payload = self.payload()
        payload["data"]["article"].pop("cover_media")
        payload["data"]["article"].pop("media_entities")
        payload["includes"]["media"] = []
        article, _ = self.fetch(payload)
        path = ingest.write_x_bookmark(self.tweet(), "tester", article)
        text = path.read_text(encoding="utf-8")

        self.assertIn("article_image_count: 0", text)
        self.assertNotIn("## Article 配图", text)
        self.assertFalse(
            ingest.x_bookmark_needs_refresh(text, self.tweet(), article, None)
        )

    def test_card_written_without_asking_keeps_asking(self) -> None:
        """详情接口没问到时不写配图数，下一轮同步还要再问一次。"""
        hint = {"title": "带图的长文", "plain_text": "开头一段讲清楚问题。"}
        path = ingest.write_x_bookmark(self.tweet(), "tester", hint)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("article_image_count", text)
        self.assertFalse(ingest.x_bookmark_needs_refresh(text, self.tweet(), hint, None))

        article, _ = self.fetch()
        self.assertTrue(
            ingest.x_bookmark_needs_refresh(text, self.tweet(), article, None)
        )

    def test_known_card_without_image_count_gets_one_extra_detail_call(self) -> None:
        tweet = self.tweet()
        hint = {
            "title": "带图的长文",
            "plain_text": self.payload()["data"]["article"]["plain_text"],
        }
        tweet["article"] = hint
        existing = ingest.write_x_bookmark(tweet, "tester", hint)
        self.assertNotIn("article_image_count", existing.read_text(encoding="utf-8"))
        calls: list[str] = []

        state = ingest.default_state()
        state["known_x_ids"] = [tweet["id"]]
        state["x_remote_bootstrapped"] = True
        payload = {
            "data": [tweet],
            "includes": {"users": [{"id": "42", "username": "tester"}]},
        }

        resolved, _ = self.fetch()

        def fetch_article(tweet_id: str, config: dict) -> dict:
            calls.append(tweet_id)
            return dict(resolved)

        with (
            patch.object(ingest, "run_xurl", lambda config: payload),
            patch.object(ingest, "fetch_x_article", fetch_article),
            patch.object(ingest, "rel", lambda path: str(path)),
            patch.object(ingest, "known_local_x_ids", lambda: {tweet["id"]}),
        ):
            imported = ingest.sync_x(state, {})

        self.assertEqual(imported, 1)
        self.assertEqual(calls, [tweet["id"]])
        text = existing.read_text(encoding="utf-8")
        self.assertIn("article_image_count: 3", text)
        self.assertIn("- 封面：https://img/cover.jpg", text)

        # 第二轮：配图数已经记下，不再为它多花一次调用。
        calls.clear()
        with (
            patch.object(ingest, "run_xurl", lambda config: payload),
            patch.object(ingest, "fetch_x_article", fetch_article),
            patch.object(ingest, "rel", lambda path: str(path)),
            patch.object(ingest, "known_local_x_ids", lambda: {tweet["id"]}),
        ):
            ingest.sync_x(state, {})

        self.assertEqual(calls, [])


class XSyncRecoveryTests(unittest.TestCase):
    def test_successful_x_sync_resolves_prior_x_errors(self) -> None:
        state = {
            "errors": [
                {"at": "2026-07-22T00:00:00-07:00", "message": "x sync failed: Unauthorized"},
                {"at": "2026-07-22T00:01:00-07:00", "message": "review failed: unrelated"},
            ]
        }
        with (
            patch.object(ingest, "sync_x", return_value=0),
            patch.object(ingest, "sync_mobile", return_value=0),
            patch.object(ingest, "discover_web_clips", return_value=0),
            patch.object(ingest, "discover_weread_cards", return_value=0),
            patch.object(ingest, "save_state"),
        ):
            result = ingest.sync_only(state, {})

        self.assertEqual(result, {"x": 0, "mobile": 0, "web": 0, "weread": 0})
        self.assertIn("resolved_at", state["errors"][0])
        self.assertNotIn("resolved_at", state["errors"][1])


class MobileInboxRecoveryTests(unittest.TestCase):
    def test_unavailable_icloud_inbox_does_not_block_local_link_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mobile = root / "icloud-inbox.txt"
            mobile.write_text("https://example.com/mobile\n", encoding="utf-8")
            local = root / "链接收件箱.md"
            local.write_text("https://example.com/local\n", encoding="utf-8")
            original_read_text = Path.read_text

            def read_text(path: Path, *args: object, **kwargs: object) -> str:
                if path == mobile:
                    raise OSError(11, "Resource deadlock avoided")
                return original_read_text(path, *args, **kwargs)

            with (
                patch.object(ingest, "LINK_INBOX", local),
                patch.object(ingest, "log") as mocked_log,
                patch.object(Path, "read_text", read_text),
            ):
                lines = ingest.mobile_lines({"mobile_inbox": str(mobile)})

        self.assertEqual(lines, ["https://example.com/local"])
        mocked_log.assert_called_once()

    def test_new_mobile_capture_uses_current_source_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            web_dir = vault / "30-资料/40-网页剪藏"
            web_dir.mkdir(parents=True)
            state = ingest.default_state()
            with (
                patch.object(ingest, "VAULT", vault),
                patch.object(ingest, "WEB_DIR", web_dir),
                patch.object(
                    ingest,
                    "mobile_lines",
                    return_value=["个人备注 https://example.com/article"],
                ),
                patch.object(
                    ingest,
                    "fetch_url",
                    return_value=("测试文章", "足够长的正文" * 20, "text/html"),
                ),
            ):
                imported = ingest.sync_mobile(state, {})

            self.assertEqual(imported, 1)
            text = next(web_dir.glob("*.md")).read_text(encoding="utf-8")
            self.assertIn('source_url: "https://example.com/article"', text)
            self.assertIn("created_at:", text)
            self.assertNotIn("\nsource:", text)
            self.assertNotIn("\ncreated:", text)


class WebClipNormalizationTests(unittest.TestCase):
    def test_web_clip_uses_current_fields_and_plain_author_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "clip.md"
            path.write_text(
                """---
title: "测试剪藏"
source: "https://example.com"
author:
  - "[[@writer]]"
published: 2026-07-20
created: 2026-07-21
---

# 测试剪藏
""",
                encoding="utf-8",
            )

            self.assertTrue(ingest.normalize_web_clip_frontmatter(path))
            text = path.read_text(encoding="utf-8")
            self.assertIn('source_url: "https://example.com"', text)
            self.assertIn('  - "@writer"', text)
            self.assertIn("published_at: 2026-07-20", text)
            self.assertIn("created_at: 2026-07-21", text)
            self.assertNotIn("\nsource:", text)
            self.assertNotIn("\npublished:", text)
            self.assertNotIn("\ncreated:", text)
            self.assertFalse(ingest.normalize_web_clip_frontmatter(path))


class WeReadIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_vault = ingest.VAULT
        self.old_log_dir = ingest.LOG_DIR
        self.old_weread_dir = ingest.WEREAD_DIR
        self.old_review_dir = ingest.REVIEW_DIR
        self.old_topic_dir = ingest.TOPIC_DIR
        self.old_topic_reserve_dir = ingest.TOPIC_RESERVE_DIR
        self.old_review_record_dir = ingest.REVIEW_RECORD_DIR
        self.old_knowledge_dir = ingest.KNOWLEDGE_DIR
        ingest.VAULT = Path(self.temp.name).resolve()
        ingest.LOG_DIR = ingest.VAULT / "logs"
        ingest.WEREAD_DIR = ingest.VAULT / "30-资料/50-读书划线"
        ingest.REVIEW_DIR = ingest.VAULT / "10-创作/10-灵感/10-待评估/剪藏复核"
        ingest.TOPIC_DIR = ingest.VAULT / "10-创作/10-灵感/20-候选选题"
        ingest.TOPIC_RESERVE_DIR = ingest.VAULT / "10-创作/10-灵感/90-选题储备"
        ingest.REVIEW_RECORD_DIR = ingest.VAULT / "90-归档/20-审核记录/剪藏复核"
        ingest.KNOWLEDGE_DIR = ingest.VAULT / "20-知识"
        ingest.WEREAD_DIR.mkdir(parents=True)

    def tearDown(self) -> None:
        ingest.VAULT = self.old_vault
        ingest.LOG_DIR = self.old_log_dir
        ingest.WEREAD_DIR = self.old_weread_dir
        ingest.REVIEW_DIR = self.old_review_dir
        ingest.TOPIC_DIR = self.old_topic_dir
        ingest.TOPIC_RESERVE_DIR = self.old_topic_reserve_dir
        ingest.REVIEW_RECORD_DIR = self.old_review_record_dir
        ingest.KNOWLEDGE_DIR = self.old_knowledge_dir
        self.temp.cleanup()

    @staticmethod
    def book_text(status: str, mark: str = "第一条划线", *, count: int = 1) -> str:
        synced_at = datetime.now().astimezone().isoformat(timespec="seconds")
        return f"""---
kind: "source"
title: "测试之书"
capture_source: "weread"
capture_id: "weread:book-42"
book_id: "book-42"
highlight_count: {count}
thought_count: 0
source_url: "https://weread.qq.com/test"
processing_status: {status}
synced_at: "{synced_at}"
---

# 《测试之书》

<!-- weread:begin 本区块由同步维护 -->

## 划线与想法

> {mark}

<!-- weread:end -->

## 我的判断

待判断。
"""

    def test_processed_existing_books_become_baseline_without_backfill(self) -> None:
        path = ingest.WEREAD_DIR / "《测试之书》.md"
        path.write_text(self.book_text("processed"), encoding="utf-8")
        state = ingest.default_state()

        discovered = ingest.discover_weread_cards(state)

        self.assertEqual(discovered, 0)
        self.assertEqual(state["pending_sources"], [])
        self.assertEqual(len(state["known_weread_versions"]), 1)
        self.assertTrue(
            state["known_weread_versions"]["weread:book-42"].startswith(
                "weread:book-42:"
            )
        )

    def test_unprocessed_book_versions_are_queued_once_and_use_version_id(self) -> None:
        path = ingest.WEREAD_DIR / "《测试之书》.md"
        path.write_text(self.book_text("unprocessed"), encoding="utf-8")
        state = ingest.default_state()

        first = ingest.discover_weread_cards(state)
        version_one = ingest.source_id(path, path.read_text(encoding="utf-8"))
        second = ingest.discover_weread_cards(state)
        path.write_text(
            self.book_text("unprocessed", "第二次新增划线", count=2),
            encoding="utf-8",
        )
        third = ingest.discover_weread_cards(state)
        version_two = ingest.source_id(path, path.read_text(encoding="utf-8"))

        self.assertEqual((first, second, third), (1, 0, 1))
        self.assertEqual(
            state["pending_sources"],
            ["30-资料/50-读书划线/《测试之书》.md"],
        )
        self.assertTrue(version_one.startswith("weread:book-42:"))
        self.assertNotEqual(version_one, version_two)

    def test_unprocessed_book_generates_standard_review_card(self) -> None:
        source = ingest.WEREAD_DIR / "《测试之书》.md"
        source.write_text(self.book_text("unprocessed"), encoding="utf-8")
        ingest.REVIEW_DIR.mkdir(parents=True)
        state = ingest.default_state()
        ingest.discover_weread_cards(state)
        version_id = ingest.source_id(source, source.read_text(encoding="utf-8"))
        review = {
            "id": version_id,
            "title": "测试之书的新划线",
            "recommendation": "保留",
            "relevance_score": 80,
            "knowledge_value_score": 85,
            "writing_value_score": 70,
            "time_sensitivity": "常青",
            "confidence": "高",
            "suggested_kind": "观点",
            "topics": ["读书"],
            "reason": "新划线包含可复用判断。",
            "reader_value": "读者可以形成一个新判断。",
            "summary": "一本书的新划线摘要。",
            "core_claims": ["完整背景更重要。"],
            "evidence": ["原书划线。"],
            "counterpoints": ["仍需实践验证。"],
            "connections": [],
            "writing_angles": ["从读书到写作。"],
        }

        with patch.object(ingest, "run_codex", return_value={"reviews": [review]}):
            count = ingest.review_pending(state, {})

        self.assertEqual(count, 1)
        card = next(ingest.REVIEW_DIR.glob("*.md"))
        card_text = card.read_text(encoding="utf-8")
        self.assertIn("kind: capture-review", card_text)
        self.assertIn(f'capture_id: "{version_id}"', card_text)
        self.assertIn(
            'source_file: "30-资料/50-读书划线/《测试之书》.md"',
            card_text,
        )
        self.assertIn('source_url: "https://weread.qq.com/test"', card_text)
        self.assertIn("source_published_at:", card_text)
        self.assertIn("- [ ] 同时沉淀知识并加入候选选题", card_text)
        rendered_actions = [
            line.removeprefix("- [ ] ")
            for line in card_text.splitlines()
            if line.startswith("- [ ] ")
        ]
        self.assertEqual(rendered_actions, ingest.CANONICAL_REVIEW_LABELS)
        self.assertEqual(state["pending_sources"], [])

    def test_both_action_marks_book_processed_and_backlinks_source(self) -> None:
        source = ingest.WEREAD_DIR / "《测试之书》.md"
        source.write_text(self.book_text("unprocessed"), encoding="utf-8")
        source_rel = ingest.rel(source)
        ingest.REVIEW_DIR.mkdir(parents=True)
        card = ingest.REVIEW_DIR / "weread-review.md"
        card.write_text(
            f"""---
kind: capture-review
status: 待审核
suggested_kind: 观点
suggested_destination: "20-知识/40-观点"
knowledge_value_score: 85
writing_value_score: 70
time_sensitivity: 常青
source_file: "{source_rel}"
source_url: "https://weread.qq.com/test"
capture_id: "{ingest.source_id(source, source.read_text(encoding='utf-8'))}"
reviewed_at: "2026-07-23T03:00:00-07:00"
---

# 一本书的新判断

## 快速判断

值得长期复用，也有写作角度。

## 摘要

这是一条从划线中提炼出的判断。

## 核心观点

- 完整背景比精准命令重要。

## 支撑证据

- 原书划线提供了一手表述。

## 漏洞与反例

- 仍需结合实践验证。

## 与现有知识的连接

- 暂无

## 可写角度

- 为什么强模型更需要背景。

## 人工审核

- [x] 同时沉淀知识并加入候选选题
- [ ] 只沉淀为长期知识
- [ ] 只加入候选选题
- [ ] 暂缓
- [ ] 标记为可恢复的待清理项
""",
            encoding="utf-8",
        )

        result = ingest.process_review_actions()

        self.assertEqual(result["both"], 1)
        source_text = source.read_text(encoding="utf-8")
        self.assertIn("processing_status: processed", source_text)
        self.assertNotIn('processing_status: "processed"', source_text)
        knowledge = next(ingest.KNOWLEDGE_DIR.rglob("*.md"))
        topic = next(ingest.TOPIC_DIR.glob("*.md"))
        for output in (knowledge, topic):
            output_text = output.read_text(encoding="utf-8")
            self.assertIn(f"[[{source_rel[:-3]}]]", output_text)
            self.assertIn(f'source_file: "{source_rel}"', output_text)
        self.assertIn("origin: weread", knowledge.read_text(encoding="utf-8"))
        self.assertIn("updated_at:", knowledge.read_text(encoding="utf-8"))

    def test_entity_and_question_have_supported_destinations(self) -> None:
        self.assertEqual(
            ingest.knowledge_destination({"suggested_kind": "人物与组织"}),
            ingest.VAULT / "20-知识/20-人物与组织",
        )
        self.assertEqual(
            ingest.knowledge_destination({"suggested_kind": "问题"}),
            ingest.VAULT / "20-知识/30-问题",
        )


class ContentPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_vault = ingest.VAULT
        self.old_log_dir = ingest.LOG_DIR
        self.old_review_dir = ingest.REVIEW_DIR
        self.old_content_pack_dir = ingest.CONTENT_PACK_DIR
        self.old_topic_dir = ingest.TOPIC_DIR
        self.old_topic_reserve_dir = ingest.TOPIC_RESERVE_DIR
        self.old_writing_task_dir = ingest.WRITING_TASK_DIR
        self.old_review_record_dir = ingest.REVIEW_RECORD_DIR
        self.old_content_pack_template = ingest.CONTENT_PACK_TEMPLATE
        self.old_knowledge_dir = ingest.KNOWLEDGE_DIR
        ingest.VAULT = Path(self.temp.name).resolve()
        ingest.LOG_DIR = ingest.VAULT / "logs"
        ingest.REVIEW_DIR = ingest.VAULT / "10-创作/10-灵感/10-待评估/剪藏复核"
        ingest.TOPIC_DIR = ingest.VAULT / "10-创作/10-灵感/20-候选选题"
        ingest.TOPIC_RESERVE_DIR = ingest.VAULT / "10-创作/10-灵感/90-选题储备"
        ingest.WRITING_TASK_DIR = ingest.VAULT / "10-创作/20-写作任务"
        ingest.CONTENT_PACK_DIR = ingest.WRITING_TASK_DIR
        ingest.REVIEW_RECORD_DIR = ingest.VAULT / "90-归档/20-审核记录/剪藏复核"
        ingest.KNOWLEDGE_DIR = ingest.VAULT / "20-知识"
        ingest.CONTENT_PACK_TEMPLATE = ingest.VAULT / "50-系统/30-模板/内容成稿包.md"
        ingest.CONTENT_PACK_TEMPLATE.parent.mkdir(parents=True)
        ingest.CONTENT_PACK_TEMPLATE.write_text(
            """---
kind: content-pack
status: active
created_at: {{date}}
updated_at: {{date}}
candidate: ""
selected_angle: ""
---

# 成稿包：{{title}}

## 已确认角度

- 候选选题：[[]]
- 本次角度：

## 可调用知识

- [[]]
""",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        ingest.VAULT = self.old_vault
        ingest.LOG_DIR = self.old_log_dir
        ingest.REVIEW_DIR = self.old_review_dir
        ingest.CONTENT_PACK_DIR = self.old_content_pack_dir
        ingest.TOPIC_DIR = self.old_topic_dir
        ingest.TOPIC_RESERVE_DIR = self.old_topic_reserve_dir
        ingest.WRITING_TASK_DIR = self.old_writing_task_dir
        ingest.REVIEW_RECORD_DIR = self.old_review_record_dir
        ingest.CONTENT_PACK_TEMPLATE = self.old_content_pack_template
        ingest.KNOWLEDGE_DIR = self.old_knowledge_dir
        self.temp.cleanup()

    @staticmethod
    def review_text() -> str:
        source_date = datetime.now().astimezone().isoformat(timespec="seconds")
        return f"""---
capture_id: "file:source.md"
source_file: "30-资料/40-网页剪藏/source.md"
source_url: "https://example.com/source"
knowledge_value_score: 90
writing_value_score: 85
time_sensitivity: 中时效
source_published_at: "{source_date}"
---

# AI 内容越多，创作者身份越重要

## 核心观点

- AI 标签只能说明制作过程，不能证明内容可信。

## 支撑证据

- 平台开始同时提供 AI 标签和账号背景。

## 漏洞与反例

- 身份不等于实名，也不等于正确。

## 与现有知识的连接

- 信任校准

## 可写角度

- AI 内容泛滥后，创作者身份会成为平台的信任资产。
"""

    def test_source_date_uses_publication_instead_of_capture_time(self) -> None:
        source = ingest.VAULT / "30-资料/30-X书签/2026-07/source.md"
        source.parent.mkdir(parents=True)
        source.write_text(
            """---
published: "2026-07-21T10:00:00.000Z"
created: "2026-07-23T10:00:00-07:00"
---
""",
            encoding="utf-8",
        )

        self.assertEqual(
            ingest.source_published_at(ingest.rel(source)),
            "2026-07-21T10:00:00.000Z",
        )

    def test_source_date_prefers_current_publication_field(self) -> None:
        source = ingest.VAULT / "30-资料/30-X书签/2026-07/current-source.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            """---
published_at: "2026-07-22T10:00:00.000Z"
published: "2026-07-21T10:00:00.000Z"
created_at: "2026-07-23T10:00:00-07:00"
---
""",
            encoding="utf-8",
        )

        self.assertEqual(
            ingest.source_published_at(ingest.rel(source)),
            "2026-07-22T10:00:00.000Z",
        )

    def test_stale_source_is_parked_instead_of_entering_today_candidates(self) -> None:
        card = ingest.REVIEW_DIR / "stale.md"
        card.parent.mkdir(parents=True)
        text = self.review_text()
        text = ingest.update_frontmatter(
            text,
            {"source_published_at": "2026-01-01T10:00:00Z"},
        )
        card.write_text(text, encoding="utf-8")

        topic = ingest.create_topic_candidate(card, text, ingest.parse_frontmatter(text))
        topic_text = topic.read_text(encoding="utf-8")

        self.assertEqual(topic.parent, ingest.TOPIC_RESERVE_DIR)
        self.assertIn("status: parked", topic_text)
        self.assertIn("freshness_status: stale", topic_text)
        self.assertIn("priority_score: 0", topic_text)

    def test_create_content_pack_preserves_truth_boundaries(self) -> None:
        card = ingest.VAULT / "10-创作/10-灵感/10-待评估/剪藏复核/card.md"
        card.parent.mkdir(parents=True)
        text = self.review_text()
        card.write_text(text, encoding="utf-8")
        path = ingest.create_content_pack(card, text, ingest.parse_frontmatter(text))
        result = path.read_text(encoding="utf-8")
        self.assertIn("kind: content-pack", result)
        self.assertIn("## 情绪与传播", result)
        self.assertIn("## 用户真实材料", result)
        self.assertIn("禁止补写：未被用户提供的个人故事和效果数字", result)
        self.assertIn("AI 标签只能说明制作过程", result)
        self.assertIn("[[30-资料/40-网页剪藏/source]]", result)

    def test_create_content_pack_is_idempotent_for_capture(self) -> None:
        card = ingest.VAULT / "card.md"
        text = self.review_text()
        card.write_text(text, encoding="utf-8")
        meta = ingest.parse_frontmatter(text)
        first = ingest.create_content_pack(card, text, meta)
        second = ingest.create_content_pack(card, text, meta)
        self.assertEqual(first, second)
        self.assertEqual(len(list(ingest.CONTENT_PACK_DIR.glob("*.md"))), 1)

    def test_topic_review_action_creates_candidate_and_archives_review(self) -> None:
        ingest.REVIEW_DIR.mkdir(parents=True)
        card = ingest.REVIEW_DIR / "card.md"
        text = self.review_text() + "\n## 人工审核\n\n- [ ] 同时沉淀知识并加入候选选题\n- [ ] 只沉淀为长期知识\n- [x] 只加入候选选题\n- [ ] 暂缓\n- [ ] 标记为可恢复的待清理项\n"
        card.write_text(text, encoding="utf-8")
        result = ingest.process_review_actions()
        archived = next(ingest.REVIEW_RECORD_DIR.rglob("card.md"))
        updated = archived.read_text(encoding="utf-8")
        self.assertEqual(result["topic"], 1)
        self.assertEqual(result["archived"], 1)
        self.assertFalse(card.exists())
        self.assertIn('status: "已进入候选"', updated)
        self.assertIn("result_file:", updated)
        self.assertEqual(len(list(ingest.TOPIC_DIR.glob("*.md"))), 1)
        self.assertEqual(len(list(ingest.CONTENT_PACK_DIR.glob("*.md"))), 0)

    def test_both_action_creates_knowledge_and_topic_from_one_source(self) -> None:
        ingest.REVIEW_DIR.mkdir(parents=True)
        card = ingest.REVIEW_DIR / "both.md"
        text = self.review_text() + "\n## 人工审核\n\n- [x] 同时沉淀知识并加入候选选题\n- [ ] 只沉淀为长期知识\n- [ ] 只加入候选选题\n- [ ] 暂缓\n- [ ] 标记为可恢复的待清理项\n"
        card.write_text(text, encoding="utf-8")

        result = ingest.process_review_actions()

        self.assertEqual(result["both"], 1)
        knowledge = list(ingest.KNOWLEDGE_DIR.rglob("*.md"))
        topics = list(ingest.TOPIC_DIR.glob("*.md"))
        self.assertEqual(len(knowledge), 1)
        self.assertEqual(len(topics), 1)
        topic_text = topics[0].read_text(encoding="utf-8")
        self.assertIn("knowledge_status: extracted", topic_text)
        self.assertIn(knowledge[0].stem, topic_text)

    def test_promote_requires_angle_and_creates_one_writing_task(self) -> None:
        ingest.TOPIC_DIR.mkdir(parents=True)
        topic = ingest.TOPIC_DIR / "topic.md"
        source_date = datetime.now().astimezone().isoformat(timespec="seconds")
        topic.write_text(
            f"""---
kind: topic-candidate
status: candidate
topic_id: topic-test
time_sensitivity: 中时效
source_published_at: "{source_date}"
source_review: ""
source_file: ""
source_url: ""
---

# 模型需要更多背景

## 可调用知识

- [[背景信息]]
""",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "必须明确"):
            ingest.promote_topic(topic, "")

        first = ingest.promote_topic(topic, "命令减少后人的价值是什么")
        second = ingest.promote_topic(topic, "命令减少后人的价值是什么")

        self.assertEqual(first, second)
        self.assertEqual(len(list(ingest.WRITING_TASK_DIR.glob("*.md"))), 1)
        self.assertIn('status: "active"', topic.read_text(encoding="utf-8"))
        task_text = first.read_text(encoding="utf-8")
        self.assertIn("命令减少后人的价值是什么", task_text)
        self.assertIn("candidate:", task_text)

    def test_stale_reserve_topic_requires_a_real_refresh_signal(self) -> None:
        ingest.TOPIC_RESERVE_DIR.mkdir(parents=True)
        topic = ingest.TOPIC_RESERVE_DIR / "stale-topic.md"
        topic.write_text(
            """---
kind: topic-candidate
status: parked
topic_id: topic-stale
time_sensitivity: 中时效
source_published_at: "2026-01-01T10:00:00Z"
source_review: ""
source_file: ""
source_url: ""
---

# 旧主题

## 可调用知识

- [[背景信息]]
""",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "refreshed_at"):
            ingest.promote_topic(topic, "旧主题为什么重新值得写")

        refreshed = ingest.update_frontmatter(
            topic.read_text(encoding="utf-8"),
            {
                "refreshed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "refresh_source": "Ray 新完成的一次实际验证",
            },
        )
        topic.write_text(refreshed, encoding="utf-8")
        task = ingest.promote_topic(topic, "旧主题为什么重新值得写")

        self.assertTrue(task.exists())
        promoted = ingest.TOPIC_DIR / topic.name
        self.assertTrue(promoted.exists())
        promoted_meta = ingest.parse_frontmatter(promoted.read_text(encoding="utf-8"))
        self.assertEqual(promoted_meta["freshness_status"], "fresh")

    def test_refreshing_review_preserves_processed_human_decision(self) -> None:
        ingest.REVIEW_DIR.mkdir(parents=True)
        card = ingest.REVIEW_DIR / "card.md"
        card.write_text(
            """---
status: 已沉淀
review_action: knowledge
action_processed_at: "2026-07-20T21:20:06-07:00"
action_error: ""
result_file: "20-知识/60-方法/旧结果.md"
---

# 旧判断

## 人工审核

- [x] 批准进入长期知识库
- [ ] 仅保留为写作素材
- [ ] 暂缓
- [ ] 标记为可恢复的待清理项
""",
            encoding="utf-8",
        )
        review = {
            "title": "更新后的判断",
            "recommendation": "保留",
            "relevance_score": 90,
            "confidence": "高",
            "suggested_kind": "方法",
            "topics": ["测试"],
            "reason": "来源已更新。",
            "summary": "新摘要。",
            "core_claims": ["新观点"],
            "evidence": ["新证据"],
            "counterpoints": ["新反例"],
            "connections": ["新连接"],
            "writing_angles": ["新角度"],
        }
        item = {
            "id": "x:123",
            "file": "30-资料/30-X书签/source.md",
            "content": '---\nsource: "https://example.com"\n---\n',
        }

        ingest.write_review_card(review, item, card)
        updated = card.read_text(encoding="utf-8")

        self.assertIn("# 更新后的判断", updated)
        self.assertIn('status: "已沉淀"', updated)
        self.assertIn('review_action: "knowledge"', updated)
        self.assertIn('action_processed_at: "2026-07-20T21:20:06-07:00"', updated)
        self.assertIn('result_file: "20-知识/60-方法/旧结果.md"', updated)
        self.assertIn("- [x] 只沉淀为长期知识", updated)
        self.assertEqual(updated.count("- [x]"), 1)

    def test_refreshing_review_preserves_queued_human_decision(self) -> None:
        previous = """---
status: 待审核
---

## 人工审核

- [ ] 同时沉淀知识并加入候选选题
- [ ] 只沉淀为长期知识
- [x] 只加入候选选题
- [ ] 暂缓
- [ ] 标记为可恢复的待清理项
"""
        refreshed = """---
status: 待审核
---

## 人工审核

- [ ] 同时沉淀知识并加入候选选题
- [ ] 只沉淀为长期知识
- [ ] 只加入候选选题
- [ ] 暂缓
- [ ] 标记为可恢复的待清理项
"""

        updated = ingest.preserve_review_decision(previous, refreshed)

        self.assertIn("- [x] 只加入候选选题", updated)
        self.assertEqual(updated.count("- [x]"), 1)


class CarriedProcessingStatusTests(unittest.TestCase):
    """D15：采集器整篇重写 frontmatter，不能顺手抹掉下游写进来的处理状态。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.saved = {name: getattr(ingest, name) for name in ("X_DIR", "X_CLEANUP_DIR")}
        ingest.X_DIR = self.root / "30-资料/30-X书签"
        ingest.X_CLEANUP_DIR = self.root / "90-归档/90-待清理"
        ingest.X_DIR.mkdir(parents=True)

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(ingest, name, value)
        self.temp.cleanup()

    @staticmethod
    def tweet() -> dict:
        return {
            "id": "2060000000000000003",
            "author_id": "42",
            "created_at": "2026-07-19T01:02:03.000Z",
            "text": "一条已经用过的书签",
            "public_metrics": {"like_count": 1},
        }

    def write_twice(self, first_extra: str) -> str:
        path = ingest.write_x_bookmark(self.tweet(), "tester", None)
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "processing_status: unprocessed", first_extra
            ),
            encoding="utf-8",
        )
        ingest.write_x_bookmark(self.tweet(), "tester", None)
        return path.read_text(encoding="utf-8")

    def test_refresh_keeps_processed_and_published_file(self) -> None:
        """发布归档标记过的书签，被 article 补抓重写一次后还得是 processed。"""
        text = self.write_twice(
            "processing_status: processed\n"
            'published_file: "[[40-发布/10-X长文/2026-07-19-测试长文]]"'
        )
        self.assertIn("processing_status: processed", text)
        self.assertIn(
            'published_file: "[[40-发布/10-X长文/2026-07-19-测试长文]]"', text
        )

    def test_new_card_still_starts_unprocessed(self) -> None:
        path = ingest.write_x_bookmark(self.tweet(), "tester", None)
        text = path.read_text(encoding="utf-8")
        self.assertIn("processing_status: unprocessed", text)
        self.assertNotIn("published_file", text)

    def test_a_mangled_status_falls_back_instead_of_breaking_the_yaml(self) -> None:
        """手改坏的值不能原样拼回 frontmatter，否则整块 YAML 都读不出来。"""
        text = self.write_twice("processing_status: 处理过了: 大概吧")
        self.assertIn("processing_status: unprocessed", text)
        self.assertEqual(
            ingest.parse_frontmatter(text).get("processing_status"), "unprocessed"
        )

    def test_repair_does_not_reset_a_bookmark_the_user_un_bookmarked(self) -> None:
        """取消收藏后书签就查不到了——正是用过的标志，不该被打回未处理。"""
        path = ingest.write_x_bookmark(self.tweet(), "tester", None)
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "processing_status: unprocessed", "processing_status: processed"
            ),
            encoding="utf-8",
        )
        self.repair()
        # update_frontmatter 一律加引号，所以这里按解析后的值断言，不比对原文。
        meta = ingest.parse_frontmatter(path.read_text(encoding="utf-8"))
        self.assertEqual(meta["processing_status"], "processed")
        self.assertEqual(meta["capture_status"], "source-unavailable")

    def test_repair_still_fills_in_a_legacy_card_with_no_status(self) -> None:
        path = ingest.X_DIR / "2026-07" / "legacy.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '---\ntitle: "旧卡"\ntweet_id: "2060000000000000003"\n---\n\n# 旧卡\n',
            encoding="utf-8",
        )
        self.repair()
        meta = ingest.parse_frontmatter(path.read_text(encoding="utf-8"))
        self.assertEqual(meta["processing_status"], "unprocessed")

    def repair(self) -> None:
        with (
            patch.object(ingest, "run_xurl_request", lambda config, args: {"data": []}),
            patch.object(ingest, "save_state", lambda state: None),
            patch.object(ingest, "known_local_x_ids", set),
            patch.object(ingest, "rel", str),
        ):
            ingest.repair_x({"known_x_ids": []}, {})


class MarkSourceProcessedTests(unittest.TestCase):
    """D15：复核走完之后，三种采集来源都该被标成已处理，不只是书籍卡。"""

    CARD = '---\ntitle: "来源卡"\nprocessing_status: unprocessed\n---\n\n# 来源卡\n'

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        names = ("VAULT", "X_DIR", "WEB_DIR", "WEREAD_DIR")
        self.saved = {name: getattr(ingest, name) for name in names}
        ingest.VAULT = self.root
        ingest.X_DIR = self.root / "30-资料/30-X书签"
        ingest.WEB_DIR = self.root / "30-资料/40-网页剪藏"
        ingest.WEREAD_DIR = self.root / "30-资料/50-读书划线"

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(ingest, name, value)
        self.temp.cleanup()

    def place(self, where: str, text: str | None = None) -> Path:
        path = self.root / where
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.CARD if text is None else text, encoding="utf-8")
        return path

    def test_marks_all_three_capture_kinds(self) -> None:
        for where in (
            "30-资料/30-X书签/2026-07/书签.md",
            "30-资料/40-网页剪藏/剪藏.md",
            "30-资料/50-读书划线/某本书.md",
        ):
            with self.subTest(where=where):
                path = self.place(where)
                self.assertTrue(ingest.mark_source_processed({"source_file": where}))
                self.assertIn(
                    "processing_status: processed", path.read_text(encoding="utf-8")
                )

    def test_adds_the_field_when_the_card_has_none(self) -> None:
        where = "30-资料/40-网页剪藏/无状态.md"
        path = self.place(where, '---\ntitle: "无状态"\n---\n\n# 无状态\n')
        self.assertTrue(ingest.mark_source_processed({"source_file": where}))
        self.assertEqual(
            ingest.parse_frontmatter(path.read_text(encoding="utf-8"))["processing_status"],
            "processed",
        )

    def test_leaves_files_outside_the_capture_dirs_alone(self) -> None:
        """source_file 指到写作任务或知识笔记时不动手——那不是采集管线的东西。"""
        where = "20-知识/某条知识.md"
        path = self.place(where)
        self.assertFalse(ingest.mark_source_processed({"source_file": where}))
        self.assertIn("processing_status: unprocessed", path.read_text(encoding="utf-8"))

    def test_refuses_traversal_and_absolute_paths(self) -> None:
        self.place("30-资料/40-网页剪藏/剪藏.md")
        for source_file in ("../../etc/hosts", "/etc/hosts", ""):
            with self.subTest(source_file=source_file):
                self.assertFalse(ingest.mark_source_processed({"source_file": source_file}))

    def test_already_processed_is_a_no_op(self) -> None:
        where = "30-资料/30-X书签/2026-07/书签.md"
        path = self.place(where, self.CARD.replace("unprocessed", "processed"))
        before = path.read_text(encoding="utf-8")
        self.assertTrue(ingest.mark_source_processed({"source_file": where}))
        self.assertEqual(path.read_text(encoding="utf-8"), before)


class MobileShareRewriteTests(unittest.TestCase):
    """D15：状态文件丢了导致同一条分享重跑时，别多出一张卡、别打回未处理。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.saved = {"VAULT": ingest.VAULT, "WEB_DIR": ingest.WEB_DIR}
        ingest.VAULT = self.root
        ingest.WEB_DIR = self.root / "30-资料/40-网页剪藏"
        ingest.WEB_DIR.mkdir(parents=True)

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(ingest, name, value)
        self.temp.cleanup()

    def sync(self) -> None:
        state = {"mobile_line_hashes": [], "known_web_files": [], "pending_sources": []}
        with (
            patch.object(
                ingest, "mobile_lines", lambda config: ["读读这个 https://example.com/a"]
            ),
            patch.object(
                ingest, "fetch_url", lambda url: ("某篇文章", "正文" * 80, "article")
            ),
        ):
            ingest.sync_mobile(state, {})

    def test_second_run_reuses_the_card_and_keeps_its_status(self) -> None:
        self.sync()
        cards = sorted(ingest.WEB_DIR.glob("*.md"))
        self.assertEqual(len(cards), 1)
        cards[0].write_text(
            cards[0].read_text(encoding="utf-8").replace(
                "processing_status: unprocessed", "processing_status: processed"
            ),
            encoding="utf-8",
        )

        self.sync()  # 状态文件丢了，同一条分享又走一遍
        cards_after = sorted(ingest.WEB_DIR.glob("*.md"))
        self.assertEqual([p.name for p in cards_after], [cards[0].name])
        self.assertIn(
            "processing_status: processed", cards_after[0].read_text(encoding="utf-8")
        )


class XArticlePublishDetectTests(unittest.TestCase):
    """X Article 发布检测：严格门控、精确匹配、命中才登记、失败或不确定只记日志。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        # 生产环境的 VAULT 在 import 时就 resolve 过；这里保持同口径，
        # 否则 macOS 的 /var → /private/var 符号链接会让 wikilink 解析判成越界。
        self.root = Path(self.temp.name).resolve()
        self.saved = {
            "VAULT": ingest.VAULT,
            "ARTICLE_DRAFT_DIR": ingest.ARTICLE_DRAFT_DIR,
            "ORAL_DRAFT_DIR": ingest.ORAL_DRAFT_DIR,
            "PUBLISHED_ROOT": ingest.PUBLISHED_ROOT,
            "LOG_DIR": ingest.LOG_DIR,
        }
        ingest.VAULT = self.root
        ingest.ARTICLE_DRAFT_DIR = self.root / "10-创作/30-文章草稿"
        ingest.ORAL_DRAFT_DIR = self.root / "10-创作/20-口播草稿"
        ingest.PUBLISHED_ROOT = self.root / "40-发布"
        ingest.LOG_DIR = self.root / "logs"
        ingest.ARTICLE_DRAFT_DIR.mkdir(parents=True)
        ingest.ORAL_DRAFT_DIR.mkdir(parents=True)
        (self.root / "40-发布/10-X长文").mkdir(parents=True)
        self.now = datetime.now().astimezone()

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(ingest, name, value)
        self.temp.cleanup()

    def write_draft(
        self,
        name: str = "2026-07-27-测试长文.md",
        *,
        title: str = "多智能体怎么分工：一份可直接用的模板",
        saved_hours_ago: float = 2.0,
        x_status: str = "draft",
        with_pack: bool = False,
        directory: Path | None = None,
    ) -> Path:
        lines = [
            "---",
            "kind: draft",
            "status: draft",
            f'title: "{title}"',
            "content_id: article-test-1",
        ]
        if with_pack:
            pack = self.root / "10-创作/20-写作任务/测试成稿包.md"
            pack.parent.mkdir(parents=True, exist_ok=True)
            pack.write_text(
                "---\nkind: content-pack\nstatus: drafting\n---\n\n# 测试成稿包\n",
                encoding="utf-8",
            )
            lines.append('content_pack: "[[10-创作/20-写作任务/测试成稿包]]"')
        if x_status:
            saved = (self.now - timedelta(hours=saved_hours_ago)).isoformat(
                timespec="seconds"
            )
            lines.append(f"x_article_status: {x_status}")
            lines.append(f"x_article_saved_at: {saved}")
        lines += ["---", "", f"# {title}", "", "正文。"]
        path = (directory or ingest.ARTICLE_DRAFT_DIR) / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def article_post(
        self, title: str, hours_ago: float, post_id: str = "2082000000000000001"
    ) -> dict:
        created = (self.now - timedelta(hours=hours_ago)).astimezone(timezone.utc)
        return {
            "id": post_id,
            "created_at": created.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "article": {"title": title},
        }

    def xurl_stub(self, posts: list[dict]) -> tuple[list[str], object]:
        calls: list[str] = []

        def fake(config: dict, args: list[str]) -> dict:
            calls.append(args[0])
            if args[0] == "/2/users/me":
                return {"data": {"id": "42", "username": "tester"}}
            return {"data": posts}

        return calls, fake

    def detect(self, posts: list[dict], dry_run: bool = False) -> tuple[dict, list[str]]:
        calls, fake = self.xurl_stub(posts)
        with patch.object(ingest, "run_xurl_request", fake):
            result = ingest.detect_x_article_publications({}, dry_run=dry_run)
        return result, calls

    def test_stale_or_absent_drafts_never_call_api(self) -> None:
        self.write_draft("过窗口.md", saved_hours_ago=100)
        self.write_draft("没推过X.md", x_status="")
        self.write_draft("已发布.md", x_status="published")
        result, calls = self.detect([self.article_post("多智能体怎么分工：一份可直接用的模板", 1)])
        self.assertEqual(calls, [])
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["published"], 0)

    def test_unmatched_draft_only_logs_and_keeps_files(self) -> None:
        path = self.write_draft(directory=ingest.ORAL_DRAFT_DIR)
        result, calls = self.detect([self.article_post("另一篇文章的标题", 1)])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["unmatched"], 1)
        self.assertIn("x_article_status: draft", path.read_text(encoding="utf-8"))
        self.assertEqual(list((self.root / "40-发布/10-X长文").glob("*.md")), [])

    def test_post_published_before_saved_at_does_not_match(self) -> None:
        item = {
            "title": "同名标题",
            "saved_at": self.now - timedelta(hours=2),
        }
        post, reason = ingest.match_x_article_post(
            item, [self.article_post("同名标题", 5)]
        )
        self.assertIsNone(post)
        self.assertEqual(reason, "not-found")

    def test_title_edited_at_publish_still_matches_by_similarity(self) -> None:
        # 实测案例：发布前把「…1 份可直接用的模板」精简成「…1 份模板」。
        item = {
            "title": "多智能体怎么分工：3 种角色、4 条边界、1 份可直接用的模板",
            "saved_at": self.now - timedelta(hours=2),
        }
        post, reason = ingest.match_x_article_post(
            item,
            [self.article_post("多智能体怎么分工：3 种角色、4 条边界、1 份模板", 1)],
        )
        self.assertIsNotNone(post)
        self.assertEqual(reason, "")

    def test_only_sharing_the_main_title_is_not_similar_enough(self) -> None:
        item = {
            "title": "多智能体怎么分工：3 种角色、4 条边界、1 份可直接用的模板",
            "saved_at": self.now - timedelta(hours=2),
        }
        post, reason = ingest.match_x_article_post(
            item, [self.article_post("多智能体怎么分工：为什么大多数人第一步就错了", 1)]
        )
        self.assertIsNone(post)
        self.assertEqual(reason, "not-found")

    def test_exact_match_beats_similar_match(self) -> None:
        item = {
            "title": "同名标题：完整副标题",
            "saved_at": self.now - timedelta(hours=2),
        }
        post, reason = ingest.match_x_article_post(
            item,
            [
                self.article_post("同名标题：完整副标", 1, "2082000000000000005"),
                self.article_post("同名标题：完整副标题", 1, "2082000000000000006"),
            ],
        )
        self.assertEqual(reason, "")
        self.assertEqual(post["id"], "2082000000000000006")

    def test_ambiguous_matches_are_left_for_manual_registration(self) -> None:
        path = self.write_draft()
        posts = [
            self.article_post("多智能体怎么分工：一份可直接用的模板", 1, "2082000000000000001"),
            self.article_post("多智能体怎么分工：一份可直接用的模板", 0.5, "2082000000000000002"),
        ]
        result, _ = self.detect(posts)
        self.assertEqual(result["unmatched"], 1)
        self.assertEqual(result["published"], 0)
        self.assertIn("x_article_status: draft", path.read_text(encoding="utf-8"))

    def test_detected_publication_archives_records_and_closes_gate(self) -> None:
        draft = self.write_draft(with_pack=True)
        result, _ = self.detect(
            [self.article_post("多智能体怎么分工：一份可直接用的模板", 1)]
        )
        self.assertEqual(result["published"], 1, result)

        article = self.root / "40-发布/10-X长文/2026-07-27-测试长文.md"
        self.assertTrue(article.is_file())
        article_text = article.read_text(encoding="utf-8")
        self.assertIn("kind: article-published", article_text)
        self.assertIn("x_article_status: published", article_text)
        self.assertIn(
            'x_article_url: "https://x.com/tester/status/2082000000000000001"',
            article_text,
        )

        draft_text = draft.read_text(encoding="utf-8")
        self.assertIn("x_article_status: published", draft_text)
        self.assertIn("status: published", draft_text)
        self.assertIn('published_file: "[[40-发布/10-X长文/2026-07-27-测试长文]]"', draft_text)

        pack_text = (self.root / "10-创作/20-写作任务/测试成稿包.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("status: published", pack_text)
        self.assertIn('published_file: "[[40-发布/10-X长文/2026-07-27-测试长文]]"', pack_text)

        self.assertTrue(list((self.root / "40-发布/00-内容反馈").glob("*.md")))

        # 门已关：下一轮不再调 API
        result_again, calls_again = self.detect([])
        self.assertEqual(calls_again, [])
        self.assertEqual(result_again["pending"], 0)

    def test_dry_run_reports_but_writes_nothing(self) -> None:
        draft = self.write_draft(with_pack=True)
        before = draft.read_text(encoding="utf-8")
        result, _ = self.detect(
            [self.article_post("多智能体怎么分工：一份可直接用的模板", 1)], dry_run=True
        )
        self.assertEqual(result["published"], 1)
        self.assertEqual(draft.read_text(encoding="utf-8"), before)
        self.assertEqual(list((self.root / "40-发布/10-X长文").glob("*.md")), [])
        self.assertIn(
            "status: drafting",
            (self.root / "10-创作/20-写作任务/测试成稿包.md").read_text(encoding="utf-8"),
        )

    def test_already_recorded_article_closes_gate_without_api(self) -> None:
        article = self.root / "40-发布/10-X长文/正式稿.md"
        article.write_text(
            "---\n"
            "kind: article-published\n"
            "status: published\n"
            "x_article_status: published\n"
            'x_article_url: "https://x.com/tester/status/2082000000000000009"\n'
            'x_article_published_at: "2026-07-28T10:00:00+00:00"\n'
            "---\n\n# 正式稿\n",
            encoding="utf-8",
        )
        draft = self.write_draft()
        draft.write_text(
            draft.read_text(encoding="utf-8").replace(
                "content_id: article-test-1",
                'content_id: article-test-1\npublished_file: "[[40-发布/10-X长文/正式稿]]"',
            ),
            encoding="utf-8",
        )
        result, calls = self.detect([])
        self.assertEqual(calls, [])
        self.assertEqual(result["reconciled"], 1)
        draft_text = draft.read_text(encoding="utf-8")
        self.assertIn("x_article_status: published", draft_text)
        self.assertIn(
            'x_article_url: "https://x.com/tester/status/2082000000000000009"', draft_text
        )


if __name__ == "__main__":
    unittest.main()
