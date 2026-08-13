from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("map_digest.py")
SPEC = importlib.util.spec_from_file_location("map_digest_test", MODULE_PATH)
assert SPEC and SPEC.loader
digest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(digest)


class MapDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name)
        self.state_file = self.vault / ".state/map-digest.json"
        self.topic_state_file = self.vault / ".state/topic-map-candidates.json"
        self.dashboard = self.vault / digest.WORKBENCH_REL
        self.weekly_dir = self.vault / digest.WEEKLY_DIR_REL
        self.now = datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc)
        self.seed()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, relative: str | Path, text: str) -> Path:
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def knowledge(
        self,
        title: str,
        *,
        created_at: str = "2026-06-01",
        topics: tuple[str, ...] = (),
        links: tuple[str, ...] = (),
    ) -> None:
        topic_lines = "\n".join(f"  - {value}" for value in topics)
        topic_block = f"topics:\n{topic_lines}\n" if topic_lines else ""
        link_lines = "\n".join(f"- [[{value}]]" for value in links)
        self.write(
            f"20-知识/40-观点/{title}.md",
            f"""---
kind: viewpoint
status: seed
created_at: {created_at}
source_file: "30-资料/{title}.md"
{topic_block}---

# {title}

这是可重复使用的长期判断。

## 知识连接

{link_lines}
""",
        )

    def seed(self) -> None:
        for title in (
            "转入子地图",
            "按主题转入",
            "升级核心",
            "归入补充",
            "等待新地图",
            "本周新增",
        ):
            self.knowledge(
                title,
                created_at="2026-07-27" if title == "本周新增" else "2026-06-01",
                topics=("子地图",) if title == "按主题转入" else (),
            )
        self.knowledge("引用一", links=("升级核心",))
        self.knowledge("引用二", links=("升级核心",))
        self.write(
            digest.MAP_DIR_REL / "父地图.md",
            """---
kind: map
map_role: theme
status: evergreen
updated_at: 2026-07-01
---

# 主题地图：父地图

## 核心知识

- [[原有核心]]

## 相关主题

- [[同级地图]]

## 新增知识

- [[转入子地图]]
- [[按主题转入]]
- [[升级核心]]
- [[归入补充]]
- [[等待新地图]]
- [[本周新增]]
""",
        )
        self.write(
            digest.MAP_DIR_REL / "子地图.md",
            """---
kind: map
map_role: theme
status: seed
origin: topic-map-candidate
updated_at: 2026-07-01
---

# 主题地图：子地图

## 起始知识

- [[转入子地图]]

## 相关主题

- [[父地图]]

## 新增知识
""",
        )
        self.topic_state_file.parent.mkdir(parents=True, exist_ok=True)
        self.topic_state_file.write_text(
            json.dumps(
                {
                    "candidates": [
                        {
                            "name": "新方向",
                            "supporting_notes": [
                                {
                                    "title": "等待新地图",
                                    "path": "20-知识/40-观点/等待新地图.md",
                                }
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def refresh(self) -> dict:
        return digest.refresh_weekly_map_digest(
            self.vault,
            self.state_file,
            self.dashboard,
            self.weekly_dir,
            self.topic_state_file,
            now=self.now,
        )

    def check_action(self, action: str) -> None:
        label = digest.ACTION_LABELS[action]
        text = self.dashboard.read_text(encoding="utf-8")
        self.dashboard.write_text(
            text.replace(f"- [ ] {label}", f"- [x] {label}", 1),
            encoding="utf-8",
        )

    def test_analysis_classifies_every_staged_note_once(self) -> None:
        result = self.refresh()
        summary = result["summary"]

        self.assertEqual(summary["new_knowledge_count"], 6)
        self.assertEqual(summary["route_child_count"], 2)
        self.assertEqual(summary["promote_core_count"], 1)
        self.assertEqual(summary["supplement_count"], 1)
        self.assertEqual(summary["new_map_candidate_count"], 1)
        self.assertEqual(summary["observe_count"], 1)
        self.assertEqual(
            sum(
                summary[key]
                for key in (
                    "route_child_count",
                    "promote_core_count",
                    "supplement_count",
                    "new_map_candidate_count",
                    "observe_count",
                    "unresolved_count",
                )
            ),
            summary["new_knowledge_count"],
        )
        self.assertEqual(summary["auto_selected_count"], 1)
        self.assertEqual(summary["human_decision_count"], 3)

    def test_weekly_refresh_writes_actionable_dashboard_and_report_without_moving(self) -> None:
        before = (self.vault / digest.MAP_DIR_REL / "父地图.md").read_text(
            encoding="utf-8"
        )

        self.refresh()

        self.assertEqual(
            (self.vault / digest.MAP_DIR_REL / "父地图.md").read_text(
                encoding="utf-8"
            ),
            before,
        )
        dashboard = self.dashboard.read_text(encoding="utf-8")
        self.assertIn("未勾选的内容保持原样", dashboard)
        self.assertIn("- [x] 确认转入已有子地图", dashboard)
        self.assertIn("- [ ] 确认转入已有子地图", dashboard)
        self.assertIn("高置信度，已自动勾选", dashboard)
        self.assertTrue((self.weekly_dir / "2026-W31.md").exists())
        self.assertTrue(
            (self.weekly_dir / digest.LATEST_REPORT_NAME).exists()
        )

    def test_checked_child_route_is_applied_once_and_keeps_target_relation(self) -> None:
        self.refresh()

        first = digest.process_map_digest_actions(
            self.vault,
            self.state_file,
            self.dashboard,
            self.weekly_dir,
            self.topic_state_file,
            now=self.now,
        )
        second = digest.process_map_digest_actions(
            self.vault,
            self.state_file,
            self.dashboard,
            self.weekly_dir,
            self.topic_state_file,
            now=self.now,
        )

        parent = (self.vault / digest.MAP_DIR_REL / "父地图.md").read_text(
            encoding="utf-8"
        )
        child = (self.vault / digest.MAP_DIR_REL / "子地图.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(first["applied"], 1)
        self.assertEqual(second["applied"], 0)
        self.assertNotIn("[[转入子地图]]", digest.section_text(parent, "新增知识"))
        self.assertIn("[[子地图]]", digest.section_text(parent, "相关主题"))
        self.assertIn("[[转入子地图]]", child)
        self.assertEqual(child.count("[[转入子地图]]"), 1)
        self.assertIn("[[按主题转入]]", digest.section_text(parent, "新增知识"))

    def test_checked_core_and_supplement_moves_preserve_both_relations(self) -> None:
        self.refresh()
        self.check_action("promote-core")
        self.check_action("supplement")

        result = digest.process_map_digest_actions(
            self.vault,
            self.state_file,
            self.dashboard,
            self.weekly_dir,
            self.topic_state_file,
            now=self.now,
        )

        parent = (self.vault / digest.MAP_DIR_REL / "父地图.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(result["applied"], 3)
        self.assertIn("[[升级核心]]", digest.section_text(parent, "核心知识"))
        self.assertIn("[[归入补充]]", digest.section_text(parent, "补充知识"))
        self.assertNotIn("[[升级核心]]", digest.section_text(parent, "新增知识"))
        self.assertNotIn("[[归入补充]]", digest.section_text(parent, "新增知识"))

    def test_second_refresh_consumes_auto_selected_action_without_duplicates(self) -> None:
        first = self.refresh()
        second = self.refresh()

        self.assertEqual(first["applied"], 0)
        self.assertEqual(second["applied"], 1)
        self.assertEqual(
            (self.vault / digest.MAP_DIR_REL / "父地图.md")
            .read_text(encoding="utf-8")
            .count("[[转入子地图]]"),
            0,
        )


if __name__ == "__main__":
    unittest.main()
