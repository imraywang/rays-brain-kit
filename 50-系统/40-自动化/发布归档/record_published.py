#!/usr/bin/env python3
"""记录一次已经发生的公开发布，并完成发布后归档。

这个脚本不发布任何东西。它不调用微信或 X 的任何接口，只在人已经在平台上完成
公开发布之后，把结果写回知识库：正式稿的平台字段、发布反馈卡、源头书签的处理
状态和去向。发布本身是用户动作，脚本只负责让归档不再靠手工惯例。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
VAULT = Path(os.environ.get("RAYS_BRAIN", str(HERE.parents[2]))).resolve()

VIDEO_DIR = "40-发布/50-口播视频"
PUBLISHED_DIRS = ("40-发布/10-X长文", "40-发布/20-X短帖", VIDEO_DIR)
FEEDBACK_DIR = "40-发布/00-内容反馈"
X_BOOKMARK_DIR = "30-资料/30-X书签"

FEEDBACK_24H_HOURS = 24
FEEDBACK_7D_DAYS = 7


class RecordError(RuntimeError):
    """所有可预期的失败都用它，main 只打印一行原因。"""


# ---------------------------------------------------------------- frontmatter

def split_frontmatter(text: str) -> tuple[list[str], str]:
    """返回（属性行, 正文）。正文含开头的 '\\n---\\n'，回写时原样拼回去。"""
    if not text.startswith("---\n"):
        raise RecordError("文件缺少页首属性")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise RecordError("文件页首属性不完整")
    return text[4:end].splitlines(), text[end:]


def parse_frontmatter(text: str) -> dict[str, str]:
    """与 knowledge_ingest.parse_frontmatter 同口径：只认顶层标量，带引号的走 JSON。"""
    result: dict[str, str] = {}
    if not text.startswith("---\n"):
        return result
    end = text.find("\n---\n", 4)
    if end < 0:
        return result
    for line in text[4:end].splitlines():
        if ":" not in line or line.startswith((" ", "\t", "-")):
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except Exception:
                pass
        result[key.strip()] = str(value)
    return result


# 知识库里状态和平台名一向不带引号（platform: X Article，已同步微信公众号），
# 而链接、时间戳这些带冒号或方括号的一向带引号。这条规则照抄现状，diff 才干净。
# 非 ASCII 一律算安全字符：中文和全角标点都不是 YAML 的指示符。
BARE_CHAR = re.compile(r"[\w.\- ]|[^\x00-\x7f]", re.UNICODE)


def render_value(value: str) -> str:
    """状态枚举保持裸值，链接和时间戳带冒号必须加引号——两边的读取方都认。"""
    if (
        value
        and value == value.strip()
        and not value.startswith("-")
        and all(BARE_CHAR.fullmatch(char) for char in value)
    ):
        return value
    return json.dumps(value, ensure_ascii=False)


def update_frontmatter(text: str, updates: dict[str, str]) -> str:
    """就地改写已有键，新键追加到属性块末尾；不动正文，也不重排其他行。"""
    lines, rest = split_frontmatter(text)
    positions: dict[str, int] = {}
    for index, line in enumerate(lines):
        if ":" not in line or line.startswith((" ", "\t", "-")):
            continue
        positions[line.split(":", 1)[0].strip()] = index
    for key, value in updates.items():
        rendered = f"{key}: {render_value(value)}"
        if key in positions:
            lines[positions[key]] = rendered
        else:
            positions[key] = len(lines)
            lines.append(rendered)
    return "---\n" + "\n".join(lines) + rest


# ---------------------------------------------------------------- 路径与链接

def rel(path: Path) -> str:
    return path.relative_to(VAULT).as_posix()


def resolve_in_vault(value: str) -> Path:
    """接受知识库相对路径或绝对路径，越界一律拒绝。"""
    raw = value.strip().strip('"')
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = VAULT / raw
    candidate = candidate.resolve()
    try:
        candidate.relative_to(VAULT)
    except ValueError:
        raise RecordError(f"路径不在知识库内：{value}") from None
    return candidate


WIKILINK = re.compile(r"\[\[([^\]|#]+)")


def wikilink_target(value: str) -> str:
    """从 "[[路径|别名]]" 取出路径部分；不是 wikilink 就原样返回。"""
    match = WIKILINK.search(value or "")
    return (match.group(1) if match else (value or "")).strip()


def wikilink(path: Path) -> str:
    return f"[[{rel(path)[:-3] if rel(path).endswith('.md') else rel(path)}]]"


def resolve_link(value: str) -> Path | None:
    """把 frontmatter 里的 wikilink 变成真实文件；找不到返回 None，不抛错。"""
    target = wikilink_target(value)
    if not target:
        return None
    candidate = VAULT / (target if target.endswith(".md") else target + ".md")
    try:
        candidate = candidate.resolve()
        candidate.relative_to(VAULT)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


# ---------------------------------------------------------------- 平台与校验

X_STATUS_URL = re.compile(r"\Ahttps://x\.com/[A-Za-z0-9_]{1,15}/status/(\d{15,25})\Z")
X_ARTICLE_URL = re.compile(r"\Ahttps://x\.com/[A-Za-z0-9_]{1,15}/article/(\d{15,25})\Z")
X_EDITOR_URL = re.compile(r"\Ahttps://x\.com/compose/articles/edit/\d+\Z")
WECHAT_URL = re.compile(r"\Ahttps://mp\.weixin\.qq\.com/s/[A-Za-z0-9_-]{8,}\Z")

DOUYIN_URL = re.compile(r"\Ahttps://www\.douyin\.com/video/\d{15,25}\Z")
DOUYIN_SHORT_URL = re.compile(r"\Ahttps://v\.douyin\.com/[A-Za-z0-9_-]{5,20}/?\Z")
XHS_URL = re.compile(
    r"\Ahttps://www\.xiaohongshu\.com/(?:explore|discovery/item)/[0-9a-f]{16,32}"
    r"(?:\?[^\s]*)?\Z"
)
XHS_SHORT_URL = re.compile(r"\Ahttps?://xhslink\.com/[A-Za-z0-9/_-]{4,40}\Z")
SHIPINHAO_URL = re.compile(r"\Ahttps://channels\.weixin\.qq\.com/(?!platform)[^\s]+\Z")

# 视频号常常根本给不出可分享的公开网页地址——这是平台事实，不是漏填。
# 用一个固定字面量而不是留空，是为了让"确实没有链接"和"忘了填"在健康检查里
# 分得开；留空的话，忘了填就再也发现不了。
NO_PUBLIC_URL = "无公开链接"

# 各平台的创作者后台地址。填进来这篇就会既显得已发布、又信息齐全，
# 健康检查再也点不出它其实没发出去——所以要单独拦，并且说清楚拦的是什么。
BACKEND_URL_HINTS = {
    "x": (X_EDITOR_URL.fullmatch, "X 的草稿编辑器地址"),
    "douyin": (
        re.compile(r"\Ahttps://creator\.douyin\.com/\S*\Z").fullmatch,
        "抖音创作者后台地址",
    ),
    "xiaohongshu": (
        re.compile(r"\Ahttps://creator\.xiaohongshu\.com/\S*\Z").fullmatch,
        "小红书创作者后台地址",
    ),
    "shipinhao": (
        re.compile(r"\Ahttps://channels\.weixin\.qq\.com/platform\S*\Z").fullmatch,
        "视频号助手（创作者后台）地址",
    ),
}

URL_RULES = {
    "x": (
        lambda u: bool(X_STATUS_URL.fullmatch(u) or X_ARTICLE_URL.fullmatch(u)),
        "https://x.com/<用户名>/status/<编号> 或 https://x.com/<用户名>/article/<编号>",
    ),
    "wechat": (
        lambda u: bool(WECHAT_URL.fullmatch(u)),
        "https://mp.weixin.qq.com/s/<编号>",
    ),
    "douyin": (
        lambda u: bool(DOUYIN_URL.fullmatch(u) or DOUYIN_SHORT_URL.fullmatch(u)),
        "https://www.douyin.com/video/<编号> 或分享短链 https://v.douyin.com/<编号>",
    ),
    "xiaohongshu": (
        lambda u: bool(XHS_URL.fullmatch(u) or XHS_SHORT_URL.fullmatch(u)),
        "https://www.xiaohongshu.com/explore/<编号> 或分享短链 http://xhslink.com/<编号>",
    ),
    "shipinhao": (
        lambda u: bool(SHIPINHAO_URL.fullmatch(u)) or u == NO_PUBLIC_URL,
        f"https://channels.weixin.qq.com/<分享地址>；确实拿不到公开地址时填 {NO_PUBLIC_URL}",
    ),
}


def check_public_url(platform: str, url: str) -> str:
    url = url.strip()
    matcher, label = BACKEND_URL_HINTS.get(platform, (None, ""))
    if matcher is not None and matcher(url):
        raise RecordError(
            f"这是{label}，不是公开地址。把它记成公开链接，健康检查就再也发现不了"
            f"这篇其实没发出去。公开地址形如 {URL_RULES[platform][1]}"
        )
    accepts, shape = URL_RULES[platform]
    if not accepts(url):
        raise RecordError(f"{PLATFORM_LABEL[platform]}公开地址必须是 {shape}")
    return url


def check_published_at(value: str) -> str:
    """必须是带时区的 ISO 时间：到期反馈按它计算，缺时区算不出正确的 24 小时。"""
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        raise RecordError(
            f"发布时间必须是 ISO 格式，例如 2026-07-19T23:30:50-07:00，收到：{value}"
        ) from None
    if moment.tzinfo is None:
        raise RecordError(f"发布时间必须带时区偏移，收到：{value}")
    return moment.isoformat(timespec="seconds")


def parse_moment(value: str) -> datetime:
    return datetime.fromisoformat(value)


# ---------------------------------------------------------------- 正式稿回写

PLATFORM_LABEL = {
    "x": "X Article",
    "wechat": "微信公众号",
    "shipinhao": "视频号",
    "douyin": "抖音",
    "xiaohongshu": "小红书",
}

# 每个平台一组字段名。健康检查按这些名字判断"发布信息待补"，写错一个就永远红着。
PLATFORM_FIELDS = {
    "x": {
        "status": "x_article_status",
        "url": "x_article_url",
        "published_at": "x_article_published_at",
    },
    "wechat": {
        "status": "wechat_status",
        "url": "wechat_public_url",
        "published_at": "wechat_published_at",
    },
    "shipinhao": {
        "status": "shipinhao_status",
        "url": "shipinhao_url",
        "published_at": "shipinhao_published_at",
    },
    "douyin": {
        "status": "douyin_status",
        "url": "douyin_url",
        "published_at": "douyin_published_at",
    },
    "xiaohongshu": {
        "status": "xiaohongshu_status",
        "url": "xiaohongshu_url",
        "published_at": "xiaohongshu_published_at",
    },
}

# 一条口播视频一次发三个平台，脚本要跑三遍。视频平台的后台数据都得人去看，
# 所以每跑一个平台就在反馈卡上立一条对应的待办。
VIDEO_PLATFORMS = frozenset({"shipinhao", "douyin", "xiaohongshu"})


def update_article(
    path: Path, platform: str, url: str, published_at: str
) -> tuple[str, dict[str, str]]:
    text = path.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)
    fields = PLATFORM_FIELDS[platform]

    recorded = meta.get(fields["url"], "").strip()
    if recorded and recorded != url:
        raise RecordError(
            f"{PLATFORM_LABEL[platform]} 已经记过另一个公开地址：{recorded}\n"
            "同一篇稿子不该有两个公开地址。确认哪个是对的，先手工改掉再重跑。"
        )

    updates = {
        fields["status"]: "published",
        fields["url"]: url,
        fields["published_at"]: published_at,
    }
    # 这个字段的存在本身就代表"平台数据还没补齐"，补齐了就该消失。
    if platform == "wechat" and meta.get("wechat_metadata_status", "").strip():
        updates["wechat_metadata_status"] = "complete"
    if meta.get("status", "").strip() != "published":
        updates["status"] = "published"
    return update_frontmatter(text, updates), updates


# ---------------------------------------------------------------- 源头书签

# 正式稿到书签之间隔着写作任务和审核卡，而且每一跳的字段名都不统一：
# source_note 直接指书签，source / content_pack 指写作任务，source_review 指审核卡，
# source_file 在写作任务和审核卡上都出现。挨个试，别假设只有一条路。
LINK_FIELDS = (
    "source_note",
    "source_bookmark",
    "source",
    "content_pack",
    "source_review",
    "source_file",
)
MAX_HOPS = 4


def is_bookmark(path: Path) -> bool:
    return rel(path).startswith(X_BOOKMARK_DIR)


def find_source_bookmark(article_meta: dict[str, str]) -> Path | None:
    """顺着正式稿找回它的源头 X 书签。找不到就返回 None，让调用方去说清楚。"""
    seen: set[Path] = set()
    frontier = [article_meta]
    for _ in range(MAX_HOPS):
        following: list[dict[str, str]] = []
        for meta in frontier:
            # 书签会改名、会换月目录，capture_id 里的推文编号才是稳定身份。
            capture_id = meta.get("capture_id", "").strip()
            if capture_id.startswith("x:"):
                found = find_bookmark_by_tweet_id(capture_id[2:])
                if found is not None:
                    return found
            for field in LINK_FIELDS:
                path = resolve_link(meta.get(field, ""))
                if path is None or path in seen:
                    continue
                seen.add(path)
                if is_bookmark(path):
                    return path
                following.append(
                    parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
                )
        if not following:
            return None
        frontier = following
    return None


def find_bookmark_by_tweet_id(tweet_id: str) -> Path | None:
    if not re.fullmatch(r"\d{15,25}", tweet_id):
        return None
    root = VAULT / X_BOOKMARK_DIR
    if not root.is_dir():
        return None
    for path in sorted(root.rglob("*.md")):
        meta = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        if meta.get("tweet_id", "").strip().strip('"') == tweet_id:
            return path
    return None


def update_bookmark(path: Path, article: Path) -> tuple[str, dict[str, str]]:
    """书签留下两件事：已经处理过，以及最后变成了哪篇稿。"""
    text = path.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)
    updates: dict[str, str] = {}
    if meta.get("processing_status", "").strip() != "processed":
        updates["processing_status"] = "processed"
    link = wikilink(article)
    if meta.get("published_file", "").strip() != link:
        updates["published_file"] = link
    if not updates:
        return text, updates
    return update_frontmatter(text, updates), updates


# ---------------------------------------------------------------- 发布反馈卡

DATE_PREFIX = re.compile(r"\A\d{4}-\d{2}-\d{2}-")


def feedback_path(article: Path, published_at: str) -> Path:
    """默认卡名 = 发布日期 + 正式稿标题。正式稿自带日期前缀时不重复写。"""
    stem = DATE_PREFIX.sub("", article.stem)
    day = published_at[:10]
    return VAULT / FEEDBACK_DIR / f"{day}-{stem}.md"


def find_feedback_card(article: Path, article_meta: dict[str, str], published_at: str) -> Path:
    linked = resolve_link(article_meta.get("feedback_file", ""))
    if linked:
        return linked
    default = feedback_path(article, published_at)
    if default.is_file():
        return default
    # 卡可能是手工建的、名字对不上，只能按 article 反查。
    root = VAULT / FEEDBACK_DIR
    target = wikilink(article)
    if root.is_dir():
        for path in sorted(root.glob("*.md")):
            meta = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            if meta.get("article", "").strip() == target:
                return path
    return default


# 逐字抄自 50-系统/30-模板/内容反馈.md，只把 {{date}} {{title}} 换成占位符。
# 字段顺序照抄模板，新卡和手工建的卡才看起来是一份东西。
FEEDBACK_TEMPLATE = """---
kind: content-feedback
status: pending
article: ""
platform: ""
published_at: ""
public_url: ""
feedback_stage: 24h
due_at: ""
feedback_24h_status: pending
feedback_7d_status: pending
created_at: "{date}"
updated_at: "{date}"
---

# 内容反馈：{title}

## 24 小时

- 曝光：
- 打开或阅读：
- 收藏：
- 转发：
- 评论与私信：
- 最强反馈：

## 7 天

- 持续流量：
- 转化或线索：
- 读者真正复用的部分：

## 回写

- [ ] 已更新候选中的剩余角度
- [ ] 已沉淀可复用知识
- [ ] 已记录下一轮要保留或改掉的做法
"""

# 逐字抄自 50-系统/30-模板/口播视频反馈.md。视频不能套图文那张卡：图文看的是点开
# 之后（收藏、转发、读完），视频要先回答有没有被划走，所以完播率和平均播放时长
# 排在点赞前面，而且三个平台的口径互不相同，必须分开记。
VIDEO_FEEDBACK_TEMPLATE = """---
kind: content-feedback
status: pending
form: 口播短视频
article: ""
topic: ""
platform: ""
published_at: ""
public_url: ""
feedback_stage: 24h
due_at: ""
feedback_24h_status: pending
feedback_7d_status: pending
created_at: "{date}"
updated_at: "{date}"
---

# 口播视频反馈：{title}

三个平台的口径互不相同，分开记，不横向比较。

## 24 小时事实快照

### 视频号

- 播放：
- 点赞：
- 推荐：
- 评论：
- 收藏：
- 分享：
- 新增关注：

### 抖音

- 播放：
- 完播率：
- 平均播放时长：
- 点赞：
- 评论：
- 收藏：
- 分享：
- 新增粉丝：

### 小红书

- 曝光：
- 阅读（点击）：
- 点击率：
- 点赞：
- 收藏：
- 评论：
- 涨粉：

## 24 小时初步信号

- 前 3 秒有没有留住人：
- 开头承诺的东西，观众等到了没有（中段有没有集中划走）：
- 评论区在追问什么、在反驳什么：
- 结尾的下集钩子换来了多少关注：

## 7 天累计快照

### 视频号

- 播放：
- 点赞：
- 推荐：
- 评论：
- 收藏：
- 分享：
- 新增关注：

### 抖音

- 播放：
- 完播率：
- 平均播放时长：
- 点赞：
- 评论：
- 收藏：
- 分享：
- 新增粉丝：

### 小红书

- 曝光：
- 阅读（点击）：
- 点击率：
- 点赞：
- 收藏：
- 评论：
- 涨粉：

## 7 天判断

- 24 小时之后还有没有继续推（有长尾是选题赢，没有是开头赢）：
- 观众真正关心的是主题、角度、利益点、情绪还是表达形式：
- 下次继续保留：
- 下次停止：
- 下一步值得测试：
- 这个题值不值得再出一集 / 值不值得改写成长文：

## 回写

- [ ] 已更新候选中的剩余角度
- [ ] 已沉淀可复用知识
- [ ] 已记录下一轮要保留或改掉的做法
"""


def is_video(article: Path) -> bool:
    return rel(article).startswith(VIDEO_DIR)


def ensure_feedback_card(
    article: Path,
    article_meta: dict[str, str],
    platform: str,
    url: str,
    published_at: str,
    now: datetime,
) -> tuple[Path, str, dict[str, str], bool]:
    """每篇正式稿一张卡。已经存在就只补空缺，绝不把做完的复盘打回 pending。"""
    path = find_feedback_card(article, article_meta, published_at)
    created = not path.is_file()
    if created:
        template = VIDEO_FEEDBACK_TEMPLATE if is_video(article) else FEEDBACK_TEMPLATE
        text = template.format(
            title=DATE_PREFIX.sub("", article.stem),
            date=now.isoformat(timespec="seconds"),
        )
    else:
        text = path.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)

    moment = parse_moment(published_at)
    due_24h = (moment + timedelta(hours=FEEDBACK_24H_HOURS)).isoformat(timespec="seconds")
    due_7d = (moment + timedelta(days=FEEDBACK_7D_DAYS)).isoformat(timespec="seconds")

    updates: dict[str, str] = {}

    def fill(key: str, value: str) -> None:
        """只填空。已经有值的字段留给人，脚本不覆盖复盘结果。"""
        if not meta.get(key, "").strip():
            updates[key] = value

    fill("kind", "content-feedback")
    fill("status", "pending")
    fill("article", wikilink(article))
    # 候选选题在正式稿上有两个字段名，新旧稿各用一个。
    topic = article_meta.get("topic", "").strip() or article_meta.get("candidate", "").strip()
    if topic:
        fill("topic", topic)
    fill("platform", article_meta.get("platform", "").strip() or PLATFORM_LABEL[platform])
    fill("published_at", published_at)
    fill("public_url", url)
    # 一条视频同时发三个平台时 public_url 只装得下第一个，所以每个平台的链接
    # 还要各自留一份——否则后两次运行的链接就丢了。
    fill(PLATFORM_FIELDS[platform]["url"], url)
    fill("feedback_24h_status", "pending")
    fill("feedback_7d_status", "pending")
    fill("feedback_7d_due_at", due_7d)
    fill("created_at", now.isoformat(timespec="seconds"))

    # due_at 始终指向下一次要做的复盘：24 小时那次做完了就该指向 7 天。
    if meta.get("feedback_24h_status", "").strip() == "complete":
        fill("feedback_stage", "7d")
        if not meta.get("due_at", "").strip():
            updates["due_at"] = due_7d
    else:
        fill("feedback_stage", "24h")
        fill("due_at", due_24h)

    # 公众号和三个视频平台的后台数据都要人去看，脚本只负责把这件待办立起来。
    if platform == "wechat":
        fill("wechat_data_status", "pending")
    if platform in VIDEO_PLATFORMS:
        fill(f"{platform}_data_status", "pending")

    if updates or created:
        updates["updated_at"] = now.isoformat(timespec="seconds")
        text = update_frontmatter(text, updates)
    return path, text, updates, created


# ---------------------------------------------------------------- 主流程

def check_article(value: str) -> Path:
    path = resolve_in_vault(value)
    if not path.is_file():
        raise RecordError(f"找不到正式稿：{value}")
    where = rel(path)
    if not any(where.startswith(d) for d in PUBLISHED_DIRS):
        raise RecordError(
            f"这个文件不在正式稿目录下：{where}\n"
            f"只记录 {' 或 '.join(PUBLISHED_DIRS)} 里的稿子。"
        )
    return path


def record(
    article_arg: str,
    platform: str,
    url: str,
    published_at: str,
    now: datetime,
    dry_run: bool,
) -> list[str]:
    article = check_article(article_arg)
    url = check_public_url(platform, url)
    published_at = check_published_at(published_at)

    article_text = article.read_text(encoding="utf-8")
    article_meta = parse_frontmatter(article_text)

    new_article_text, article_updates = update_article(article, platform, url, published_at)
    card, card_text, card_updates, card_created = ensure_feedback_card(
        article, article_meta, platform, url, published_at, now
    )
    if not article_meta.get("feedback_file", "").strip():
        article_updates["feedback_file"] = wikilink(card)
        new_article_text = update_frontmatter(
            new_article_text, {"feedback_file": wikilink(card)}
        )

    bookmark = find_source_bookmark(article_meta)
    bookmark_updates: dict[str, str] = {}
    new_bookmark_text = ""
    if bookmark is not None:
        new_bookmark_text, bookmark_updates = update_bookmark(bookmark, article)

    report: list[str] = []
    report.append(f"正式稿 {rel(article)}")
    for key, value in article_updates.items():
        report.append(f"  {key}: {value}")
    report.append(("新建反馈卡 " if card_created else "反馈卡 ") + rel(card))
    for key, value in card_updates.items():
        report.append(f"  {key}: {value}")
    if bookmark is None:
        report.append("源头书签 未找到——这篇稿子没有可回写的 X 书签，或者链接已经断了")
    elif bookmark_updates:
        report.append(f"源头书签 {rel(bookmark)}")
        for key, value in bookmark_updates.items():
            report.append(f"  {key}: {value}")
    else:
        report.append(f"源头书签 {rel(bookmark)}（已经是最新，未改动）")

    # 《个人知识库工作流》六「发布与回流」剩下的几步靠判断，脚本不替人做，
    # 但必须说出来——否则跑完这条命令很容易以为归档已经全齐了。
    report.append("")
    report.append("还需要你自己完成（脚本不做判断）：")
    todo = article_meta.get
    if todo("knowledge_backfill_status", "").strip() not in ("complete", "not-needed"):
        report.append("  - 把可复用的观点或方法写回 20-知识/，再更新 knowledge_backfill_status")
    if todo("remaining_angles_status", "").strip() not in ("complete", "not-needed"):
        report.append("  - 标记本次已用角度、保留剩余角度，再更新 remaining_angles_status")
    topic_link = todo("topic", "").strip() or todo("candidate", "").strip()
    if topic_link:
        report.append(f"  - 把候选选题 {wikilink_target(topic_link)} 改为 partially-published 或 closed")

    if dry_run:
        report.append("")
        report.append("（--dry-run：以上都没有真的写入）")
        return report

    article.write_text(new_article_text, encoding="utf-8")
    if card_updates or card_created:
        card.parent.mkdir(parents=True, exist_ok=True)
        card.write_text(card_text, encoding="utf-8")
    if bookmark is not None and bookmark_updates:
        bookmark.write_text(new_bookmark_text, encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="记录一次已经完成的公开发布，并完成发布后归档。本脚本不发布任何东西。"
    )
    parser.add_argument("--article", required=True, help="正式稿路径（知识库相对路径即可）")
    parser.add_argument("--platform", required=True, choices=sorted(PLATFORM_FIELDS))
    parser.add_argument("--url", required=True, help="平台上的公开地址，不是草稿编辑器地址")
    parser.add_argument(
        "--published-at", required=True, help="发布时间，带时区，例如 2026-07-19T23:30:50-07:00"
    )
    parser.add_argument("--now", default="", help="仅供测试：覆盖当前时间")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要写什么，不落盘")
    args = parser.parse_args(argv)

    try:
        now = parse_moment(args.now) if args.now else datetime.now().astimezone()
        for line in record(
            args.article, args.platform, args.url, args.published_at, now, args.dry_run
        ):
            print(line)
    except RecordError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
