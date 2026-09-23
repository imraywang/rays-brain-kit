"""在临时目录里搭一个够真实的小知识库，给 record_published.py 做端到端测试。

字段名和链接形状都照抄真实库：正式稿用 source 指写作任务，写作任务用 capture_id
和无引号的 source_file 指回 X 书签，审核卡是另一条并行的路。
"""
from __future__ import annotations

from pathlib import Path

TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "30-模板"

# 主链：正式稿 → 活跃母稿 → 写作任务 → X 书签。发布后母稿和任务都应退出活跃区。
ARTICLE = """---
kind: article-published
status: published
title: 测试长文
created_at: 2026-07-19
platform: X Article，已同步微信公众号
topic: "[[10-创作/10-灵感/20-候选选题/2026-07-19-测试候选]]"
content_pack: "[[10-创作/20-写作任务/2026-07-18-测试写作任务]]"
draft_source: "[[10-创作/30-文章草稿/2026-07-19-测试长文母稿]]"
x_article_status: draft
x_article_editor_url: "https://x.com/compose/articles/edit/2079080041305276416"
wechat_status: draft
wechat_metadata_status: pending
wechat_draft_media_id: "MEDIA-ID"
---

# 测试长文

正文。
"""

ARTICLE_DRAFT = """---
kind: article-draft
status: published
content_pack: "[[10-创作/20-写作任务/2026-07-18-测试写作任务]]"
published_file: "[[40-发布/10-X长文/2026-07-19-测试长文]]"
---

# 测试长文母稿

![配图](../../60-素材/配图.png)
"""

CONTENT_PACK = """---
kind: content-pack
status: published
capture_id: "x:2050000000000000002"
source_file: "30-资料/30-X书签/2026-07/2026-07-18-测试书签.md"
source_review: "90-归档/20-审核记录/剪藏复核/2026-07/测试审核卡-0e3e62d4.md"
candidate: "[[10-创作/10-灵感/20-候选选题/2026-07-19-测试候选]]"
---

# 测试写作任务
"""

REVIEW_CARD = """---
kind: capture-review
capture_id: "x:2050000000000000002"
source_file: "30-资料/30-X书签/2026-07/2026-07-18-测试书签.md"
status: 已进入候选
review_action: writing
---

# 测试审核卡
"""

BOOKMARK = """---
title: "测试书签"
source_url: "https://x.com/tester/status/2050000000000000002"
created_at: "2026-07-18T01:02:03-07:00"
tags:
  - x-bookmark
capture_source: x-bookmark
capture_id: "x:2050000000000000002"
processing_status: unprocessed
capture_version: 2
capture_status: complete
attachment_count: 0
tweet_id: "2050000000000000002"
---

# 测试书签

正文。
"""

TOPIC = """---
kind: topic-candidate
status: in-progress
---

# 测试候选
"""

# 旁链：只有网页剪藏来源的稿子，回写时应该干净地报"找不到书签"。
WEB_ARTICLE = """---
kind: article-published
status: published
title: 网页来源长文
platform: X Article
source_review: "[[90-归档/20-审核记录/剪藏复核/2026-07/网页审核卡-c4eef71e]]"
x_article_status: draft
---

# 网页来源长文
"""

WEB_REVIEW = """---
kind: capture-review
capture_id: "web:98f0a1"
source_file: "30-资料/40-网页剪藏/2026-07-18-某个视频.md"
status: 已沉淀
---

# 网页审核卡
"""

WEB_CLIP = """---
title: "某个视频"
capture_source: web-clip
processing_status: unprocessed
---

# 某个视频
"""

# 口播视频：一条稿子一次发三个平台，来源是微信读书而不是 X 书签，
# 所以它同时也是"找不到源头书签时要干净地说出来"的第二条样本。
VIDEO_SCRIPT = """---
kind: video-published
status: published
form: 口播短视频逐字稿
series: 测试系列
episode: 1
platform: "视频号；抖音；小红书"
topic: "[[10-创作/10-灵感/20-候选选题/2026-07-19-测试候选]]"
oral_script: "[[10-创作/25-口播草稿/2026-07-24-测试口播母稿]]"
shipinhao_status: draft
douyin_status: draft
xiaohongshu_status: draft
---

# 测试口播稿

逐字稿正文。
"""

ORAL_DRAFT = """---
kind: oral-script
status: published
published_file: "[[40-发布/50-口播视频/2026-07-24-测试口播]]"
---

# 测试口播母稿

逐字稿正文。
"""

FILES = {
    "article": ("40-发布/10-X长文/2026-07-19-测试长文.md", ARTICLE),
    "draft": ("10-创作/30-文章草稿/2026-07-19-测试长文母稿.md", ARTICLE_DRAFT),
    "video": ("40-发布/50-口播视频/2026-07-24-测试口播.md", VIDEO_SCRIPT),
    "oral_draft": ("10-创作/25-口播草稿/2026-07-24-测试口播母稿.md", ORAL_DRAFT),
    "pack": ("10-创作/20-写作任务/2026-07-18-测试写作任务.md", CONTENT_PACK),
    "review": ("90-归档/20-审核记录/剪藏复核/2026-07/测试审核卡-0e3e62d4.md", REVIEW_CARD),
    "bookmark": ("30-资料/30-X书签/2026-07/2026-07-18-测试书签.md", BOOKMARK),
    "topic": ("10-创作/10-灵感/20-候选选题/2026-07-19-测试候选.md", TOPIC),
    "web_article": ("40-发布/10-X长文/2026-07-21-网页来源长文.md", WEB_ARTICLE),
    "web_review": ("90-归档/20-审核记录/剪藏复核/2026-07/网页审核卡-c4eef71e.md", WEB_REVIEW),
    "web_clip": ("30-资料/40-网页剪藏/2026-07-18-某个视频.md", WEB_CLIP),
}


def build(root: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for key, (where, body) in FILES.items():
        path = root / where
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        paths[key] = path
    image = root / "60-素材/配图.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"test-image")
    (root / "40-发布/00-内容反馈").mkdir(parents=True, exist_ok=True)
    for name in ("内容反馈.md", "口播视频反馈.md"):
        target = root / "50-系统/30-模板" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((TEMPLATE_ROOT / name).read_text(encoding="utf-8"), encoding="utf-8")
    return paths
