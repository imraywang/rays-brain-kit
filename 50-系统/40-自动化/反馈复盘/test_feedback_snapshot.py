from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("feedback_snapshot.py")
SPEC = importlib.util.spec_from_file_location("feedback_snapshot_test", MODULE_PATH)
assert SPEC and SPEC.loader
fb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fb)

TZ = timezone(timedelta(hours=-7))
PUBLISHED = datetime(2026, 7, 19, 23, 30, tzinfo=TZ)

CARD = """---
kind: content-feedback
status: pending
article: "[[40-发布/10-X长文/测试文章]]"
platform: X Article + 微信公众号
published_at: 2026-07-19T23:30:00-07:00
feedback_stage: 7d
due_at: 2026-07-26T23:30:00-07:00
feedback_24h_status: complete
feedback_7d_status: pending
public_url: "https://x.com/wangray/status/12345"
wechat_data_status: pending
---

# 测试反馈卡

## 首次事实快照

### X Article

- 浏览：1936
- 点赞：7
- 收藏：0
- 转发：0
- 评论：3
- 抓取时间：2026-07-23 02:56 PT

## 7 天累计快照

### X Article

- 浏览：
- 点赞：
- 收藏：
- 转发：
- 评论：

## 7 天判断

- 下次继续保留：

## 回写去向

- 长期知识：[[20-知识/40-观点/测试观点]]
"""

METRICS = {
    "impression_count": 2270,
    "like_count": 7,
    "bookmark_count": 4,
    "retweet_count": 1,
    "quote_count": 1,
    "reply_count": 3,
}


class FeedbackSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name)
        self.feedback_dir = self.vault / fb.FEEDBACK_DIR_REL
        self.feedback_dir.mkdir(parents=True)
        self.board = self.vault / fb.WORKBENCH_REL
        self.knowledge = self.vault / "20-知识/40-观点/测试观点.md"
        self.knowledge.parent.mkdir(parents=True)
        self.knowledge.write_text("# 测试观点\n\n正文。\n", encoding="utf-8")
        self.card = self.feedback_dir / "2026-07-19-测试.md"
        self.card.write_text(CARD, encoding="utf-8")
        self.now = PUBLISHED + timedelta(days=11)
        self.calls = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fetch(self, tweet_id: str) -> dict:
        self.calls += 1
        assert tweet_id == "12345"
        return dict(METRICS)

    def test_7d_snapshot_filled_and_wechat_downgraded(self) -> None:
        summary = fb.run(self.vault, now=self.now, fetch=self.fetch)
        text = self.card.read_text(encoding="utf-8")
        meta = fb.parse_frontmatter(text)
        self.assertEqual(summary["snapshotted"], 1)
        self.assertIn("- 浏览：2270", text.split("## 7 天累计快照")[1])
        self.assertIn("- 转发：2", text)  # 转推 1 + 引用 1
        self.assertEqual(meta["feedback_7d_data_status"], "captured")
        self.assertEqual(meta["wechat_data_status"], "unavailable")
        # 24h 已 complete，不重复抓；首次快照原值保留
        self.assertIn("- 浏览：1936", text.split("## 首次事实快照")[1].split("## 7 天累计快照")[0])
        self.assertEqual(self.calls, 1)

    def test_manual_values_not_overwritten(self) -> None:
        text = CARD.replace("## 7 天累计快照\n\n### X Article\n\n- 浏览：", "## 7 天累计快照\n\n### X Article\n\n- 浏览：9999")
        self.card.write_text(text, encoding="utf-8")
        fb.run(self.vault, now=self.now, fetch=self.fetch)
        self.assertIn("- 浏览：9999", self.card.read_text(encoding="utf-8"))

    def test_semantic_only_7d_template_keeps_the_numeric_snapshot(self) -> None:
        text = CARD.replace(
            "- 浏览：\n- 点赞：\n- 收藏：\n- 转发：\n- 评论：",
            "- 持续流量：\n- 转化或线索：\n- 读者真正复用的部分：",
        )
        self.card.write_text(text, encoding="utf-8")
        fb.run(self.vault, now=self.now, fetch=self.fetch)
        updated = self.card.read_text(encoding="utf-8")
        section = updated.split("## 7 天累计快照", 1)[1].split("## 7 天判断", 1)[0]
        self.assertIn("- 曝光：2270", section)
        self.assertIn("- 收藏：4", section)
        self.assertIn("- 转发：2", section)
        self.assertIn("- 评论与私信：3", section)
        self.assertIn("- 持续流量：", section)
        meta = fb.parse_frontmatter(updated)
        self.assertEqual(meta["feedback_7d_data_status"], "captured")

    def test_board_lists_ready_item_with_suggestion(self) -> None:
        fb.run(self.vault, now=self.now, fetch=self.fetch)
        board = self.board.read_text(encoding="utf-8")
        self.assertIn("feedback-review:2026-07-19-测试", board)
        self.assertIn("- [ ] 继续深挖", board)
        self.assertIn("←建议", board)

    def test_confirmed_verdict_closes_and_writes_back(self) -> None:
        fb.run(self.vault, now=self.now, fetch=self.fetch)
        board_text = self.board.read_text(encoding="utf-8")
        board_text = board_text.replace("- [ ] 停止此角度", "- [x] 停止此角度")
        board_text = board_text.replace("  - 理由：", "  - 理由：角度已写透，读者没有新问题\n", 1)
        self.board.write_text(board_text, encoding="utf-8")

        fb.run(self.vault, now=self.now + timedelta(days=1), fetch=self.fetch)
        card = self.card.read_text(encoding="utf-8")
        meta = fb.parse_frontmatter(card)
        self.assertEqual(meta["status"], "complete")
        self.assertEqual(meta["feedback_stage"], "closed")
        self.assertTrue(meta["feedback_completed_at"])
        self.assertIn("## 复盘结论", card)
        self.assertIn("停止此角度", card)
        knowledge = self.knowledge.read_text(encoding="utf-8")
        self.assertIn("## 发布反馈", knowledge)
        self.assertIn("停止此角度", knowledge)
        # 关闭后不再出现在待判断区
        board = self.board.read_text(encoding="utf-8")
        self.assertNotIn("feedback-review:2026-07-19-测试", board)

    def test_conflicting_checkboxes_skipped(self) -> None:
        fb.run(self.vault, now=self.now, fetch=self.fetch)
        board_text = self.board.read_text(encoding="utf-8")
        board_text = board_text.replace("- [ ] 继续深挖", "- [x] 继续深挖")
        board_text = board_text.replace("- [ ] 归档", "- [x] 归档")
        self.board.write_text(board_text, encoding="utf-8")
        summary = fb.run(self.vault, now=self.now + timedelta(days=1), fetch=self.fetch)
        meta = fb.parse_frontmatter(self.card.read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "pending")
        self.assertTrue(any("勾选了 2 个结论" in n for n in summary["notes"]))

    def test_stale_close(self) -> None:
        fb.run(self.vault, now=PUBLISHED + timedelta(days=45), fetch=self.fetch)
        meta = fb.parse_frontmatter(self.card.read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "not-needed")
        self.assertEqual(meta["feedback_stage"], "closed")
        self.assertIn("close_reason", meta)

    def test_video_card_without_links_goes_to_waiting(self) -> None:
        video = self.feedback_dir / "2026-07-24-视频.md"
        video.write_text(
            "---\nkind: content-feedback\nstatus: pending\nform: 口播短视频\n"
            "published_at: \"\"\npublic_url: \"\"\ndue_at: \"\"\n---\n\n# 视频反馈\n",
            encoding="utf-8",
        )
        fb.run(self.vault, now=self.now, fetch=self.fetch)
        board = self.board.read_text(encoding="utf-8")
        self.assertIn("等待条件补齐", board)
        self.assertIn("2026-07-24-视频", board)

    def test_legacy_completed_status_is_normalized(self) -> None:
        self.card.write_text(
            CARD.replace("status: pending", "status: 复盘完成", 1).replace(
                "feedback_7d_status: pending",
                "feedback_7d_status: pending\nshipinhao_data_status: pending",
            ),
            encoding="utf-8",
        )
        summary = fb.run(self.vault, now=self.now, fetch=self.fetch)
        meta = fb.parse_frontmatter(self.card.read_text(encoding="utf-8"))
        self.assertEqual(summary["normalized"], 1)
        self.assertEqual(meta["status"], "complete")
        self.assertEqual(meta["feedback_stage"], "closed")
        self.assertEqual(meta["feedback_7d_status"], "unavailable")
        self.assertEqual(meta["shipinhao_data_status"], "unavailable")

    def test_close_current_without_data_is_explicit_and_syncs_article(self) -> None:
        article = self.vault / "40-发布/10-X长文/测试文章.md"
        article.parent.mkdir(parents=True)
        article.write_text(
            "---\nkind: article-published\nstatus: published\n"
            "feedback_24h_status: pending\nfeedback_7d_status: pending\n"
            "shipinhao_data_status: pending\ndouyin_data_status: pending\n---\n\n# 测试\n",
            encoding="utf-8",
        )
        summary = fb.run(
            self.vault,
            now=self.now,
            fetch=self.fetch,
            close_current_without_data=True,
        )
        meta = fb.parse_frontmatter(self.card.read_text(encoding="utf-8"))
        self.assertEqual(summary["closed_without_data"], 1)
        self.assertEqual(meta["status"], "complete")
        self.assertEqual(meta["feedback_24h_status"], "complete")
        self.assertEqual(meta["feedback_7d_status"], "unavailable")
        self.assertEqual(meta["verdict"], "data-unavailable")
        article_meta = fb.parse_frontmatter(article.read_text(encoding="utf-8"))
        self.assertEqual(article_meta["feedback_24h_status"], "complete")
        self.assertEqual(article_meta["feedback_7d_status"], "unavailable")
        self.assertEqual(article_meta["shipinhao_data_status"], "unavailable")
        self.assertEqual(article_meta["douyin_data_status"], "unavailable")

    def test_pure_video_platform_card_downgraded_and_ready(self) -> None:
        video = self.feedback_dir / "2026-07-24-纯视频.md"
        video.write_text(
            "---\nkind: content-feedback\nstatus: pending\nform: 口播短视频\n"
            "platform: \"视频号；抖音；小红书\"\n"
            "published_at: \"\"\npublic_url: \"\"\ndue_at: \"\"\n"
            f"publication_confirmed_at: {PUBLISHED.isoformat()}\n"
            "shipinhao_data_status: pending\ndouyin_data_status: pending\n"
            "xiaohongshu_data_status: pending\n---\n\n# 纯视频反馈\n",
            encoding="utf-8",
        )
        fb.run(self.vault, now=self.now, fetch=self.fetch)
        meta = fb.parse_frontmatter(video.read_text(encoding="utf-8"))
        self.assertEqual(meta["shipinhao_data_status"], "unavailable")
        self.assertEqual(meta["douyin_data_status"], "unavailable")
        self.assertEqual(meta["xiaohongshu_data_status"], "unavailable")
        board = self.board.read_text(encoding="utf-8")
        self.assertIn("feedback-review:2026-07-24-纯视频", board)
        self.assertIn("暂无平台数据", board)

    def test_lean_template_card_filled(self) -> None:
        lean = self.feedback_dir / "2026-07-28-精简.md"
        lean.write_text(
            "---\nkind: content-feedback\nstatus: pending\n"
            "published_at: \"2026-07-28T02:55:00-07:00\"\n"
            "public_url: \"https://x.com/wangray/status/12345\"\n"
            "feedback_stage: 24h\ndue_at: \"2026-07-29T02:55:00-07:00\"\n"
            "feedback_24h_status: pending\nfeedback_7d_status: pending\n---\n\n"
            "# 内容反馈：精简\n\n## 24 小时\n\n- 曝光：\n- 打开或阅读：\n- 收藏：\n- 转发：\n- 评论与私信：\n- 最强反馈：\n\n"
            "## 7 天\n\n- 曝光：\n- 收藏：\n- 转发：\n- 评论与私信：\n\n## 回写\n",
            encoding="utf-8",
        )
        fb.run(self.vault, now=datetime(2026, 7, 30, 10, 0, tzinfo=TZ), fetch=self.fetch)
        text = lean.read_text(encoding="utf-8")
        meta = fb.parse_frontmatter(text)
        sec_24h = text.split("## 24 小时")[1].split("## 7 天")[0]
        self.assertIn("- 曝光：2270", sec_24h)
        self.assertIn("- 评论与私信：3", sec_24h)
        self.assertIn("- 打开或阅读：\n", sec_24h)  # 无对应指标，保持空
        self.assertEqual(meta["feedback_stage"], "7d")
        self.assertEqual(meta["feedback_24h_status"], "complete")

    def test_fetch_failure_does_not_block_board(self) -> None:
        def bad_fetch(tweet_id: str) -> dict:
            raise RuntimeError("rate limited")

        summary = fb.run(self.vault, now=self.now, fetch=bad_fetch)
        self.assertTrue(summary["errors"])
        board = self.board.read_text(encoding="utf-8")
        self.assertIn("抓取失败", board)
        meta = fb.parse_frontmatter(self.card.read_text(encoding="utf-8"))
        self.assertEqual(meta.get("feedback_7d_data_status", ""), "")


if __name__ == "__main__":
    unittest.main()
