from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

WORKER_PATH = Path(__file__).with_name("draft_worker.py")
SPEC = importlib.util.spec_from_file_location("draft_worker", WORKER_PATH)
worker = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(worker)


class DraftWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name)
        self.tasks = self.vault / "10-创作/20-写作任务"
        self.tasks.mkdir(parents=True)
        self.task = self.tasks / "任务.md"
        self.task.write_text(
            "---\nkind: content-pack\nstatus: active\nselected_angle: 一个角度\n"
            "topic_id: topic-1\ncontent_id: article-1\n"
            "candidate: \"[[10-创作/10-灵感/20-候选选题/候选]]\"\n---\n\n# 成稿包\n\n任务书正文。\n",
            encoding="utf-8",
        )
        candidate_dir = self.vault / "10-创作/10-灵感/20-候选选题"
        candidate_dir.mkdir(parents=True)
        (candidate_dir / "候选.md").write_text(
            "---\nkind: topic-candidate\n---\n\n# 候选\n\n候选材料内容。\n", encoding="utf-8"
        )
        self.patchers = [
            mock.patch.object(worker, "VAULT", self.vault),
            mock.patch.object(worker, "DRAFTS_DIR", self.vault / "10-创作/30-文章草稿"),
            mock.patch.object(
                worker, "QUEUE_FILE", self.vault / "50-系统/40-自动化/AI任务队列.md"
            ),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def test_gather_context_includes_task_and_linked_materials(self) -> None:
        meta, materials = worker.gather_context(self.task)
        self.assertEqual(meta["selected_angle"], "一个角度")
        self.assertIn("成稿包（本次写作的唯一任务书）", materials)
        self.assertIn("候选材料内容", materials)

    def test_write_draft_and_task_backfill_and_queue_tick(self) -> None:
        queue = self.vault / "50-系统/40-自动化/AI任务队列.md"
        queue.parent.mkdir(parents=True, exist_ok=True)
        queue.write_text(
            "# AI 任务队列\n\n## 待处理\n- [ ] 2026-07-28 10:00 · 起草 · [[10-创作/20-写作任务/任务]]\n",
            encoding="utf-8",
        )
        meta, _ = worker.gather_context(self.task)
        result = {
            "title": "一篇测试草稿",
            "summary": "两句话摘要。",
            "body_markdown": "开头三句话。\n\n## 小标题\n\n正文段落。",
            "open_questions": ["缺一个亲历案例"],
        }
        draft = worker.write_draft(self.task, meta, result, "gpt-5.5")
        text = draft.read_text(encoding="utf-8")
        self.assertIn("kind: draft", text)
        self.assertIn("status: ai-draft", text)
        self.assertIn("generated_by: codex-gpt-5.5", text)
        self.assertIn("content_pack:", text)
        self.assertIn("# 一篇测试草稿", text)
        self.assertIn("## 待人工确认", text)
        self.assertIn("- [ ] 缺一个亲历案例", text)

        worker.update_task(self.task, draft)
        task_text = self.task.read_text(encoding="utf-8")
        self.assertIn("status: drafted", task_text)
        self.assertIn("draft:", task_text)

        self.assertTrue(worker.tick_queue(self.task, draft))
        queue_text = queue.read_text(encoding="utf-8")
        self.assertIn("- [x] 2026-07-28 10:00 · 起草", queue_text)
        self.assertIn("→ [[10-创作/30-文章草稿/", queue_text)
        # 再勾一次应无未勾选项可勾
        self.assertFalse(worker.tick_queue(self.task, draft))

    def test_write_draft_requires_body(self) -> None:
        meta, _ = worker.gather_context(self.task)
        with self.assertRaises(RuntimeError):
            worker.write_draft(self.task, meta, {"title": "空", "body_markdown": ""}, "m")

    def test_unique_path_avoids_overwrite(self) -> None:
        drafts = self.vault / "10-创作/30-文章草稿"
        drafts.mkdir(parents=True)
        (drafts / "a.md").write_text("x", encoding="utf-8")
        self.assertEqual(worker.unique_path(drafts, "a.md").name, "a-2.md")


if __name__ == "__main__":
    unittest.main()
