---
kind: runbook
updated_at: 2026-07-23
---

# 内容快照

每天 18:05 为文字内容建立一次本地版本快照。它不是远端备份，也不覆盖 `60-素材/` 中被 Git 忽略的媒体文件。

## 安全边界

- 只处理 `00-入口`、`10-创作`、`20-知识`、`30-资料`、`40-发布`、`70-思考`、`90-归档`、`HOME.md` 和 `README.md`。
- `60-素材` 不进入文字快照；其中被 Git 忽略的图片和视频需要单独备份。
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

## 独立恢复备份

本地版本快照不能代替另一块磁盘上的备份。Codex 自动化「Rays Brain 外置硬盘每日备份」（`~/.codex/automations/rays-brain/`）每天 02:30 运行一次；外接磁盘“协作”连接时也可以手动运行：

`python3 external_backup.py`

它会在 `/Volumes/协作/RaysBrain-Recovery/` 建立经过逐文件校验的版本：

- 包含当前全部知识库内容和 `60-素材/`；
- 另外保存文字历史记录和必要的运行状态；
- 排除各层级的 Obsidian 设置、`.claude/worktrees/` 临时工作树、程序缓存和对标视频中间文件；
- 默认保留最近 8 个版本，旧版本只会在新的备份完成并验证后清理；
- “协作”磁盘没有连接时会明确失败，不会退回电脑本地伪装成独立备份。
