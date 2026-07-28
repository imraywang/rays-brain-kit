---
kind: runbook
updated_at: 2026-07-23
---

# 内容快照

每天 18:05 为文字内容建立一次本地版本快照。它不是远端备份，也不覆盖 `60-素材/` 中被 Git 忽略的媒体文件。

## 安全边界

- 只处理 `00-入口`、`10-创作`、`20-知识`、`30-资料`、`40-发布`、`60-素材`、`70-思考`、`90-归档`、`HOME.md` 和 `README.md`。
- `50-系统`、`.obsidian` 和其他配置不会被自动加入。
- 发现已有人工暂存内容时，本次直接跳过。
- 同一时间只允许一个快照运行；失败后会撤回本次暂存，不会碰原文件。
- 文件名按原始文字核对，中文目录和中文文件名不会被误判为白名单外文件。
- 运行前尝试同步微信读书。同步失败会记录日志，但不阻止保存当时已有的内容变化。

## 人工验收

只检查环境，不同步、不暂存、不提交：

`python3 content_snapshot.py --check`

实际运行：

`python3 content_snapshot.py`

运行日志在 `~/Library/Logs/rays-brain/content-snapshot.log`，最近一次成功结果在 `~/.local/state/rays-brain/content-snapshot-success.json`。

定时任务由 `~/Library/LaunchAgents/com.ray.content-snapshot.plist` 启动。健康日报同时检查它是否已加载、上次是否失败。
