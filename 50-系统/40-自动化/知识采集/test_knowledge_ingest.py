from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("knowledge_ingest.py")
SPEC = importlib.util.spec_from_file_location("knowledge_ingest", MODULE_PATH)
assert SPEC and SPEC.loader
ingest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ingest)


class XBookmarkCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_x_dir = ingest.X_DIR
        ingest.X_DIR = Path(self.temp.name)

    def tearDown(self) -> None:
        ingest.X_DIR = self.old_x_dir
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


class ContentPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_vault = ingest.VAULT
        self.old_log_dir = ingest.LOG_DIR
        self.old_review_dir = ingest.REVIEW_DIR
        self.old_content_pack_dir = ingest.CONTENT_PACK_DIR
        self.old_knowledge_dir = ingest.KNOWLEDGE_DIR
        ingest.VAULT = Path(self.temp.name)
        ingest.LOG_DIR = ingest.VAULT / "logs"
        ingest.REVIEW_DIR = ingest.VAULT / "10-创作/10-灵感/10-待评估/剪藏复核"
        ingest.CONTENT_PACK_DIR = ingest.VAULT / "10-创作/10-灵感/20-成稿包"
        ingest.KNOWLEDGE_DIR = ingest.VAULT / "20-知识"

    def tearDown(self) -> None:
        ingest.VAULT = self.old_vault
        ingest.LOG_DIR = self.old_log_dir
        ingest.REVIEW_DIR = self.old_review_dir
        ingest.CONTENT_PACK_DIR = self.old_content_pack_dir
        ingest.KNOWLEDGE_DIR = self.old_knowledge_dir
        self.temp.cleanup()

    @staticmethod
    def review_text() -> str:
        return """---
capture_id: "file:source.md"
source_file: "30-资料/40-网页剪藏/source.md"
source_url: "https://example.com/source"
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

    def test_writing_review_action_creates_pack_and_records_result(self) -> None:
        ingest.REVIEW_DIR.mkdir(parents=True)
        card = ingest.REVIEW_DIR / "card.md"
        text = self.review_text() + "\n## 人工审核\n\n- [ ] 批准进入长期知识库\n- [x] 仅保留为写作素材\n- [ ] 暂缓\n- [ ] 标记为可恢复的待清理项\n"
        card.write_text(text, encoding="utf-8")
        result = ingest.process_review_actions()
        updated = card.read_text(encoding="utf-8")
        self.assertEqual(result["writing"], 1)
        self.assertIn('status: "可写作"', updated)
        self.assertIn("result_file:", updated)
        self.assertEqual(len(list(ingest.CONTENT_PACK_DIR.glob("*.md"))), 1)


if __name__ == "__main__":
    unittest.main()
