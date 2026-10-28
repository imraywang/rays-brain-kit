from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "platform_sync.py"

LINKED = """---
kind: video-published
status: published
title: "同样借钱买显卡，凭什么马斯克借得到400亿"
created_at: 2026-10-25
duration_s: 195
platform: 抖音；小红书
douyin_status: published
douyin_url: "https://www.douyin.com/video/7700887182759694947"
---

# 正文
"""

UNLINKED = """---
kind: video-published
status: published
title: "为什么企业上AI，前半年几乎看不到效果"
created_at: 2026-10-26
duration_s: 128
platform: 小红书
douyin_status: pending
---

# 正文
"""

REUPLOADED = """---
kind: video-published
status: published
title: "你用AI没花钱，那国内这条AI产业链的钱是谁出的"
created_at: 2026-10-25
duration_s: 350
platform: 抖音
douyin_account: 解说号
douyin_status: published
douyin_url: "https://www.douyin.com/video/7700000000000000001"
---

# 正文
"""


class PlatformSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        video_dir = self.root / "40-发布/50-口播视频"
        video_dir.mkdir(parents=True)
        (video_dir / "2026-10-25-买卡.md").write_text(LINKED, encoding="utf-8")
        (video_dir / "2026-10-26-企业上AI.md").write_text(UNLINKED, encoding="utf-8")
        (video_dir / "2026-10-25-产业链.md").write_text(REUPLOADED, encoding="utf-8")
        self.payload = {
            "platform": "douyin",
            "account": "解说号",
            "fetched_at": "2026-10-27T10:30:00-07:00",
            "complete": True,
            "items": [
                {"id": "7700887182759694947", "title": "马斯克凭什么能借400亿买卡",
                 "published_at": "2026-10-26T01:15:20-07:00", "duration_s": 195.1,
                 "views": 2015, "status": "published"},
                {"id": "7701013019997270966", "title": "为什么企业上AI，前半年几乎看不到效果",
                 "published_at": "2026-10-26T09:23:38-07:00", "duration_s": 128.4,
                 "views": 1579, "status": "published"},
            ],
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_sync(self, *flags: str) -> str:
        source = self.root / "payload.json"
        source.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--input", str(source), *flags],
            capture_output=True, text=True, env={"RAYS_BRAIN": str(self.root), "PATH": "/usr/bin"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def note(self, name: str) -> str:
        return (self.root / "40-发布/50-口播视频" / name).read_text(encoding="utf-8")

    def test_preview_writes_nothing(self) -> None:
        report = self.run_sync()
        self.assertIn("已有链接、只更新数据：1", report)
        self.assertIn("按时长和标题新对上：1", report)
        self.assertEqual(self.note("2026-10-25-买卡.md"), LINKED)

    def test_linked_note_gets_account_and_views(self) -> None:
        self.run_sync("--write")
        text = self.note("2026-10-25-买卡.md")
        self.assertIn("douyin_account: 解说号", text)
        self.assertIn("douyin_views: 2015", text)
        self.assertIn('stats_synced_at: "2026-10-27T10:30:00-07:00"', text)

    def test_proposed_match_needs_explicit_accept(self) -> None:
        self.run_sync("--write")
        self.assertNotIn("7701013019997270966", self.note("2026-10-26-企业上AI.md"))
        self.run_sync("--write", "--accept-proposed")
        text = self.note("2026-10-26-企业上AI.md")
        self.assertIn('douyin_url: "https://www.douyin.com/video/7701013019997270966"', text)
        self.assertIn("douyin_status: published", text)
        self.assertIn("platform: 小红书；抖音", text)

    def test_reports_link_missing_from_complete_list(self) -> None:
        report = self.run_sync()
        self.assertIn("后台已经找不到", report)
        self.assertIn("7700000000000000001", report)
        # 记过的地址不会被另一条作品覆盖
        self.run_sync("--write", "--accept-proposed")
        self.assertIn("7700000000000000001", self.note("2026-10-25-产业链.md"))

    def test_douyin_requires_account(self) -> None:
        self.payload.pop("account")
        source = self.root / "payload.json"
        source.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--input", str(source)],
            capture_output=True, text=True, env={"RAYS_BRAIN": str(self.root), "PATH": "/usr/bin"},
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
