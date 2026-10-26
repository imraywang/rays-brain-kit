from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOW = datetime.fromisoformat("2026-10-26T08:00:00-07:00")


def load_radar(state: Path):
    os.environ["RAYS_BRAIN_STATE"] = str(state)
    spec = importlib.util.spec_from_file_location("radar_under_test", HERE / "radar.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def ts(days_ago: float) -> int:
    return int((NOW - timedelta(days=days_ago)).timestamp())


def douyin_obj(item_id: str, title: str, fans: int, likes: int, days_ago: float = 3) -> dict:
    return {
        "item_id": item_id, "item_title": title, "nick_name": f"作者{item_id}",
        "fans_cnt": fans, "like_cnt": likes, "play_cnt": likes * 10,
        "publish_time": ts(days_ago), "follow_rate": 0.002,
        "item_cover_url": "https://p3-sign.douyinpic.com/cover.jpeg",
    }


def xhs_note(note_id: str, title: str, likes: int, collects: int, userid: str, days_ago: float = 3) -> dict:
    return {
        "id": note_id, "title": title, "desc": f"{title} #科普",
        "liked_count": likes, "collected_count": collects, "comments_count": 10, "shared_count": 5,
        "timestamp": ts(days_ago), "xsec_token": "TOKEN=",
        "user": {"nickname": f"红薯{userid}", "userid": userid},
        "images_list": [{"url_size_large": "https://sns-na-i11.xhscdn.com/a.webp"}],
    }


class FakeTikHub:
    """按端点返回预置响应，并记录调用。"""

    def __init__(self, radar, douyin: dict[str, list], xhs: dict[str, list], fans: dict[str, object]):
        self.radar = radar
        self.douyin = douyin
        self.xhs = xhs
        self.fans = fans
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, endpoint, *, params=None, body=None, config, budget):
        budget.spend(endpoint)
        self.calls.append((endpoint, params or body))
        if endpoint == self.radar.DOUYIN_LOW_FAN:
            objs = self.douyin.get(body["keyword"], []) if body["page"] == 1 else []
            return {"code": 0, "data": {"page": {"total": len(objs)}, "objs": objs}}
        if endpoint == self.radar.XHS_SEARCH:
            return {"data": {"items": [{"note": note} for note in self.xhs.get(params["keyword"], [])]}}
        if endpoint == self.radar.XHS_USER:
            return {"data": {"fans": self.fans[params["user_id"]]}}
        raise AssertionError(endpoint)


class RadarTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.vault = Path(self.tmp.name) / "vault"
        self.radar = load_radar(self.state)
        self.config = json.loads((HERE / "radar_config.json").read_text(encoding="utf-8"))
        self.config["douyin"]["keywords"] = ["Vibe知识大赏", "Claude"]
        self.config["xiaohongshu"]["keywords"] = ["AI科普"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def fake(self) -> FakeTikHub:
        return FakeTikHub(
            self.radar,
            douyin={
                "Vibe知识大赏": [
                    douyin_obj("1", "Vibe知识大赏｜那些没有回报的努力，都去哪了？ #vibe知识大赏", 26154, 429727),
                    douyin_obj("2", "粉丝太多的号 #vibe知识大赏", 779146, 409444),
                    douyin_obj("3", "太旧的视频", 2000, 90000, days_ago=40),
                ],
                "Claude": [
                    douyin_obj("1", "Vibe知识大赏｜那些没有回报的努力，都去哪了？", 26154, 430000),
                    douyin_obj("4", "霸总 AI 短剧第 3 集 #ai漫剧", 3000, 80000),
                    douyin_obj("5", "Claude 必装的 8 个 Skill", 2000, 30000),
                ],
            },
            xhs={
                "AI科普": [
                    xhs_note("6ad8b39e000000002c014ef2", "那些没有回报的努力，都去哪了？", 21871, 13236, "u1"),
                    xhs_note("6ad8b39e000000002c014ef3", "大号发的", 5000, 3000, "u2"),
                    xhs_note("6ad8b39e000000002c014ef4", "没人看的", 10, 3, "u3"),
                ],
            },
            fans={"u1": 2929, "u2": "12.5万"},
        )

    def run_fetch(self, fake=None):
        fake = fake or self.fake()
        record = self.radar.run_fetch(self.config, tikhub={"api_key": "x"}, call=fake, now=NOW)
        return record, fake

    def view_ids(self) -> dict[str, dict]:
        view = self.radar.radar_view(self.config, now=NOW)
        return {row["id"]: row for row in view["items"]}

    def test_fetch_filters_low_fan_hits_and_merges_keywords(self) -> None:
        record, fake = self.run_fetch()
        self.assertEqual(record["errors"], [])
        rows = self.view_ids()
        hit = rows["douyin:1"]
        self.assertTrue(hit["passed"])
        self.assertEqual(hit["keywords"], ["Claude", "Vibe知识大赏"])
        self.assertEqual(hit["likes"], 430000)
        self.assertEqual(hit["title"], "Vibe知识大赏｜那些没有回报的努力，都去哪了？")
        self.assertNotIn("douyin:2", rows)  # 粉丝超上限
        self.assertNotIn("douyin:3", rows)  # 超出时效
        self.assertNotIn("douyin:4", rows)  # 命中排除词
        self.assertTrue(rows["douyin:5"]["listicle"])
        self.assertFalse(hit["listicle"])

    def test_xhs_fans_are_enriched_only_for_qualified_notes_and_cached(self) -> None:
        _, fake = self.run_fetch()
        user_calls = [params["user_id"] for endpoint, params in fake.calls if endpoint == self.radar.XHS_USER]
        self.assertEqual(sorted(user_calls), ["u1", "u2"])  # u3 互动不够，不花钱查
        rows = self.view_ids()
        note = rows["xiaohongshu:6ad8b39e000000002c014ef2"]
        self.assertEqual(note["fans"], 2929)
        self.assertEqual(note["interactions"], 21871 + 13236)
        self.assertTrue(note["passed"])
        self.assertIn("xsec_token=TOKEN%3D", note["url"])
        self.assertNotIn("xiaohongshu:6ad8b39e000000002c014ef3", rows)  # 12.5 万粉，被解析后筛掉
        _, second = self.run_fetch()
        self.assertFalse(any(endpoint == self.radar.XHS_USER for endpoint, _ in second.calls))

    def test_budget_stops_run_and_keeps_partial_results(self) -> None:
        self.config["max_calls"] = 1
        record, _ = self.run_fetch()
        self.assertEqual(record["calls"], 1)
        self.assertTrue(any("调用上限" in error for error in record["errors"]))
        self.assertIn("douyin:1", self.view_ids())

    def test_adopt_writes_candidate_note_once(self) -> None:
        self.run_fetch()
        result = self.radar.adopt_item(
            "douyin:1", "用渗流相变讲平台期", vault=self.vault,
            topics_dir="10-创作/10-灵感/20-候选选题", attention_days=7, now=NOW,
        )
        path = self.vault / result["candidate"]
        text = path.read_text(encoding="utf-8")
        self.assertIn("kind: topic-candidate", text)
        self.assertIn("status: candidate", text)
        self.assertIn('source_url: "https://www.douyin.com/video/1"', text)
        self.assertIn('attention_until: "2026-11-02"', text)
        self.assertIn("- [ ] A1 · 用渗流相变讲平台期", text)
        self.assertIn("粉丝 26,154", text)
        again = self.radar.adopt_item(
            "douyin:1", "", vault=self.vault, topics_dir="10-创作/10-灵感/20-候选选题", attention_days=7, now=NOW,
        )
        self.assertTrue(again["existing"])
        self.assertEqual(len(list(path.parent.iterdir())), 1)
        self.assertEqual(self.view_ids()["douyin:1"]["status"], "adopted")
        with self.assertRaises(ValueError):
            self.radar.mark_item("douyin:1", "reject")

    def test_reject_with_author_block_hides_author_and_restore_undoes(self) -> None:
        self.run_fetch()
        self.radar.mark_item("douyin:5", "reject", block_author=True, now=NOW)
        rows = self.view_ids()
        self.assertEqual(rows["douyin:5"]["status"], "rejected")
        self.assertIn("作者5", self.radar.radar_view(self.config, now=NOW)["blocked_authors"])
        self.radar.mark_item("douyin:5", "restore", now=NOW)
        view = self.radar.radar_view(self.config, now=NOW)
        self.assertEqual(view["blocked_authors"], [])
        self.assertEqual({row["id"]: row for row in view["items"]}["douyin:5"]["status"], "new")

    def test_old_items_are_pruned_but_adopted_ones_stay(self) -> None:
        self.run_fetch()
        self.radar.adopt_item(
            "douyin:1", "", vault=self.vault, topics_dir="topics", attention_days=7, now=NOW,
        )
        empty = FakeTikHub(self.radar, {}, {}, {})
        self.radar.run_fetch(self.config, tikhub={"api_key": "x"}, call=empty, now=NOW + timedelta(days=40))
        store = json.loads((self.state / "radar" / "items.json").read_text(encoding="utf-8"))
        self.assertEqual(list(store), ["douyin:1"])

    def test_cover_proxy_only_allows_platform_cdns_and_caches(self) -> None:
        self.run_fetch()
        fetched = []

        def fetch(url):
            fetched.append(url)
            return b"\xff\xd8\xffdata", "image/jpeg"

        data, kind = self.radar.cover_bytes("douyin:1", fetch)
        self.assertEqual(kind, "image/jpeg")
        self.radar.cover_bytes("douyin:1", fetch)
        self.assertEqual(len(fetched), 1)
        store_path = self.state / "radar" / "items.json"
        store = json.loads(store_path.read_text(encoding="utf-8"))
        store["douyin:5"]["cover"] = "https://evil.example.com/x.jpg"
        store_path.write_text(json.dumps(store), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.radar.cover_bytes("douyin:5", fetch)

    def test_number_parsing(self) -> None:
        self.assertEqual(self.radar.to_int("1.2万"), 12000)
        self.assertEqual(self.radar.to_int("3,456"), 3456)
        self.assertEqual(self.radar.to_int("10w+"), 100000)
        self.assertIsNone(self.radar.to_int("未知"))


if __name__ == "__main__":
    unittest.main()
