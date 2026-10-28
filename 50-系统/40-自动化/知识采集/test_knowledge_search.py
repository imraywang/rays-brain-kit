from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import knowledge_search as search  # noqa: E402


def note(kind: str, title: str, body: str, extra: str = "") -> str:
    return f"---\nkind: {kind}\nstatus: seed\n{extra}---\n\n# {title}\n\n{body}\n"


class KnowledgeSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name)
        self.write("20-知识/10-概念/技术扩散的中介窗口.md", note(
            "concept", "技术扩散的中介窗口",
            "- Paul David：工厂电气化后生产率长期不涨，因为只是把蒸汽机换成电机。\n"
            "- 技术可用不等于生产率提升，中间隔着组织重构，企业 AI 落地也是这样。",
            'source_url: "https://www.jstor.org/stable/2006600"\n',
        ))
        self.write("20-知识/20-人物与组织/Lex Fridman.md", note(
            "entity", "Lex Fridman", "MIT 研究自动驾驶出身。",
            'aliases: ["莱克斯·弗里德曼", "Lex Fridman"]\n',
        ))
        for index in range(12):
            self.write(f"20-知识/40-观点/代码笔记{index}.md", note(
                "viewpoint", f"写代码的第 {index} 条观点", "代码只是工具，重要的是建模和验证。",
            ))
        self.write("20-知识/70-知识地图/AI 商业结构.md", note(
            "map", "AI 商业结构", "电气化 生产率 企业 AI 落地 技术扩散",
        ))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, relative: str, text: str) -> None:
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def titles(self, *terms: str) -> list[str]:
        results, _ = search.search(self.vault, list(terms))
        return [item["title"] for item in results]

    def test_finds_mechanism_note_from_topic_wording(self) -> None:
        titles = self.titles("企业上AI", "前半年看不到效果", "电气化", "生产率")
        self.assertEqual(titles[0], "技术扩散的中介窗口")

    def test_common_words_alone_are_not_relevant(self) -> None:
        self.assertEqual(self.titles("宝可梦", "反编译", "代码"), [])

    def test_exact_title_or_alias_ranks_first(self) -> None:
        self.assertEqual(self.titles("莱克斯·弗里德曼", "播客", "访谈")[0], "Lex Fridman")

    def test_maps_and_readme_are_skipped(self) -> None:
        self.write("20-知识/README.md", "电气化 生产率")
        results, total = search.search(self.vault, ["电气化", "生产率"])
        self.assertEqual(total, 14)
        self.assertNotIn("AI 商业结构", [item["title"] for item in results])

    def test_markdown_groups_sources_and_says_no_match(self) -> None:
        self.write("30-资料/10-自主调研/2026-10-25-产业链/调研.md", note(
            "research", "国内 AI 产业链", "光模块：中际旭创 2026-10-23 收盘市值……", 'created_at: "2026-10-25"\n'))
        self.write("40-发布/50-口播视频/2026-10-25-产业链.md", note(
            "video-published", "你用AI没花钱", "第四站，光模块。", 'publication_confirmed_at: "2026-10-25T21:06:00-07:00"\n'))
        self.write("40-发布/00-内容反馈/快照.md", note("data-snapshot", "快照", "光模块 播放量"))
        groups = search.search_all(self.vault, ["光模块"])
        text = search.markdown(["光模块"], groups, "2026-10-28")
        self.assertIn("### 知识库预检（2026-10-28）", text)
        self.assertIn("**长期知识**", text)
        self.assertIn("- 无相关", text)  # 长期知识里没有
        self.assertIn("[[30-资料/10-自主调研/2026-10-25-产业链/调研|国内 AI 产业链]]（2026-10-25（3 天前）", text)
        self.assertIn("[[40-发布/50-口播视频/2026-10-25-产业链|你用AI没花钱]]", text)
        self.assertNotIn("快照", text)

    def test_knowledge_hit_shows_source_and_timeline(self) -> None:
        self.write("20-知识/20-人物与组织/长鑫.md", note(
            "entity", "长鑫", "做内存。\n\n## 时间线\n\n- 2026-07-27 科创板上市（来源：x）\n- 2026-05-01 递交招股书（来源：y）\n"))
        groups = search.search_all(self.vault, ["电气化", "长鑫"])
        text = search.markdown(["电气化", "长鑫"], groups, "2026-10-28")
        self.assertIn("有一手出处", text)
        self.assertIn("时间线 2 条，最新 2026-07-27", text)

if __name__ == "__main__":
    unittest.main()
