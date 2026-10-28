#!/usr/bin/env python3
"""记录一次已经发生的公开发布，并完成发布后归档。

这个脚本不发布任何东西。它不调用微信或 X 的任何接口，只在人已经在平台上完成
公开发布之后，把结果写回知识库：正式稿的平台字段、发布反馈卡、源头书签的处理
状态和去向，以及母稿、独立口播稿和成稿包的本地收尾。发布本身是用户动作，
脚本只负责让归档不再靠手工惯例。
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
ARTICLE_FEEDBACK_TEMPLATE = "50-系统/30-模板/内容反馈.md"
VIDEO_FEEDBACK_TEMPLATE = "50-系统/30-模板/口播视频反馈.md"
FEEDBACK_CONFIG = "50-系统/40-自动化/反馈复盘/config.json"

ACTIVE_WRITING_TASK_DIR = "10-创作/20-写作任务"
ACTIVE_ARTICLE_DRAFT_DIR = "10-创作/30-文章草稿"
ACTIVE_ORAL_DRAFT_DIR = "10-创作/25-口播草稿"
ARCHIVE_WRITING_TASK_DIR = "90-归档/30-创作记录/旧成稿包"
ARCHIVE_ARTICLE_DRAFT_DIR = "90-归档/30-创作记录/旧文章草稿"
ARCHIVE_ORAL_DRAFT_DIR = "90-归档/30-创作记录/旧口播草稿"
REFERENCE_SUFFIXES = frozenset({".md", ".json", ".base", ".canvas", ".yaml", ".yml"})
REFERENCE_SKIP_DIRS = frozenset({".git", ".obsidian", "node_modules", "__pycache__"})

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
    return path.resolve().relative_to(VAULT).as_posix()


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


# ---------------------------------------------------------------- 发布后收尾

LOCAL_MARKDOWN_LINK = re.compile(r"(!?\[[^\]]*\]\()([^)]+)(\))")


def is_under(path: Path, relative_dir: str) -> bool:
    try:
        path.resolve().relative_to((VAULT / relative_dir).resolve())
    except ValueError:
        return False
    return True


def archive_target(path: Path, month: str) -> tuple[Path, str] | None:
    """返回（目标路径, 类型）。只有三个活跃生产目录会被自动收尾。"""
    if is_under(path, ACTIVE_ARTICLE_DRAFT_DIR):
        return VAULT / ARCHIVE_ARTICLE_DRAFT_DIR / month / path.name, "article-draft"
    if is_under(path, ACTIVE_ORAL_DRAFT_DIR):
        return VAULT / ARCHIVE_ORAL_DRAFT_DIR / month / path.name, "oral-script"
    if is_under(path, ACTIVE_WRITING_TASK_DIR):
        return VAULT / ARCHIVE_WRITING_TASK_DIR / month / path.name, "content-pack"
    return None


def rebase_markdown_links(text: str, old_parent: Path, new_parent: Path) -> str:
    """移动 Markdown 后重算本地相对链接；网页、锚点、绝对路径保持不动。"""
    def replace(match: re.Match[str]) -> str:
        original = match.group(2)
        raw = original.strip()
        wrapped = raw.startswith("<") and raw.endswith(">")
        if wrapped:
            raw = raw[1:-1]
        if raw.startswith(("http://", "https://", "mailto:", "#", "/", "data:")):
            return match.group(0)
        target = (old_parent / raw).resolve()
        try:
            target.relative_to(VAULT)
        except ValueError:
            return match.group(0)
        rebased = os.path.relpath(target, new_parent).replace(os.sep, "/")
        rendered = f"<{rebased}>" if wrapped or " " in rebased else rebased
        prefix = original[: len(original) - len(original.lstrip())]
        suffix = original[len(original.rstrip()):]
        return match.group(1) + prefix + rendered + suffix + match.group(3)

    return LOCAL_MARKDOWN_LINK.sub(replace, text)


def reference_files() -> list[Path]:
    files: list[Path] = []
    for path in VAULT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in REFERENCE_SUFFIXES:
            continue
        parts = path.relative_to(VAULT).parts
        if any(part in REFERENCE_SKIP_DIRS for part in parts):
            continue
        files.append(path)
    return files


def replace_path_references(text: str, moves: dict[Path, Path]) -> str:
    """同时修复知识库相对路径与保存在清单里的绝对路径。"""
    for old, new in moves.items():
        old_rel = rel(old)
        new_rel = rel(new)
        text = text.replace(str(old), str(new))
        text = text.replace(old_rel, new_rel)
        if old_rel.endswith(".md") and new_rel.endswith(".md"):
            text = text.replace(old_rel[:-3], new_rel[:-3])
    return text


def closeout_published_chain(
    article: Path,
    published_at: str,
    now: datetime,
    dry_run: bool = False,
) -> list[str]:
    """把已发布内容的母稿与成稿包移出活跃区，并修复全库引用。

    只处理正式稿明确链接到的文件；候选剩余角度、A/B 取舍等语义判断仍留给人。
    """
    article_text = article.read_text(encoding="utf-8")
    article_meta = parse_frontmatter(article_text)
    month = published_at[:7]
    published_link = wikilink(article)

    linked: list[Path] = []
    sources: list[Path] = []
    # 新正式稿统一用 draft_source；早期口播正式稿使用 oral_script。两种都是正式稿
    # 明确声明的生产来源，不能因为字段名演进就把旧口播永远留在草稿区。
    for key in ("draft_source", "oral_script"):
        source = resolve_link(article_meta.get(key, ""))
        if source is not None and source not in sources:
            sources.append(source)
            linked.append(source)

    pack = resolve_link(article_meta.get("content_pack", ""))
    if pack is None:
        for source in sources:
            source_meta = parse_frontmatter(
                source.read_text(encoding="utf-8", errors="replace")
            )
            pack = resolve_link(source_meta.get("content_pack", ""))
            if pack is not None:
                break
    if pack is not None and pack not in linked:
        linked.append(pack)

    moves: dict[Path, Path] = {}
    kinds: dict[Path, str] = {}
    for path in linked:
        result = archive_target(path, month)
        if result is None:
            continue
        target, kind = result
        if target.exists() and target.resolve() != path.resolve():
            raise RecordError(
                f"发布登记已完成，但收尾停止：归档目标已存在 {rel(target)}。"
                "为避免覆盖，请先确认同名文件。"
            )
        moves[path] = target
        kinds[path] = kind

    report = ["发布后收尾"]
    if not moves:
        report.append("  没有仍在活跃目录的母稿或写作任务")
        return report

    staged: dict[Path, str] = {}
    for old, new in moves.items():
        text = old.read_text(encoding="utf-8")
        kind = kinds[old]
        reason = (
            "对应内容已公开发布，写作任务移出活跃区并保留追溯"
            if kind == "content-pack"
            else "对应内容已公开发布，母稿移出活跃草稿区并保留追溯"
        )
        updates = {
            "status": "archived",
            "archived_at": now.isoformat(timespec="seconds"),
            "archived_reason": reason,
            "published_file": published_link,
        }
        if kind in {"article-draft", "oral-script"}:
            updates["kind"] = kind
        text = update_frontmatter(text, updates)
        if old.suffix.lower() == ".md":
            text = rebase_markdown_links(text, old.parent, new.parent)
        staged[new] = replace_path_references(text, moves)
        report.append(f"  {rel(old)} -> {rel(new)}")

    moved_sources = set(moves)
    changed_references = 0
    for path in reference_files():
        if path in moved_sources:
            continue
        original = path.read_text(encoding="utf-8", errors="replace")
        updated = replace_path_references(original, moves)
        if updated != original:
            staged[path] = updated
            changed_references += 1
    report.append(f"  同步修复 {changed_references} 个引用文件")

    if dry_run:
        report.append("  （预演：没有移动或改写文件）")
        return report

    # 先写好归档副本和全部引用，再移除旧副本。中途失败最多留下可恢复的双份，
    # 不会出现先删源文件、后写目标失败的数据丢失。
    for path, text in staged.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp-closeout")
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)
    for old, new in moves.items():
        if not new.is_file():
            raise RecordError(f"收尾校验失败：没有生成归档文件 {rel(new)}")
        old.unlink()
    return report


def closeout_anchor(article: Path, explicit: str = "") -> str:
    """收尾只需要归档月份；优先精确发布时间，旧稿可退到自身日期。"""
    if explicit.strip():
        return check_published_at(explicit)
    meta = parse_frontmatter(article.read_text(encoding="utf-8", errors="replace"))
    for key in (
        "x_article_published_at",
        "wechat_published_at",
        "shipinhao_published_at",
        "douyin_published_at",
        "xiaohongshu_published_at",
        "published_at",
        "created_at",
    ):
        value = meta.get(key, "").strip()
        if re.match(r"\A\d{4}-\d{2}(?:-\d{2})?", value):
            return value
    raise RecordError("找不到可用于归档月份的发布时间或创建日期")


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

# 保留这个字面量兼容已有记录。新规则下链接本身是可选项，留空同样合法；如果用户
# 明确知道平台没有公开地址，仍可写「无公开链接」表达这个事实。
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
    if not url:
        return ""
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
    """发布时间可留空；一旦填写，必须是带时区的 ISO 时间。"""
    value = value.strip()
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
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

# 每个平台一组字段名。status 是发布事实；url 和 published_at 是可选的追溯资料。
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
# 候选选题在正式稿上用过四个字段名：长文用 topic / candidate，视频用 topic_candidate / topic_card
CANDIDATE_KEYS = ("topic", "candidate", "topic_candidate", "topic_card")
OPEN_CANDIDATE_STATUSES = frozenset({"candidate", "active", "selected", "writing", "in-progress"})


def merge_platform_labels(existing: str, label: str) -> str:
    """平台名称是发布事实：去重追加，不要求链接或准确时间。"""
    existing = existing.strip().strip('"').strip("'")
    if not existing:
        return label
    if label in existing:
        return existing
    return f"{existing}；{label}"


def update_article(
    path: Path,
    platform: str,
    url: str,
    published_at: str,
    confirmed_at: str,
) -> tuple[str, dict[str, str]]:
    text = path.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)
    fields = PLATFORM_FIELDS[platform]

    recorded = meta.get(fields["url"], "").strip()
    if recorded and url and recorded != url:
        raise RecordError(
            f"{PLATFORM_LABEL[platform]} 已经记过另一个公开地址：{recorded}\n"
            "同一篇稿子不该有两个公开地址。确认哪个是对的，先手工改掉再重跑。"
        )

    updates = {
        fields["status"]: "published",
        "platform": merge_platform_labels(
            meta.get("platform", ""), PLATFORM_LABEL[platform]
        ),
    }
    if not meta.get("publication_confirmed_at", "").strip():
        updates["publication_confirmed_at"] = confirmed_at
    # 两项资料都可选。空值不能覆盖以前已经登记的真实资料；新记录仍保留空字段，
    # 让不同平台的属性结构保持一致。
    if url or fields["url"] not in meta:
        updates[fields["url"]] = url
    if published_at or fields["published_at"] not in meta:
        updates[fields["published_at"]] = published_at
    # 旧字段保留兼容：发布事实已经确认后，这张本地登记就算完成。
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


def feedback_path(article: Path, date_anchor: str) -> Path:
    """默认卡名 = 日期锚点 + 正式稿标题；无平台时间时使用本地确认日期。"""
    stem = DATE_PREFIX.sub("", article.stem)
    day = date_anchor[:10]
    return VAULT / FEEDBACK_DIR / f"{day}-{stem}.md"


def find_feedback_card(article: Path, article_meta: dict[str, str], date_anchor: str) -> Path:
    linked = resolve_link(article_meta.get("feedback_file", ""))
    if linked:
        return linked
    default = feedback_path(article, date_anchor)
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


def is_video(article: Path) -> bool:
    return rel(article).startswith(VIDEO_DIR)


def video_feedback_paused() -> bool:
    """反馈复盘/config.json 的 video_feedback_paused 为真时，口播视频登记发布不再建反馈卡。"""
    try:
        config = json.loads((VAULT / FEEDBACK_CONFIG).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(config.get("video_feedback_paused"))


def render_feedback_template(article: Path, title: str, date_value: str) -> str:
    """反馈卡只从 vault 模板读取，避免脚本与模板各维护一份。"""
    relative = VIDEO_FEEDBACK_TEMPLATE if is_video(article) else ARTICLE_FEEDBACK_TEMPLATE
    path = VAULT / relative
    if not path.is_file():
        raise RecordError(f"缺少反馈模板：{relative}")
    return (
        path.read_text(encoding="utf-8")
        .replace("{{title}}", title)
        .replace("{{date}}", date_value)
    )


def ensure_feedback_card(
    article: Path,
    article_meta: dict[str, str],
    platform: str,
    url: str,
    published_at: str,
    now: datetime,
) -> tuple[Path, str, dict[str, str], bool]:
    """每篇正式稿一张卡。已经存在就只补空缺，绝不把做完的复盘打回 pending。"""
    date_anchor = published_at or now.isoformat(timespec="seconds")
    path = find_feedback_card(article, article_meta, date_anchor)
    created = not path.is_file()
    if created:
        text = render_feedback_template(
            article,
            DATE_PREFIX.sub("", article.stem),
            now.isoformat(timespec="seconds"),
        )
    else:
        text = path.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)

    # 没有平台时间时，从本地确认登记的时刻安排反馈；不把这个时刻冒充平台发布时间。
    moment = parse_moment(date_anchor)
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
    article_platform = merge_platform_labels(
        article_meta.get("platform", ""), PLATFORM_LABEL[platform]
    )
    platform_value = meta.get("platform", "").strip()
    for known_label in PLATFORM_LABEL.values():
        if known_label in article_platform:
            platform_value = merge_platform_labels(platform_value, known_label)
    if meta.get("platform", "").strip() != platform_value:
        updates["platform"] = platform_value
    if published_at:
        fill("published_at", published_at)
    if url:
        fill("public_url", url)
    fill("publication_confirmed_at", now.isoformat(timespec="seconds"))
    # 一条视频同时发三个平台时 public_url 只装得下第一个，所以每个平台的链接
    # 还要各自留一份——否则后两次运行的链接就丢了。
    if url:
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

    confirmed_at = now.isoformat(timespec="seconds")
    new_article_text, article_updates = update_article(
        article, platform, url, published_at, confirmed_at
    )
    card: Path | None = None
    card_text, card_updates, card_created = "", {}, False
    if not (is_video(article) and video_feedback_paused()):
        card, card_text, card_updates, card_created = ensure_feedback_card(
            article, article_meta, platform, url, published_at, now
        )
    if card is not None and not article_meta.get("feedback_file", "").strip():
        article_updates["feedback_file"] = wikilink(card)
        new_article_text = update_frontmatter(
            new_article_text, {"feedback_file": wikilink(card)}
        )

    bookmark = find_source_bookmark(article_meta)
    bookmark_updates: dict[str, str] = {}
    new_bookmark_text = ""
    if bookmark is not None:
        new_bookmark_text, bookmark_updates = update_bookmark(bookmark, article)

    # 在写入发布字段前先预检收尾目标。同名冲突必须在任何落盘动作之前暴露，
    # 避免出现“平台字段已经写了，但母稿只收了一半”的状态。
    closeout_date_anchor = published_at or confirmed_at
    closeout_preview = closeout_published_chain(
        article, closeout_date_anchor, now, dry_run=True
    )

    report: list[str] = []
    report.append(f"正式稿 {rel(article)}")
    for key, value in article_updates.items():
        report.append(f"  {key}: {value}")
    if card is None:
        report.append("反馈卡 未建立——口播视频反馈已暂停（反馈复盘/config.json）")
    else:
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
    topic_link = next(
        (todo(key, "").strip() for key in CANDIDATE_KEYS if todo(key, "").strip()), ""
    )
    candidate = resolve_link(topic_link) if topic_link else None
    candidate_text = ""
    if candidate is not None and parse_frontmatter(
        candidate.read_text(encoding="utf-8")
    ).get("status", "").strip() in OPEN_CANDIDATE_STATUSES:
        # 已经发布，候选不该再占今日候选；还有剩余角度时人可以再改成 partially-published
        candidate_text = update_frontmatter(candidate.read_text(encoding="utf-8"), {
            "status": "published",
            "published_file": wikilink(article),
            "updated_at": now.isoformat(timespec="seconds"),
        })
        closeout_preview.append(f"候选选题 {rel(candidate)} → status: published")
        report.append(
            f"  - 候选选题 {rel(candidate)} 会改为 published；还有剩余角度时再改成 partially-published"
        )
    elif topic_link:
        report.append(f"  - 把候选选题 {wikilink_target(topic_link)} 改为 partially-published 或 closed")

    if dry_run:
        report.append("")
        report.extend(closeout_preview)
        report.append("")
        report.append("（--dry-run：以上都没有真的写入）")
        return report

    article.write_text(new_article_text, encoding="utf-8")
    if card is not None and (card_updates or card_created):
        card.parent.mkdir(parents=True, exist_ok=True)
        card.write_text(card_text, encoding="utf-8")
    if bookmark is not None and bookmark_updates:
        bookmark.write_text(new_bookmark_text, encoding="utf-8")
    if candidate is not None and candidate_text:
        candidate.write_text(candidate_text, encoding="utf-8")
        report.append(f"候选选题 {rel(candidate)} 已改为 published")
    report.append("")
    report.extend(closeout_published_chain(article, closeout_date_anchor, now))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="记录一次已经完成的公开发布，并完成发布后归档。本脚本不发布任何东西。"
    )
    parser.add_argument("--article", required=True, help="正式稿路径（知识库相对路径即可）")
    parser.add_argument("--closeout-only", action="store_true", help="只补做发布后收尾，不改平台字段")
    parser.add_argument("--platform", choices=sorted(PLATFORM_FIELDS))
    parser.add_argument("--url", default="", help="可选：平台公开地址，不是草稿编辑器地址")
    parser.add_argument(
        "--published-at", default="", help="可选：发布时间；填写时需带时区，例如 2026-07-19T23:30:50-07:00"
    )
    parser.add_argument("--now", default="", help="仅供测试：覆盖当前时间")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要写什么，不落盘")
    args = parser.parse_args(argv)

    try:
        now = parse_moment(args.now) if args.now else datetime.now().astimezone()
        if args.closeout_only:
            article = check_article(args.article)
            anchor = closeout_anchor(article, args.published_at)
            lines = closeout_published_chain(article, anchor, now, args.dry_run)
        else:
            if not args.platform:
                raise RecordError(
                    "登记发布需要提供 --platform；公开链接和发布时间可以留空。"
                    "只补收尾请使用 --closeout-only"
                )
            lines = record(
                args.article, args.platform, args.url, args.published_at, now, args.dry_run
            )
        for line in lines:
            print(line)
    except RecordError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
