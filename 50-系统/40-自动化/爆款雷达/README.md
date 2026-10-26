---
kind: guide
updated_at: 2026-10-26
---

# 爆款雷达

从 TikHub 拉抖音低粉爆款榜和小红书笔记搜索，筛出「粉丝少却爆了」的选题，在知识工作台的「📡 雷达」页里一键加入候选选题。

第一次用、想从头跑起来再看懂实现：先读 [新手指南](新手指南.md)。

## 用法

```
python3 50-系统/40-自动化/爆款雷达/radar.py fetch --dry-run   # 看本轮最多调用多少次、花多少钱
python3 50-系统/40-自动化/爆款雷达/radar.py fetch             # 拉一轮
python3 50-系统/40-自动化/爆款雷达/radar.py status            # 最近一轮结果
```

工作台雷达页右上角的「重新获取」按钮做的是同一件事：后台派生一个 `radar.py fetch` 进程，同一时间只跑一个，日志写在 `$RAYS_BRAIN_STATE/logs/radar-run.log`。

## 数据放在哪

全部在 `$RAYS_BRAIN_STATE/radar/`（默认 `~/.local/state/rays-brain/radar/`），**不进 vault**：

- `items.json`：按「平台:作品 id」去重后的素材，保留首次发现时间和最新数据。超过 `retention_days`（30 天）没再出现的条目自动清掉，已加入选题的除外。
- `feedback.json`：每条的处理状态（加入选题、不合适、已看），以及屏蔽的作者。
- `xhs_authors.json`：小红书作者粉丝数缓存，30 天内不重复查。
- `runs.jsonl`：每轮的调用次数、花费、过门槛条数、错误。
- `covers/`：封面缓存。工作台的 CSP 不允许外链图片，加上平台有防盗链，封面统一走本机 `/api/radar/cover` 代理，只允许 douyinpic.com 和 xhscdn.com 两个域名。

只有点「加入选题」时，才会在 `10-创作/10-灵感/20-候选选题/` 写一张 `kind: topic-candidate`、`origin: radar` 的候选卡，之后照常走候选 7 天到期、立项、ray-explainer 的流程。

## 配置（radar_config.json）

- 关键词、阈值按平台分开。抖音不带垂类 tag：探针发现加了 tag 反而又旧又弱。
- 判断是否过门槛：粉丝 ≤ `max_fans`、互动 ≥ `min_interactions`、爆款倍数（互动 ÷ 粉丝）≥ `min_multiplier`、发布 ≤ `max_age_days` 天。小红书的互动是点赞 + 收藏。
- `listicle_pattern`：只看标题，用来判断是不是清单类（必装、合集、N 个技巧之类）。
- `exclude_words`：标题或描述命中就直接隐藏（AI 短剧、漫剧这类噪音）。
- `max_calls`：单次运行的计费上限护栏，到了就停，已经拉到的数据照样保存。

默认配置每轮最多约 30 次调用，约 $0.16（抖音 $0.001 一次，小红书搜索和作者补查都是 $0.01 一次）。凭据和对标拆解共用 `$RAYS_BRAIN_STATE/tikhub.json`。

## 已知边界

- 抖音榜单的 `like_cnt` 可能是时间窗内的新增点赞，不是累计点赞（探针观察到的现象，还没确认）。
- 小红书搜索不带粉丝数，每轮最多只给 `enrich_limit` 位作者补查。补查不到的条目倍数显示「待定」。
- 首轮（10-26）拉回 689 条、过门槛 159 条，其中小红书只有 2 条。门槛还要按实际使用再调。
- 还没有配定时任务，目前靠手动跑或工作台按钮触发。

## 测试

```
python3 -m unittest 50-系统/40-自动化/爆款雷达/test_radar.py
```
