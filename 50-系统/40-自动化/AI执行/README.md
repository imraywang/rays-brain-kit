---
kind: runbook
updated_at: 2026-07-28
---

# AI 执行

工作台「让 Codex 起草这篇」背后的执行器。把知识工作台从看板升级为发起操作的地方：选好选题、立项成写作任务后，一键让 Codex 读材料起草，写好的初稿自动回到 vault 的草稿列。

## 安全边界

- **Codex 永远不直接写 vault。** 它在 `--sandbox read-only` 里读成稿包、候选、调研和源头材料，产出结构化 JSON；草稿落盘、任务状态回写、AI 任务队列勾销都由 `draft_worker.py` 完成，全程留操作日志。
- **产出是待审初稿，不是定稿。** 草稿 `status: ai-draft`、`generated_by: codex-<model>`，正文末尾附「待确认」清单。事实复核、语气把关、定稿仍是人（或 /ray 的 ray-writer 链路）的事。
- **表达先行写进提示词**：以 Ray 口吻把观点说满，正文不自保；缺经历或数据可以按逻辑编补，但每一处都必须记进 open_questions（落盘为「待确认」）由 Ray 决定。见 [draft-prompt.md](draft-prompt.md)，可直接编辑调教。
- 单并发：同一时间只允许一篇在起草（工作台端点防重入）。

## 组成

- `draft_worker.py`：执行器。收集上下文 → `codex exec`（复用知识采集的 `config.json` 里的 `codex_path` / `codex_model`）→ 落盘草稿 → 回写任务（status → drafted、补 draft 链接）→ 勾掉 AI 任务队列对应行。
- `draft-prompt.md`：起草提示词模板（任务纪律 + 输出要求）。
- `draft.schema.json`：Codex 输出契约（title / summary / body_markdown / facts_used / open_questions）。

## 用法

工作台：写作任务的阅读面板 →「让 Codex 起草这篇」。进行中状态在管线面板可见；完成后草稿列自动出现新卡并推系统通知。

终端手动跑一篇：

```bash
python3 draft_worker.py "10-创作/20-写作任务/<任务文件>.md"
```

日志在状态目录 `logs/ai-draft.log`。

## 与 AI 任务队列的关系

两条路都通向同一张队列表：「排入 AI 起草队列」留给 /ray 会话深度处理（完整 ray-writer 链路）；「让 Codex 起草这篇」是快速初稿。worker 完成时如果队列里有这篇的待处理行，会顺手勾掉并补产物链接，避免重复认领。
