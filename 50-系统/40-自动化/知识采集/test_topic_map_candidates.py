from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("topic_map_candidates.py")
SPEC = importlib.util.spec_from_file_location("topic_map_candidates_test", MODULE_PATH)
assert SPEC and SPEC.loader
topic_maps = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(topic_maps)


class TopicMapCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name)
        self.state_file = self.vault / ".state/topic-map-candidates.json"
        self.candidate_note = self.vault / topic_maps.CANDIDATE_NOTE_REL
        self.now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, relative: str | Path, text: str) -> Path:
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def seed_topic(self, topic: str = "上下文工程", count: int = 4) -> None:
        for index in range(count):
            review_rel = (
                "10-创作/10-灵感/10-待评估/剪藏复核/"
                f"来源-{index}.md"
            )
            self.write(
                review_rel,
                f"""---
kind: capture-review
status: 待审核
topics:
  - {topic}
---

# 来源 {index}
""",
            )
            self.write(
                f"20-知识/10-概念/{topic}判断-{index}.md",
                f"""---
kind: concept
status: seed
source_review: "{review_rel}"
source_file: "30-资料/来源-{index}.md"
---

# {topic}判断 {index}

{topic}需要把输入边界、检索和压缩放在同一个系统里。
""",
            )

    def seed_map_index(self) -> Path:
        return self.write(
            topic_maps.MAP_INDEX_REL,
            """---
kind: map
map_role: directory
status: evergreen
updated_at: 2026-07-01
---

# 知识地图索引

## 现有地图
""",
        )

    def test_object_index_does_not_count_as_theme_coverage(self) -> None:
        self.write(
            "20-知识/10-概念/只有对象索引.md",
            "---\nkind: concept\nstatus: seed\n---\n\n# 只有对象索引\n",
        )
        self.write(
            "20-知识/10-概念/已有主题.md",
            "---\nkind: concept\nstatus: seed\n---\n\n# 已有主题\n",
        )
        self.write(
            "20-知识/70-知识地图/人物与思想索引.md",
            """---
kind: map
map_role: object-index
status: evergreen
---

# 人物与思想索引

[[只有对象索引]]
""",
        )
        self.write(
            "20-知识/70-知识地图/真正主题.md",
            """---
kind: map
map_role: theme
status: evergreen
---

# 主题地图：真正主题

[[已有主题]]
""",
        )

        issues = topic_maps.theme_map_coverage_issues(self.vault)

        self.assertEqual(
            [item["file"] for item in issues],
            ["20-知识/10-概念/只有对象索引.md"],
        )

    def test_dots_inside_a_title_do_not_break_theme_coverage(self) -> None:
        self.write(
            "20-知识/60-方法/Kimi K2.5 的推理方法.md",
            "---\nkind: playbook\nstatus: seed\n---\n\n# Kimi K2.5 的推理方法\n",
        )
        self.write(
            "20-知识/60-方法/CLAUDE.md 应记录项目陷阱.md",
            "---\nkind: playbook\nstatus: seed\n---\n\n# CLAUDE.md 应记录项目陷阱\n",
        )
        self.write(
            "20-知识/70-知识地图/上下文工程.md",
            """---
kind: map
map_role: theme
status: evergreen
---

# 主题地图：上下文工程

- [[Kimi K2.5 的推理方法]]
- [[CLAUDE.md 应记录项目陷阱]]
""",
        )

        self.assertEqual(topic_maps.theme_map_coverage_issues(self.vault), [])

    def test_refresh_proposes_but_never_creates_a_map(self) -> None:
        self.seed_topic()
        self.seed_map_index()

        state = topic_maps.refresh_topic_map_candidates(
            self.vault,
            self.state_file,
            self.candidate_note,
            now=self.now,
        )

        self.assertEqual(len(state["candidates"]), 1)
        self.assertEqual(state["candidates"][0]["name"], "上下文工程")
        self.assertEqual(state["candidates"][0]["proposal"], "新建")
        self.assertFalse(
            (self.vault / topic_maps.MAP_DIR_REL / "上下文工程.md").exists()
        )
        note_text = self.candidate_note.read_text(encoding="utf-8")
        self.assertIn("系统只发现知识结构变化，不会自行创建", note_text)
        self.assertIn("- [ ] 建立这张主题地图", note_text)

    def test_human_approval_creates_one_theme_map_and_updates_index(self) -> None:
        self.seed_topic()
        index_path = self.seed_map_index()
        state = topic_maps.refresh_topic_map_candidates(
            self.vault,
            self.state_file,
            self.candidate_note,
            now=self.now,
        )
        candidate_id = state["candidates"][0]["id"]

        result = topic_maps.apply_topic_map_candidate_action(
            self.vault,
            self.state_file,
            candidate_id,
            "approve",
            self.candidate_note,
            now=self.now,
        )
        topic_maps.refresh_topic_map_candidates(
            self.vault,
            self.state_file,
            self.candidate_note,
            now=self.now,
        )

        map_path = self.vault / topic_maps.MAP_DIR_REL / "上下文工程.md"
        self.assertTrue(result["ok"])
        self.assertTrue(map_path.exists())
        map_text = map_path.read_text(encoding="utf-8")
        self.assertIn("map_role: theme", map_text)
        self.assertEqual(map_text.count("# 主题地图：上下文工程"), 1)
        self.assertEqual(
            index_path.read_text(encoding="utf-8").count("[[上下文工程]]"),
            1,
        )

    def test_new_theme_map_is_listed_before_navigation_indexes(self) -> None:
        index = """# 知识地图索引

## 现有地图

### 主题地图

- [[已有主题]]

### 导航与对象索引

- [[人物与思想索引]]

## 使用方法
"""

        updated = topic_maps.add_map_to_index(index, "新主题")

        self.assertLess(updated.index("[[新主题]]"), updated.index("### 导航与对象索引"))
        self.assertEqual(updated.count("[[新主题]]"), 1)

    def test_checked_watch_action_is_applied_on_next_refresh(self) -> None:
        self.seed_topic()
        self.seed_map_index()
        topic_maps.refresh_topic_map_candidates(
            self.vault,
            self.state_file,
            self.candidate_note,
            now=self.now,
        )
        text = self.candidate_note.read_text(encoding="utf-8")
        self.candidate_note.write_text(
            text.replace(
                "- [ ] 继续观察 14 天",
                "- [x] 继续观察 14 天",
                1,
            ),
            encoding="utf-8",
        )

        state = topic_maps.refresh_topic_map_candidates(
            self.vault,
            self.state_file,
            self.candidate_note,
            now=self.now,
        )

        self.assertEqual(state["candidates"], [])
        self.assertEqual(len(state["observing"]), 1)
        self.assertEqual(state["observing"][0]["next_review_at"], "2026-08-13")
        self.assertFalse(
            (self.vault / topic_maps.MAP_DIR_REL / "上下文工程.md").exists()
        )

    def test_broad_existing_language_is_not_recommended_as_a_new_map(self) -> None:
        self.seed_topic("AI Agent 与工作流")
        self.seed_map_index()

        state = topic_maps.refresh_topic_map_candidates(
            self.vault,
            self.state_file,
            self.candidate_note,
            now=self.now,
        )

        self.assertEqual(state["candidates"], [])
        self.assertEqual(state["observing"], [])


if __name__ == "__main__":
    unittest.main()
