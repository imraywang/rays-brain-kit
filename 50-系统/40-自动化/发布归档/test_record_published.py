"""D14/D15：发布后归档必须可重跑、不越界、不伪造平台数据。"""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

import fake_vault

X_URL = "https://x.com/tester/status/2079091643693863273"
WECHAT_URL = "https://mp.weixin.qq.com/s/AbCdEfGh12345"
PUBLISHED_AT = "2026-07-19T23:30:50-07:00"
NOW = "2026-07-20T09:00:00-07:00"
ARTICLE = "40-发布/10-X长文/2026-07-19-测试长文.md"
CARD = "40-发布/00-内容反馈/2026-07-19-测试长文.md"

DOUYIN_URL = "https://www.douyin.com/video/7530000000000000000"
XHS_URL = "https://www.xiaohongshu.com/explore/66a1b2c3d4e5f60718293a4b"
SHIPINHAO_URL = "https://channels.weixin.qq.com/pages/finder/feed?id=Ab12Cd34"
VIDEO = "40-发布/50-口播视频/2026-07-24-测试口播.md"
VIDEO_CARD = "40-发布/00-内容反馈/2026-07-19-测试口播.md"


def load(root: Path):
    os.environ["RAYS_BRAIN"] = str(root)
    spec = importlib.util.spec_from_file_location(
        "record_published", Path(__file__).with_name("record_published.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordPublishedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = fake_vault.build(self.root)
        self.rp = load(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_record(self, **kwargs) -> list[str]:
        options = {
            "article_arg": ARTICLE,
            "platform": "x",
            "url": X_URL,
            "published_at": PUBLISHED_AT,
            "now": self.rp.parse_moment(NOW),
            "dry_run": False,
        }
        options.update(kwargs)
        return self.rp.record(**options)

    def meta(self, where: str) -> dict[str, str]:
        return self.rp.parse_frontmatter((self.root / where).read_text(encoding="utf-8"))

    # ---------------------------------------------------------- 正式稿

    def test_records_the_three_fields_the_health_check_reads(self) -> None:
        self.run_record()
        meta = self.meta(ARTICLE)
        self.assertEqual(meta["x_article_status"], "published")
        self.assertEqual(meta["x_article_url"], X_URL)
        self.assertEqual(meta["x_article_published_at"], PUBLISHED_AT)

    def test_wechat_uses_its_own_field_names_and_clears_pending_metadata(self) -> None:
        self.run_record(platform="wechat", url=WECHAT_URL)
        meta = self.meta(ARTICLE)
        self.assertEqual(meta["wechat_public_url"], WECHAT_URL)
        self.assertEqual(meta["wechat_published_at"], PUBLISHED_AT)
        self.assertEqual(meta["wechat_metadata_status"], "complete")
        # X 那边一个字段都不该被碰。
        self.assertEqual(meta["x_article_status"], "draft")

    def test_editor_url_is_refused(self) -> None:
        """草稿编辑器地址记成公开地址，健康检查就再也发现不了这篇没发。"""
        with self.assertRaises(self.rp.RecordError) as caught:
            self.run_record(url="https://x.com/compose/articles/edit/2079080041305276416")
        self.assertIn("草稿编辑器", str(caught.exception))

    def test_wrong_platform_url_is_refused(self) -> None:
        with self.assertRaises(self.rp.RecordError):
            self.run_record(platform="wechat", url=X_URL)
        with self.assertRaises(self.rp.RecordError):
            self.run_record(platform="x", url=WECHAT_URL)

    def test_naive_timestamp_is_refused(self) -> None:
        """时间可留空；一旦填写，没有时区仍然报错。"""
        with self.assertRaises(self.rp.RecordError):
            self.run_record(published_at="2026-07-19T23:30:50")

    def test_url_and_publish_time_are_optional(self) -> None:
        self.run_record(url="", published_at="")
        meta = self.meta(ARTICLE)
        self.assertEqual(meta["x_article_status"], "published")
        self.assertEqual(meta["x_article_url"], "")
        self.assertEqual(meta["x_article_published_at"], "")
        self.assertEqual(meta["publication_confirmed_at"], NOW)

        card = "40-发布/00-内容反馈/2026-07-20-测试长文.md"
        card_meta = self.meta(card)
        self.assertEqual(card_meta["published_at"], "")
        self.assertEqual(card_meta["public_url"], "")
        self.assertEqual(card_meta["publication_confirmed_at"], NOW)
        self.assertEqual(card_meta["due_at"], "2026-07-21T09:00:00-07:00")

    def test_cli_only_requires_the_platform(self) -> None:
        result = self.rp.main([
            "--article", ARTICLE,
            "--platform", "x",
            "--now", NOW,
            "--dry-run",
        ])
        self.assertEqual(result, 0)

    def test_second_different_url_is_refused_instead_of_overwritten(self) -> None:
        self.run_record()
        with self.assertRaises(self.rp.RecordError) as caught:
            self.run_record(url="https://x.com/tester/status/2079091643693863274")
        self.assertIn("已经记过另一个公开地址", str(caught.exception))

    def test_only_files_under_the_publish_dirs_are_accepted(self) -> None:
        with self.assertRaises(self.rp.RecordError):
            self.run_record(article_arg="30-资料/30-X书签/2026-07/2026-07-18-测试书签.md")

    def test_paths_outside_the_vault_are_refused(self) -> None:
        with self.assertRaises(self.rp.RecordError):
            self.run_record(article_arg="/etc/hosts")

    def test_dry_run_writes_nothing(self) -> None:
        before = (self.root / ARTICLE).read_text(encoding="utf-8")
        self.run_record(dry_run=True)
        self.assertEqual((self.root / ARTICLE).read_text(encoding="utf-8"), before)
        self.assertFalse((self.root / CARD).exists())
        self.assertTrue(self.paths["draft"].exists())
        self.assertTrue(self.paths["pack"].exists())

    # ---------------------------------------------------------- 发布后收尾

    def test_published_article_closes_out_draft_and_content_pack(self) -> None:
        self.run_record()
        archived_draft = self.root / (
            "90-归档/30-创作记录/旧文章草稿/2026-07/2026-07-19-测试长文母稿.md"
        )
        archived_pack = self.root / (
            "90-归档/30-创作记录/旧成稿包/2026-07/2026-07-18-测试写作任务.md"
        )
        self.assertFalse(self.paths["draft"].exists())
        self.assertFalse(self.paths["pack"].exists())
        self.assertTrue(archived_draft.exists())
        self.assertTrue(archived_pack.exists())
        self.assertEqual(self.rp.parse_frontmatter(archived_draft.read_text())["status"], "archived")
        self.assertEqual(self.rp.parse_frontmatter(archived_pack.read_text())["status"], "archived")

        article = (self.root / ARTICLE).read_text(encoding="utf-8")
        self.assertIn("90-归档/30-创作记录/旧文章草稿/2026-07", article)
        self.assertIn("90-归档/30-创作记录/旧成稿包/2026-07", article)
        # 母稿搬深了两级，配图相对路径必须跟着重算。
        self.assertIn("../../../../60-素材/配图.png", archived_draft.read_text(encoding="utf-8"))

    def test_closeout_is_idempotent(self) -> None:
        self.run_record()
        report = "\n".join(self.run_record())
        self.assertIn("没有仍在活跃目录", report)

    def test_closeout_only_repairs_an_old_published_chain(self) -> None:
        article = self.root / ARTICLE
        report = self.rp.closeout_published_chain(
            article,
            PUBLISHED_AT,
            self.rp.parse_moment(NOW),
        )
        self.assertTrue(any("旧文章草稿" in line for line in report))
        self.assertFalse(self.paths["draft"].exists())
        self.assertFalse(self.paths["pack"].exists())

    def test_closeout_stops_before_writes_when_archive_target_exists(self) -> None:
        collision = self.root / (
            "90-归档/30-创作记录/旧文章草稿/2026-07/2026-07-19-测试长文母稿.md"
        )
        collision.parent.mkdir(parents=True, exist_ok=True)
        collision.write_text("已有文件", encoding="utf-8")
        before = (self.root / ARTICLE).read_text(encoding="utf-8")
        with self.assertRaises(self.rp.RecordError):
            self.run_record()
        self.assertEqual((self.root / ARTICLE).read_text(encoding="utf-8"), before)
        self.assertTrue(self.paths["draft"].exists())

    # ---------------------------------------------------------- 反馈卡

    def test_creates_the_card_from_the_vault_template(self) -> None:
        template = self.root / self.rp.ARTICLE_FEEDBACK_TEMPLATE
        template.write_text(
            template.read_text(encoding="utf-8") + "\n<!-- template-source-marker -->\n",
            encoding="utf-8",
        )
        self.run_record()
        meta = self.meta(CARD)
        self.assertEqual(meta["kind"], "content-feedback")
        self.assertEqual(meta["status"], "pending")
        self.assertEqual(meta["article"], "[[40-发布/10-X长文/2026-07-19-测试长文]]")
        self.assertEqual(meta["public_url"], X_URL)
        self.assertEqual(meta["feedback_stage"], "24h")
        self.assertEqual(meta["due_at"], "2026-07-20T23:30:50-07:00")
        self.assertEqual(meta["feedback_7d_due_at"], "2026-07-26T23:30:50-07:00")
        body = (self.root / CARD).read_text(encoding="utf-8")
        self.assertIn("## 24 小时", body)
        self.assertIn("## 7 天", body)
        section_7d = body.split("## 7 天", 1)[1].split("## 回写", 1)[0]
        self.assertIn("- 曝光：", section_7d)
        self.assertIn("- 抓取时间：", section_7d)
        self.assertIn("template-source-marker", body)
        # 正式稿要指得回反馈卡，否则健康检查找不到这张卡。
        self.assertEqual(
            self.meta(ARTICLE)["feedback_file"], "[[40-发布/00-内容反馈/2026-07-19-测试长文]]"
        )

    def test_rerun_does_not_rebuild_the_card(self) -> None:
        self.run_record()
        card = self.root / CARD
        card.write_text(
            card.read_text(encoding="utf-8").replace("- 曝光：", "- 曝光：1234"),
            encoding="utf-8",
        )
        self.run_record()
        self.assertIn("- 曝光：1234", card.read_text(encoding="utf-8"))

    def test_a_finished_review_is_never_reset_to_pending(self) -> None:
        """复盘做完了又跑一次归档，不能把人的结论打回去重做。"""
        self.run_record()
        card = self.root / CARD
        card.write_text(
            self.rp.update_frontmatter(
                card.read_text(encoding="utf-8"),
                {"feedback_24h_status": "complete", "feedback_stage": "7d"},
            ),
            encoding="utf-8",
        )
        self.run_record(platform="wechat", url=WECHAT_URL)
        meta = self.meta(CARD)
        self.assertEqual(meta["feedback_24h_status"], "complete")
        self.assertEqual(meta["feedback_stage"], "7d")

    def test_wechat_publish_raises_the_backend_data_todo(self) -> None:
        self.run_record(platform="wechat", url=WECHAT_URL)
        self.assertEqual(self.meta(CARD)["wechat_data_status"], "pending")

    # ---------------------------------------------------------- D15 源头书签

    def test_bookmark_is_marked_processed_through_the_writing_task(self) -> None:
        """正式稿只指向写作任务，书签要靠 capture_id 再跳一层才找得到。"""
        self.run_record()
        meta = self.meta("30-资料/30-X书签/2026-07/2026-07-18-测试书签.md")
        self.assertEqual(meta["processing_status"], "processed")
        self.assertEqual(meta["published_file"], "[[40-发布/10-X长文/2026-07-19-测试长文]]")

    def test_renamed_bookmark_is_still_found_by_tweet_id(self) -> None:
        moved = self.root / "30-资料/30-X书签/2026-07/换了个名字.md"
        self.paths["bookmark"].rename(moved)
        self.run_record()
        self.assertEqual(
            self.rp.parse_frontmatter(moved.read_text(encoding="utf-8"))["processing_status"],
            "processed",
        )

    def test_a_web_clip_source_is_reported_not_marked(self) -> None:
        """来源是网页剪藏时没有书签可回写，要说清楚，不能顺手改剪藏卡。"""
        report = self.run_record(article_arg="40-发布/10-X长文/2026-07-21-网页来源长文.md")
        self.assertTrue(any("未找到" in line for line in report))
        self.assertEqual(
            self.meta("30-资料/40-网页剪藏/2026-07-18-某个视频.md")["processing_status"],
            "unprocessed",
        )

    # ---------------------------------------------------------- 归档提醒

    def test_report_names_the_steps_it_did_not_do(self) -> None:
        report = "\n".join(self.run_record())
        self.assertIn("还需要你自己完成", report)
        self.assertIn("knowledge_backfill_status", report)
        self.assertIn("remaining_angles_status", report)
        self.assertIn("partially-published", report)

    # ---------------------------------------------------------- 写法

    def test_quoting_matches_the_vault_convention(self) -> None:
        self.run_record()
        text = (self.root / ARTICLE).read_text(encoding="utf-8")
        self.assertIn("x_article_status: published", text)
        self.assertIn(f'x_article_url: "{X_URL}"', text)
        self.assertIn("platform: X Article，已同步微信公众号", text)
        self.assertIn('feedback_file: "[[40-发布/00-内容反馈/2026-07-19-测试长文]]"', text)

    def test_body_is_left_alone(self) -> None:
        self.run_record()
        self.assertTrue(
            (self.root / ARTICLE).read_text(encoding="utf-8").endswith("\n# 测试长文\n\n正文。\n")
        )

    # ---------------------------------------------------------- 口播视频

    def run_video(self, **kwargs) -> list[str]:
        options = {
            "article_arg": VIDEO,
            "platform": "douyin",
            "url": DOUYIN_URL,
            "published_at": PUBLISHED_AT,
            "now": self.rp.parse_moment(NOW),
            "dry_run": False,
        }
        options.update(kwargs)
        return self.rp.record(**options)

    def test_each_video_platform_writes_its_own_fields(self) -> None:
        self.run_video()
        self.run_video(platform="xiaohongshu", url=XHS_URL)
        self.run_video(platform="shipinhao", url=SHIPINHAO_URL)
        meta = self.meta(VIDEO)
        self.assertEqual(meta["douyin_url"], DOUYIN_URL)
        self.assertEqual(meta["xiaohongshu_url"], XHS_URL)
        self.assertEqual(meta["shipinhao_url"], SHIPINHAO_URL)
        for platform in ("douyin", "xiaohongshu", "shipinhao"):
            self.assertEqual(meta[f"{platform}_status"], "published")
            self.assertEqual(meta[f"{platform}_published_at"], PUBLISHED_AT)
        archived = self.root / (
            "90-归档/30-创作记录/旧口播草稿/2026-07/2026-07-24-测试口播母稿.md"
        )
        self.assertTrue(archived.exists())
        self.assertFalse(self.paths["oral_draft"].exists())

    def test_platform_name_is_required_and_accumulates_without_duplicates(self) -> None:
        video = self.root / VIDEO
        video.write_text(
            video.read_text(encoding="utf-8").replace(
                'platform: "视频号；抖音；小红书"', 'platform: ""'
            ),
            encoding="utf-8",
        )
        self.run_video(platform="douyin", url="")
        self.run_video(platform="shipinhao", url="")
        article_meta = self.meta(VIDEO)
        card_meta = self.meta(VIDEO_CARD)
        self.assertEqual(article_meta["platform"], "抖音；视频号")
        self.assertEqual(card_meta["platform"], "抖音；视频号")

    def test_video_card_uses_the_video_template(self) -> None:
        """图文那张卡没有完播率，套错模板等于三个平台的主指标一个都不记。"""
        self.run_video()
        text = (self.root / VIDEO_CARD).read_text(encoding="utf-8")
        self.assertIn("完播率", text)
        self.assertIn("平均播放时长", text)
        self.assertNotIn("打开或阅读", text)
        self.assertEqual(self.meta(VIDEO_CARD)["form"], "口播短视频")

    def test_three_platforms_keep_three_links_on_one_card(self) -> None:
        """一条视频三个平台共用一张卡，public_url 只装得下第一个，其余不能丢。"""
        self.run_video()
        self.run_video(platform="xiaohongshu", url=XHS_URL)
        self.run_video(platform="shipinhao", url=SHIPINHAO_URL)
        meta = self.meta(VIDEO_CARD)
        self.assertEqual(meta["public_url"], DOUYIN_URL)
        self.assertEqual(meta["douyin_url"], DOUYIN_URL)
        self.assertEqual(meta["xiaohongshu_url"], XHS_URL)
        self.assertEqual(meta["shipinhao_url"], SHIPINHAO_URL)

    def test_every_video_platform_raises_its_backend_data_todo(self) -> None:
        self.run_video()
        self.run_video(platform="xiaohongshu", url=XHS_URL)
        self.run_video(platform="shipinhao", url=SHIPINHAO_URL)
        meta = self.meta(VIDEO_CARD)
        for platform in ("douyin", "xiaohongshu", "shipinhao"):
            self.assertEqual(meta[f"{platform}_data_status"], "pending")

    def test_creator_backend_urls_are_refused(self) -> None:
        """后台地址记成公开地址，健康检查就再也发现不了这篇其实没发出去。"""
        for platform, url in (
            ("douyin", "https://creator.douyin.com/creator-micro/content/manage"),
            ("xiaohongshu", "https://creator.xiaohongshu.com/publish/publish"),
            ("shipinhao", "https://channels.weixin.qq.com/platform/post/list"),
        ):
            with self.assertRaises(self.rp.RecordError):
                self.run_video(platform=platform, url=url)

    def test_shipinhao_accepts_blank_or_the_no_public_url_literal(self) -> None:
        """链接可留空；需要时仍可明确记录平台没有公开地址。"""
        self.run_video(platform="shipinhao", url="")
        self.assertEqual(self.meta(VIDEO)["shipinhao_url"], "")
        self.run_video(platform="shipinhao", url=self.rp.NO_PUBLIC_URL)
        self.assertEqual(self.meta(VIDEO)["shipinhao_url"], self.rp.NO_PUBLIC_URL)

    def test_a_video_without_an_x_bookmark_is_reported_not_invented(self) -> None:
        """口播稿来自微信读书，没有 X 书签可回写，要说清楚而不是乱改别的卡。"""
        report = "\n".join(self.run_video())
        self.assertIn("源头书签 未找到", report)
        self.assertEqual(
            self.meta("30-资料/30-X书签/2026-07/2026-07-18-测试书签.md")["processing_status"],
            "unprocessed",
        )


if __name__ == "__main__":
    unittest.main()
