---
kind: runbook
updated_at: 2026-07-25
---

# 发布归档

公开发布完成之后，把结果写回知识库：正式稿的平台字段、发布反馈卡、源头书签的处理状态和去向。这是 [[50-系统/20-流程/个人知识库工作流|个人知识库工作流]] 第六节「发布与回流」里能自动完成的那几步。

## 安全边界

- **这个脚本不发布任何东西。** 它不调用微信或 X 的任何接口，不打开浏览器，不碰 `Publish` 按钮。它只在人已经在平台上完成公开发布之后运行，把已经发生的事记下来。公开发布始终是用户动作。
- 只改 `40-发布/10-X长文`、`40-发布/20-X短帖`、`40-发布/50-口播视频` 里的正式稿，以及这篇稿子牵出来的反馈卡和 X 书签。传进来的路径不在知识库内、或者不在正式稿目录下，直接报错退出。
- 不伪造平台数据。公开地址必须是真的公开地址：草稿编辑器地址 `https://x.com/compose/articles/edit/…` 会被明确拒绝，因为把它记成公开链接会让健康检查再也发现不了这篇其实没发出去。发布时间必须带时区，否则算不出正确的 24 小时到期时间。
- 同一篇稿子在同一个平台已经记过另一个公开地址时，报错停下，不覆盖。哪个是对的只有人知道。
- 反馈卡只填空，不覆盖。已经写完的 24 小时复盘不会被打回 `pending`，正文里手写的数字也不会被重建的模板冲掉。

## 用法

```
python3 record_published.py \
  --article "40-发布/10-X长文/2026-07-19-AI内容越多平台越需要回答这是谁说的.md" \
  --platform x \
  --url "https://x.com/wangray/status/2079091643693863273" \
  --published-at "2026-07-19T23:30:50-07:00"
```

`--platform` 取 `x`、`wechat`、`shipinhao`、`douyin`、`xiaohongshu`，每个平台各写各的字段，互不影响。同一篇稿子发几个平台就跑几次。加 `--dry-run` 只打印将要写什么，不落盘。

### 口播视频

`--article` 传的是 `40-发布/50-口播视频/` 里的**逐字稿**，不是视频文件；视频本身不进知识库。一条视频一次发三个平台，就跑三次：

```
python3 record_published.py \
  --article "40-发布/50-口播视频/2026-07-24-个人级FDE第1集-被优化的人的下一站.md" \
  --platform douyin \
  --url "https://www.douyin.com/video/7530000000000000000" \
  --published-at "2026-07-24T20:10:00+08:00"
```

反馈卡自动换成 [[50-系统/30-模板/口播视频反馈|口播视频反馈模板]]——图文那张卡没有完播率和平均播放时长，而视频要先回答的是"有没有被划走"，套错模板等于三个平台的主指标一个都不记。`public_url` 只装得下第一个跑的平台，所以每个平台的链接还各自留一份（`douyin_url`、`xiaohongshu_url`、`shipinhao_url`），后两次运行的链接不会丢。三个平台的后台数据都要人去看，每跑一个就立一条 `<平台>_data_status: pending`。

视频号常常根本给不出可分享的公开网页地址。这时 `--url` 填固定字面量 `无公开链接`——留空和"确实没有链接"在健康检查里必须是两件事，否则忘了填就再也发现不了。三个平台的创作者后台地址（`creator.douyin.com`、`creator.xiaohongshu.com`、`channels.weixin.qq.com/platform`）和 X 的草稿编辑器地址一样会被拒。

## 它写了什么

**正式稿**：按平台写 `x_article_status` / `x_article_url` / `x_article_published_at`，或 `wechat_status` / `wechat_public_url` / `wechat_published_at`。这三个名字就是健康日报「发布信息待补」读的字段，写错一个这篇就会一直红着。微信那边如果还挂着 `wechat_metadata_status: pending`，补齐后改成 `complete`。

**反馈卡**：`40-发布/00-内容反馈/` 下没有对应卡片就按 [[50-系统/30-模板/内容反馈|内容反馈模板]] 建一张，`due_at` 指向发布后 24 小时。已有卡片只补空字段。发的是公众号时立起 `wechat_data_status: pending`——后台数据要人去看。

**源头书签**：顺着正式稿找回它的 X 书签，标记 `processing_status: processed`，并记下 `published_file` 指向这篇稿。正式稿到书签之间隔着写作任务和审核卡，字段名还不统一（`source_note` 直接指书签，`source` 和 `content_pack` 指写作任务，`source_review` 指审核卡），所以脚本挨条试，最后用 `capture_id` 里的推文编号兜底——书签改名或换了月目录也还找得到。来源是网页剪藏、或者链子断了，就在报告里说「未找到」，不去改别的卡。

## 它没做什么

「发布与回流」剩下的几步要人判断，脚本每次运行都会把还欠的列出来：把可复用的观点写回 `20-知识/` 并更新 `knowledge_backfill_status`、标记已用角度并更新 `remaining_angles_status`、把候选选题改成 `partially-published` 还是 `closed`。跑完这条命令不等于归档全齐了。

## 测试

`python3 -m unittest test_record_published`

用 `fake_vault.py` 在临时目录里搭一个小知识库，字段名和链接形状照抄真实库，不碰真正的知识库。
