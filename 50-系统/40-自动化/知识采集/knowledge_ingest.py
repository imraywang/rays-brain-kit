#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
VAULT = Path(os.environ.get("RAYS_BRAIN", str(HERE.parents[2]))).resolve()
SYSTEM_DIR = HERE
REVIEW_PROTOCOL_FILE = HERE.parent / "review_protocol.json"
# 运行时状态与日志放在 vault 外，避免 iCloud 高频同步与冲突副本破坏去重状态
STATE_HOME = Path(os.environ.get("RAYS_BRAIN_STATE", str(Path.home() / ".local/state/rays-brain")))
STATE_FILE = STATE_HOME / "state.json"
LOG_DIR = STATE_HOME / "logs"
CONFIG_FILE = SYSTEM_DIR / "config.json"
SCHEMA_FILE = SYSTEM_DIR / "review.schema.json"
PROMPT_FILE = SYSTEM_DIR / "review-prompt.md"
CONTENT_PACK_TEMPLATE = SYSTEM_DIR.parents[1] / "30-模板/内容成稿包.md"
X_DIR = VAULT / "30-资料/30-X书签"
X_CLEANUP_DIR = VAULT / "90-归档/90-待清理"
WEB_DIR = VAULT / "30-资料/40-网页剪藏"
WEREAD_DIR = VAULT / "30-资料/50-读书划线"
LINK_INBOX = VAULT / "30-资料/00-待抓取/链接收件箱.md"
REVIEW_DIR = VAULT / "10-创作/10-灵感/10-待评估/剪藏复核"
TOPIC_DIR = VAULT / "10-创作/10-灵感/20-候选选题"
TOPIC_RESERVE_DIR = VAULT / "10-创作/10-灵感/90-选题储备"
WRITING_TASK_DIR = VAULT / "10-创作/20-写作任务"
CONTENT_PACK_DIR = WRITING_TASK_DIR
REVIEW_RECORD_DIR = VAULT / "90-归档/20-审核记录/剪藏复核"
KNOWLEDGE_DIR = VAULT / "20-知识"
LEGACY_ACTIONS_SCRIPT = SYSTEM_DIR / "legacy_actions.py"
TOPIC_FRESHNESS_DAYS = {"强时效": 3, "中时效": 14, "常青": 30}
ARTICLE_DRAFT_DIR = VAULT / "10-创作/30-文章草稿"
ORAL_DRAFT_DIR = VAULT / "10-创作/20-口播草稿"
PUBLISHED_ROOT = VAULT / "40-发布"
FEEDBACK_REL_PREFIX = "40-发布/00-内容反馈"
RECORD_PUBLISHED_SCRIPT = SYSTEM_DIR.parent / "发布归档/record_published.py"
# X Article 发布检测只在草稿推入后 72 小时内轮询；超窗口留给工作台「登记发布」表单。
X_ARTICLE_DETECT_WINDOW_HOURS = 72
# 草稿 kind → (正式稿子目录, 正式稿 kind)。口径抄自知识仪表盘 record_publication。
X_PUBLISH_ARCHIVE_RULES = {
    "draft": ("10-X长文", "article-published"),
    "article-draft": ("10-X长文", "article-published"),
    "oral-script": ("50-口播视频", "video-published"),
}


def now_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{now_local()} {message}"
    with (LOG_DIR / "pipeline.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "initialized_at": now_local(),
        "known_web_files": [],
        "known_weread_versions": {},
        "known_x_ids": [],
        "mobile_line_hashes": [],
        "pending_sources": [],
        "reviewed_sources": {},
        "review_replacements": {},
        "errors": [],
    }


def load_state() -> dict[str, Any]:
    state = load_json(STATE_FILE, default_state())
    for key, value in default_state().items():
        state.setdefault(key, value)
    return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = now_local()
    atomic_json(STATE_FILE, state)


def rel(path: Path) -> str:
    return path.relative_to(VAULT).as_posix()


def sanitize_filename(value: str, limit: int = 72) -> str:
    value = html.unescape(value)
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return (value[:limit].rstrip() or "未命名")


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def x_id_of(path: Path) -> str:
    """书签文件的推文 ID：优先 frontmatter tweet_id，回退正文 H1，最后回退文件名。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    meta = parse_frontmatter(text)
    tweet_id = str(meta.get("tweet_id", "")).strip().strip('"')
    if re.fullmatch(r"\d{18,20}", tweet_id):
        return tweet_id
    head = "\n".join(text.split("\n")[:40])
    match = re.search(r"—\s*(\d{18,20})\s*$", head, flags=re.M)
    if not match:
        match = re.search(r"(\d{18,20})", path.stem)
    return match.group(1) if match else ""


def existing_x_ids() -> set[str]:
    ids: set[str] = set()
    for path in X_DIR.rglob("*.md"):
        tweet_id = x_id_of(path)
        if tweet_id:
            ids.add(tweet_id)
    return ids


def archived_x_ids() -> set[str]:
    """已经进入可恢复清理区的书签仍参与去重，避免后续同步重新导入。"""
    ids: set[str] = set()
    if not X_CLEANUP_DIR.exists():
        return ids
    for path in X_CLEANUP_DIR.rglob("*.md"):
        archived_rel = path.relative_to(X_CLEANUP_DIR).as_posix()
        if "30-资料/30-X书签/" not in archived_rel:
            continue
        tweet_id = x_id_of(path)
        if tweet_id:
            ids.add(tweet_id)
    return ids


def known_local_x_ids() -> set[str]:
    return existing_x_ids() | archived_x_ids()


def archived_source_for(original_rel: str) -> str:
    if (
        not original_rel
        or Path(original_rel).is_absolute()
        or ".." in Path(original_rel).parts
        or not X_CLEANUP_DIR.exists()
    ):
        return ""
    matches = [
        rel(path)
        for path in X_CLEANUP_DIR.rglob(Path(original_rel).name)
        if path.is_file() and rel(path).endswith("/" + original_rel)
    ]
    return matches[0] if len(matches) == 1 else ""


def reconcile_reviewed_source_paths(state: dict[str, Any]) -> int:
    """修复清理移动后仍指向旧资料路径的状态记录。"""
    repaired = 0
    for item in state.get("reviewed_sources", {}).values():
        source_rel = str(item.get("source", ""))
        if not source_rel or (VAULT / source_rel).exists():
            continue
        archived_rel = archived_source_for(source_rel)
        if archived_rel:
            item["source"] = archived_rel
            repaired += 1
    return repaired


def initialize() -> dict[str, int]:
    for path in (
        SYSTEM_DIR,
        LOG_DIR,
        X_DIR,
        WEB_DIR,
        WEREAD_DIR,
        REVIEW_DIR,
        TOPIC_DIR,
        TOPIC_RESERVE_DIR,
        WRITING_TASK_DIR,
        REVIEW_RECORD_DIR,
        LINK_INBOX.parent,
    ):
        path.mkdir(parents=True, exist_ok=True)
    state = load_state()
    if not STATE_FILE.exists():
        state["known_web_files"] = sorted(rel(p) for p in WEB_DIR.glob("*.md"))
        state["known_x_ids"] = sorted(known_local_x_ids())
    repaired_state_sources = reconcile_reviewed_source_paths(state)
    save_state(state)
    return {
        "known_web_files": len(state["known_web_files"]),
        "known_x_ids": len(state["known_x_ids"]),
        "repaired_state_sources": repaired_state_sources,
    }


def run_xurl_request(config: dict[str, Any], args: list[str]) -> dict[str, Any]:
    cmd = [config.get("xurl_path", "xurl")]
    if config.get("x_app"):
        cmd += ["--app", str(config["x_app"])]
    cmd += args
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=90)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "xurl failed")
    return json.loads(proc.stdout)


def run_xurl(config: dict[str, Any]) -> dict[str, Any]:
    me = run_xurl_request(config, ["/2/users/me"])
    user_id = str(me.get("data", {}).get("id", ""))
    if not user_id:
        raise RuntimeError("X API did not return the authenticated user id")
    max_results = max(5, min(100, int(config.get("x_max_results", 100))))
    endpoint = (
        f"/2/users/{user_id}/bookmarks?max_results={max_results}"
        "&tweet.fields=article,attachments,author_id,created_at,entities,note_tweet,public_metrics"
        "&expansions=attachments.media_keys,author_id"
        "&media.fields=media_key,type,url,preview_image_url,alt_text"
    )
    return run_xurl_request(config, [endpoint])


def x_full_text(tweet: dict[str, Any]) -> str:
    """Prefer Note Tweet text because the regular text field is truncated for long posts."""
    return str(tweet.get("note_tweet", {}).get("text") or tweet.get("text", "")).strip()


def x_source_url(tweet_id: str, username: str = "") -> str:
    if username and username != "unknown":
        return f"https://x.com/{username}/status/{tweet_id}"
    return f"https://x.com/i/status/{tweet_id}"


def x_expanded_urls(tweet: dict[str, Any]) -> list[str]:
    expanded: list[str] = []
    containers = [tweet.get("note_tweet", {}).get("entities", {}), tweet.get("entities", {})]
    for entities in containers:
        for item in entities.get("urls", []):
            value = item.get("unwound_url") or item.get("expanded_url")
            if value:
                value = str(value).replace("http://x.com/", "https://x.com/", 1)
                if value not in expanded:
                    expanded.append(value)
    return expanded


def x_media_items(
    tweet: dict[str, Any], media_by_key: dict[str, dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    media_by_key = media_by_key or {}
    result: list[dict[str, Any]] = []
    for key in tweet.get("attachments", {}).get("media_keys", []):
        item = dict(media_by_key.get(str(key), {}))
        item.setdefault("media_key", str(key))
        if not item.get("type"):
            item["type"] = "video" if str(key).startswith("13_") else "photo"
        result.append(item)
    return result


def x_article_url(tweet: dict[str, Any]) -> str:
    containers = [tweet.get("note_tweet", {}).get("entities", {}), tweet.get("entities", {})]
    for entities in containers:
        for item in entities.get("urls", []):
            for key in ("unwound_url", "expanded_url"):
                value = str(item.get(key, ""))
                if re.search(r"x\.com/i/article/\d+", value):
                    return value.replace("http://", "https://", 1)
    return ""


def clean_x_article_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(
        char for char in value
        if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf"}
    )
    value = "\n".join(line.rstrip() for line in value.splitlines())
    return re.sub(r"\n{4,}", "\n\n\n", value).strip()


ARTICLE_IMAGE_ANCHOR_CHARS = 40


def x_article_images(article: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Article 的封面和插图只存在于详情接口的 includes 里，正文纯文本不带它们。"""
    if not article:
        return []
    media_by_key = article.get("media_by_key") or {}
    raw_text = str(article.get("raw_plain_text") or article.get("plain_text") or "")
    images: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(ref: Any, role: str) -> None:
        entity = ref if isinstance(ref, dict) else {}
        key = str(
            entity.get("media_key")
            or entity.get("id")
            or (ref if isinstance(ref, str) else "")
        ).strip()
        if not key or key in seen:
            return
        seen.add(key)
        item = dict(media_by_key.get(key, {}))
        item["media_key"] = key
        item["role"] = role
        start = entity.get("start")
        if isinstance(start, int) and 0 < start <= len(raw_text):
            head = raw_text[max(0, start - ARTICLE_IMAGE_ANCHOR_CHARS):start]
            anchor = re.sub(r"\s+", " ", head).strip()
            if anchor:
                item["anchor"] = anchor
        images.append(item)

    cover = article.get("cover_media")
    for ref in cover if isinstance(cover, list) else [cover]:
        if ref:
            add(ref, "cover")
    entities = article.get("media_entities")
    if isinstance(entities, list):
        for ref in entities:
            add(ref, "body")
    return images


def fetch_x_article(tweet_id: str, config: dict[str, Any]) -> dict[str, Any]:
    endpoint = (
        f"/2/tweets/{tweet_id}"
        "?tweet.fields=article,author_id,created_at,entities,public_metrics"
        "&expansions=article.cover_media,article.media_entities,author_id"
        # 只声明 expansions 不声明 media.fields 时，includes.media 只回 media_key，图片地址全丢。
        "&media.fields=media_key,type,url,preview_image_url,alt_text,width,height"
    )
    payload = run_xurl_request(config, [endpoint])
    article = payload.get("data", {}).get("article", {})
    raw_text = str(article.get("plain_text", ""))
    article["plain_text"] = clean_x_article_text(raw_text)
    article["raw_plain_text"] = raw_text
    article["media_by_key"] = {
        str(item["media_key"]): item
        for item in payload.get("includes", {}).get("media", [])
        if item.get("media_key")
    }
    # 问过详情接口才算问过配图；"没问过"和"真的没有图"必须分得开。
    article["media_resolved"] = True
    return article


def existing_x_path(tweet_id: str) -> Path | None:
    matches = sorted(path for path in X_DIR.rglob("*.md") if x_id_of(path) == tweet_id)
    return matches[0] if matches else None


def tweet_snippet_title(text: str, limit: int = 40) -> str:
    """短推没有标题时，用清洗后的正文首句当作可识别标题。"""
    cleaned = re.sub(r"https?://\S+", "", text)
    cleaned = re.sub(r"[\[\]#^|*_`>]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    sentence = re.split(r"[。！？!?\n]", cleaned, maxsplit=1)[0].strip()
    candidate = (sentence or cleaned)[:limit].rstrip("，,;；:： ")
    return candidate


def preserved_x_notes(existing_text: str) -> str:
    """Keep hand-written notes while replacing an old machine-captured source body."""
    if not existing_text:
        return ""
    explicit = markdown_section(existing_text, "个人笔记")
    if parse_frontmatter(existing_text).get("capture_source") == "x-bookmark":
        return f"## 个人笔记\n\n{explicit}" if explicit else ""

    body = existing_text
    if body.startswith("---\n"):
        end = body.find("\n---\n", 4)
        if end >= 0:
            body = body[end + 5:]
    body = re.sub(r"^# .+\n+", "", body.lstrip(), count=1)
    source_heading = re.search(
        r"^## (?:原文全文|原文|推文|Article 原文|数据)\s*$", body, flags=re.M
    )
    if not source_heading:
        return f"## 个人笔记\n\n{explicit}" if explicit else ""
    prefix = body[:source_heading.start()]
    kept: list[str] = []
    boilerplate = re.compile(
        r"^\s*(?:[-*]\s*)?\*{0,2}(?:作者|来源|日期|收藏时间|采集日期|数据|Author|Source|Date|Type|Engagement)\*{0,2}\s*[:：]",
        flags=re.I,
    )
    for line in prefix.splitlines():
        if line.strip() == "---" or boilerplate.match(line):
            continue
        kept.append(line.rstrip())
    value = "\n".join(kept).strip()
    value = re.sub(r"\n{3,}", "\n\n", value)
    if not value:
        return ""
    value = re.sub(r"^(#{2,5})(\s+)", r"\1#\2", value, flags=re.M)
    return f"## 个人笔记\n\n{value}"


def legacy_x_notes_from_git(path: Path) -> str:
    """Recover pre-migration notes from the tracked version during one-time repair."""
    try:
        source_rel = path.relative_to(VAULT).as_posix()
    except ValueError:
        return ""
    proc = subprocess.run(
        ["git", "show", "HEAD:" + source_rel],
        cwd=VAULT,
        text=True,
        capture_output=True,
        timeout=10,
    )
    return preserved_x_notes(proc.stdout) if proc.returncode == 0 else ""


def x_bookmark_needs_refresh(
    existing_text: str,
    tweet: dict[str, Any],
    article: dict[str, Any] | None,
    media_by_key: dict[str, dict[str, Any]] | None = None,
) -> bool:
    meta = parse_frontmatter(existing_text)
    if meta.get("capture_version") != "2" or not (
        meta.get("source_url") or meta.get("source")
    ):
        return True
    if meta.get("capture_status") != "complete":
        return True
    full_text = x_full_text(tweet)
    if full_text and full_text not in existing_text:
        return True
    media_count = len(x_media_items(tweet, media_by_key))
    if meta.get("attachment_count") != str(media_count):
        return True
    article_body = clean_x_article_text(
        str((article or {}).get("plain_text") or tweet.get("article", {}).get("plain_text") or "")
    )
    if article_body and (
        meta.get("article_fetch_status") != "success"
        or meta.get("article_chars") != str(len(article_body))
        or article_body not in existing_text
    ):
        return True
    if (article or {}).get("media_resolved"):
        if meta.get("article_image_count") != str(len(x_article_images(article))):
            return True
    return False


def write_x_bookmark(
    tweet: dict[str, Any],
    username: str,
    article: dict[str, Any] | None,
    article_error: str = "",
    captured_at: str = "",
    media_by_key: dict[str, dict[str, Any]] | None = None,
    notes_override: str | None = None,
) -> Path:
    tweet_id = str(tweet["id"])
    created = tweet.get("created_at", "")
    day = created[:10] or datetime.now().strftime("%Y-%m-%d")
    metrics = tweet.get("public_metrics", {})
    full_text = x_full_text(tweet)
    expanded = x_expanded_urls(tweet)
    media = x_media_items(tweet, media_by_key)
    article_images = x_article_images(article)
    article_url = x_article_url(tweet)
    article_hint = tweet.get("article", {})
    article_title = str((article or {}).get("title") or article_hint.get("title") or "").strip()
    article_body = clean_x_article_text(
        str((article or {}).get("plain_text") or article_hint.get("plain_text") or "")
    )
    has_article = bool(article_hint or article_url)
    display_title = (
        article_title
        or tweet_snippet_title(full_text)
        or f"@{username} — {tweet_id}"
    )
    if article_body:
        article_status = "success"
    elif article_error:
        article_status = "failed"
    elif has_article:
        article_status = "unavailable"
    else:
        article_status = "not_applicable"
    # frontmatter 每次同步都整个重写，所以下游写进来的状态得先读出来再放回去：
    # 一次 article 补抓就能把「已处理」和发布去向抹掉。
    path = existing_x_path(tweet_id)
    existing_text = path.read_text(encoding="utf-8", errors="replace") if path else ""
    carried = parse_frontmatter(existing_text)
    processing_status = carried.get("processing_status", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", processing_status):
        processing_status = "unprocessed"
    published_file = carried.get("published_file", "").strip()
    lines = [
        "---",
        f"title: {yaml_string(display_title)}",
        f"source_url: {yaml_string(x_source_url(tweet_id, username))}",
        f"author: {yaml_string('@' + username)}",
        f"published_at: {yaml_string(created)}",
        f"created_at: {yaml_string(captured_at or now_local())}",
        "tags:",
        "  - x-bookmark",
    ]
    if has_article:
        lines.append("  - x-article")
    lines.extend([
        "capture_source: x-bookmark",
        f"capture_id: {yaml_string('x:' + tweet_id)}",
        f"processing_status: {processing_status}",
        "capture_version: 2",
        "capture_status: complete",
        f"attachment_count: {len(media)}",
        f"tweet_id: {yaml_string(tweet_id)}",
    ])
    if published_file:
        lines.append(f"published_file: {yaml_string(published_file)}")
    if has_article:
        lines.extend([
            "content_type: x-article",
            f"article_title: {yaml_string(article_title)}",
            f"article_url: {yaml_string(article_url)}",
            f"article_fetch_status: {article_status}",
            f"article_chars: {len(article_body)}",
        ])
        # 只有真的问过详情接口才敢写这个数，否则下次同步会以为已经问过。
        if (article or {}).get("media_resolved"):
            lines.append(f"article_image_count: {len(article_images)}")
    notes = preserved_x_notes(existing_text) if notes_override is None else notes_override
    lines.extend(["---", "", f"# {display_title}"])
    if notes:
        lines.extend(["", notes])
    lines.extend(["", "## 推文", "", full_text])
    if article_body:
        lines.extend(["", "## Article 原文", "", article_body])
    elif has_article:
        lines.extend(["", "## Article 原文", "", "正文暂未获取，系统会在下次同步时重试。"])
    if article_images:
        lines.extend(["", "## Article 配图", ""])
        body_index = 0
        for item in article_images:
            if item.get("role") == "cover":
                label = "封面"
            else:
                body_index += 1
                label = f"插图 {body_index}"
            address = item.get("url") or item.get("preview_image_url") or ""
            lines.append(
                f"- {label}：{address}"
                if address
                else f"- {label}：地址未返回（media_key {item.get('media_key', '')}）"
            )
            if item.get("anchor"):
                lines.append(f"  - 位置：接在「{item['anchor']}」之后")
            if item.get("alt_text"):
                lines.append(f"  - 说明：{item['alt_text']}")
    if article_url:
        lines.extend(["", "## Article 链接", "", article_url])
    lines.extend([
        "", "## 数据", "",
        f"- 点赞：{metrics.get('like_count', 0)}",
        f"- 转发：{metrics.get('retweet_count', 0)}",
        f"- 回复：{metrics.get('reply_count', 0)}",
        f"- 收藏：{metrics.get('bookmark_count', 0)}",
        f"- 浏览：{metrics.get('impression_count', 0)}",
    ])
    if media:
        lines.extend(["", "## 附件", ""])
        type_indexes: dict[str, int] = {}
        for item in media:
            media_type = str(item.get("type", "photo"))
            type_indexes[media_type] = type_indexes.get(media_type, 0) + 1
            index = type_indexes[media_type]
            suffix = "video" if media_type in {"video", "animated_gif"} else "photo"
            attachment_url = f"{x_source_url(tweet_id, username)}/{suffix}/{index}"
            label = "视频" if suffix == "video" else "图片"
            lines.append(f"- {label} {index}：{attachment_url}")
            direct_url = item.get("url") or item.get("preview_image_url")
            if direct_url:
                lines.append(f"  - 预览：{direct_url}")
            if item.get("alt_text"):
                lines.append(f"  - 说明：{item['alt_text']}")
    if expanded:
        lines.extend(["", "## 附带链接", ""] + [f"- {value}" for value in expanded])
    # 已有文件（如 article 补抓重写）时复用原文件名，保证既有引用不断
    if path is None:
        month_dir = X_DIR / day[:7]
        month_dir.mkdir(parents=True, exist_ok=True)
        base = sanitize_filename(display_title, limit=48)
        path = month_dir / f"{day}-{base}.md"
        counter = 2
        while path.exists():
            path = month_dir / f"{day}-{base}（{counter}）.md"
            counter += 1
    rendered = "\n".join(lines)
    path.write_text(
        "\n".join(line.rstrip() for line in rendered.splitlines()) + "\n",
        encoding="utf-8",
    )
    return path


def sync_x(state: dict[str, Any], config: dict[str, Any]) -> int:
    payload = run_xurl(config)
    users = {u["id"]: u for u in payload.get("includes", {}).get("users", [])}
    media_by_key = {
        str(item["media_key"]): item
        for item in payload.get("includes", {}).get("media", [])
        if item.get("media_key")
    }
    known = set(state["known_x_ids"]) | known_local_x_ids()
    first_remote_sync = not state.get("x_remote_bootstrapped", False)
    bootstrap_limit = int(config.get("x_bootstrap_import_latest", 5))
    imported = 0
    for index, tweet in enumerate(payload.get("data", [])):
        tweet_id = str(tweet["id"])
        author = users.get(str(tweet.get("author_id")), {})
        username = author.get("username", tweet.get("author_id", "unknown"))
        existing_path = existing_x_path(tweet_id)
        article_hint = tweet.get("article", {})
        if tweet_id in known:
            if not existing_path:
                continue
            existing_text = existing_path.read_text(encoding="utf-8", errors="replace")
            article = article_hint if article_hint.get("plain_text") else None
            try:
                if article_hint and not article:
                    article = fetch_x_article(tweet_id, config)
                if (
                    article_hint
                    and not (article or {}).get("media_resolved")
                    and "article_image_count" not in parse_frontmatter(existing_text)
                ):
                    try:
                        article = fetch_x_article(tweet_id, config)
                    except Exception as exc:
                        log(f"x article media fetch failed {tweet_id}: {exc}")
                if not x_bookmark_needs_refresh(existing_text, tweet, article, media_by_key):
                    continue
                existing_meta = parse_frontmatter(existing_text)
                captured_at = (
                    existing_meta.get("created_at", "")
                    or existing_meta.get("created", "")
                )
                path = write_x_bookmark(
                    tweet, username, article, captured_at=captured_at, media_by_key=media_by_key
                )
                source_rel = rel(path)
                item_id = "x:" + tweet_id
                previous = state["reviewed_sources"].pop(item_id, None)
                if previous and previous.get("review"):
                    state["review_replacements"][item_id] = previous["review"]
                if source_rel not in state["pending_sources"]:
                    state["pending_sources"].append(source_rel)
                imported += 1
            except Exception as exc:
                log(f"x bookmark refresh failed {tweet_id}: {exc}")
            continue
        # Start from "now": import a small real sample, baseline the older remote backlog.
        if first_remote_sync and index >= bootstrap_limit:
            known.add(tweet_id)
            continue
        article = None
        article_error = ""
        if article_hint:
            try:
                article = fetch_x_article(tweet_id, config)
            except Exception as exc:
                article = article_hint if article_hint.get("plain_text") else None
                if not article:
                    article_error = str(exc)
                log(f"x article fetch failed {tweet_id}: {exc}")
        path = write_x_bookmark(
            tweet, username, article, article_error=article_error, media_by_key=media_by_key
        )
        source_rel = rel(path)
        if source_rel not in state["pending_sources"]:
            state["pending_sources"].append(source_rel)
        known.add(tweet_id)
        imported += 1
    state["known_x_ids"] = sorted(known)
    state["x_remote_bootstrapped"] = True
    return imported


def repair_x(state: dict[str, Any], config: dict[str, Any]) -> dict[str, int]:
    """Refresh existing local X bookmarks without importing or reviewing new remote items."""
    local_ids = sorted(existing_x_ids())
    payloads: list[dict[str, Any]] = []
    for start in range(0, len(local_ids), 100):
        ids = ",".join(local_ids[start:start + 100])
        endpoint = (
            f"/2/tweets?ids={ids}"
            "&tweet.fields=article,attachments,author_id,created_at,entities,note_tweet,public_metrics"
            "&expansions=attachments.media_keys,author_id"
            "&media.fields=media_key,type,url,preview_image_url,alt_text"
        )
        payloads.append(run_xurl_request(config, [endpoint]))
    users = {
        str(u["id"]): u
        for payload in payloads
        for u in payload.get("includes", {}).get("users", [])
    }
    media_by_key = {
        str(item["media_key"]): item
        for payload in payloads
        for item in payload.get("includes", {}).get("media", [])
        if item.get("media_key")
    }
    remote_ids: set[str] = set()
    repaired = 0
    for tweet in (tweet for payload in payloads for tweet in payload.get("data", [])):
        tweet_id = str(tweet["id"])
        remote_ids.add(tweet_id)
        path = existing_x_path(tweet_id)
        if not path:
            continue
        existing_text = path.read_text(encoding="utf-8", errors="replace")
        notes = preserved_x_notes(existing_text) or legacy_x_notes_from_git(path)
        article_hint = tweet.get("article", {})
        article = article_hint if article_hint.get("plain_text") else None
        article_error = ""
        if article_hint:
            try:
                article = fetch_x_article(tweet_id, config)
            except Exception as exc:
                if not article:
                    article_error = str(exc)
        author = users.get(str(tweet.get("author_id")), {})
        username = str(author.get("username") or tweet.get("author_id") or "unknown")
        existing_meta = parse_frontmatter(existing_text)
        captured_at = (
            existing_meta.get("created_at", "")
            or existing_meta.get("created", "")
        )
        write_x_bookmark(
            tweet,
            username,
            article,
            article_error=article_error,
            captured_at=captured_at,
            media_by_key=media_by_key,
            notes_override=notes,
        )
        repaired += 1

    unavailable = 0
    for path in sorted(X_DIR.rglob("*.md")):
        tweet_id = x_id_of(path)
        if not tweet_id or tweet_id in remote_ids:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        updates = {
            "source_url": x_source_url(tweet_id),
            "capture_source": "x-bookmark",
            "capture_id": "x:" + tweet_id,
            "capture_version": "2",
            "capture_status": "source-unavailable",
        }
        # processing_status 只补不重置：书签从远端消失，通常正是因为用过之后取消了收藏。
        # 在这里重置就等于每跑一次修复，把所有已处理的卡都打回未处理。
        if not parse_frontmatter(text).get("processing_status", "").strip():
            updates["processing_status"] = "unprocessed"
        path.write_text(update_frontmatter(text, updates), encoding="utf-8")
        unavailable += 1
    state["known_x_ids"] = sorted(known_local_x_ids())
    save_state(state)
    return {"repaired": repaired, "unavailable": unavailable}


# ---------------------------------------------------------- X Article 发布检测
# 草稿推入 X 草稿箱后，人会在平台上手动点发布；发布成功的 Article 会以帖子形式
# 出现在自己的时间线上。这里定期比对时间线，把「已发布」自动登记回知识库。
# 检测失败或不确定时只记日志不动文件，工作台「登记发布」表单是人工兜底。


def parse_iso_moment(value: str) -> datetime | None:
    raw = str(value or "").strip().strip('"')
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.astimezone()


def normalize_title(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(value or ""))).strip()


def draft_display_title(text: str, meta: dict[str, str]) -> str:
    title = meta.get("title", "").strip()
    if title:
        return title
    match = re.search(r"^# (.+)$", text, flags=re.M)
    return match.group(1).strip() if match else ""


def resolve_vault_wikilink(value: str) -> Path | None:
    match = re.match(r"\s*\"?\[\[([^\]|#]+)", value or "")
    target = match.group(1).strip() if match else ""
    if not target:
        return None
    candidate = VAULT / (target if target.endswith(".md") else target + ".md")
    try:
        candidate = candidate.resolve()
        candidate.relative_to(VAULT)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def x_article_pending_drafts(now: datetime) -> list[dict[str, Any]]:
    """严格门控：只有 72 小时内推过 X 草稿箱、还没确认发布的草稿才允许触发 API 调用。"""
    pending: list[dict[str, Any]] = []
    for root in (ARTICLE_DRAFT_DIR, ORAL_DRAFT_DIR):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta = parse_frontmatter(text)
            if meta.get("x_article_status", "").strip() != "draft":
                continue
            saved_at = parse_iso_moment(meta.get("x_article_saved_at", ""))
            if saved_at is None:
                log(f"x article detect: 无法解析 x_article_saved_at，跳过 {rel(path)}")
                continue
            if now - saved_at > timedelta(hours=X_ARTICLE_DETECT_WINDOW_HOURS):
                continue
            title = draft_display_title(text, meta)
            if not title:
                log(f"x article detect: 草稿没有标题，无法匹配，跳过 {rel(path)}")
                continue
            pending.append({"path": path, "meta": meta, "title": title, "saved_at": saved_at})
    return pending


def fetch_x_own_posts(config: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """一次调用拉自己最近的帖子，覆盖本轮全部待确认草稿。"""
    me = run_xurl_request(config, ["/2/users/me"])
    data = me.get("data", {})
    user_id = str(data.get("id", ""))
    username = str(data.get("username", "")).strip()
    if not user_id or not username:
        raise RuntimeError("X API did not return the authenticated user id/username")
    max_results = max(5, min(100, int(config.get("x_timeline_max_results", 50))))
    endpoint = (
        f"/2/users/{user_id}/tweets?max_results={max_results}"
        "&exclude=replies,retweets"
        "&tweet.fields=article,created_at"
    )
    payload = run_xurl_request(config, [endpoint])
    return username, payload.get("data", [])


# 发布前人常会在 X 编辑器里微调标题（实测：「…1 份可直接用的模板」发布成
# 「…1 份模板」），精确匹配追不上这种改动，所以放一档高相似度兜底。
# 阈值取 0.75：副标题级别的删改仍在 0.85 以上，只共享主标题的两篇约 0.5。
X_ARTICLE_TITLE_SIMILARITY = 0.75


def match_x_article_post(
    item: dict[str, Any], posts: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str]:
    """帖子时间晚于 x_article_saved_at 且 Article 标题匹配；精确命中优先于
    相似命中，无论哪一档都要求唯一，多条命中算不确定，留给人工登记。"""
    wanted = normalize_title(item["title"])
    exact: list[dict[str, Any]] = []
    fuzzy: list[dict[str, Any]] = []
    for post in posts:
        title = normalize_title(post.get("article", {}).get("title", ""))
        if not title:
            continue
        created = parse_iso_moment(post.get("created_at", ""))
        if created is None or created <= item["saved_at"]:
            continue
        if title == wanted:
            exact.append(post)
        elif (
            difflib.SequenceMatcher(None, wanted, title).ratio()
            >= X_ARTICLE_TITLE_SIMILARITY
        ):
            fuzzy.append(post)
    pool = exact or fuzzy
    if not pool:
        return None, "not-found"
    if len(pool) > 1:
        return None, "ambiguous"
    return pool[0], ""


def find_published_by_content_id(content_id: str) -> Path | None:
    if not content_id or not PUBLISHED_ROOT.exists():
        return None
    for path in sorted(PUBLISHED_ROOT.rglob("*.md")):
        if rel(path).startswith(FEEDBACK_REL_PREFIX):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if parse_frontmatter(text).get("content_id", "") == content_id:
            return path
    return None


def resolve_published_article(meta: dict[str, str]) -> Path | None:
    article = resolve_vault_wikilink(meta.get("published_file", ""))
    if article is not None and rel(article).startswith("40-发布/"):
        return article
    return find_published_by_content_id(meta.get("content_id", "").strip())


def ensure_published_article(
    draft_path: Path, text: str, meta: dict[str, str], dry_run: bool
) -> tuple[Path, bool]:
    """找到草稿对应的正式稿；缺正式稿时先归档正式稿（口径同仪表盘 record_publication）。"""
    article = resolve_published_article(meta)
    if article is not None:
        return article, False
    rule = X_PUBLISH_ARCHIVE_RULES.get(meta.get("kind", "").strip())
    if rule is None:
        raise RuntimeError(f"kind={meta.get('kind', '')!r} 不在正式稿归档规则里")
    subdir, published_kind = rule
    target = PUBLISHED_ROOT / subdir / draft_path.name
    if target.exists():
        raise RuntimeError(f"40-发布/{subdir} 已有同名文件但 content_id 对不上，留给人工确认")
    if not dry_run:
        updated = update_frontmatter(
            text,
            {
                "kind": published_kind,
                "status": "published",
                "draft_source": f"[[{rel(draft_path)[:-3]}]]",
            },
            render=frontmatter_value,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(updated, encoding="utf-8")
    return target, True


def run_record_published(article: Path, url: str, published_at: str, dry_run: bool) -> str:
    """record_published.py 会校验公开地址（拒绝草稿编辑器地址）并完成正式稿与源头书签归档。"""
    if not RECORD_PUBLISHED_SCRIPT.is_file():
        raise RuntimeError(f"找不到发布归档脚本：{RECORD_PUBLISHED_SCRIPT}")
    cmd = [
        sys.executable, str(RECORD_PUBLISHED_SCRIPT),
        "--article", rel(article), "--platform", "x",
        "--url", url, "--published-at", published_at,
    ]
    if dry_run:
        cmd.append("--dry-run")
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=120,
        env={**os.environ, "RAYS_BRAIN": str(VAULT)},
        cwd=str(RECORD_PUBLISHED_SCRIPT.parent),
    )
    if proc.returncode != 0:
        detail = " / ".join(line.strip() for line in proc.stderr.splitlines() if line.strip())
        raise RuntimeError(detail[-500:] or "record_published failed")
    return proc.stdout


def writeback_draft_and_pack(
    draft_path: Path, article: Path, url: str, published_at: str, dry_run: bool
) -> list[str]:
    """把发布结果回写草稿与成稿包；已是 published 的字段不重写，人工填过的不覆盖。"""
    link = f"[[{rel(article)[:-3]}]]"
    updated: list[str] = []
    text = draft_path.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)
    updates: dict[str, str] = {}
    if meta.get("x_article_status", "").strip() != "published":
        updates["x_article_status"] = "published"
    if url and meta.get("x_article_url", "").strip() != url:
        updates["x_article_url"] = url
    if published_at and not meta.get("x_article_published_at", "").strip():
        updates["x_article_published_at"] = published_at
    if meta.get("status", "").strip() != "published":
        updates["status"] = "published"
    if meta.get("published_file", "").strip().strip('"') != link:
        updates["published_file"] = link
    if updates:
        if not dry_run:
            draft_path.write_text(
                update_frontmatter(text, updates, render=frontmatter_value),
                encoding="utf-8",
            )
        updated.append(rel(draft_path))
    pack = resolve_vault_wikilink(meta.get("content_pack", ""))
    if pack is not None:
        try:
            pack_text = pack.read_text(encoding="utf-8")
            if parse_frontmatter(pack_text).get("status", "").strip() != "published":
                if not dry_run:
                    pack.write_text(
                        update_frontmatter(
                            pack_text,
                            {"status": "published", "published_file": link},
                            render=frontmatter_value,
                        ),
                        encoding="utf-8",
                    )
                updated.append(rel(pack))
        except (OSError, ValueError):
            pass
    return updated


def handle_x_article_hit(item: dict[str, Any], url: str, published_at: str, dry_run: bool) -> None:
    path = item["path"]
    text = path.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)
    article, created = ensure_published_article(path, text, meta, dry_run)
    if dry_run and created:
        log(f"x article detect dry-run: 会先归档正式稿 {rel(article)} 再登记 {url}")
    else:
        report = run_record_published(article, url, published_at, dry_run)
        for line in report.splitlines():
            if line.strip():
                log(f"record_published: {line.rstrip()}")
    updated = writeback_draft_and_pack(path, article, url, published_at, dry_run)
    prefix = "x article detect dry-run: 检测到已发布" if dry_run else "x article detect: 已登记发布"
    log(f"{prefix} {rel(path)} → {url}（回写 {', '.join(updated) or '无需改动'}）")


def detect_x_article_publications(config: dict[str, Any], dry_run: bool = False) -> dict[str, int]:
    result = {"pending": 0, "reconciled": 0, "published": 0, "unmatched": 0, "errors": 0}
    now = datetime.now().astimezone()
    drafts = x_article_pending_drafts(now)
    if not drafts:
        return result
    remaining: list[dict[str, Any]] = []
    for item in drafts:
        # 先本地对账：正式稿已经登记过 X 发布（比如走了工作台表单），
        # 只需把草稿这头的门关上，不必再调 API。
        article = resolve_published_article(item["meta"])
        if article is not None:
            ameta = parse_frontmatter(article.read_text(encoding="utf-8", errors="replace"))
            if ameta.get("x_article_status", "").strip() == "published":
                updated = writeback_draft_and_pack(
                    item["path"],
                    article,
                    ameta.get("x_article_url", "").strip(),
                    ameta.get("x_article_published_at", "").strip(),
                    dry_run,
                )
                result["reconciled"] += 1
                suffix = "（dry-run 未写入）" if dry_run else ""
                log(
                    f"x article detect: 正式稿已登记过发布，关闭草稿检测门 "
                    f"{rel(item['path'])}（回写 {', '.join(updated) or '无需改动'}）{suffix}"
                )
                continue
        remaining.append(item)
    result["pending"] = len(remaining)
    if not remaining:
        return result
    username, posts = fetch_x_own_posts(config)
    claimed: set[str] = set()
    for item in remaining:
        post, reason = match_x_article_post(item, posts)
        if post is not None and str(post["id"]) in claimed:
            # 一条帖子只可能对应一篇文章；被两篇草稿同时匹配到就是不确定。
            log(
                f"x article detect: 帖子已归属本轮另一篇草稿，不确定，留给人工登记 "
                f"{rel(item['path'])}"
            )
            result["unmatched"] += 1
            continue
        if post is None:
            if reason == "ambiguous":
                log(
                    f"x article detect: 标题匹配到多条帖子，不确定，留给人工登记 "
                    f"{rel(item['path'])}"
                )
            result["unmatched"] += 1
            continue
        created = parse_iso_moment(post.get("created_at", ""))
        if created is None:
            log(f"x article detect: 帖子缺少 created_at，留给人工登记 {rel(item['path'])}")
            result["unmatched"] += 1
            continue
        url = x_source_url(str(post["id"]), username)
        # 匹配成功就占住这条帖子：即使登记失败，也不能让下一篇草稿接盘。
        claimed.add(str(post["id"]))
        try:
            handle_x_article_hit(item, url, created.isoformat(timespec="seconds"), dry_run)
            result["published"] += 1
        except Exception as exc:
            result["errors"] += 1
            log(f"x article detect: 登记发布失败，留给人工登记 {rel(item['path'])}: {exc}")
    if result["unmatched"]:
        log(f"x article detect: {result['unmatched']} 篇草稿本轮未发现已发布帖子")
    return result


class ArticleExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.in_title = False
        self.skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self.in_title = True
        if tag in {"script", "style", "svg", "noscript", "nav", "footer"}:
            self.skip_depth += 1
        if tag in {"p", "div", "article", "section", "h1", "h2", "h3", "li", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in {"script", "style", "svg", "noscript", "nav", "footer"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        if not self.skip_depth:
            value = re.sub(r"\s+", " ", data).strip()
            if value:
                self.parts.append(value)

    def text(self) -> str:
        value = " ".join(self.parts)
        value = re.sub(r" *\n *", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


def fetch_url(url: str, max_bytes: int = 2_000_000) -> tuple[str, str, str]:
    proc = subprocess.run(
        [
            "/usr/bin/curl", "-fsSL", "--max-time", "25", "--max-filesize", str(max_bytes),
            "-A", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) KnowledgeCapture/1.0", url,
        ],
        capture_output=True,
        timeout=35,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace").strip() or "curl failed")
    raw = proc.stdout[:max_bytes]
    source = raw.decode("utf-8", errors="replace")
    extractor = ArticleExtractor()
    extractor.feed(source)
    return extractor.title.strip(), extractor.text(), "text/html"


URL_RE = re.compile(r"https?://[^\s<>\]\)]+")


def mobile_lines(config: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    mobile_inbox = str(config.get("mobile_inbox", "")).strip()
    if mobile_inbox:
        mobile_file = Path(mobile_inbox).expanduser()
        if mobile_file.exists():
            try:
                lines.extend(
                    mobile_file.read_text(encoding="utf-8", errors="replace").splitlines()
                )
            except OSError as exc:
                # iCloud Drive 偶尔会在文件同步占用时返回 Resource deadlock。
                # 手机收件箱暂时不可读不应中断 X、网页和本地链接收件箱的处理。
                log(f"mobile inbox temporarily unavailable {mobile_file}: {exc}")
    if LINK_INBOX.exists():
        inbox_text = LINK_INBOX.read_text(encoding="utf-8", errors="replace")
        inbox_text = re.sub(r"```.*?```", "", inbox_text, flags=re.S)
        for line in inbox_text.splitlines():
            if URL_RE.search(line):
                lines.append(line)
    return [line.strip() for line in lines if URL_RE.search(line)]


def sync_mobile(state: dict[str, Any], config: dict[str, Any]) -> int:
    known = set(state["mobile_line_hashes"])
    imported = 0
    for line in mobile_lines(config):
        line_hash = hashlib.sha256(line.encode()).hexdigest()
        if line_hash in known:
            continue
        match = URL_RE.search(line)
        if not match:
            continue
        url = match.group(0).rstrip(".,;，。；")
        note = (line[:match.start()] + line[match.end():]).strip(" -[]\t")
        fetched_at = now_local()
        try:
            title, body, content_type = fetch_url(url)
            fetch_status = "success" if len(body) >= 100 else "partial"
        except Exception as exc:
            title, body, content_type = "", "", ""
            fetch_status = "failed"
            log(f"mobile fetch failed {url}: {exc}")
        title = title or note or url.split("/")[2]
        short_hash = line_hash[:8]
        day = datetime.now().strftime("%Y-%m-%d")
        # 状态文件丢了会让同一条分享再走一遍这里。认得出旧卡才不会多出一张只有日期
        # 不同的重复卡，也才不会把已经审过的状态打回未处理——短哈希是它的稳定身份。
        existing = next(iter(sorted(WEB_DIR.glob(f"*-{short_hash}.md"))), None)
        path = existing or WEB_DIR / f"{day}-{sanitize_filename(title)}-{short_hash}.md"
        carried = (
            parse_frontmatter(existing.read_text(encoding="utf-8", errors="replace"))
            if existing
            else {}
        )
        processing_status = carried.get("processing_status", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", processing_status):
            processing_status = "unprocessed"
        lines = [
            "---",
            f"title: {yaml_string(title)}",
            f"source_url: {yaml_string(url)}",
            f"created_at: {yaml_string(fetched_at)}",
            "tags:",
            "  - mobile-share",
            "capture_source: mobile-share",
            f"capture_id: {yaml_string('mobile:' + line_hash)}",
            f"processing_status: {processing_status}",
            f"fetch_status: {fetch_status}",
            f"content_type: {yaml_string(content_type)}",
            "---",
            "",
            f"# {title}",
            "",
        ]
        if note:
            lines.extend([f"> 分享备注：{note}", ""])
        if body:
            lines.append(body[:120_000])
        else:
            lines.extend(["正文未能自动抓取，复核时请打开原始链接。", "", url])
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        source_rel = rel(path)
        state["known_web_files"].append(source_rel)
        if source_rel not in state["pending_sources"]:
            state["pending_sources"].append(source_rel)
        known.add(line_hash)
        imported += 1
    state["mobile_line_hashes"] = sorted(known)
    state["known_web_files"] = sorted(set(state["known_web_files"]))
    return imported


def discover_web_clips(state: dict[str, Any]) -> int:
    known = set(state["known_web_files"])
    discovered = 0
    for path in WEB_DIR.glob("*.md"):
        normalize_web_clip_frontmatter(path)
        source_rel = rel(path)
        if source_rel in known:
            continue
        known.add(source_rel)
        if source_rel not in state["pending_sources"]:
            state["pending_sources"].append(source_rel)
        discovered += 1
    state["known_web_files"] = sorted(known)
    return discovered


def normalize_web_clip_frontmatter(path: Path) -> bool:
    """Keep Web Clipper sources aligned and avoid accidental unresolved author links."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if not text.startswith("---\n"):
        return False
    end = text.find("\n---\n", 4)
    if end < 0:
        return False
    lines = text[4:end].splitlines()
    existing_keys = {
        line.split(":", 1)[0].strip()
        for line in lines
        if ":" in line and not line.startswith((" ", "\t", "-"))
    }
    rename = {
        "source": "source_url",
        "published": "published_at",
        "created": "created_at",
    }
    changed = False
    in_author = False
    normalized: list[str] = []
    for line in lines:
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key, value = line.split(":", 1)
            key = key.strip()
            in_author = key == "author"
            replacement = rename.get(key)
            if replacement:
                if replacement in existing_keys:
                    changed = True
                    continue
                line = f"{replacement}:{value}"
                changed = True
        elif in_author:
            match = re.fullmatch(
                r'(\s*-\s*)["\']?\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]["\']?\s*',
                line,
            )
            if match:
                line = match.group(1) + yaml_string(match.group(2).strip())
                changed = True
        normalized.append(line)
    if not changed:
        return False
    updated = "---\n" + "\n".join(normalized) + text[end:]
    path.write_text(updated, encoding="utf-8")
    return True


def is_weread_card(path: Path, text: str) -> bool:
    meta = parse_frontmatter(text)
    capture_id = meta.get("capture_id", "")
    return (
        meta.get("capture_source") == "weread"
        or capture_id.startswith("weread:")
    )


def weread_source_key(path: Path, text: str) -> str:
    meta = parse_frontmatter(text)
    book_id = meta.get("book_id", "").strip()
    capture_id = meta.get("capture_id", "").strip()
    if not book_id and capture_id.startswith("weread:"):
        book_id = capture_id.split(":", 1)[1]
    return f"weread:{book_id}" if book_id else f"weread-file:{rel(path)}"


def weread_version_id(path: Path, text: str) -> str:
    """一本书的一次划线/想法版本；人工判断和处理状态不参与签名。"""
    meta = parse_frontmatter(text)
    match = re.search(
        r"<!--\s*weread:begin\b.*?-->(.*?)<!--\s*weread:end\s*-->",
        text,
        flags=re.S,
    )
    notes = match.group(1).strip() if match else markdown_section(text, "划线与想法")
    payload = "\n".join(
        [
            weread_source_key(path, text),
            meta.get("highlight_count", ""),
            meta.get("thought_count", ""),
            notes,
        ]
    )
    signature = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{weread_source_key(path, text)}:{signature}"


def discover_weread_cards(state: dict[str, Any]) -> int:
    """只把未处理的新版本送审；已处理书籍在首次接入时直接登记为基线。"""
    known = state.get("known_weread_versions")
    if not isinstance(known, dict):
        known = {}
    pending = state.setdefault("pending_sources", [])
    discovered = 0
    if not WEREAD_DIR.exists():
        state["known_weread_versions"] = known
        return discovered

    for path in sorted(WEREAD_DIR.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log(f"weread card temporarily unavailable {path}: {exc}")
            continue
        if not is_weread_card(path, text):
            continue
        meta = parse_frontmatter(text)
        source_rel = rel(path)
        source_key = weread_source_key(path, text)
        version_id = weread_version_id(path, text)
        processing_status = meta.get("processing_status", "").strip().lower()
        previous = known.get(source_key)

        if processing_status == "processed":
            # 第一次部署时，现有已处理书籍只登记版本，不回灌审核队列。
            known[source_key] = version_id
            if source_rel in pending:
                pending[:] = [item for item in pending if item != source_rel]
            continue
        if processing_status != "unprocessed" or previous == version_id:
            continue

        known[source_key] = version_id
        if source_rel not in pending:
            pending.append(source_rel)
        discovered += 1

    state["known_weread_versions"] = known
    return discovered


def parse_frontmatter(text: str) -> dict[str, str]:
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


# 草稿和成稿包是人工维护的文件：状态枚举一向裸值，链接和时间戳一向带引号。
# 渲染口径抄自 发布归档/record_published.py 的 render_value，两边 diff 才一致。
FRONTMATTER_BARE_CHAR = re.compile(r"[\w.\- ]|[^\x00-\x7f]", re.UNICODE)


def frontmatter_value(value: str) -> str:
    if (
        value
        and value == value.strip()
        and not value.startswith("-")
        and all(FRONTMATTER_BARE_CHAR.fullmatch(char) for char in value)
    ):
        return value
    return json.dumps(value, ensure_ascii=False)


def update_frontmatter(text: str, updates: dict[str, str], render=yaml_string) -> str:
    if not text.startswith("---\n"):
        raise ValueError("审核卡缺少页首属性")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("审核卡页首属性不完整")
    lines = text[4:end].splitlines()
    positions: dict[str, int] = {}
    for index, line in enumerate(lines):
        if ":" not in line or line.startswith((" ", "\t", "-")):
            continue
        key = line.split(":", 1)[0].strip()
        positions[key] = index
    for key, value in updates.items():
        rendered = f"{key}: {render(value)}"
        if key in positions:
            lines[positions[key]] = rendered
        else:
            lines.append(rendered)
    return "---\n" + "\n".join(lines) + text[end:]


def markdown_section(text: str, heading: str) -> str:
    match = re.search(
        rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)",
        text,
        flags=re.M | re.S,
    )
    return match.group(1).strip() if match else ""


def load_review_protocol() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    data = load_json(REVIEW_PROTOCOL_FILE, {})
    actions = data.get("actions", [])
    if not isinstance(actions, list) or not actions:
        raise RuntimeError(f"审核协议缺少 actions：{REVIEW_PROTOCOL_FILE}")
    labels: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for item in actions:
        key = str(item.get("key", "")).strip()
        label = str(item.get("label", "")).strip()
        if not key or not label or key in labels or label in aliases:
            raise RuntimeError("审核协议包含空值或重复的 key/label")
        labels[key] = label
        aliases[label] = key
        for alias in item.get("aliases", []):
            alias = str(alias).strip()
            if not alias or alias in aliases:
                raise RuntimeError(f"审核协议包含空值或重复别名：{alias!r}")
            aliases[alias] = key
    key_aliases = {
        str(key).strip(): str(value).strip()
        for key, value in data.get("key_aliases", {}).items()
    }
    if any(target not in labels for target in key_aliases.values()):
        raise RuntimeError("审核协议 key_aliases 指向不存在的动作")
    return labels, aliases, key_aliases


REVIEW_ACTION_LABELS, REVIEW_ACTIONS, REVIEW_ACTION_KEY_ALIASES = load_review_protocol()
CANONICAL_REVIEW_LABELS = list(REVIEW_ACTION_LABELS.values())


def checked_review_actions(text: str) -> list[str]:
    checked: list[str] = []
    for label, action in REVIEW_ACTIONS.items():
        if re.search(rf"^- \[[xX]\]\s+{re.escape(label)}\s*$", text, flags=re.M):
            checked.append(action)
    return list(dict.fromkeys(checked))


def preserve_review_decision(previous_text: str, refreshed_text: str) -> str:
    """A source refresh may update the analysis, but must not erase a human decision."""
    previous_meta = parse_frontmatter(previous_text)
    checked = checked_review_actions(previous_text)
    action = previous_meta.get("review_action", "")
    action = REVIEW_ACTION_KEY_ALIASES.get(action, action)
    if action not in REVIEW_ACTION_LABELS:
        action = checked[0] if len(checked) == 1 else ""
    if not action:
        return refreshed_text

    selected_label = REVIEW_ACTION_LABELS[action]
    for label in CANONICAL_REVIEW_LABELS:
        refreshed_text = re.sub(
            rf"^- \[[ xX]\]\s+{re.escape(label)}\s*$",
            f"- [{'x' if label == selected_label else ' '}] {label}",
            refreshed_text,
            flags=re.M,
        )

    if previous_meta.get("action_processed_at"):
        preserved = {
            key: previous_meta[key]
            for key in (
                "status",
                "review_action",
                "action_processed_at",
                "action_error",
                "result_file",
                "knowledge_file",
                "topic_file",
                "archived_at",
            )
            if key in previous_meta
        }
        refreshed_text = update_frontmatter(refreshed_text, preserved)
    return refreshed_text


KIND_BY_FOLDER = {
    "10-概念": "concept",
    "20-人物与组织": "entity",
    "30-问题": "question",
    "40-观点": "viewpoint",
    "50-案例": "case",
    "60-方法": "playbook",
    "70-知识地图": "map",
}


def knowledge_destination(meta: dict[str, str]) -> Path:
    defaults = {
        "概念": "20-知识/10-概念",
        "人物与组织": "20-知识/20-人物与组织",
        "问题": "20-知识/30-问题",
        "观点": "20-知识/40-观点",
        "案例": "20-知识/50-案例",
        "方法": "20-知识/60-方法",
    }
    raw = meta.get("suggested_destination") or defaults.get(
        meta.get("suggested_kind", ""), "20-知识/40-观点"
    )
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts or not raw.startswith("20-知识/"):
        candidate = Path(defaults.get(meta.get("suggested_kind", ""), "20-知识/40-观点"))
    return VAULT / candidate


def semantic_knowledge_title(text: str, fallback: str) -> str:
    title = re.sub(r"\s+", " ", fallback).strip()
    generic = bool(
        re.fullmatch(r"@[A-Za-z0-9_.-]+\s*[\u2014\-]\s*\d{18,20}", title)
        or re.fullmatch(r"\d{18,20}", title)
        or title.startswith(("http://", "https://"))
    )
    if not generic:
        return title
    summary = markdown_section(text, "摘要")
    candidate = re.split(r"[\u3002！？!?]", summary, maxsplit=1)[0].strip()
    candidate = re.sub(
        r"^(作者|文章|原文)(提出|认为|介绍|宣布|强调|描述|写道)",
        "",
        candidate,
    ).strip("：: ")
    return candidate[:42].rstrip("，,; ") or "待命名知识"


def existing_knowledge_for_capture(capture_id: str) -> Path | None:
    for path in KNOWLEDGE_DIR.rglob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if parse_frontmatter(text).get("capture_id") == capture_id:
            return path
    return None


def available_knowledge_path(destination: Path, title: str, capture_id: str) -> Path:
    existing = existing_knowledge_for_capture(capture_id)
    if existing:
        return existing
    base = sanitize_filename(title, limit=42)
    candidate = destination / f"{base}.md"
    counter = 2
    while candidate.exists():
        candidate = destination / f"{base}（{counter}）.md"
        counter += 1
    return candidate


def linkify_connections(body: str) -> str:
    """知识连接栏：条目能精确匹配现有知识笔记标题时写成 wiki 链接，匹配不到的保持纯文本（作为值得补写的空位）。"""
    titles = {
        unicodedata.normalize("NFC", path.stem)
        for path in KNOWLEDGE_DIR.rglob("*.md")
    }
    out_lines = []
    for line in body.splitlines():
        m = re.match(r"^(\s*[-*]\s+)(.+?)\s*$", line)
        if m and "[[" not in m.group(2):
            candidate = unicodedata.normalize("NFC", m.group(2).strip())
            if candidate in titles:
                line = f"{m.group(1)}[[{candidate}]]"
        out_lines.append(line)
    return "\n".join(out_lines)


def create_knowledge_note(card_path: Path, text: str, meta: dict[str, str]) -> Path:
    title_match = re.search(r"^# (.+)$", text, flags=re.M)
    raw_title = title_match.group(1).strip() if title_match else card_path.stem
    title = semantic_knowledge_title(text, raw_title)
    capture_id = meta.get("capture_id") or rel(card_path)
    destination = knowledge_destination(meta)
    destination.mkdir(parents=True, exist_ok=True)
    path = available_knowledge_path(destination, title, capture_id)
    if path.exists():
        return path
    sections = [
        ("核心摘要", markdown_section(text, "摘要")),
        ("关键观点", markdown_section(text, "核心观点")),
        ("支撑证据", markdown_section(text, "支撑证据")),
        ("边界与反例", markdown_section(text, "漏洞与反例")),
        ("知识连接", linkify_connections(markdown_section(text, "与现有知识的连接"))),
    ]
    card_rel = rel(card_path)
    source_file = meta.get("source_file", "")
    knowledge_id = "knowledge-" + hashlib.sha256(
        f"{capture_id}\n{title}".encode("utf-8")
    ).hexdigest()[:12]
    # kind 以目标文件夹推导，保持与《个人知识库工作流》的枚举一致（kind 是分类的唯一真源）
    kind = KIND_BY_FOLDER.get(destination.name, "concept")
    if "30-X书签" in source_file:
        origin = "x-bookmark"
    elif "40-网页剪藏" in source_file:
        origin = "web-clip"
    elif "50-读书划线" in source_file:
        origin = "weread"
    else:
        origin = "capture"
    lines = [
        "---",
        f"kind: {kind}",
        "status: seed",
        f"origin: {origin}",
        f"created_at: {yaml_string(now_local())}",
        f"updated_at: {yaml_string(now_local())}",
        f"knowledge_id: {knowledge_id}",
        f"capture_id: {yaml_string(capture_id)}",
        f"source_review: {yaml_string(card_rel)}",
        f"source_file: {yaml_string(source_file)}",
        f"source_url: {yaml_string(meta.get('source_url', ''))}",
        "---",
        "",
        f"# {title}",
    ]
    for heading, body in sections:
        lines.extend(["", f"## {heading}", "", body or "待补充"])
    lines.extend(["", "## 来源与复核", "", f"- 审核卡：[[{card_rel[:-3]}]]"])
    if source_file:
        lines.append(f"- 原始资料：[[{source_file[:-3]}]]")
    if meta.get("source_url"):
        lines.append(f"- 原始链接：{meta['source_url']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def int_score(meta: dict[str, str], key: str, fallback: int = 0) -> int:
    try:
        return max(0, min(100, int(float(meta.get(key, str(fallback))))))
    except (TypeError, ValueError):
        return fallback


def topic_review_after(time_sensitivity: str) -> str:
    days = TOPIC_FRESHNESS_DAYS.get(time_sensitivity, 14)
    return (datetime.now().astimezone().date() + timedelta(days=days)).isoformat()


def iso_date(value: str) -> date | None:
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0), "%Y-%m-%d").date()
    except ValueError:
        return None


def source_published_at(source_file: str, explicit: str = "") -> str:
    """返回素材真正进入世界的日期；不把本地抓取时间当成发布时间。"""
    if explicit and iso_date(explicit):
        return explicit

    source_path = VAULT / source_file
    source_meta: dict[str, str] = {}
    if source_file and source_path.exists():
        source_meta = parse_frontmatter(
            source_path.read_text(encoding="utf-8", errors="replace")
        )

    if source_file.startswith("30-资料/50-读书划线/"):
        # 读书划线以本次同步的新画线为素材事件，而不是图书出版日期。
        keys = ("synced_at", "last_note_at")
    else:
        keys = ("published_at", "published")
    for key in keys:
        value = source_meta.get(key, "")
        if value and iso_date(value):
            return value

    # 文件名日期和 created 往往只是本地抓取时间，不能冒充原始发布时间。
    return ""


def topic_freshness(
    meta: dict[str, str],
    time_sensitivity: str,
    *,
    today: date | None = None,
) -> tuple[str, str, str]:
    """返回原始发布时间、有效期和 fresh/stale/unknown。"""
    source_date_value = source_published_at(
        meta.get("source_file", ""),
        meta.get("source_published_at", ""),
    )
    anchor = source_date_value
    if meta.get("refreshed_at", "").strip() and meta.get("refresh_source", "").strip():
        anchor = meta["refreshed_at"]
    anchor_date = iso_date(anchor)
    if not anchor_date:
        return source_date_value, "", "unknown"

    valid_days = TOPIC_FRESHNESS_DAYS.get(time_sensitivity, 14)
    fresh_until = anchor_date + timedelta(days=valid_days)
    current_date = today or datetime.now().astimezone().date()
    status = "fresh" if current_date <= fresh_until else "stale"
    return source_date_value, fresh_until.isoformat(), status


def refresh_topic_freshness(path: Path) -> bool:
    """从原始资料重算候选的衍生日期；不改变选题阶段和人工判断。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    meta = parse_frontmatter(text)
    if meta.get("kind") != "topic-candidate":
        return False
    time_sensitivity = meta.get("time_sensitivity", "")
    if time_sensitivity not in TOPIC_FRESHNESS_DAYS:
        time_sensitivity = "中时效"
    source_date, fresh_until, freshness_status = topic_freshness(
        meta, time_sensitivity
    )
    updates = {
        "source_published_at": source_date,
        "fresh_until": fresh_until,
        "freshness_status": freshness_status,
    }
    if not meta.get("review_after", "").strip() and fresh_until:
        updates["review_after"] = fresh_until
    if all(meta.get(key, "") == value for key, value in updates.items()):
        return False
    updates["updated_at"] = now_local()
    path.write_text(update_frontmatter(text, updates), encoding="utf-8")
    return True


def backfill_topic_freshness() -> dict[str, Any]:
    """为既有候选补齐可由原始资料重建的新鲜度字段。"""
    scanned = 0
    updated = 0
    unresolved: list[str] = []
    for root in (TOPIC_DIR, TOPIC_RESERVE_DIR):
        if not root.exists():
            continue
        for path in sorted(root.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            meta = parse_frontmatter(text)
            if meta.get("kind") != "topic-candidate":
                continue
            scanned += 1
            if refresh_topic_freshness(path):
                updated += 1
                meta = parse_frontmatter(
                    path.read_text(encoding="utf-8", errors="replace")
                )
            if (
                not (
                    meta.get("source_published_at", "").strip()
                    or (
                        meta.get("refreshed_at", "").strip()
                        and meta.get("refresh_source", "").strip()
                    )
                )
                or not meta.get("fresh_until", "").strip()
            ):
                unresolved.append(rel(path))
    return {
        "scanned": scanned,
        "updated": updated,
        "unresolved": unresolved,
    }


def existing_topic_for_capture(capture_id: str) -> Path | None:
    for root in (TOPIC_DIR, TOPIC_RESERVE_DIR):
        if not root.exists():
            continue
        for path in root.glob("*.md"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if parse_frontmatter(text).get("capture_id") == capture_id:
                return path
    return None


def available_topic_path(title: str, capture_id: str, destination: Path = TOPIC_DIR) -> Path:
    existing = existing_topic_for_capture(capture_id)
    if existing:
        return existing
    destination.mkdir(parents=True, exist_ok=True)
    dated_title = f"{now_local()[:10]}-{sanitize_filename(title, limit=54)}"
    candidate = destination / f"{dated_title}.md"
    counter = 2
    while candidate.exists():
        candidate = destination / f"{dated_title}（{counter}）.md"
        counter += 1
    return candidate


def normalized_angle_lines(raw: str) -> list[str]:
    angles: list[str] = []
    for line in raw.splitlines():
        cleaned = re.sub(r"^\s*[-*]\s+(?:\[[ xX]\]\s*)?", "", line).strip()
        if cleaned and cleaned not in {"暂无", "暂无可写角度", "待确认"}:
            angles.append(cleaned)
    return list(dict.fromkeys(angles))


def create_topic_candidate(
    card_path: Path,
    text: str,
    meta: dict[str, str],
    knowledge_path: Path | None = None,
    *,
    status: str = "candidate",
    destination: Path | None = None,
) -> Path:
    """把“值得写”保存为轻量候选；只有明确开写后才建立成稿包。"""
    title_match = re.search(r"^# (.+)$", text, flags=re.M)
    title = title_match.group(1).strip() if title_match else card_path.stem
    capture_id = meta.get("capture_id") or rel(card_path)

    relevance = int_score(meta, "relevance_score")
    knowledge_score = int_score(meta, "knowledge_value_score", relevance)
    has_writing_score = bool(meta.get("writing_value_score", "").strip())
    writing_score = int_score(meta, "writing_value_score")
    priority_score = int_score(meta, "priority_score", writing_score)
    time_sensitivity = meta.get("time_sensitivity", "")
    has_time_sensitivity = time_sensitivity in TOPIC_FRESHNESS_DAYS
    if not has_time_sensitivity:
        time_sensitivity = "中时效"
    source_file = meta.get("source_file", "")
    source_url = meta.get("source_url", "")
    source_date, fresh_until, freshness_status = topic_freshness(meta, time_sensitivity)

    requested_status = status
    parked_reason = ""
    if status == "candidate" and not has_writing_score:
        status = "parked"
        parked_reason = "旧审核卡缺少独立写作价值评分，需重新评估后再进入今日候选"
    elif status == "candidate" and not has_time_sensitivity:
        status = "parked"
        parked_reason = "旧审核卡缺少时效判断，需重新评估后再进入今日候选"
    elif status == "candidate" and freshness_status != "fresh":
        status = "parked"
        parked_reason = (
            "原始资料已超过当前时效窗口，需有新进展或本人新实践后再唤醒"
            if freshness_status == "stale"
            else "无法确认原始资料日期，需补齐来源时间后再进入今日候选"
        )
    elif status == "parked":
        parked_reason = "有价值但近期不写"

    if status == "parked" and requested_status == "candidate":
        priority_score = 0
    destination = destination or (TOPIC_RESERVE_DIR if status == "parked" else TOPIC_DIR)
    if status == "parked" and requested_status == "candidate":
        destination = TOPIC_RESERVE_DIR
    path = available_topic_path(title, capture_id, destination)
    if path.exists():
        refresh_topic_freshness(path)
        return path

    card_rel = rel(card_path)
    topic_id = "topic-" + hashlib.sha256(capture_id.encode("utf-8")).hexdigest()[:12]
    angles = normalized_angle_lines(markdown_section(text, "可写角度"))
    claims = markdown_section(text, "核心观点") or "- 待从原始资料中提炼"
    evidence = markdown_section(text, "支撑证据") or "- 待核对"
    counterpoints = markdown_section(text, "漏洞与反例") or "- 待补充"
    connections = linkify_connections(
        markdown_section(text, "与现有知识的连接")
    ) or "- 暂无明确连接"
    reason = markdown_section(text, "快速判断") or markdown_section(text, "摘要")
    knowledge_rel = rel(knowledge_path) if knowledge_path else ""
    knowledge_status = "extracted" if knowledge_path else "pending"
    lines = [
        "---",
        "kind: topic-candidate",
        f"status: {status}",
        f"topic_id: {topic_id}",
        f"priority_score: {priority_score}",
        f"knowledge_value_score: {knowledge_score}",
        f"writing_value_score: {writing_score}",
        f"time_sensitivity: {time_sensitivity}",
        f"source_published_at: {yaml_string(source_date)}",
        f"fresh_until: {yaml_string(fresh_until)}",
        f"freshness_status: {freshness_status}",
        f"review_after: {fresh_until or topic_review_after(time_sensitivity)}",
        f"parked_reason: {yaml_string(parked_reason)}",
        f"knowledge_status: {knowledge_status}",
        f"created_at: {yaml_string(now_local())}",
        f"updated_at: {yaml_string(now_local())}",
        f"last_progress_at: {yaml_string(now_local())}",
        f"capture_id: {yaml_string(capture_id)}",
        f"source_review: {yaml_string(card_rel)}",
        f"source_file: {yaml_string(source_file)}",
        f"source_url: {yaml_string(source_url)}",
        f"knowledge_file: {yaml_string(knowledge_rel)}",
        'content_pack: ""',
        'draft: ""',
        'published_file: ""',
        'performance_file: ""',
        "---",
        "",
        f"# {title}",
        "",
        "## 选题判断",
        "",
        reason or "待补充为什么这个题值得占用一次写作机会。",
        "",
        f"- 知识价值：{knowledge_score}/100",
        f"- 写作价值：{writing_score}/100",
        f"- 当前优先级：{priority_score}/100",
        f"- 时效：{time_sensitivity}",
        f"- 原始资料日期：{source_date or '待确认'}",
        f"- 今日候选有效至：{fresh_until or '待确认'}",
        "",
        "## 待用角度",
        "",
    ]
    if angles:
        lines.extend(f"- [ ] A{index} · {angle}" for index, angle in enumerate(angles, 1))
    else:
        lines.append("- [ ] A1 · 待确认一个能给读者明确收益的角度")
    lines.extend([
        "",
        "## 已用角度",
        "",
        "- 暂无",
        "",
        "## 核心事实",
        "",
        claims,
        "",
        "## 证据与边界",
        "",
        "### 已有证据",
        "",
        evidence,
        "",
        "### 反例与限制",
        "",
        counterpoints,
        "",
        "## 可调用知识",
        "",
        connections,
    ])
    if knowledge_path:
        lines.append(f"- 本次同步沉淀：[[{knowledge_rel[:-3]}]]")
    lines.extend([
        "",
        "## 来源",
        "",
        f"- 审核记录：[[{card_rel[:-3]}]]",
    ])
    if source_file:
        lines.append(f"- 原始资料：[[{source_file[:-3]}]]")
    if source_url:
        lines.append(f"- 原始链接：{source_url}")
    lines.extend([
        "",
        "## 推进",
        "",
        "- [ ] 选定一个角度",
        "- [ ] 确认读者收益和事实条件",
        "- [ ] 建立写作任务",
        "",
        "> 候选选题只回答“是否值得写、还能怎么写”。确定近期要写之后，再建立一篇文章对应一个角度的写作任务。",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def existing_content_pack_for_capture(capture_id: str) -> Path | None:
    if not CONTENT_PACK_DIR.exists():
        return None
    for path in CONTENT_PACK_DIR.glob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if parse_frontmatter(text).get("capture_id") == capture_id:
            return path
    return None


def available_content_pack_path(title: str, capture_id: str) -> Path:
    existing = existing_content_pack_for_capture(capture_id)
    if existing:
        return existing
    CONTENT_PACK_DIR.mkdir(parents=True, exist_ok=True)
    dated_title = f"{now_local()[:10]}-{sanitize_filename(title, limit=54)}"
    candidate = CONTENT_PACK_DIR / f"{dated_title}.md"
    counter = 2
    while candidate.exists():
        candidate = CONTENT_PACK_DIR / f"{dated_title}（{counter}）.md"
        counter += 1
    return candidate


def create_content_pack(card_path: Path, text: str, meta: dict[str, str]) -> Path:
    """旧接口：直接从审核卡建成稿包。新流程应先建候选，再显式 promote。"""
    title_match = re.search(r"^# (.+)$", text, flags=re.M)
    title = title_match.group(1).strip() if title_match else card_path.stem
    capture_id = meta.get("capture_id") or rel(card_path)
    path = available_content_pack_path(title, capture_id)
    if path.exists():
        return path

    source_file = meta.get("source_file", "")
    source_url = meta.get("source_url", "")
    claims = markdown_section(text, "核心观点") or "- 待从原始资料中提炼"
    evidence = markdown_section(text, "支撑证据") or "- 待核对"
    counterpoints = markdown_section(text, "漏洞与反例") or "- 待补充"
    connections = linkify_connections(markdown_section(text, "与现有知识的连接")) or "- 待连接"
    angles = markdown_section(text, "可写角度") or "- 待确认"
    card_rel = rel(card_path)

    lines = [
        "---",
        "kind: content-pack",
        "status: seed",
        f"created_at: {yaml_string(now_local())}",
        f"updated_at: {yaml_string(now_local())}",
        f"capture_id: {yaml_string(capture_id)}",
        f"source_review: {yaml_string(card_rel)}",
        f"source_file: {yaml_string(source_file)}",
        f"source_url: {yaml_string(source_url)}",
        'platform: ""',
        'prototype: ""',
        'research: ""',
        'draft: ""',
        "---",
        "",
        f"# 成稿包：{title}",
        "",
        "## 写作任务",
        "",
        "- 目标读者：待确认",
        "- 发布平台：待确认",
        "- 文章原型：待确认",
        "- 读者读完后的动作或变化：待确认",
        "",
        "## 核心问题",
        "",
        "待从候选角度中确定一个问题。",
        "",
        "## 一句话判断",
        "",
        "待确认。它必须是作者真正愿意署名承担的判断。",
        "",
        "## 候选观点",
        "",
        claims,
        "",
        "## 事实与边界",
        "",
        "### 已有证据",
        "",
        evidence,
        "",
        "### 待核对事实",
        "",
        "- 人物、数字、时间、产品状态和关键因果待逐项核对。",
        "",
        "### 明确边界",
        "",
        counterpoints,
        "",
        "## 用户真实材料",
        "",
        "- 只记录用户明确提供或能从本地材料核实的经历、对话、情绪、耗时、成本和结果。",
        "- 当前可用：待确认。",
        "- 禁止补写：未被用户提供的个人故事和效果数字。",
        "",
        "## 情绪与传播",
        "",
        "- 读者原有情绪：待确认",
        "- 文章情绪核：待确认",
        "- 情绪曲线：具体刺激 → 疑问加深 → 意外或反转 → 判断成形 → 结尾余波",
        "- 核心冲突句：待确认",
        "- 可传播句候选：待写两到三句，必须准确承载观点",
        "- 网感边界：自然口语和当下语境，不堆热梗，不制造虚假危机",
        "",
        "## 最强反方",
        "",
        counterpoints,
        "",
        "## 可调用知识",
        "",
        connections,
        "",
        "## 候选角度",
        "",
        angles,
        "",
        "## 来源",
        "",
        f"- 审核卡：[[{card_rel[:-3]}]]",
    ]
    if source_file:
        lines.append(f"- 原始资料：[[{source_file[:-3]}]]")
    if source_url:
        lines.append(f"- 原始链接：{source_url}")
    lines.extend([
        "",
        "## 生产状态",
        "",
        "- [ ] 核心判断已由用户确认",
        "- [ ] 个人材料使用范围已确认",
        "- [ ] 关键事实已核对",
        "- [ ] 调研与事实清单已建立",
        "- [ ] 完整草稿已建立",
        "- [ ] 真实性、文章性、情绪传播和个人语气检查已通过",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def promote_topic(topic_path: Path, selected_angle: str) -> Path:
    """把一个明确角度从候选池提升为正式写作任务。"""
    selected_angle = selected_angle.strip()
    if not selected_angle:
        raise ValueError("建立写作任务前必须明确本次角度")
    topic_path = topic_path.resolve()
    allowed_roots = (TOPIC_DIR.resolve(), TOPIC_RESERVE_DIR.resolve())
    if (
        topic_path.suffix != ".md"
        or not topic_path.exists()
        or not any(topic_path.parent == root for root in allowed_roots)
    ):
        raise ValueError("目标必须是候选选题目录中的 Markdown 文件")
    topic_text = topic_path.read_text(encoding="utf-8", errors="replace")
    topic_meta = parse_frontmatter(topic_text)
    if topic_meta.get("kind") != "topic-candidate":
        raise ValueError("目标不是候选选题")
    if topic_meta.get("status") == "closed":
        raise ValueError("该写作角度已经关闭；如有新事实，请建立新候选或先明确重新开启原因")

    if topic_meta.get("status") in {"candidate", "parked"}:
        time_sensitivity = topic_meta.get("time_sensitivity", "")
        source_date, fresh_until, freshness_status = topic_freshness(
            topic_meta,
            time_sensitivity if time_sensitivity in TOPIC_FRESHNESS_DAYS else "中时效",
        )
        if freshness_status != "fresh":
            raise ValueError(
                "原始资料已过时或日期不明；重新推进前请补充 refreshed_at 和 refresh_source，"
                "说明新的外部进展或本人新实践"
            )
        topic_text = update_frontmatter(
            topic_text,
            {
                "source_published_at": source_date,
                "fresh_until": fresh_until,
                "freshness_status": freshness_status,
                "parked_reason": "",
            },
        )
        topic_path.write_text(topic_text, encoding="utf-8")
        topic_meta = parse_frontmatter(topic_text)

    if topic_path.parent == TOPIC_RESERVE_DIR.resolve():
        TOPIC_DIR.mkdir(parents=True, exist_ok=True)
        destination = unique_destination(TOPIC_DIR, topic_path.name)
        old_rel = rel(topic_path)
        shutil.move(topic_path, destination)
        topic_path = destination
        replace_markdown_references(old_rel, rel(topic_path))
        topic_text = topic_path.read_text(encoding="utf-8", errors="replace")
        topic_meta = parse_frontmatter(topic_text)

    title_match = re.search(r"^#\s+(.+?)\s*$", topic_text, flags=re.M)
    title = title_match.group(1).strip() if title_match else topic_path.stem
    topic_rel = rel(topic_path)
    topic_id = topic_meta.get("topic_id") or "topic-" + hashlib.sha256(
        topic_rel.encode("utf-8")
    ).hexdigest()[:12]
    content_id = "article-" + hashlib.sha256(
        f"{topic_id}\n{selected_angle}".encode("utf-8")
    ).hexdigest()[:12]
    for existing in WRITING_TASK_DIR.glob("*.md") if WRITING_TASK_DIR.exists() else []:
        existing_meta = parse_frontmatter(
            existing.read_text(encoding="utf-8", errors="replace")
        )
        if existing_meta.get("content_id") == content_id:
            return existing

    WRITING_TASK_DIR.mkdir(parents=True, exist_ok=True)
    path = WRITING_TASK_DIR / (
        f"{now_local()[:10]}-{sanitize_filename(title, limit=38)}-"
        f"{sanitize_filename(selected_angle, limit=28)}.md"
    )
    if path.exists():
        path = unique_destination(WRITING_TASK_DIR, path.name)
    if not CONTENT_PACK_TEMPLATE.exists():
        raise ValueError(f"找不到成稿包模板：{CONTENT_PACK_TEMPLATE}")
    rendered = CONTENT_PACK_TEMPLATE.read_text(encoding="utf-8")
    rendered = rendered.replace("{{date}}", now_local()).replace("{{title}}", title)
    rendered = update_frontmatter(
        rendered,
        {
            "status": "active",
            "content_id": content_id,
            "topic_id": topic_id,
            "candidate": f"[[{topic_rel[:-3]}]]",
            "selected_angle": selected_angle,
            "source_review": topic_meta.get("source_review", ""),
            "source_file": topic_meta.get("source_file", ""),
            "source_url": topic_meta.get("source_url", ""),
        },
    )
    rendered = rendered.replace(
        "- 候选选题：[[]]", f"- 候选选题：[[{topic_rel[:-3]}]]"
    )
    rendered = rendered.replace("- 本次角度：", f"- 本次角度：{selected_angle}", 1)
    callable_knowledge = markdown_section(topic_text, "可调用知识")
    if callable_knowledge:
        rendered = rendered.replace(
            "## 可调用知识\n\n- [[]]",
            "## 可调用知识\n\n" + callable_knowledge,
        )
    path.write_text(rendered, encoding="utf-8")

    updated_topic = update_frontmatter(
        topic_text,
        {
            "status": "active",
            "topic_id": topic_id,
            "selected_angle": selected_angle,
            "content_pack": f"[[{rel(path)[:-3]}]]",
            "last_progress_at": now_local(),
            "updated_at": now_local(),
        },
    )
    topic_path.write_text(updated_topic, encoding="utf-8")
    return path


def unique_destination(base: Path, name: str) -> Path:
    candidate = base / name
    counter = 2
    while candidate.exists():
        candidate = base / f"{Path(name).stem}（{counter}）{Path(name).suffix}"
        counter += 1
    return candidate


def replace_markdown_references(old_rel: str, new_rel: str) -> int:
    """移动流程记录后同步显式路径链接；原始内容和媒体不做改写。"""
    old_note = old_rel[:-3] if old_rel.endswith(".md") else old_rel
    new_note = new_rel[:-3] if new_rel.endswith(".md") else new_rel
    changed = 0
    for note in VAULT.rglob("*.md"):
        try:
            text = note.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        updated = text.replace(old_rel, new_rel).replace(old_note, new_note)
        if updated != text:
            note.write_text(updated, encoding="utf-8")
            changed += 1
    return changed


def review_archive_destination(path: Path, meta: dict[str, str], cleanup: bool = False) -> Path:
    if cleanup:
        root = VAULT / "90-归档/90-待清理" / now_local()[:10] / "审核记录"
    else:
        month = (meta.get("reviewed_at") or now_local())[:7]
        root = REVIEW_RECORD_DIR / month
    root.mkdir(parents=True, exist_ok=True)
    return unique_destination(root, path.name)


def archive_cleanup_source(meta: dict[str, str]) -> str:
    source_rel = meta.get("source_file", "")
    source = VAULT / source_rel
    if (
        not source_rel
        or Path(source_rel).is_absolute()
        or ".." in Path(source_rel).parts
        or not source_rel.startswith("30-资料/")
        or not source.exists()
    ):
        return source_rel
    destination = VAULT / "90-归档/90-待清理" / now_local()[:10] / source_rel
    if destination.exists():
        destination = unique_destination(destination.parent, destination.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(source, destination)
    new_rel = rel(destination)
    replace_markdown_references(source_rel, new_rel)
    return new_rel


def update_reviewed_source_reference(
    state: dict[str, Any] | None,
    capture_id: str,
    old_rel: str,
    new_rel: str,
) -> None:
    if state is None or not new_rel or new_rel == old_rel:
        return
    for item_id, item in state.get("reviewed_sources", {}).items():
        if item_id == capture_id or item.get("source") == old_rel:
            item["source"] = new_rel


def capture_source_path(source_rel: str) -> Path | None:
    """把审核卡上的 source_file 解回采集目录里的来源卡；越界或不存在都返回 None。

    三个采集目录就是可写边界：里面的文件都是采集器自己写的来源卡。source_file
    指到别处（写作任务、知识笔记、知识库外）一律不动。
    """
    if not source_rel or Path(source_rel).is_absolute() or ".." in Path(source_rel).parts:
        return None
    source = VAULT / source_rel
    if not source.exists():
        return None
    for allowed in (X_DIR, WEB_DIR, WEREAD_DIR):
        try:
            source.relative_to(allowed)
        except ValueError:
            continue
        return source
    return None


def mark_source_processed(meta: dict[str, str]) -> bool:
    """审核动作完成后回写来源卡；保持各同步器能识别的无引号状态值。

    三种采集来源一视同仁：书籍卡、X 书签、网页剪藏走完复核就都算处理过了。
    原来只回写书籍卡，于是 X 书签和剪藏不管被怎么处理，卡上都永远写着 unprocessed，
    回采集目录翻的人看不出哪些已经用过。
    """
    source = capture_source_path(meta.get("source_file", ""))
    if source is None:
        return False
    text = source.read_text(encoding="utf-8", errors="replace")
    if re.search(r"^processing_status:\s*processed\s*$", text, flags=re.M):
        return True
    if re.search(r"^processing_status:\s*.*$", text, flags=re.M):
        updated = re.sub(
            r"^processing_status:\s*.*$",
            "processing_status: processed",
            text,
            count=1,
            flags=re.M,
        )
    else:
        end = text.find("\n---\n", 4)
        if end < 0:
            return False
        updated = text[:end] + "\nprocessing_status: processed" + text[end:]
    source.write_text(updated, encoding="utf-8")
    return True


def archive_review_card(
    path: Path,
    meta: dict[str, str],
    state: dict[str, Any] | None,
    *,
    cleanup: bool = False,
    destination: Path | None = None,
) -> Path:
    old_rel = rel(path)
    destination = destination or review_archive_destination(path, meta, cleanup)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(path, destination)
    new_rel = rel(destination)
    replace_markdown_references(old_rel, new_rel)
    if state is not None:
        capture_id = meta.get("capture_id", "")
        for item_id, item in state.get("reviewed_sources", {}).items():
            if item.get("review") == old_rel or (capture_id and item_id == capture_id):
                item["review"] = new_rel
                item["archived_at"] = now_local()
    return destination


def process_review_actions(state: dict[str, Any] | None = None) -> dict[str, int]:
    result = {
        "processed": 0,
        "knowledge": 0,
        "topic": 0,
        "both": 0,
        "paused": 0,
        "cleanup": 0,
        "archived": 0,
        "invalid": 0,
    }
    if not REVIEW_DIR.exists():
        return result
    for path in sorted(REVIEW_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        if meta.get("action_processed_at"):
            continue
        actions = checked_review_actions(text)
        if len(actions) > 1:
            if meta.get("action_error") != "同时勾选了多个审核选项":
                path.write_text(
                    update_frontmatter(text, {"action_error": "同时勾选了多个审核选项"}),
                    encoding="utf-8",
                )
            result["invalid"] += 1
            continue
        if not actions:
            if meta.get("action_error"):
                path.write_text(update_frontmatter(text, {"action_error": ""}), encoding="utf-8")
            continue
        action = REVIEW_ACTION_KEY_ALIASES.get(actions[0], actions[0])
        cleanup = action == "cleanup"
        archive_target = review_archive_destination(path, meta, cleanup)
        updates = {
            "review_action": action,
            "action_processed_at": now_local(),
            "archived_at": now_local(),
            "action_error": "",
            "result_file": "",
            "knowledge_file": "",
            "topic_file": "",
        }
        knowledge_path: Path | None = None
        topic_path: Path | None = None
        if action in {"knowledge", "both"}:
            knowledge_path = create_knowledge_note(archive_target, text, meta)
            updates["knowledge_file"] = rel(knowledge_path)
        if action in {"topic", "both"}:
            topic_path = create_topic_candidate(
                archive_target, text, meta, knowledge_path=knowledge_path
            )
            updates["topic_file"] = rel(topic_path)

        if action == "both":
            updates["status"] = "已双向分流"
            updates["result_file"] = rel(topic_path) if topic_path else rel(knowledge_path)  # type: ignore[arg-type]
        elif action == "knowledge":
            updates["status"] = "已沉淀"
            updates["result_file"] = rel(knowledge_path)  # type: ignore[arg-type]
        elif action == "topic":
            topic_status = (
                parse_frontmatter(
                    topic_path.read_text(encoding="utf-8", errors="replace")
                ).get("status", "")
                if topic_path
                else ""
            )
            updates["status"] = "已进入储备" if topic_status == "parked" else "已进入候选"
            updates["result_file"] = rel(topic_path)  # type: ignore[arg-type]
        elif action == "paused":
            updates["status"] = "暂缓"
        else:
            updates["status"] = "待清理"
        mark_source_processed(meta)
        if action == "cleanup":
            original_source = meta.get("source_file", "")
            archived_source = archive_cleanup_source(meta)
            if archived_source != original_source:
                updates["original_source_file"] = original_source
                updates["source_file"] = archived_source
                update_reviewed_source_reference(
                    state,
                    meta.get("capture_id", ""),
                    original_source,
                    archived_source,
                )
        updated = update_frontmatter(text, updates)
        path.write_text(updated, encoding="utf-8")
        updated_meta = {**meta, **updates}
        archived = archive_review_card(
            path,
            updated_meta,
            state,
            cleanup=cleanup,
            destination=archive_target,
        )
        result["processed"] += 1
        result[action] += 1
        result["archived"] += 1
        log(f"review action {action}: {rel(archived)}")
    return result


def process_legacy_actions() -> dict[str, Any]:
    if not LEGACY_ACTIONS_SCRIPT.exists():
        return {"processed": 0, "waiting": 0, "status": "not-installed"}
    proc = subprocess.run(
        [sys.executable, str(LEGACY_ACTIONS_SCRIPT), "apply"],
        text=True,
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-3000:] or proc.stdout[-3000:] or "legacy actions failed")
    data = json.loads(proc.stdout)
    if data.get("processed"):
        log(f"legacy review actions processed: {data['processed']}")
    return data


def source_id(path: Path, text: str) -> str:
    meta = parse_frontmatter(text)
    if is_weread_card(path, text):
        return weread_version_id(path, text)
    return meta.get("capture_id") or "file:" + rel(path)


def existing_knowledge_titles(limit: int = 300) -> list[str]:
    titles = sorted(
        unicodedata.normalize("NFC", path.stem)
        for path in KNOWLEDGE_DIR.rglob("*.md")
        if "90-元数据" not in path.parts and path.name != "README.md"
    )
    return titles[:limit]


def existing_topic_titles(limit: int = 80) -> list[str]:
    titles: list[str] = []
    roots = [
        TOPIC_DIR,
        TOPIC_RESERVE_DIR,
        KNOWLEDGE_DIR / "70-知识地图",
    ]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            text = path.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"^#\s+(.+?)\s*$", text, flags=re.M)
            title = match.group(1).strip() if match else path.stem
            if title not in titles:
                titles.append(title)
            if len(titles) >= limit:
                return titles
    return titles


def build_prompt(items: list[dict[str, str]], config: dict[str, Any]) -> str:
    instruction = PROMPT_FILE.read_text(encoding="utf-8")
    topics = "、".join(config.get("focus_topics", []))
    titles = "、".join(existing_knowledge_titles())
    existing_topics = "、".join(existing_topic_titles())
    chunks = [
        instruction,
        f"\n当前重点主题：{topics}\n",
        f"现有长期知识笔记标题（connections 中凡能对应上的，必须原样使用这些标题）：{titles}\n",
        f"现有主题与候选选题标题（用于识别新资料是否补充旧主题，不等于长期知识）：{existing_topics}\n",
        "待分析资料：",
    ]
    for item in items:
        chunks.extend([
            "\n--- ITEM ---",
            f"id: {item['id']}",
            f"file: {item['file']}",
            item["content"],
        ])
    return "\n".join(chunks)


def run_codex(prompt: str, config: dict[str, Any]) -> dict[str, Any]:
    codex = config.get("codex_path", "codex")
    with tempfile.NamedTemporaryFile(prefix="knowledge-review-", suffix=".json", delete=False) as f:
        output_path = Path(f.name)
    cmd = [
        codex, "exec", "--ephemeral", "--sandbox", "read-only",
        "--skip-git-repo-check", "-C", str(VAULT),
        "--output-schema", str(SCHEMA_FILE),
        "-o", str(output_path), "-",
    ]
    model = config.get("codex_model")
    if model:
        cmd[2:2] = ["--model", model]
    try:
        proc = subprocess.run(cmd, input=prompt, text=True, capture_output=True, timeout=420)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[-4000:] or proc.stdout[-4000:] or "codex failed")
        return json.loads(output_path.read_text(encoding="utf-8"))
    finally:
        output_path.unlink(missing_ok=True)


def card_path(review: dict[str, Any], item_id: str) -> Path:
    title = sanitize_filename(review["title"])
    short = hashlib.sha256(item_id.encode()).hexdigest()[:8]
    return REVIEW_DIR / f"{datetime.now().strftime('%Y-%m-%d')}-{title}-{short}.md"


def normalized_relevance_score(value: Any) -> int:
    score = max(0, min(100, int(value)))
    # Some models occasionally answer on a 1–10 scale despite the schema.
    return score * 10 if 0 < score <= 10 else score


def write_review_card(
    review: dict[str, Any], item: dict[str, str], replacement_path: Path | None = None
) -> Path:
    path = replacement_path or card_path(review, item["id"])
    previous_text = (
        path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    )
    destination_map = {
        "概念": "20-知识/10-概念",
        "人物与组织": "20-知识/20-人物与组织",
        "问题": "20-知识/30-问题",
        "观点": "20-知识/40-观点",
        "案例": "20-知识/50-案例",
        "方法": "20-知识/60-方法",
        "无": "90-归档/90-待清理",
    }
    source_meta = parse_frontmatter(item["content"])
    source_url = source_meta.get("source_url", "") or source_meta.get("source", "")
    source_date = source_published_at(item["file"])
    suggested_kind = review["suggested_kind"]
    relevance_score = normalized_relevance_score(review["relevance_score"])
    knowledge_value_score = normalized_relevance_score(
        review.get("knowledge_value_score", relevance_score)
    )
    writing_value_score = normalized_relevance_score(
        review.get("writing_value_score", relevance_score)
    )
    time_sensitivity = review.get("time_sensitivity", "中时效")
    if time_sensitivity not in {"强时效", "中时效", "常青"}:
        time_sensitivity = "中时效"
    lines = [
        "---",
        "kind: capture-review",
        "status: 待审核",
        f"recommendation: {review['recommendation']}",
        f"relevance_score: {relevance_score}",
        f"knowledge_value_score: {knowledge_value_score}",
        f"writing_value_score: {writing_value_score}",
        f"time_sensitivity: {time_sensitivity}",
        f"confidence: {review['confidence']}",
        f"suggested_kind: {suggested_kind}",
        f"suggested_destination: {yaml_string(destination_map.get(suggested_kind, ''))}",
        f"source_file: {yaml_string(item['file'])}",
        f"source_url: {yaml_string(source_url)}",
        f"source_published_at: {yaml_string(source_date)}",
        f"capture_id: {yaml_string(item['id'])}",
        f"reviewed_at: {yaml_string(now_local())}",
        "topics:",
    ]
    lines.extend([f"  - {yaml_string(x)}" for x in review.get("topics", [])] or ["  - 待分类"])
    lines.extend([
        "---", "", f"# {review['title']}", "",
        "## 快速判断", "",
        f"**建议：{review['recommendation']}｜知识价值：{knowledge_value_score}/100｜写作价值：{writing_value_score}/100｜{time_sensitivity}｜置信度：{review['confidence']}**", "",
        review["reason"], "",
        "## 读者收益", "", review.get("reader_value", "待确认读者读完后能采取什么行动或形成什么判断。"), "",
        "## 摘要", "", review["summary"], "",
        "## 核心观点", "",
    ])
    lines.extend([f"- {x}" for x in review.get("core_claims", [])] or ["- 无"])
    lines.extend(["", "## 支撑证据", ""])
    lines.extend([f"- {x}" for x in review.get("evidence", [])] or ["- 原文未提供足够证据"])
    lines.extend(["", "## 漏洞与反例", ""])
    lines.extend([f"- {x}" for x in review.get("counterpoints", [])] or ["- 暂无"])
    lines.extend(["", "## 与现有知识的连接", ""])
    lines.extend([f"- {x}" for x in review.get("connections", [])] or ["- 暂无明确连接"])
    lines.extend(["", "## 可写角度", ""])
    lines.extend([f"- {x}" for x in review.get("writing_angles", [])] or ["- 暂无"])
    lines.extend([
        "", "## 原始来源", "",
        f"- 文件：[[{item['file'][:-3]}]]",
        f"- 链接：{source_url or '未提供'}",
        "", "## 人工审核", "",
    ])
    lines.extend([f"- [ ] {label}" for label in CANONICAL_REVIEW_LABELS])
    lines.extend([
        "", "> 选择一条处理路径。“同时沉淀”会从同一份原始素材分别生成长期知识与候选选题；所有处理记录随后退出待评估区。", "",
    ])
    rendered = "\n".join(lines)
    if previous_text:
        rendered = preserve_review_decision(previous_text, rendered)
    path.write_text(rendered, encoding="utf-8")
    return path


def review_pending(state: dict[str, Any], config: dict[str, Any]) -> int:
    pending = []
    for source_rel in list(dict.fromkeys(state["pending_sources"])):
        path = VAULT / source_rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        item_id = source_id(path, text)
        if item_id in state["reviewed_sources"]:
            continue
        pending.append({"id": item_id, "file": source_rel, "content": text})
    if not pending:
        for error in state["errors"]:
            if error.get("message", "").startswith("review failed:") and not error.get("resolved_at"):
                error["resolved_at"] = now_local()
        return 0
    max_items = int(config.get("review_batch_size", 8))
    max_item_chars = int(config.get("review_item_max_chars", 150_000))
    max_batch_chars = int(config.get("review_batch_max_chars", 180_000))
    batch = []
    used_chars = 0
    for item in pending:
        content = item["content"][:max_item_chars]
        if batch and (len(batch) >= max_items or used_chars + len(content) > max_batch_chars):
            break
        batch.append({**item, "content": content})
        used_chars += len(content)
    result = run_codex(build_prompt(batch, config), config)
    for error in state["errors"]:
        if error.get("message", "").startswith("review failed:") and not error.get("resolved_at"):
            error["resolved_at"] = now_local()
    by_id = {item["id"]: item for item in batch}
    count = 0
    for review in result.get("reviews", []):
        item = by_id.get(review.get("id"))
        if not item:
            continue
        replacement = state["review_replacements"].get(review["id"])
        replacement_path = VAULT / replacement if replacement else None
        path = write_review_card(review, item, replacement_path)
        state["reviewed_sources"][review["id"]] = {
            "source": item["file"], "review": rel(path), "at": now_local()
        }
        state["review_replacements"].pop(review["id"], None)
        count += 1
    reviewed_ids = set(state["reviewed_sources"])
    remaining = []
    for source_rel in state["pending_sources"]:
        source_path = VAULT / source_rel
        if not source_path.exists():
            continue
        text = source_path.read_text(encoding="utf-8", errors="replace")
        if source_id(source_path, text) not in reviewed_ids:
            remaining.append(source_rel)
    state["pending_sources"] = remaining
    return count


def sync_only(state: dict[str, Any], config: dict[str, Any]) -> dict[str, int]:
    result = {"x": 0, "mobile": 0, "web": 0, "weread": 0}
    try:
        result["x"] = sync_x(state, config)
        for error in state["errors"]:
            if error.get("message", "").startswith("x sync failed:") and not error.get(
                "resolved_at"
            ):
                error["resolved_at"] = now_local()
    except Exception as exc:
        message = f"x sync failed: {exc}"
        state["errors"].append({"at": now_local(), "message": message})
        log(message)
    result["mobile"] = sync_mobile(state, config)
    result["web"] = discover_web_clips(state)
    result["weread"] = discover_weread_cards(state)
    save_state(state)
    return result


def status(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "known_x_ids": len(state["known_x_ids"]),
        "known_web_files": len(state["known_web_files"]),
        "known_weread_books": len(state.get("known_weread_versions", {})),
        "mobile_lines": len(state["mobile_line_hashes"]),
        "pending": len(state["pending_sources"]),
        "reviewed": len(state["reviewed_sources"]),
        "recent_errors": [error for error in state["errors"] if not error.get("resolved_at")][-5:],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Ray's Brain unified capture and review pipeline")
    parser.add_argument(
        "command",
        choices=[
            "init",
            "sync",
            "repair-x",
            "review",
            "actions",
            "promote",
            "backfill-topics",
            "run",
            "detect-x-articles",
            "status",
        ],
    )
    parser.add_argument("target", nargs="?", help="promote 时传入候选选题的 vault 相对路径")
    parser.add_argument("--angle", default="", help="promote 时明确本篇文章采用的角度")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="detect-x-articles 时只打印会做什么，不写任何文件",
    )
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(status(load_state()), ensure_ascii=False, indent=2))
        return
    initialize()
    state = load_state()
    config = load_json(CONFIG_FILE, {})
    if args.command == "init":
        result: Any = status(state)
    elif args.command == "backfill-topics":
        result = backfill_topic_freshness()
    elif args.command == "promote":
        if not args.target:
            parser.error("promote 需要候选选题路径")
        target = (VAULT / args.target).resolve()
        task = promote_topic(target, args.angle)
        result = {"promoted": rel(target), "writing_task": rel(task)}
    elif args.command == "sync":
        result = sync_only(state, config)
    elif args.command == "repair-x":
        result = repair_x(state, config)
    elif args.command == "detect-x-articles":
        result = detect_x_article_publications(config, dry_run=args.dry_run)
    elif args.command == "review":
        result = {
            "actions": process_review_actions(state),
            "legacy_actions": process_legacy_actions(),
            "reviewed": review_pending(state, config),
        }
        save_state(state)
    elif args.command == "actions":
        result = {
            "actions": process_review_actions(state),
            "legacy_actions": process_legacy_actions(),
        }
        save_state(state)
    elif args.command == "run":
        actions_before = process_review_actions(state)
        legacy_actions_before = process_legacy_actions()
        result = sync_only(state, config)
        try:
            result["x_articles"] = detect_x_article_publications(config)
            for error in state["errors"]:
                if error.get("message", "").startswith(
                    "x article detect failed:"
                ) and not error.get("resolved_at"):
                    error["resolved_at"] = now_local()
        except Exception as exc:
            message = f"x article detect failed: {exc}"
            state["errors"].append({"at": now_local(), "message": message})
            log(message)
        if config.get("review_enabled", False):
            try:
                result["reviewed"] = review_pending(state, config)
            except Exception as exc:
                message = f"review failed: {exc}"
                state["errors"].append({"at": now_local(), "message": message})
                log(message)
                result["review_error"] = str(exc)
        else:
            result["reviewed"] = 0
            result["review_paused"] = "awaiting explicit privacy approval"
        actions_after = process_review_actions(state)
        result["actions"] = {
            key: actions_before.get(key, 0) + actions_after.get(key, 0)
            for key in actions_before
        }
        legacy_actions_after = process_legacy_actions()
        result["legacy_actions"] = {
            "processed": legacy_actions_before.get("processed", 0) + legacy_actions_after.get("processed", 0),
            "waiting": legacy_actions_after.get("waiting", 0),
        }
        save_state(state)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
