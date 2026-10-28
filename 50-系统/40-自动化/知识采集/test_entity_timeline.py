from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import entity_timeline as timeline  # noqa: E402

PROFILE = """---
kind: entity
status: seed
entity_type: "公司"
updated_at: "2026-10-01T00:00:00-07:00"
aliases: ["长鑫存储", "CXMT"]
---

# 长鑫

## 关注主题

内存。

## 相关对象

- [[20-知识/20-人物与组织/SK 海力士]]
"""


class EntityTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name)
        self.profile = self.vault / "20-知识/20-人物与组织/长鑫.md"
        self.profile.parent.mkdir(parents=True)
        self.profile.write_text(PROFILE, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(HERE / "entity_timeline.py"), "--vault", str(self.vault), *args],
            capture_output=True, text=True,
        )

    def test_finds_profile_by_alias(self) -> None:
        self.assertEqual(timeline.find_entity(self.vault, "cxmt"), self.profile)
        with self.assertRaises(SystemExit):
            timeline.find_entity(self.vault, "长江存储")

    def test_preview_then_write_inserts_section_before_related(self) -> None:
        args = ["--entity", "长鑫存储", "--date", "2026-07-27", "--fact", "科创板上市，首日收 49 元。",
                "--source", "[[30-资料/10-自主调研/产业链]]"]
        self.assertEqual(self.run_cli(*args).returncode, 0)
        self.assertEqual(self.profile.read_text(encoding="utf-8"), PROFILE)
        self.assertEqual(self.run_cli(*args, "--write").returncode, 0)
        text = self.profile.read_text(encoding="utf-8")
        self.assertIn("## 时间线\n\n- 2026-07-27 科创板上市，首日收 49 元（来源：[[30-资料/10-自主调研/产业链]]）\n\n## 相关对象", text)
        self.assertNotIn('updated_at: "2026-10-01', text)

    def test_batch_sorts_newest_first_and_skips_duplicates(self) -> None:
        facts = [
            {"entity": "长鑫", "date": "2026-05-01", "fact": "递交招股书", "source": "https://a"},
            {"entity": "长鑫", "date": "2026-07-27", "fact": "上市", "source": "https://b"},
        ]
        source = self.vault / "facts.json"
        source.write_text(json.dumps(facts, ensure_ascii=False), encoding="utf-8")
        self.run_cli("--from", str(source), "--write")
        result = self.run_cli("--from", str(source), "--write")
        self.assertIn("2 条已记过", result.stdout)
        text = self.profile.read_text(encoding="utf-8")
        self.assertLess(text.index("2026-07-27"), text.index("2026-05-01"))
        self.assertEqual(text.count("2026-07-27 上市"), 1)

    def test_create_builds_profile_through_ingest(self) -> None:
        args = ["--entity", "中际旭创", "--date", "2026-03-30", "--fact", "2025 收入 382.4 亿元，海外占 90.58%",
                "--source", "https://example.com/report", "--create", "公司"]
        preview = self.run_cli(*args)
        self.assertIn("将新建对象档案：中际旭创", preview.stdout)
        self.assertFalse((self.vault / "20-知识/20-人物与组织/中际旭创.md").exists())
        result = self.run_cli(*args, "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = (self.vault / "20-知识/20-人物与组织/中际旭创.md").read_text(encoding="utf-8")
        self.assertIn("kind: entity", text)
        self.assertIn("entity_id: entity-", text)
        self.assertIn("- 2026-03-30 2025 收入 382.4 亿元，海外占 90.58%（来源：https://example.com/report）", text)

    def test_rejects_bad_date_or_missing_source(self) -> None:
        self.assertNotEqual(self.run_cli("--entity", "长鑫", "--date", "7月27日", "--fact", "x", "--source", "y").returncode, 0)
        self.assertNotEqual(self.run_cli("--entity", "长鑫", "--date", "2026-07-27", "--fact", "x").returncode, 0)


if __name__ == "__main__":
    unittest.main()
