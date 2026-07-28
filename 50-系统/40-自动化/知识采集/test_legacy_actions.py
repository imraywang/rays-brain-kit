from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("legacy_actions_under_test", HERE / "legacy_actions.py")
assert SPEC and SPEC.loader
legacy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(legacy)


class LegacyActionArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.vault = Path(self.tempdir.name).resolve()
        legacy.VAULT = self.vault
        legacy.REVIEW_DIR = self.vault / "10-创作/10-灵感/10-待评估/存量知识复核"
        legacy.REFERENCE_DIR = self.vault / "30-资料/20-参考资料/历史知识降级"
        legacy.ARCHIVE_DIR = self.vault / "90-归档/90-待清理/2026-07-23"
        legacy.BACKUP_DIR = self.vault / "90-归档/10-迁移记录"
        legacy.REVIEW_RECORD_DIR = self.vault / "90-归档/20-审核记录/存量知识复核"
        legacy.REVIEW_DIR.mkdir(parents=True)
        (self.vault / "20-知识/40-观点").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_processed_card_leaves_active_queue_and_keeps_traceable_link(self) -> None:
        source = self.vault / "20-知识/40-观点/示例.md"
        source.write_text(
            "---\nkind: view\nstatus: legacy\n---\n\n# 示例\n\n正文足够长。" + "内容" * 180,
            encoding="utf-8",
        )
        card = legacy.REVIEW_DIR / "示例复核.md"
        card.write_text(
            "---\n"
            "status: 待处理\n"
            'source_file: "20-知识/40-观点/示例.md"\n'
            "---\n\n"
            "# 示例复核\n\n"
            "- [x] 保留为长期知识\n"
            "- [ ] 合并到建议目标\n"
            "- [ ] 降级为参考资料\n"
            "- [ ] 标记为可恢复的待清理项\n",
            encoding="utf-8",
        )

        result = legacy.apply()

        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["archived"], 1)
        self.assertEqual(list(legacy.REVIEW_DIR.glob("*.md")), [])
        archived = list(legacy.REVIEW_RECORD_DIR.rglob("*.md"))
        self.assertEqual(len(archived), 1)
        source_text = source.read_text(encoding="utf-8")
        self.assertIn(archived[0].relative_to(self.vault).as_posix(), source_text)


if __name__ == "__main__":
    unittest.main()
