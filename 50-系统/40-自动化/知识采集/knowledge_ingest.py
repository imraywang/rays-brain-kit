#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import fcntl
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
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from topic_map_candidates import is_theme_map, refresh_topic_map_candidates
from map_digest import process_map_digest_actions

VAULT = Path(os.environ.get("RAYS_BRAIN", str(HERE.parents[2]))).resolve()
SYSTEM_DIR = HERE
REVIEW_PROTOCOL_FILE = HERE.parent / "review_protocol.json"
# 运行时状态与日志放在 vault 外，避免 iCloud 高频同步与冲突副本破坏去重状态
STATE_HOME = Path(os.environ.get("RAYS_BRAIN_STATE", str(Path.home() / ".local/state/rays-brain")))
STATE_FILE = STATE_HOME / "state.json"
RUN_LOCK_FILE = STATE_HOME / "pipeline.lock"
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
ENTITY_REGISTRY_FILE = SYSTEM_DIR / "entity_registry.json"
TOPIC_MAP_CANDIDATE_STATE = STATE_HOME / "topic-map-candidates.json"
TOPIC_MAP_CANDIDATE_NOTE = VAULT / "00-入口/10-工作台/主题地图候选.md"
MAP_DIGEST_STATE = STATE_HOME / "map-digest.json"
MAP_DIGEST_NOTE = VAULT / "00-入口/10-工作台/知识地图整理.md"
MAP_DIGEST_WEEKLY_DIR = VAULT / "00-入口/20-日报/知识地图整理"
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


def load_topic_attention_days() -> int:
    protocol = load_json(REVIEW_PROTOCOL_FILE, {})
    try:
        days = int(protocol.get("topic_attention", {}).get("candidate_days", 7))
    except (AttributeError, TypeError, ValueError):
        days = 7
    if not 1 <= days <= 30:
        raise RuntimeError("候选注意力期限必须在 1–30 天之间")
    return days


TOPIC_ATTENTION_DAYS = load_topic_attention_days()


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
        "source_health": {},
    }


def load_state() -> dict[str, Any]:
    state = load_json(STATE_FILE, default_state())
    for key, value in default_state().items():
        state.setdefault(key, value)
    return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = now_local()
    atomic_json(STATE_FILE, state)


def acquire_run_lock() -> Any | None:
    STATE_HOME.mkdir(parents=True, exist_ok=True)
    handle = RUN_LOCK_FILE.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()} {now_local()}\n")
    handle.flush()
    return handle


SOURCE_LABELS = {
    "x": "X 书签",
    "mobile": "手机分享",
    "web": "网页剪藏",
    "weread": "微信读书",
}


def record_source_success(state: dict[str, Any], source: str) -> None:
    health = state.setdefault("source_health", {}).setdefault(source, {})
    health.update(
        {
            "label": SOURCE_LABELS.get(source, source),
            "status": "healthy",
            "last_attempt_at": now_local(),
            "last_success_at": now_local(),
            "consecutive_failures": 0,
            "message": "",
        }
    )


def record_source_failure(
    state: dict[str, Any], source: str, message: str
) -> None:
    health = state.setdefault("source_health", {}).setdefault(source, {})
    failures = int(health.get("consecutive_failures", 0) or 0) + 1
    health.update(
        {
            "label": SOURCE_LABELS.get(source, source),
            "status": "degraded",
            "last_attempt_at": now_local(),
            "last_failure_at": now_local(),
            "consecutive_failures": failures,
            "message": str(message)[:1000],
        }
    )


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
    explicit_sections: list[str] = []
    for heading in ("来源摘要", "个人笔记"):
        body = markdown_section(existing_text, heading)
        if body:
            explicit_sections.append(f"## {heading}\n\n{body}")
    explicit = "\n\n".join(explicit_sections)
    if parse_frontmatter(existing_text).get("capture_source") == "x-bookmark":
        return explicit

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
        return explicit
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
_MOBILE_READ_ERRORS: list[str] = []


def mobile_lines_with_status(
    config: dict[str, Any],
) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    errors: list[str] = []
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
                message = f"mobile inbox temporarily unavailable {mobile_file}: {exc}"
                errors.append(message)
                log(message)
    if LINK_INBOX.exists():
        inbox_text = LINK_INBOX.read_text(encoding="utf-8", errors="replace")
        inbox_text = re.sub(r"```.*?```", "", inbox_text, flags=re.S)
        for line in inbox_text.splitlines():
            if URL_RE.search(line):
                lines.append(line)
    return [line.strip() for line in lines if URL_RE.search(line)], errors


def mobile_lines(config: dict[str, Any]) -> list[str]:
    global _MOBILE_READ_ERRORS
    lines, errors = mobile_lines_with_status(config)
    _MOBILE_READ_ERRORS = errors
    return lines


def sync_mobile(state: dict[str, Any], config: dict[str, Any]) -> int:
    global _MOBILE_READ_ERRORS
    known = set(state["mobile_line_hashes"])
    imported = 0
    _MOBILE_READ_ERRORS = []
    lines = mobile_lines(config)
    read_errors = list(_MOBILE_READ_ERRORS)
    if read_errors:
        record_source_failure(state, "mobile", "；".join(read_errors))
    else:
        record_source_success(state, "mobile")
    for line in lines:
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
    read_errors: list[str] = []
    for path in WEB_DIR.glob("*.md"):
        normalize_web_clip_frontmatter(path, errors=read_errors)
        source_rel = rel(path)
        if source_rel in known:
            continue
        known.add(source_rel)
        if source_rel not in state["pending_sources"]:
            state["pending_sources"].append(source_rel)
        discovered += 1
    state["known_web_files"] = sorted(known)
    if read_errors:
        record_source_failure(state, "web", "；".join(read_errors))
    else:
        record_source_success(state, "web")
    return discovered


def normalize_web_clip_frontmatter(
    path: Path, *, errors: list[str] | None = None
) -> bool:
    """Keep Web Clipper sources aligned and avoid accidental unresolved author links."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        if errors is not None:
            errors.append(f"{rel(path)}: {exc}")
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
    read_errors: list[str] = []
    if not WEREAD_DIR.exists():
        state["known_weread_versions"] = known
        record_source_success(state, "weread")
        return discovered

    for path in sorted(WEREAD_DIR.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            message = f"weread card temporarily unavailable {path}: {exc}"
            read_errors.append(message)
            log(message)
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
    if read_errors:
        record_source_failure(state, "weread", "；".join(read_errors))
    else:
        record_source_success(state, "weread")
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


PERSONAL_CONTEXT_HEADINGS = (
    "个人笔记",
    "我的判断",
    "我的实践",
    "我的思考",
    "我的补充",
    "使用感受",
)
PERSONAL_CONTEXT_PLACEHOLDERS = {
    "待判断",
    "待判断。",
    "待补充",
    "待补充。",
    "暂无",
    "无",
}


def personal_context_from_source(text: str) -> str:
    """只读取来源卡中明确属于 Ray 的栏目，不从外部正文猜个人经验。"""
    sections: list[str] = []
    for heading in PERSONAL_CONTEXT_HEADINGS:
        body = markdown_section(text, heading)
        body = re.sub(r"<!--.*?-->", "", body, flags=re.S).strip()
        if not body:
            continue
        visible = [
            re.sub(r"^\s*[-*>]\s*", "", line).strip()
            for line in body.splitlines()
            if line.strip()
        ]
        meaningful = [
            line for line in visible if line not in PERSONAL_CONTEXT_PLACEHOLDERS
        ]
        if not meaningful:
            continue
        sections.append(body[:6000].rstrip())
    return "\n\n".join(dict.fromkeys(sections))


def replace_markdown_section_body(text: str, heading: str, body: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\s*$", text, flags=re.M)
    if not match:
        return text.rstrip() + f"\n\n## {heading}\n\n{body.rstrip()}\n"
    next_heading = re.search(r"^## ", text[match.end() :], flags=re.M)
    end = match.end() + next_heading.start() if next_heading else len(text)
    suffix = text[end:].lstrip("\n")
    rendered = text[: match.end()] + "\n\n" + body.rstrip() + "\n"
    return rendered + ("\n" + suffix if suffix else "")


def add_personal_context(text: str, value: str) -> str:
    value = value.strip()
    if not value:
        return text
    existing = markdown_section(text, "Ray 的经验")
    normalized_existing = re.sub(r"\s+", " ", existing).strip()
    normalized_value = re.sub(r"\s+", " ", value).strip()
    if normalized_value and normalized_value in normalized_existing:
        return text
    combined = "\n\n".join(item for item in (existing, value) if item.strip())
    return replace_markdown_section_body(text, "Ray 的经验", combined)


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


def load_auto_knowledge_policy() -> dict[str, Any]:
    data = load_json(REVIEW_PROTOCOL_FILE, {})
    raw = data.get("auto_routing", {}).get("auto_knowledge", {})
    if not isinstance(raw, dict):
        raise RuntimeError("审核协议的 auto_knowledge 必须是对象")
    try:
        minimum = int(raw.get("min_knowledge_value_score", 90))
        gap = int(raw.get("min_knowledge_over_writing_gap", 5))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("自动知识分流的分数门槛必须是整数") from exc
    if not 0 <= minimum <= 100 or not 0 <= gap <= 100:
        raise RuntimeError("自动知识分流的分数门槛必须在 0–100 之间")
    return {
        "enabled": bool(raw.get("enabled", False)),
        "recommendation": str(raw.get("recommendation", "保留")).strip(),
        "confidence": str(raw.get("confidence", "高")).strip(),
        "min_knowledge_value_score": minimum,
        "min_knowledge_over_writing_gap": gap,
        "require_explicit_complete_units": bool(
            raw.get("require_explicit_complete_units", True)
        ),
        "policy_id": str(raw.get("policy_id", "high-confidence-knowledge-v1")).strip(),
    }


AUTO_KNOWLEDGE_POLICY = load_auto_knowledge_policy()


def load_review_attention_days() -> int:
    data = load_json(REVIEW_PROTOCOL_FILE, {})
    try:
        days = int(data.get("review_attention", {}).get("unreviewed_days", 7))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError("待审核注意力期限必须是整数") from exc
    if not 1 <= days <= 30:
        raise RuntimeError("待审核注意力期限必须在 1–30 天之间")
    return days


REVIEW_ATTENTION_DAYS = load_review_attention_days()


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
                "knowledge_files_json",
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
    "25-产品与模型": "entity",
    "30-问题": "question",
    "40-观点": "viewpoint",
    "50-案例": "case",
    "60-方法": "playbook",
    "70-知识地图": "map",
}

KNOWLEDGE_DESTINATIONS = {
    "概念": "20-知识/10-概念",
    "人物与组织": "20-知识/20-人物与组织",
    "问题": "20-知识/30-问题",
    "观点": "20-知识/40-观点",
    "案例": "20-知识/50-案例",
    "方法": "20-知识/60-方法",
}
VALID_KNOWLEDGE_KINDS = set(KNOWLEDGE_DESTINATIONS)


def knowledge_destination(meta: dict[str, str]) -> Path:
    raw = meta.get("suggested_destination") or KNOWLEDGE_DESTINATIONS.get(
        meta.get("suggested_kind", ""), "20-知识/40-观点"
    )
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts or not raw.startswith("20-知识/"):
        candidate = Path(
            KNOWLEDGE_DESTINATIONS.get(
                meta.get("suggested_kind", ""), "20-知识/40-观点"
            )
        )
    return VAULT / candidate


def string_list(value: Any, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = re.sub(r"\s+", " ", str(item)).strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def normalized_entities(review: dict[str, Any]) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    valid_types = {"人物", "组织", "公司", "产品", "模型"}
    for item in review.get("entities", []) if isinstance(review.get("entities"), list) else []:
        if not isinstance(item, dict):
            continue
        name = re.sub(r"\s+", " ", str(item.get("name", ""))).strip()
        if not name:
            continue
        entity_type = str(item.get("entity_type", "人物")).strip()
        if entity_type not in valid_types:
            entity_type = "人物"
        entities.append(
            {
                "name": name,
                "entity_type": entity_type,
                "aliases": [
                    alias
                    for alias in string_list(item.get("aliases", []), 6)
                    if alias != name
                ],
                "description": re.sub(
                    r"\s+", " ", str(item.get("description", ""))
                ).strip(),
                "viewpoints": string_list(item.get("viewpoints", []), 6),
            }
        )
        if len(entities) >= 8:
            break
    primary_keys = {entity_name_key(entity["name"]) for entity in entities}
    for entity in entities:
        entity["aliases"] = [
            alias
            for alias in entity["aliases"]
            if entity_name_key(alias) not in primary_keys
            or entity_name_key(alias) == entity_name_key(entity["name"])
        ]
    return entities


def normalized_knowledge_units(
    review: dict[str, Any], *, strict: bool = False
) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    entity_keys = {
        entity_name_key(value)
        for entity in normalized_entities(review)
        for value in [entity["name"], *entity.get("aliases", [])]
    }
    raw_units = review.get("knowledge_units")
    if isinstance(raw_units, list):
        for index, item in enumerate(raw_units, 1):
            if not isinstance(item, dict):
                if strict:
                    return []
                continue
            kind = str(item.get("kind", "")).strip()
            title = re.sub(r"\s+", " ", str(item.get("title", ""))).strip()
            if (
                kind == "人物与组织"
                and entity_name_key(title) not in entity_keys
            ):
                if title.endswith(("？", "?")) or title.startswith(("如何", "为什么", "是否")):
                    kind = "问题"
                elif re.search(r"方法|流程|步骤|原则|框架", title):
                    kind = "方法"
                elif re.search(r"实践|案例|转向|演化|路线|线索", title):
                    kind = "案例"
                else:
                    kind = "观点"
            summary = str(item.get("summary", "")).strip()
            core_claims = string_list(item.get("core_claims", []), 5)
            evidence = string_list(item.get("evidence", []), 5)
            meaningful_title = bool(re.search(r"[A-Za-z0-9\u4e00-\u9fff]", title))
            complete = (
                meaningful_title
                and len(title) >= 4
                and len(summary) >= 10
                and bool(core_claims)
                and bool(evidence)
            )
            if kind not in VALID_KNOWLEDGE_KINDS or not meaningful_title:
                if strict:
                    return []
                continue
            if strict and not complete:
                return []
            units.append(
                {
                    "id": f"K{len(units) + 1}",
                    "title": title,
                    "kind": kind,
                    "summary": summary,
                    "core_claims": core_claims,
                    "evidence": evidence,
                    "counterpoints": string_list(item.get("counterpoints", []), 4),
                    "connections": string_list(item.get("connections", []), 6),
                    "people": string_list(item.get("people", []), 8),
                }
            )
            if len(units) >= 8:
                break
    if units:
        return units
    if strict and isinstance(raw_units, list):
        return []
    kind = str(review.get("suggested_kind", "")).strip()
    if kind not in VALID_KNOWLEDGE_KINDS:
        return []
    title = re.sub(r"\s+", " ", str(review.get("title", ""))).strip()
    if not title:
        return []
    return [
        {
            "id": "K1",
            "title": title,
            "kind": kind,
            "summary": str(review.get("summary", "")).strip(),
            "core_claims": string_list(review.get("core_claims", []), 5),
            "evidence": string_list(review.get("evidence", []), 5),
            "counterpoints": string_list(review.get("counterpoints", []), 4),
            "connections": string_list(review.get("connections", []), 6),
            "people": [],
        }
    ]


def json_meta(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def meta_json_list(meta: dict[str, str], key: str) -> list[Any]:
    raw = meta.get(key, "")
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def legacy_unit_from_card(text: str, meta: dict[str, str]) -> dict[str, Any] | None:
    kind = meta.get("suggested_kind", "") or "观点"
    if kind not in VALID_KNOWLEDGE_KINDS:
        return None
    title_match = re.search(r"^# (.+)$", text, flags=re.M)
    raw_title = title_match.group(1).strip() if title_match else "待命名知识"
    title = semantic_knowledge_title(text, raw_title)
    return {
        "id": "K1",
        "title": title,
        "kind": kind,
        "summary": markdown_section(text, "摘要"),
        "core_claims": markdown_bullets(markdown_section(text, "核心观点")),
        "evidence": markdown_bullets(markdown_section(text, "支撑证据")),
        "counterpoints": markdown_bullets(markdown_section(text, "漏洞与反例")),
        "connections": markdown_bullets(markdown_section(text, "与现有知识的连接")),
        "people": markdown_link_titles(markdown_section(text, "人物与组织")),
    }


def knowledge_units_from_card(text: str, meta: dict[str, str]) -> list[dict[str, Any]]:
    raw_units = meta_json_list(meta, "knowledge_units_json")
    if raw_units:
        return normalized_knowledge_units(
            {
                "knowledge_units": raw_units,
                "entities": meta_json_list(meta, "entities_json"),
            }
        )
    legacy = legacy_unit_from_card(text, meta)
    return [legacy] if legacy else []


def entities_from_card(meta: dict[str, str]) -> list[dict[str, Any]]:
    return normalized_entities({"entities": meta_json_list(meta, "entities_json")})


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


def markdown_bullets(body: str) -> list[str]:
    result: list[str] = []
    for line in body.splitlines():
        match = re.match(r"^\s*[-*]\s+(?:\[[ xX]\]\s+)?(.+?)\s*$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if value and value not in {"无", "暂无", "待补充"} and value not in result:
            result.append(value)
    return result


def markdown_link_titles(body: str) -> list[str]:
    return list(
        dict.fromkeys(
            raw.split("|", 1)[-1].split("#", 1)[0].strip()
            for raw in re.findall(r"\[\[([^\]]+)\]\]", body)
            if raw.strip()
        )
    )


def existing_knowledge_for_unit(knowledge_unit_id: str) -> Path | None:
    if not knowledge_unit_id or not KNOWLEDGE_DIR.exists():
        return None
    for path in KNOWLEDGE_DIR.rglob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        if meta.get("knowledge_unit_id") == knowledge_unit_id:
            return path
        if knowledge_unit_id in [
            str(item) for item in meta_json_list(meta, "knowledge_unit_ids_json")
        ]:
            return path
    return None


def existing_knowledge_with_title(title: str) -> Path | None:
    normalized = unicodedata.normalize("NFC", title).casefold()
    if not KNOWLEDGE_DIR.exists():
        return None
    for path in KNOWLEDGE_DIR.rglob("*.md"):
        if path.name == "README.md" or "90-元数据" in path.parts:
            continue
        if unicodedata.normalize("NFC", path.stem).casefold() == normalized:
            return path
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"^#\s+(.+?)\s*$", text, flags=re.M)
        if match and unicodedata.normalize("NFC", match.group(1)).casefold() == normalized:
            return path
    return None


def available_knowledge_path(
    destination: Path, title: str, knowledge_unit_id: str
) -> tuple[Path, bool]:
    existing = existing_knowledge_for_unit(knowledge_unit_id)
    if existing:
        return existing, True
    matching_title = existing_knowledge_with_title(title)
    if matching_title:
        return matching_title, True
    base = sanitize_filename(title, limit=42)
    candidate = destination / f"{base}.md"
    return candidate, False


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


def replace_markdown_section(text: str, heading: str, body: str) -> str:
    block = f"## {heading}\n\n{body.strip()}"
    pattern = re.compile(
        rf"^## {re.escape(heading)}\s*\n.*?(?=^## |\Z)",
        flags=re.M | re.S,
    )
    if pattern.search(text):
        return pattern.sub(block + "\n\n", text, count=1).rstrip() + "\n"
    return text.rstrip() + "\n\n" + block + "\n"


def add_unique_bullets(text: str, heading: str, values: list[str]) -> str:
    values = [value.strip() for value in values if value.strip()]
    if not values:
        return text
    existing = markdown_section(text, heading)
    lines = existing.splitlines() if existing else []
    normalized_existing = {
        unicodedata.normalize("NFC", re.sub(r"^\s*[-*]\s+", "", line).strip())
        for line in lines
    }
    for value in values:
        normalized = unicodedata.normalize("NFC", value)
        if normalized not in normalized_existing:
            lines.append(f"- {value}")
            normalized_existing.add(normalized)
    return replace_markdown_section(text, heading, "\n".join(lines))


def ensure_map_membership(
    note_path: Path,
    map_title: str,
    *,
    section: str = "新增知识",
) -> bool:
    """把已确认的知识关系写回地图；已有链接时不重复。"""
    map_path = existing_knowledge_with_title(map_title)
    if map_path is None:
        return False
    map_text = map_path.read_text(encoding="utf-8", errors="replace")
    note_rel = rel(note_path)[:-3]
    if (
        f"[[{note_path.stem}]]" in map_text
        or f"[[{note_path.stem}|" in map_text
        or f"[[{note_rel}]]" in map_text
        or f"[[{note_rel}|" in map_text
    ):
        return False
    updated = add_unique_bullets(map_text, section, [f"[[{note_path.stem}]]"])
    updated = update_frontmatter(
        updated, {"updated_at": datetime.now().astimezone().date().isoformat()}
    )
    map_path.write_text(updated, encoding="utf-8")
    return True


def connect_unit_to_declared_maps(path: Path, unit: dict[str, Any]) -> int:
    """只采用提炼结果中明确点名的已有地图，不做猜测性归类。"""
    linked = 0
    for connection in string_list(unit.get("connections", []), 6):
        title = re.sub(r"^\[\[|\]\]$", "", connection.strip()).split("|", 1)[0]
        candidate = existing_knowledge_with_title(title)
        if candidate is None:
            continue
        candidate_text = candidate.read_text(encoding="utf-8", errors="replace")
        if not is_theme_map(candidate_text):
            continue
        if ensure_map_membership(path, candidate.stem):
            linked += 1
    return linked


def set_frontmatter_raw(text: str, key: str, raw_value: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end < 0:
        return text
    lines = text[4:end].splitlines()
    replacement = f"{key}: {raw_value}"
    for index, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    return "---\n" + "\n".join(lines) + text[end:]


def knowledge_origin(source_file: str) -> str:
    if "30-X书签" in source_file:
        return "x-bookmark"
    if "40-网页剪藏" in source_file:
        return "web-clip"
    if "50-读书划线" in source_file:
        return "weread"
    return "capture"


def entity_name_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\s·•._\-—–，,。:：'\"()（）/@]+", "", value)


def entity_registry() -> dict[str, Any]:
    return load_json(
        ENTITY_REGISTRY_FILE,
        {"canonical_overrides": [], "model_names": [], "remove_aliases": {}},
    )


def entity_type_family(entity_type: str) -> str:
    if entity_type == "人物":
        return "person"
    if entity_type in {"组织", "公司"}:
        return "organization"
    if entity_type in {"产品", "模型"}:
        return "offering"
    return ""


def entity_root_for_type(entity_type: str) -> Path:
    folder = "25-产品与模型" if entity_type in {"产品", "模型"} else "20-人物与组织"
    return KNOWLEDGE_DIR / folder


def entity_roots() -> tuple[Path, Path]:
    return (
        KNOWLEDGE_DIR / "20-人物与组织",
        KNOWLEDGE_DIR / "25-产品与模型",
    )


def stable_entity_id(name: str, entity_type: str) -> str:
    raw = f"{entity_type}:{entity_name_key(name)}".encode("utf-8")
    return "entity-" + hashlib.sha256(raw).hexdigest()[:16]


def canonicalize_entity(entity: dict[str, Any]) -> dict[str, Any]:
    result = dict(entity)
    name = str(result.get("name", "")).strip()
    aliases = string_list(result.get("aliases", []), 12)
    entity_type = str(result.get("entity_type", "人物")).strip()
    keys = {entity_name_key(value) for value in [name, *aliases] if value}
    registry = entity_registry()
    for override in registry.get("canonical_overrides", []):
        if not isinstance(override, dict):
            continue
        canonical = str(override.get("canonical_name", "")).strip()
        override_aliases = string_list(override.get("aliases", []), 12)
        override_keys = {
            entity_name_key(value)
            for value in [canonical, *override_aliases]
            if value
        }
        if not keys.intersection(override_keys):
            continue
        name = canonical
        entity_type = str(override.get("entity_type", entity_type)).strip()
        aliases = list(
            dict.fromkeys(
                value
                for value in [*aliases, *override_aliases]
                if value and entity_name_key(value) != entity_name_key(name)
            )
        )
        break
    model_keys = {
        entity_name_key(str(value))
        for value in registry.get("model_names", [])
        if str(value).strip()
    }
    if entity_name_key(name) in model_keys:
        entity_type = "模型"
    if entity_type not in {"人物", "组织", "公司", "产品", "模型"}:
        entity_type = "人物"
    removed = {
        entity_name_key(value)
        for value in registry.get("remove_aliases", {}).get(name, [])
    }
    aliases = [
        value
        for value in aliases
        if entity_name_key(value) not in removed
        and entity_name_key(value) != entity_name_key(name)
    ]
    result.update(
        {
            "name": name,
            "entity_type": entity_type,
            "aliases": list(dict.fromkeys(aliases))[:12],
        }
    )
    return result


def entity_aliases(path: Path, text: str) -> list[str]:
    meta = parse_frontmatter(text)
    aliases = [str(item) for item in meta_json_list(meta, "aliases")]
    title = re.search(r"^#\s+(.+?)\s*$", text, flags=re.M)
    return [path.stem, title.group(1).strip() if title else path.stem, *aliases]


def find_entity_profile(
    name: str,
    aliases: list[str] | None = None,
    entity_type: str = "",
) -> Path | None:
    name_key = entity_name_key(name)
    requested_aliases = {
        entity_name_key(value)
        for value in (aliases or [])
        if entity_name_key(value)
    }
    requested_family = entity_type_family(entity_type)
    ranked: list[tuple[int, Path]] = []
    for root in entity_roots():
        if not root.exists():
            continue
        for path in root.glob("*.md"):
            if path.name == "README.md":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            meta = parse_frontmatter(text)
            current_type = meta.get("entity_type", "人物").strip('"') or "人物"
            if requested_family and entity_type_family(current_type) != requested_family:
                continue
            title = re.search(r"^#\s+(.+?)\s*$", text, flags=re.M)
            primary = {
                entity_name_key(path.stem),
                entity_name_key(title.group(1).strip() if title else path.stem),
            }
            profile_aliases = {
                entity_name_key(value)
                for value in meta_json_list(meta, "aliases")
                if entity_name_key(value)
            }
            score = 0
            if name_key in primary:
                score = 40
            elif requested_aliases.intersection(primary):
                score = 30
            elif name_key in profile_aliases:
                score = 20
            elif requested_aliases.intersection(profile_aliases):
                score = 10
            if score:
                ranked.append((score, path))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], item[1].as_posix()))
    top_score = ranked[0][0]
    top = [path for score, path in ranked if score == top_score]
    return top[0] if len(top) == 1 else None


def alias_mentioned(text: str, alias: str) -> bool:
    alias = alias.strip()
    if len(alias) < 2:
        return False
    if re.fullmatch(r"[A-Za-z0-9_.@\- ]+", alias):
        return bool(
            re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])",
                text,
                flags=re.I,
            )
        )
    return alias in text


def entity_profile_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for root in entity_roots():
        if not root.exists():
            continue
        for path in root.glob("*.md"):
            if path.name == "README.md":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            names = list(dict.fromkeys(entity_aliases(path, text)))
            meta = parse_frontmatter(text)
            viewpoints = markdown_bullets(markdown_section(text, "已提取观点"))
            viewpoints += markdown_bullets(markdown_section(text, "已提取信息"))
            records.append(
                {
                    "path": path,
                    "name": path.stem,
                    "entity_id": meta.get("entity_id", "").strip('"'),
                    "entity_type": meta.get("entity_type", "人物").strip('"') or "人物",
                    "aliases": [value for value in names if value != path.stem],
                    "description": markdown_section(text, "关注主题"),
                    "viewpoints": list(dict.fromkeys(viewpoints)),
                }
            )
    return records


def mentioned_entity_records(text: str) -> list[dict[str, Any]]:
    return [
        record
        for record in entity_profile_records()
        if any(alias_mentioned(text, value) for value in [record["name"], *record["aliases"]])
    ]


def entity_for_name(
    name: str, entities: list[dict[str, Any]]
) -> dict[str, Any]:
    wanted = entity_name_key(name)
    for entity in entities:
        names = [entity["name"], *entity.get("aliases", [])]
        if wanted in {entity_name_key(value) for value in names}:
            return entity
    return {
        "name": name,
        "entity_type": "人物",
        "aliases": [],
        "description": "",
        "viewpoints": [],
    }


def declared_entity_for_name(
    name: str, entities: list[dict[str, Any]]
) -> dict[str, Any] | None:
    wanted = entity_name_key(name)
    for entity in entities:
        names = [entity["name"], *entity.get("aliases", [])]
        if wanted in {entity_name_key(value) for value in names}:
            return entity
    return None


def ensure_entity_profile(
    entity: dict[str, Any],
    *,
    source_file: str,
    source_url: str,
    knowledge_path: Path | None = None,
) -> Path:
    entity = canonicalize_entity(entity)
    name = str(entity.get("name", "")).strip()
    entity_type = str(entity.get("entity_type", "人物"))
    aliases = string_list(entity.get("aliases", []), 12)
    existing = find_entity_profile(name, aliases, entity_type)
    root = entity_root_for_type(entity_type)
    root.mkdir(parents=True, exist_ok=True)
    path = existing or root / f"{sanitize_filename(name, limit=42)}.md"
    if existing and existing.parent != root and not (root / existing.name).exists():
        target = root / existing.name
        existing.replace(target)
        path = target
    source_link = f"[[{source_file[:-3]}]]" if source_file else ""
    knowledge_link = (
        f"[[{rel(knowledge_path)[:-3]}]]" if knowledge_path is not None else ""
    )
    index_title = (
        "产品与模型索引"
        if entity_type in {"产品", "模型"}
        else "人物与思想索引"
    )
    info_heading = "已提取信息" if entity_type in {"产品", "模型"} else "已提取观点"
    if path.exists():
        text = path.read_text(encoding="utf-8", errors="replace")
        original = text
        known_aliases = [str(item) for item in meta_json_list(parse_frontmatter(text), "aliases")]
        removed = {
            entity_name_key(value)
            for value in entity_registry().get("remove_aliases", {}).get(name, [])
        }
        merged_aliases = list(
            dict.fromkeys(
                alias
                for alias in [*known_aliases, *aliases]
                if alias
                and entity_name_key(alias) != entity_name_key(path.stem)
                and entity_name_key(alias) not in removed
            )
        )
        text = set_frontmatter_raw(text, "aliases", json.dumps(merged_aliases, ensure_ascii=False))
        text = update_frontmatter(
            text,
            {
                "entity_id": parse_frontmatter(text).get("entity_id", "").strip('"')
                or stable_entity_id(path.stem, entity_type),
                "entity_type": entity_type,
            },
        )
        if info_heading == "已提取信息" and "## 已提取观点" in text:
            text = text.replace("## 已提取观点", "## 已提取信息", 1)
        text = add_unique_bullets(
            text, info_heading, string_list(entity.get("viewpoints", []), 6)
        )
        if knowledge_link:
            text = add_unique_bullets(text, "对应知识卡", [knowledge_link])
        other_index = (
            "[[人物与思想索引]]"
            if index_title == "产品与模型索引"
            else "[[产品与模型索引]]"
        )
        text = re.sub(
            rf"^\s*[-*]\s+{re.escape(other_index)}\s*$\n?",
            "",
            text,
            flags=re.M,
        )
        text = add_unique_bullets(text, "对应知识卡", [f"[[{index_title}]]"])
        if "## 相关人物" in text:
            text = text.replace("## 相关人物", "## 相关对象", 1)
        if source_link:
            text = add_unique_bullets(text, "来源与边界", [source_link])
        if text != original:
            path.write_text(
                update_frontmatter(text, {"updated_at": now_local()}),
                encoding="utf-8",
            )
        ensure_map_membership(
            path,
            index_title,
            section=(
                "新收录人物"
                if entity_type == "人物"
                else "组织与公司"
                if entity_type in {"组织", "公司"}
                else entity_type
            ),
        )
        return path

    description = str(entity.get("description", "")).strip() or "待通过后续资料持续补充。"
    viewpoints = string_list(entity.get("viewpoints", []), 6)
    lines = [
        "---",
        "kind: entity",
        "status: seed",
        "origin: entity-merge",
        f"entity_id: {stable_entity_id(name, entity_type)}",
        f"entity_type: {entity_type}",
        f"created_at: {yaml_string(now_local())}",
        f"updated_at: {yaml_string(now_local())}",
        f"aliases: {json.dumps(aliases, ensure_ascii=False)}",
        f"source_file: {yaml_string(source_file)}",
        f"source_url: {yaml_string(source_url)}",
        "---",
        "",
        f"# {name}",
        "",
        "## 关注主题",
        "",
        description,
        "",
        f"## {info_heading}",
        "",
        *([f"- {value}" for value in viewpoints] or ["- 待通过后续资料补充。"]),
        "",
        "## 对应知识卡",
        "",
        f"- [[{index_title}]]",
    ]
    if knowledge_link:
        lines.append(f"- {knowledge_link}")
    lines.extend(["", "## 相关对象", "", "- 待补充", "", "## 来源与边界", ""])
    if source_link:
        lines.append(f"- {source_link}")
    if source_url:
        lines.append(f"- {source_url}")
    lines.append("- 这里只保留当前知识库来源能够支持的信息；预测和时效性判断需要持续更新。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ensure_map_membership(
        path,
        index_title,
        section=(
            "新收录人物"
            if entity_type == "人物"
            else "组织与公司"
            if entity_type in {"组织", "公司"}
            else entity_type
        ),
    )
    return path


def link_related_entity_profiles(profile_paths: list[Path]) -> int:
    """把同一份资料中明确声明的人物、组织、公司、产品和模型互相连接。"""
    paths = list(dict.fromkeys(path for path in profile_paths if path.is_file()))
    updated_count = 0
    for path in paths:
        related = [other for other in paths if other != path]
        if not related:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        updated = text.replace("## 相关人物", "## 相关对象", 1)
        updated = re.sub(r"(## 相关对象\s*\n+)- 待补充\s*\n", r"\1", updated, count=1)
        updated = add_unique_bullets(
            updated,
            "相关对象",
            [f"[[{other.stem}]]" for other in related],
        )
        if updated != text:
            path.write_text(
                update_frontmatter(updated, {"updated_at": now_local()}),
                encoding="utf-8",
            )
            updated_count += 1
    return updated_count


def add_people_links(path: Path, people_paths: list[Path]) -> None:
    if not people_paths or path.parent in entity_roots():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    links = [f"[[{person.stem}]]" for person in people_paths]
    if "## 人物连接" in text:
        text = text.replace("## 人物连接", "## 相关对象", 1)
    updated = add_unique_bullets(text, "相关对象", links)
    if updated != text:
        path.write_text(updated, encoding="utf-8")


def merge_knowledge_note(
    path: Path,
    unit: dict[str, Any],
    *,
    knowledge_unit_id: str,
    card_rel: str,
    source_file: str,
    source_url: str,
    personal_context: str = "",
) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    meta = parse_frontmatter(text)
    ids = [str(item) for item in meta_json_list(meta, "knowledge_unit_ids_json")]
    if knowledge_unit_id in ids or meta.get("knowledge_unit_id") == knowledge_unit_id:
        if personal_context:
            updated = add_personal_context(text, personal_context)
            updated = update_frontmatter(
                updated, {"personal_context_status": "captured", "updated_at": now_local()}
            )
            if updated != text:
                path.write_text(updated, encoding="utf-8")
        return
    ids = list(dict.fromkeys([*ids, knowledge_unit_id]))
    text = set_frontmatter_raw(text, "knowledge_unit_ids_json", json.dumps(ids, ensure_ascii=False))
    text = update_frontmatter(text, {"updated_at": now_local()})
    text = add_unique_bullets(
        text, "关键观点", string_list(unit.get("core_claims", []), 5)
    )
    text = add_unique_bullets(
        text, "支撑证据", string_list(unit.get("evidence", []), 5)
    )
    text = add_unique_bullets(
        text, "边界与反例", string_list(unit.get("counterpoints", []), 4)
    )
    connections = [
        value if "[[" in value else f"[[{value}]]"
        for value in string_list(unit.get("connections", []), 6)
        if existing_knowledge_with_title(value)
    ]
    text = add_unique_bullets(text, "知识连接", connections)
    source_lines = [f"审核卡：[[{card_rel[:-3]}]]"]
    if source_file:
        source_lines.append(f"原始资料：[[{source_file[:-3]}]]")
    if source_url:
        source_lines.append(f"原始链接：{source_url}")
    text = add_unique_bullets(text, "来源与复核", source_lines)
    if personal_context:
        text = add_personal_context(text, personal_context)
        text = update_frontmatter(text, {"personal_context_status": "captured"})
    path.write_text(text, encoding="utf-8")


def create_knowledge_unit_note(
    card_path: Path,
    meta: dict[str, str],
    unit: dict[str, Any],
    entities: list[dict[str, Any]],
    personal_context: str = "",
) -> Path:
    title = str(unit["title"]).strip()
    capture_id = meta.get("capture_id") or rel(card_path)
    knowledge_unit_id = f"{capture_id}#{unit.get('id', 'K1')}"
    source_file = meta.get("source_file", "")
    source_url = meta.get("source_url", "")
    card_rel = rel(card_path)

    if unit["kind"] == "人物与组织":
        entity = entity_for_name(title, entities)
        if not entity.get("description"):
            entity["description"] = str(unit.get("summary", ""))
        entity["viewpoints"] = list(
            dict.fromkeys(
                [
                    *string_list(entity.get("viewpoints", []), 6),
                    *string_list(unit.get("core_claims", []), 5),
                ]
            )
        )[:6]
        return ensure_entity_profile(
            entity,
            source_file=source_file,
            source_url=source_url,
        )

    destination = knowledge_destination({"suggested_kind": unit["kind"]})
    destination.mkdir(parents=True, exist_ok=True)
    path, existed = available_knowledge_path(destination, title, knowledge_unit_id)
    if existed:
        merge_knowledge_note(
            path,
            unit,
            knowledge_unit_id=knowledge_unit_id,
            card_rel=card_rel,
            source_file=source_file,
            source_url=source_url,
            personal_context=personal_context,
        )
    else:
        knowledge_id = "knowledge-" + hashlib.sha256(
            f"{knowledge_unit_id}\n{title}".encode("utf-8")
        ).hexdigest()[:12]
        kind = KIND_BY_FOLDER.get(destination.name, "concept")
        sections = [
            ("核心摘要", str(unit.get("summary", "")).strip()),
            (
                "关键观点",
                "\n".join(f"- {value}" for value in string_list(unit.get("core_claims", []), 5)),
            ),
            (
                "支撑证据",
                "\n".join(f"- {value}" for value in string_list(unit.get("evidence", []), 5)),
            ),
            (
                "边界与反例",
                "\n".join(f"- {value}" for value in string_list(unit.get("counterpoints", []), 4)),
            ),
            (
                "知识连接",
                linkify_connections(
                    "\n".join(
                        f"- {value}"
                        for value in string_list(unit.get("connections", []), 6)
                    )
                ),
            ),
        ]
        lines = [
            "---",
            f"kind: {kind}",
            "status: seed",
            f"origin: {knowledge_origin(source_file)}",
            f"created_at: {yaml_string(now_local())}",
            f"updated_at: {yaml_string(now_local())}",
            f"knowledge_id: {knowledge_id}",
            f"knowledge_unit_id: {yaml_string(knowledge_unit_id)}",
            f"knowledge_unit_ids_json: {json.dumps([knowledge_unit_id], ensure_ascii=False)}",
            f"capture_id: {yaml_string(capture_id)}",
            f"source_review: {yaml_string(card_rel)}",
            f"source_file: {yaml_string(source_file)}",
            f"source_url: {yaml_string(source_url)}",
            *(
                ["personal_context_status: captured"]
                if personal_context
                else []
            ),
            "---",
            "",
            f"# {title}",
        ]
        for heading, body in sections:
            lines.extend(["", f"## {heading}", "", body or "待补充"])
        if personal_context:
            lines.extend(["", "## Ray 的经验", "", personal_context])
        lines.extend(["", "## 来源与复核", "", f"- 审核卡：[[{card_rel[:-3]}]]"])
        if source_file:
            lines.append(f"- 原始资料：[[{source_file[:-3]}]]")
        if source_url:
            lines.append(f"- 原始链接：{source_url}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    people_paths: list[Path] = []
    for name in string_list(unit.get("people", []), 8):
        entity = declared_entity_for_name(name, entities)
        if entity is None:
            existing_profile = find_entity_profile(name)
            if existing_profile is None:
                continue
            entity = {
                "name": existing_profile.stem,
                "entity_type": parse_frontmatter(
                    existing_profile.read_text(encoding="utf-8", errors="replace")
                ).get("entity_type", "人物").strip('"'),
                "aliases": [name] if name != existing_profile.stem else [],
                "description": "",
                "viewpoints": [],
            }
        person_path = ensure_entity_profile(
            entity,
            source_file=source_file,
            source_url=source_url,
            knowledge_path=path,
        )
        people_paths.append(person_path)
    add_people_links(path, people_paths)
    connect_unit_to_declared_maps(path, unit)
    return path


def create_knowledge_notes(
    card_path: Path, text: str, meta: dict[str, str]
) -> list[Path]:
    units = knowledge_units_from_card(text, meta)
    declared_entities = entities_from_card(meta)
    entities = list(declared_entities)
    source_file = meta.get("source_file", "")
    source_path = VAULT / source_file
    source_text = ""
    if source_file and source_path.exists():
        try:
            source_text = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            source_text = ""
    mentioned = mentioned_entity_records(source_text) if source_text else []
    personal_context = personal_context_from_source(source_text) if source_text else ""
    existing_entity_keys = {
        entity_name_key(value)
        for entity in entities
        for value in [entity["name"], *entity.get("aliases", [])]
    }
    for record in mentioned:
        if entity_name_key(record["name"]) not in existing_entity_keys:
            entities.append(
                {
                    "name": record["name"],
                    "entity_type": record["entity_type"],
                    "aliases": record["aliases"],
                    "description": record["description"],
                    "viewpoints": [],
                }
            )
    for unit in units:
        unit_text = " ".join(
            [
                str(unit.get("title", "")),
                str(unit.get("summary", "")),
                *string_list(unit.get("core_claims", []), 5),
                *string_list(unit.get("evidence", []), 5),
            ]
        )
        inferred = [
            record["name"]
            for record in mentioned
            if len(mentioned) == 1
            or any(
                alias_mentioned(unit_text, value)
                for value in [record["name"], *record["aliases"]]
            )
        ]
        unit["people"] = list(
            dict.fromkeys(
                [
                    *string_list(unit.get("people", []), 8),
                    *inferred,
                ]
            )
        )[:8]
    paths: list[Path] = []
    personal_context_attached = False
    for unit in units:
        unit_personal_context = (
            personal_context
            if personal_context and not personal_context_attached and unit["kind"] != "人物与组织"
            else ""
        )
        path = create_knowledge_unit_note(
            card_path,
            meta,
            unit,
            entities,
            personal_context=unit_personal_context,
        )
        if unit_personal_context:
            personal_context_attached = True
        if path not in paths:
            paths.append(path)
    declared_profiles = [
        ensure_entity_profile(
            entity,
            source_file=source_file,
            source_url=meta.get("source_url", ""),
        )
        for entity in declared_entities
    ]
    link_related_entity_profiles(declared_profiles)
    return paths


def reconcile_people_links() -> dict[str, int]:
    """用对象档案和别名补齐现有长期知识的双向连接，不创建猜测性对象。"""
    records = entity_profile_records()
    linked_notes = 0
    updated_profiles: set[Path] = set()
    allowed_kinds = {"concept", "question", "viewpoint", "case", "playbook"}
    for path in KNOWLEDGE_DIR.rglob("*.md"):
        if path.name == "README.md" or path.parent in entity_roots():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if parse_frontmatter(text).get("kind") not in allowed_kinds:
            continue
        matches = [
            record
            for record in records
            if any(
                alias_mentioned(text, value)
                for value in [record["name"], *record["aliases"]]
            )
        ]
        if not matches:
            continue
        before = text
        add_people_links(path, [record["path"] for record in matches])
        after = path.read_text(encoding="utf-8", errors="replace")
        if after != before:
            linked_notes += 1
        for record in matches:
            profile = record["path"]
            profile_text = profile.read_text(encoding="utf-8", errors="replace")
            updated = add_unique_bullets(
                profile_text,
                "对应知识卡",
                [
                    f"[[{rel(path)[:-3]}]]",
                    (
                        "[[产品与模型索引]]"
                        if record["entity_type"] in {"产品", "模型"}
                        else "[[人物与思想索引]]"
                    ),
                ],
            )
            if updated != profile_text:
                profile.write_text(
                    update_frontmatter(updated, {"updated_at": now_local()}),
                    encoding="utf-8",
                )
                updated_profiles.add(profile)
    return {
        "linked_notes": linked_notes,
        "updated_profiles": len(updated_profiles),
        "profiles": len(records),
    }


def reconcile_entity_relationships() -> dict[str, int]:
    """按已处理审核卡中的共同出现关系，补齐人物与组织档案之间的连接。"""
    reviewed_cards = 0
    updated_profiles = 0
    for review_path in REVIEW_RECORD_DIR.rglob("*.md"):
        text = review_path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        if meta.get("review_action", "").strip().strip('"') not in {
            "both",
            "knowledge",
            "topic",
        }:
            continue
        entities = entities_from_card(meta)
        if len(entities) < 2:
            continue
        source_file = meta.get("source_file", "").strip().strip('"')
        source_url = meta.get("source_url", "").strip().strip('"')
        profiles = [
            ensure_entity_profile(
                entity,
                source_file=source_file,
                source_url=source_url,
            )
            for entity in entities
        ]
        reviewed_cards += 1
        updated_profiles += link_related_entity_profiles(profiles)
    return {
        "entity_relationship_cards": reviewed_cards,
        "entity_relationship_profiles": updated_profiles,
    }


def reconcile_map_links() -> dict[str, int]:
    """补齐实体索引，以及审核结果中已经明确声明的地图关系。"""
    indexed_entities = 0
    declared_map_links = 0
    for record in entity_profile_records():
        entity_type = record["entity_type"]
        if entity_type in {"产品", "模型"}:
            remove_map_membership(record["path"].stem, "人物与思想索引")
            map_title = "产品与模型索引"
            section = entity_type
        else:
            remove_map_membership(record["path"].stem, "产品与模型索引")
            map_title = "人物与思想索引"
            section = "新收录人物" if entity_type == "人物" else "组织与公司"
        if ensure_map_membership(
            record["path"],
            map_title,
            section=section,
        ):
            indexed_entities += 1

    for root in (REVIEW_DIR, REVIEW_RECORD_DIR):
        if not root.exists():
            continue
        for review_path in root.rglob("*.md"):
            text = review_path.read_text(encoding="utf-8", errors="replace")
            meta = parse_frontmatter(text)
            units = knowledge_units_from_card(text, meta)
            if not units:
                continue
            paths = [
                VAULT / str(value)
                for value in meta_json_list(meta, "knowledge_files_json")
                if str(value).strip()
            ]
            legacy = meta.get("knowledge_file", "").strip().strip('"')
            if legacy and VAULT / legacy not in paths:
                paths.insert(0, VAULT / legacy)
            by_title = {
                unicodedata.normalize("NFC", path.stem).casefold(): path
                for path in paths
                if path.is_file()
            }
            for unit in units:
                path = by_title.get(
                    unicodedata.normalize("NFC", str(unit["title"])).casefold()
                )
                if path is not None:
                    declared_map_links += connect_unit_to_declared_maps(path, unit)
    return {
        "indexed_entities": indexed_entities,
        "declared_map_links": declared_map_links,
    }


def reconcile_map_graph_links(max_rounds: int = 6) -> dict[str, int]:
    """沿已经存在的知识连接继承主题地图，最多进入两个最相关地图。"""
    map_paths: list[Path] = []
    note_paths: list[Path] = []
    for path in KNOWLEDGE_DIR.rglob("*.md"):
        if path.name == "README.md" or "90-元数据" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        kind = parse_frontmatter(text).get("kind")
        if kind == "map" and is_theme_map(text):
            map_paths.append(path)
        elif kind not in {"map", "guide", "entity"}:
            note_paths.append(path)

    def keys_for(path: Path) -> set[str]:
        return {
            unicodedata.normalize("NFC", path.stem).casefold(),
            unicodedata.normalize("NFC", rel(path)[:-3]).casefold(),
        }

    note_lookup: dict[str, Path] = {}
    for path in note_paths:
        for key in keys_for(path):
            note_lookup[key] = path
    memberships: dict[Path, set[Path]] = {path: set() for path in note_paths}
    for map_path in map_paths:
        map_text = map_path.read_text(encoding="utf-8", errors="replace")
        for raw in re.findall(r"\[\[([^\]]+)\]\]", map_text):
            target = raw.split("|", 1)[0].split("#", 1)[0].strip().removesuffix(".md")
            target_path = note_lookup.get(
                unicodedata.normalize("NFC", target).casefold()
            )
            if target_path is not None:
                memberships[target_path].add(map_path)

    added = 0
    rounds = 0
    for round_index in range(max_rounds):
        planned: list[tuple[Path, list[Path]]] = []
        for path in note_paths:
            if memberships[path]:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            scores: dict[Path, int] = {}
            for raw in re.findall(r"\[\[([^\]]+)\]\]", text):
                target = raw.split("|", 1)[0].split("#", 1)[0].strip().removesuffix(".md")
                linked_path = note_lookup.get(
                    unicodedata.normalize("NFC", target).casefold()
                )
                if linked_path is None or linked_path == path:
                    continue
                for map_path in memberships.get(linked_path, set()):
                    scores[map_path] = scores.get(map_path, 0) + 1
            if scores:
                ranked = sorted(
                    scores,
                    key=lambda candidate: (-scores[candidate], candidate.stem),
                )[:2]
                planned.append((path, ranked))
        if not planned:
            break
        rounds = round_index + 1
        for path, maps in planned:
            for map_path in maps:
                if ensure_map_membership(path, map_path.stem):
                    added += 1
                memberships[path].add(map_path)
    return {
        "graph_map_links": added,
        "graph_rounds": rounds,
        "graph_uncovered": sum(not value for value in memberships.values()),
    }


def remove_map_membership(
    note_title: str, map_title: str, *, section: str | None = None
) -> bool:
    map_path = existing_knowledge_with_title(map_title)
    if map_path is None:
        return False
    text = map_path.read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(
        rf"^\s*[-*]\s+\[\[{re.escape(note_title)}(?:[|#][^\]]*)?\]\].*\n?",
        flags=re.M,
    )
    if section:
        body = markdown_section(text, section)
        updated_body = pattern.sub("", body)
        updated = (
            replace_markdown_section(text, section, updated_body)
            if updated_body != body
            else text
        )
    else:
        updated = pattern.sub("", text)
    if updated == text:
        return False
    map_path.write_text(
        update_frontmatter(
            updated,
            {"updated_at": datetime.now().astimezone().date().isoformat()},
        ),
        encoding="utf-8",
    )
    return True


def repair_descriptive_entity_units() -> dict[str, int]:
    """把“某人的实践/观点”等错误人物卡恢复成观点或案例卡。"""
    repaired = 0
    for review_path in REVIEW_RECORD_DIR.rglob("*.md"):
        text = review_path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        raw_units = meta_json_list(meta, "knowledge_units_json")
        entities = meta_json_list(meta, "entities_json")
        if not raw_units:
            continue
        normalized = normalized_knowledge_units(
            {"knowledge_units": raw_units, "entities": entities}
        )
        knowledge_rels = [
            str(value)
            for value in meta_json_list(meta, "knowledge_files_json")
            if str(value).strip()
        ]
        changed = False
        for raw_unit, unit in zip(raw_units, normalized):
            if (
                raw_unit.get("kind") != "人物与组织"
                or unit["kind"] == "人物与组织"
            ):
                continue
            old_path = next(
                (
                    VAULT / value
                    for value in knowledge_rels
                    if Path(value).stem == str(raw_unit.get("title", "")).strip()
                ),
                None,
            )
            if (
                old_path is None
                or not old_path.is_file()
                or old_path.parent not in entity_roots()
            ):
                continue
            archive = (
                VAULT
                / "90-归档/90-待清理"
                / datetime.now().astimezone().date().isoformat()
                / "错误分类知识"
                / old_path.name
            )
            archive.parent.mkdir(parents=True, exist_ok=True)
            suffix = 2
            base_archive = archive
            while archive.exists():
                archive = base_archive.with_name(
                    f"{base_archive.stem}-{suffix}{base_archive.suffix}"
                )
                suffix += 1
            shutil.move(str(old_path), str(archive))
            new_path = create_knowledge_unit_note(
                review_path,
                meta,
                unit,
                normalized_entities({"entities": entities}),
            )
            old_rel = rel(old_path)
            new_rel = rel(new_path)
            knowledge_rels = [
                new_rel if value == old_rel else value for value in knowledge_rels
            ]
            remove_map_membership(old_path.stem, "人物与思想索引")
            remove_map_membership(old_path.stem, "产品与模型索引")
            for key in ("knowledge_file", "result_file"):
                if meta.get(key, "").strip().strip('"') == old_rel:
                    meta[key] = new_rel
            repaired += 1
            changed = True
        if changed:
            updates = {
                "knowledge_files_json": json_meta(knowledge_rels),
            }
            for key in ("knowledge_file", "result_file"):
                if meta.get(key):
                    updates[key] = meta[key]
            review_path.write_text(update_frontmatter(text, updates), encoding="utf-8")
    return {"reclassified_entity_units": repaired}


def reconcile_declared_entity_types() -> dict[str, int]:
    """以审核卡中明确提取的实体类型纠正既有人物/组织档案。"""
    updated: set[Path] = set()
    for review_path in REVIEW_RECORD_DIR.rglob("*.md"):
        meta = parse_frontmatter(
            review_path.read_text(encoding="utf-8", errors="replace")
        )
        source_file = meta.get("source_file", "")
        source_url = meta.get("source_url", "")
        referenced_keys = {
            entity_name_key(name)
            for unit in knowledge_units_from_card(
                review_path.read_text(encoding="utf-8", errors="replace"), meta
            )
            for name in string_list(unit.get("people", []), 8)
        }
        for entity in entities_from_card(meta):
            profile = find_entity_profile(
                entity["name"],
                entity.get("aliases", []),
                str(entity.get("entity_type", "")),
            )
            entity_keys = {
                entity_name_key(value)
                for value in [entity["name"], *entity.get("aliases", [])]
            }
            if profile is None and not entity_keys.intersection(referenced_keys):
                continue
            if profile is None:
                created = ensure_entity_profile(
                    entity,
                    source_file=source_file,
                    source_url=source_url,
                )
                updated.add(created)
                continue
            before = profile.read_text(encoding="utf-8", errors="replace")
            ensure_entity_profile(
                entity,
                source_file=source_file,
                source_url=source_url,
            )
            if profile.read_text(encoding="utf-8", errors="replace") != before:
                updated.add(profile)
    return {"corrected_entity_profiles": len(updated)}


def normalize_profile_entity_types() -> dict[str, int]:
    """旧人物档案没有类型时，按原目录语义补为人物。"""
    updated = 0
    for root in entity_roots():
        default_type = "产品" if root.name == "25-产品与模型" else "人物"
        for path in root.glob("*.md"):
            if path.name == "README.md":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if parse_frontmatter(text).get("entity_type", "").strip('"'):
                continue
            path.write_text(
                update_frontmatter(
                    text,
                    {"entity_type": default_type, "updated_at": now_local()},
                ),
                encoding="utf-8",
            )
            updated += 1
    return {"typed_legacy_profiles": updated}


def entity_profile_by_stem(stem: str, *, exclude: Path | None = None) -> Path | None:
    wanted = entity_name_key(stem)
    matches = [
        path
        for root in entity_roots()
        if root.exists()
        for path in root.glob("*.md")
        if path.name != "README.md"
        and path != exclude
        and entity_name_key(path.stem) == wanted
    ]
    return matches[0] if len(matches) == 1 else None


def rewrite_entity_references(
    old_name: str,
    new_name: str,
    old_rel: str,
    new_rel: str,
) -> int:
    updated_files = 0
    old_no_suffix = old_rel.removesuffix(".md")
    new_no_suffix = new_rel.removesuffix(".md")
    wiki_pattern = re.compile(
        rf"(?<=\[\[){re.escape(old_name)}(?=(?:[|#]|\]\]))"
    )
    for path in VAULT.rglob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        updated = text.replace(old_rel, new_rel)
        updated = updated.replace(old_no_suffix, new_no_suffix)
        updated = wiki_pattern.sub(new_name, updated)
        if updated == text:
            continue
        path.write_text(updated, encoding="utf-8")
        updated_files += 1
    return updated_files


def merge_entity_profile_text(
    target_text: str,
    source_text: str,
    *,
    canonical_name: str,
    entity_type: str,
) -> str:
    target_meta = parse_frontmatter(target_text)
    source_meta = parse_frontmatter(source_text)
    aliases = list(
        dict.fromkeys(
            value
            for value in [
                *[str(item) for item in meta_json_list(target_meta, "aliases")],
                *[str(item) for item in meta_json_list(source_meta, "aliases")],
                re.search(r"^#\s+(.+?)\s*$", source_text, flags=re.M).group(1).strip()
                if re.search(r"^#\s+(.+?)\s*$", source_text, flags=re.M)
                else "",
            ]
            if value and entity_name_key(value) != entity_name_key(canonical_name)
        )
    )
    removed = {
        entity_name_key(value)
        for value in entity_registry().get("remove_aliases", {}).get(canonical_name, [])
    }
    aliases = [value for value in aliases if entity_name_key(value) not in removed]
    updated = set_frontmatter_raw(
        target_text,
        "aliases",
        json.dumps(aliases, ensure_ascii=False),
    )
    updates = {
        "entity_id": target_meta.get("entity_id", "").strip('"')
        or stable_entity_id(canonical_name, entity_type),
        "entity_type": entity_type,
        "updated_at": now_local(),
    }
    if not target_meta.get("source_file") and source_meta.get("source_file"):
        updates["source_file"] = source_meta["source_file"]
    if not target_meta.get("source_url") and source_meta.get("source_url"):
        updates["source_url"] = source_meta["source_url"]
    updated = update_frontmatter(updated, updates)

    target_focus = markdown_section(updated, "关注主题").strip()
    source_focus = markdown_section(source_text, "关注主题").strip()
    if source_focus and source_focus not in target_focus:
        updated = replace_markdown_section(
            updated,
            "关注主题",
            "\n\n".join(value for value in [target_focus, source_focus] if value),
        )

    info_heading = "已提取信息" if entity_type in {"产品", "模型"} else "已提取观点"
    if info_heading == "已提取信息" and "## 已提取观点" in updated:
        updated = updated.replace("## 已提取观点", "## 已提取信息", 1)
    source_points = markdown_bullets(markdown_section(source_text, "已提取观点"))
    source_points += markdown_bullets(markdown_section(source_text, "已提取信息"))
    updated = add_unique_bullets(updated, info_heading, source_points)
    updated = add_unique_bullets(
        updated,
        "对应知识卡",
        markdown_bullets(markdown_section(source_text, "对应知识卡")),
    )
    if "## 相关人物" in updated:
        updated = updated.replace("## 相关人物", "## 相关对象", 1)
    related = markdown_bullets(markdown_section(source_text, "相关人物"))
    related += markdown_bullets(markdown_section(source_text, "相关对象"))
    updated = add_unique_bullets(updated, "相关对象", related)
    updated = add_unique_bullets(
        updated,
        "来源与边界",
        markdown_bullets(markdown_section(source_text, "来源与边界")),
    )
    wrong_index = (
        "[[人物与思想索引]]"
        if entity_type in {"产品", "模型"}
        else "[[产品与模型索引]]"
    )
    updated = re.sub(
        rf"^\s*[-*]\s+{re.escape(wrong_index)}\s*$\n?",
        "",
        updated,
        flags=re.M,
    )
    return updated


def dedupe_map_wikilink_bullets(text: str) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*[-*]\s+\[\[([^\]|#]+)", line)
        if not match:
            lines.append(line)
            continue
        key = unicodedata.normalize("NFC", match.group(1).strip()).casefold()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def dedupe_section_wikilink_bullets(text: str) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for line in text.splitlines():
        if re.match(r"^#{1,6}\s+", line):
            seen = set()
            lines.append(line)
            continue
        match = re.match(r"^\s*[-*]\s+\[\[([^\]|#]+)", line)
        if not match:
            lines.append(line)
            continue
        key = unicodedata.normalize("NFC", match.group(1).strip()).casefold()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def migrate_entity_layout(*, dry_run: bool = False) -> dict[str, Any]:
    """合并已确认的重复身份，并让实体类型、目录、索引和稳定身份保持一致。"""
    registry = entity_registry()
    planned_merges: list[str] = []
    planned_renames: list[str] = []
    planned_moves: list[str] = []
    planned_types: list[str] = []
    planned_ids: list[str] = []
    paths = [
        path
        for root in entity_roots()
        if root.exists()
        for path in sorted(root.glob("*.md"))
        if path.name != "README.md"
    ]
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        entity = canonicalize_entity(
            {
                "name": path.stem,
                "entity_type": meta.get("entity_type", "人物").strip('"') or "人物",
                "aliases": [str(item) for item in meta_json_list(meta, "aliases")],
            }
        )
        canonical_name = entity["name"]
        entity_type = entity["entity_type"]
        desired_root = entity_root_for_type(entity_type)
        target = (
            entity_profile_by_stem(canonical_name, exclude=path)
            if entity_name_key(canonical_name) != entity_name_key(path.stem)
            else None
        )
        if target is not None:
            planned_merges.append(f"{path.stem} → {target.stem}")
            if dry_run:
                continue
            target_text = target.read_text(encoding="utf-8", errors="replace")
            merged = merge_entity_profile_text(
                target_text,
                text,
                canonical_name=target.stem,
                entity_type=entity_type,
            )
            target.write_text(merged, encoding="utf-8")
            old_rel = rel(path)
            new_rel = rel(target)
            archive = (
                VAULT
                / "90-归档/90-待清理"
                / datetime.now().astimezone().date().isoformat()
                / "重复实体档案"
                / path.name
            )
            archive.parent.mkdir(parents=True, exist_ok=True)
            suffix = 2
            while archive.exists():
                archive = archive.with_name(f"{archive.stem}-{suffix}{archive.suffix}")
                suffix += 1
            path.replace(archive)
            rewrite_entity_references(path.stem, target.stem, old_rel, new_rel)
            continue
        current = path
        if entity_name_key(canonical_name) != entity_name_key(path.stem):
            planned_renames.append(f"{path.stem} → {canonical_name}")
            if not dry_run:
                renamed = path.with_name(f"{sanitize_filename(canonical_name, limit=42)}.md")
                old_rel = rel(path)
                path.replace(renamed)
                rewrite_entity_references(path.stem, renamed.stem, old_rel, rel(renamed))
                current = renamed
        if not meta.get("entity_id", "").strip('"'):
            planned_ids.append(current.stem)
        if meta.get("entity_type", "").strip('"') != entity_type:
            planned_types.append(f"{current.stem}: {entity_type}")
        if current.parent != desired_root:
            planned_moves.append(f"{current.parent.name}/{current.name} → {desired_root.name}")
        if dry_run:
            continue
        text = current.read_text(encoding="utf-8", errors="replace")
        text = re.sub(
            r"^#\s+.+?$",
            f"# {current.stem}",
            text,
            count=1,
            flags=re.M,
        )
        aliases = list(
            dict.fromkeys(
                value
                for value in entity.get("aliases", [])
                if entity_name_key(value) != entity_name_key(current.stem)
            )
        )
        text = set_frontmatter_raw(text, "aliases", json.dumps(aliases, ensure_ascii=False))
        text = update_frontmatter(
            text,
            {
                "entity_id": parse_frontmatter(text).get("entity_id", "").strip('"')
                or stable_entity_id(current.stem, entity_type),
                "entity_type": entity_type,
                "updated_at": now_local(),
            },
        )
        if entity_type in {"产品", "模型"} and "## 已提取观点" in text:
            text = text.replace("## 已提取观点", "## 已提取信息", 1)
        if "## 相关人物" in text:
            text = text.replace("## 相关人物", "## 相关对象", 1)
        index_title = (
            "产品与模型索引"
            if entity_type in {"产品", "模型"}
            else "人物与思想索引"
        )
        other_index = (
            "[[人物与思想索引]]"
            if index_title == "产品与模型索引"
            else "[[产品与模型索引]]"
        )
        text = re.sub(
            rf"^\s*[-*]\s+{re.escape(other_index)}\s*$\n?",
            "",
            text,
            flags=re.M,
        )
        text = add_unique_bullets(text, "对应知识卡", [f"[[{index_title}]]"])
        current.write_text(
            dedupe_section_wikilink_bullets(text),
            encoding="utf-8",
        )
        if current.parent != desired_root:
            desired_root.mkdir(parents=True, exist_ok=True)
            target_path = desired_root / current.name
            old_rel = rel(current)
            current.replace(target_path)
            rewrite_entity_references(
                current.stem,
                target_path.stem,
                old_rel,
                rel(target_path),
            )
    if not dry_run:
        people_index = KNOWLEDGE_DIR / "70-知识地图/人物与思想索引.md"
        if people_index.exists():
            text = people_index.read_text(encoding="utf-8", errors="replace")
            text = re.sub(
                r"^## 组织、公司与产品\s*\n.*?(?=^## 新收录人物|\Z)",
                "",
                text,
                flags=re.M | re.S,
            )
            if "## 组织与公司" not in text:
                text = text.replace(
                    "## 新收录人物",
                    "## 组织与公司\n\n## 新收录人物",
                    1,
                )
            people_index.write_text(
                dedupe_map_wikilink_bullets(text),
                encoding="utf-8",
            )
        reconcile_map_links()
        for index_name in ("人物与思想索引.md", "产品与模型索引.md"):
            index_path = KNOWLEDGE_DIR / "70-知识地图" / index_name
            if index_path.exists():
                index_path.write_text(
                    dedupe_map_wikilink_bullets(
                        index_path.read_text(encoding="utf-8", errors="replace")
                    ),
                    encoding="utf-8",
                )
        for note_path in KNOWLEDGE_DIR.rglob("*.md"):
            if note_path.name == "README.md" or note_path.parent.name == "70-知识地图":
                continue
            text = note_path.read_text(encoding="utf-8", errors="replace")
            if "## 人物连接" in text:
                text = text.replace("## 人物连接", "## 相关对象", 1)
            updated = dedupe_section_wikilink_bullets(text)
            if updated != text:
                note_path.write_text(updated, encoding="utf-8")
    return {
        "dry_run": dry_run,
        "merges": planned_merges,
        "renames": planned_renames,
        "moves": planned_moves,
        "type_updates": planned_types,
        "entity_ids_added": planned_ids,
        "registry_overrides": len(registry.get("canonical_overrides", [])),
    }


def reconcile_writing_knowledge_handoff() -> dict[str, int]:
    """候选知识增加后，把新增引用同步到已经存在的写作任务。"""
    updated_tasks = 0
    added_links = 0
    if not WRITING_TASK_DIR.exists():
        return {"updated_writing_tasks": 0, "writing_knowledge_links": 0}
    for task_path in WRITING_TASK_DIR.glob("*.md"):
        text = task_path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        candidate_match = re.search(
            r"\[\[([^\]|#]+)", meta.get("candidate", "")
        )
        if not candidate_match:
            continue
        candidate_path = VAULT / f"{candidate_match.group(1)}.md"
        if not candidate_path.is_file():
            continue
        candidate_meta = parse_frontmatter(
            candidate_path.read_text(encoding="utf-8", errors="replace")
        )
        knowledge_rels = [
            str(value)
            for value in meta_json_list(candidate_meta, "knowledge_files_json")
            if str(value).strip()
        ]
        legacy = candidate_meta.get("knowledge_file", "").strip().strip('"')
        if legacy and legacy not in knowledge_rels:
            knowledge_rels.insert(0, legacy)
        missing = [
            value
            for value in knowledge_rels
            if f"[[{value.removesuffix('.md')}" not in text
            and f"[[{Path(value).stem}" not in text
        ]
        if not missing:
            continue
        updated = add_unique_bullets(
            text,
            "可调用知识",
            [
                f"本次同步沉淀：[[{value.removesuffix('.md')}]]"
                for value in missing
            ],
        )
        task_path.write_text(
            update_frontmatter(updated, {"updated_at": now_local()}),
            encoding="utf-8",
        )
        updated_tasks += 1
        added_links += len(missing)
    return {
        "updated_writing_tasks": updated_tasks,
        "writing_knowledge_links": added_links,
    }


def reconcile_knowledge_links() -> dict[str, int]:
    result = repair_descriptive_entity_units()
    result.update(reconcile_declared_entity_types())
    result.update(normalize_profile_entity_types())
    result.update(reconcile_entity_relationships())
    result.update(reconcile_people_links())
    result.update(reconcile_map_links())
    result.update(reconcile_map_graph_links())
    result.update(reconcile_writing_knowledge_handoff())
    topic_map_state = refresh_topic_map_candidates(
        VAULT,
        TOPIC_MAP_CANDIDATE_STATE,
        TOPIC_MAP_CANDIDATE_NOTE,
    )
    result.update(
        {
            "topic_map_candidates": len(topic_map_state.get("candidates", [])),
            "topic_map_observing": len(topic_map_state.get("observing", [])),
            "topic_map_unmapped": int(
                topic_map_state.get("summary", {}).get("true_unmapped_count", 0)
            ),
        }
    )
    map_digest = process_map_digest_actions(
        VAULT,
        MAP_DIGEST_STATE,
        MAP_DIGEST_NOTE,
        MAP_DIGEST_WEEKLY_DIR,
        TOPIC_MAP_CANDIDATE_STATE,
    )
    result.update(
        {
            "map_digest_actions_applied": int(map_digest.get("applied", 0)),
            "map_digest_action_errors": len(map_digest.get("errors", [])),
        }
    )
    return result


def create_knowledge_note(card_path: Path, text: str, meta: dict[str, str]) -> Path:
    paths = create_knowledge_notes(card_path, text, meta)
    if not paths:
        raise ValueError("审核卡没有可沉淀的知识单元")
    return paths[0]


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


def topic_attention(
    meta: dict[str, str],
    *,
    today: date | None = None,
) -> tuple[str, str, str]:
    """返回进入候选池的时间、退出日期和 fresh/expired/unknown。

    这个窗口只表达 Ray 愿意为一个未立项候选保留多久注意力，
    与原始资料新不新无关。旧卡未带新字段时，只回退到它的建立时间。
    """
    entered_at = (
        meta.get("attention_entered_at", "").strip()
        or meta.get("created_at", "").strip()
        or meta.get("last_progress_at", "").strip()
    )
    entered_date = iso_date(entered_at)
    if not entered_date:
        return entered_at, "", "unknown"
    attention_until = entered_date + timedelta(days=TOPIC_ATTENTION_DAYS)
    current_date = today or datetime.now().astimezone().date()
    attention_status = "fresh" if current_date < attention_until else "expired"
    return entered_at, attention_until.isoformat(), attention_status


def topic_review_record(meta: dict[str, str]) -> Path | None:
    review_rel = meta.get("source_review", "").strip().strip('"')
    if not review_rel or Path(review_rel).is_absolute() or ".." in Path(review_rel).parts:
        return None
    candidate = (VAULT / review_rel).resolve()
    try:
        candidate.relative_to(VAULT.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def settle_expired_topic_knowledge(
    meta: dict[str, str], *, dry_run: bool = False
) -> tuple[list[Path], int, str]:
    if meta.get("knowledge_status") == "extracted":
        return [], 0, ""
    review_path = topic_review_record(meta)
    if review_path is None:
        return [], 0, "找不到原审核记录"
    review_text = review_path.read_text(encoding="utf-8", errors="replace")
    review_meta = parse_frontmatter(review_text)
    units = knowledge_units_from_card(review_text, review_meta)
    if not units:
        return [], 0, "原审核记录没有可沉淀的知识单元"
    if dry_run:
        return [], len(units), ""
    try:
        paths = create_knowledge_notes(review_path, review_text, review_meta)
    except Exception as exc:
        return [], 0, re.sub(r"\s+", " ", str(exc)).strip()[:500]
    return paths, len(paths), "" if paths else "未生成知识笔记"


def refresh_topic_attention(
    path: Path,
    *,
    today: date | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """补齐未立项候选的注意力窗口；到期先沉淀知识，再关闭候选。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    meta = parse_frontmatter(text)
    if meta.get("kind") != "topic-candidate" or meta.get("status") != "candidate":
        return {"changed": False, "closed": False, "unresolved": False}

    entered_at, attention_until, attention_status = topic_attention(meta, today=today)
    if not attention_until:
        return {"changed": False, "closed": False, "unresolved": True}

    updates = {
        "attention_entered_at": entered_at,
        "attention_until": attention_until,
        "attention_status": attention_status,
        "review_after": attention_until,
    }
    closed = attention_status == "expired"
    knowledge_paths: list[Path] = []
    knowledge_notes = 0
    knowledge_error = ""
    if closed:
        knowledge_paths, knowledge_notes, knowledge_error = (
            settle_expired_topic_knowledge(meta, dry_run=dry_run)
        )
        if knowledge_paths:
            knowledge_rels = [rel(item) for item in knowledge_paths]
            updates.update(
                {
                    "knowledge_status": "extracted",
                    "knowledge_file": knowledge_rels[0],
                    "knowledge_files_json": json_meta(knowledge_rels),
                    "knowledge_settled_at": now_local(),
                    "knowledge_settlement_error": "",
                }
            )
        elif knowledge_error:
            updates["knowledge_settlement_error"] = knowledge_error
        updates.update(
            {
                "status": "closed",
                "closed_at": now_local(),
                "closed_reason": (
                    f"进入候选池 {TOPIC_ATTENTION_DAYS} 天内未立项，"
                    "候选已退出，资料与知识保留"
                ),
                "updated_at": now_local(),
            }
        )
    changed = any(meta.get(key, "") != value for key, value in updates.items())
    if changed and not dry_run:
        updated_text = update_frontmatter(text, updates)
        if knowledge_paths:
            updated_text = add_unique_bullets(
                updated_text,
                "可调用知识",
                [f"到期沉淀：[[{rel(item)[:-3]}]]" for item in knowledge_paths],
            )
        path.write_text(updated_text, encoding="utf-8")
    return {
        "changed": changed,
        "closed": closed,
        "unresolved": False,
        "knowledge_notes": knowledge_notes,
        "knowledge_error": knowledge_error,
    }


def maintain_topic_attention(
    *,
    today: date | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """维护活跃候选的 7 天窗口；到期沉淀知识并关闭，但不移动或删除。"""
    result: dict[str, Any] = {
        "scanned": 0,
        "updated": 0,
        "closed": 0,
        "knowledge_notes": 0,
        "knowledge_unresolved": [],
        "unresolved": [],
        "changes": [],
        "dry_run": dry_run,
    }
    if not TOPIC_DIR.exists():
        return result
    for path in sorted(TOPIC_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        if meta.get("kind") != "topic-candidate" or meta.get("status") != "candidate":
            continue
        result["scanned"] += 1
        change = refresh_topic_attention(path, today=today, dry_run=dry_run)
        if change["unresolved"]:
            result["unresolved"].append(rel(path))
        if change["changed"]:
            result["updated"] += 1
            result["changes"].append(rel(path))
        if change["closed"]:
            result["closed"] += 1
            result["knowledge_notes"] += int(change.get("knowledge_notes", 0))
            if change.get("knowledge_error"):
                result["knowledge_unresolved"].append(
                    {"path": rel(path), "reason": change["knowledge_error"]}
                )
    return result


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
    knowledge_paths: list[Path] | None = None,
    status: str = "candidate",
    destination: Path | None = None,
) -> Path:
    """把“值得写”保存为轻量候选；只有明确开写后才建立成稿包。"""
    knowledge_paths = list(
        dict.fromkeys(
            [*(knowledge_paths or []), *([knowledge_path] if knowledge_path else [])]
        )
    )
    knowledge_path = knowledge_paths[0] if knowledge_paths else None
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
    created_at = now_local()
    attention_entered_at = created_at if status == "candidate" else ""
    attention_start = iso_date(attention_entered_at)
    attention_until = (
        (attention_start + timedelta(days=TOPIC_ATTENTION_DAYS)).isoformat()
        if attention_start
        else ""
    )
    attention_status = "fresh" if attention_until else "unknown"

    requested_status = status
    parked_reason = ""
    if status == "candidate" and not has_writing_score:
        status = "parked"
        parked_reason = "旧审核卡缺少独立写作价值评分，需重新评估后再进入今日候选"
    elif status == "candidate" and not has_time_sensitivity:
        status = "parked"
        parked_reason = "旧审核卡缺少时效判断，需重新评估后再进入今日候选"
    elif status == "parked":
        parked_reason = "有价值但近期不写"

    if status == "parked" and requested_status == "candidate":
        priority_score = 0
    if status != "candidate":
        attention_entered_at = ""
        attention_until = ""
        attention_status = "inactive"
    destination = destination or (TOPIC_RESERVE_DIR if status == "parked" else TOPIC_DIR)
    if status == "parked" and requested_status == "candidate":
        destination = TOPIC_RESERVE_DIR
    path = available_topic_path(title, capture_id, destination)
    if path.exists():
        refresh_topic_freshness(path)
        refresh_topic_attention(path)
        return path

    card_rel = rel(card_path)
    topic_id = "topic-" + hashlib.sha256(capture_id.encode("utf-8")).hexdigest()[:12]
    angles = normalized_angle_lines(markdown_section(text, "可写角度"))
    claims = markdown_section(text, "核心观点") or "- 待从原始资料中提炼"
    evidence = markdown_section(text, "支撑证据") or "- 待核对"
    counterpoints = markdown_section(text, "漏洞与反例") or "- 待补充"
    personal_context = markdown_section(text, "Ray 的经验")
    connections = linkify_connections(
        markdown_section(text, "与现有知识的连接")
    ) or "- 暂无明确连接"
    reason = markdown_section(text, "快速判断") or markdown_section(text, "摘要")
    knowledge_rel = rel(knowledge_path) if knowledge_path else ""
    knowledge_rels = [rel(path) for path in knowledge_paths]
    knowledge_status = "extracted" if knowledge_paths else "pending"
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
        f"attention_entered_at: {yaml_string(attention_entered_at)}",
        f"attention_until: {yaml_string(attention_until)}",
        f"attention_status: {attention_status}",
        f"review_after: {attention_until or topic_review_after(time_sensitivity)}",
        f"parked_reason: {yaml_string(parked_reason)}",
        f"knowledge_status: {knowledge_status}",
        f"personal_context_status: {'captured' if personal_context else 'missing'}",
        f"created_at: {yaml_string(created_at)}",
        f"updated_at: {yaml_string(created_at)}",
        f"last_progress_at: {yaml_string(created_at)}",
        f"capture_id: {yaml_string(capture_id)}",
        f"source_review: {yaml_string(card_rel)}",
        f"source_file: {yaml_string(source_file)}",
        f"source_url: {yaml_string(source_url)}",
        f"knowledge_file: {yaml_string(knowledge_rel)}",
        f"knowledge_files_json: {yaml_string(json_meta(knowledge_rels))}",
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
        f"- 资料时效提示至：{fresh_until or '待确认'}",
        f"- 候选池保留至：{attention_until or '待立项时确认'}",
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
    ])
    if personal_context:
        lines.extend([
            "",
            "## Ray 的经验",
            "",
            personal_context,
        ])
    lines.extend([
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
    for item_rel in knowledge_rels:
        lines.append(f"- 本次同步沉淀：[[{item_rel[:-3]}]]")
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


def attach_knowledge_to_topic(topic_path: Path, knowledge_paths: list[Path]) -> None:
    if not topic_path.exists() or not knowledge_paths:
        return
    text = topic_path.read_text(encoding="utf-8", errors="replace")
    meta = parse_frontmatter(text)
    existing = [
        str(item) for item in meta_json_list(meta, "knowledge_files_json")
    ]
    legacy = meta.get("knowledge_file", "").strip()
    if legacy and legacy not in existing:
        existing.insert(0, legacy)
    knowledge_rels = list(
        dict.fromkeys([*existing, *[rel(path) for path in knowledge_paths]])
    )
    text = update_frontmatter(
        text,
        {
            "knowledge_status": "extracted",
            "knowledge_file": knowledge_rels[0],
            "knowledge_files_json": json_meta(knowledge_rels),
            "updated_at": now_local(),
        },
    )
    text = add_unique_bullets(
        text,
        "可调用知识",
        [f"本次同步沉淀：[[{item[:-3]}]]" for item in knowledge_rels],
    )
    topic_path.write_text(text, encoding="utf-8")


def detach_knowledge_from_topic(topic_path: Path, knowledge_rels: list[str]) -> None:
    if not topic_path.exists() or not knowledge_rels:
        return
    text = topic_path.read_text(encoding="utf-8", errors="replace")
    meta = parse_frontmatter(text)
    old_set = {value.strip().strip('"') for value in knowledge_rels if value.strip()}
    existing = [
        str(item)
        for item in meta_json_list(meta, "knowledge_files_json")
        if str(item) not in old_set
    ]
    legacy = meta.get("knowledge_file", "").strip().strip('"')
    if legacy and legacy not in old_set and legacy not in existing:
        existing.insert(0, legacy)
    updates = {
        "knowledge_files_json": json_meta(existing),
        "knowledge_file": existing[0] if existing else "",
        "updated_at": now_local(),
    }
    text = update_frontmatter(text, updates)
    section = markdown_section(text, "可调用知识")
    old_targets = {value.removesuffix(".md") for value in old_set}
    kept = [
        line
        for line in section.splitlines()
        if not any(f"[[{target}]]" in line for target in old_targets)
    ]
    text = replace_markdown_section(text, "可调用知识", "\n".join(kept))
    topic_path.write_text(text, encoding="utf-8")


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
    rendered = "\n".join(lines)
    personal_context = markdown_section(text, "Ray 的经验")
    if personal_context:
        current = markdown_section(rendered, "用户真实材料")
        rendered = replace_markdown_section_body(
            rendered,
            "用户真实材料",
            "### 已记录个人材料（使用前确认）\n\n"
            + personal_context
            + "\n\n### 使用边界\n\n"
            + current,
        )
    path.write_text(rendered, encoding="utf-8")
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
        if topic_meta.get("status") == "parked" and freshness_status != "fresh":
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
    personal_context = markdown_section(topic_text, "Ray 的经验")
    if personal_context:
        current = markdown_section(rendered, "用户真实材料")
        rendered = replace_markdown_section_body(
            rendered,
            "用户真实材料",
            "### 已记录个人材料（使用前确认）\n\n"
            + personal_context
            + "\n\n### 使用边界\n\n"
            + current,
        )
    path.write_text(rendered, encoding="utf-8")

    updated_topic = update_frontmatter(
        topic_text,
        {
            "status": "active",
            "topic_id": topic_id,
            "selected_angle": selected_angle,
            "content_pack": f"[[{rel(path)[:-3]}]]",
            "attention_status": "promoted",
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
        "knowledge_notes": 0,
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
        if action == "topic" and meta.get("knowledge_auto_at"):
            # 初始知识已经落地时，“只进选题”会留下相互矛盾的记录。
            # 统一按双向保留处理，让候选继承已有知识；仍不会自动立项。
            action = "both"
        cleanup = action == "cleanup"
        archive_target = review_archive_destination(path, meta, cleanup)
        updates = {
            "review_action": action,
            "action_processed_at": now_local(),
            "archived_at": now_local(),
            "action_error": "",
            "result_file": "",
            "knowledge_file": "",
            "knowledge_files_json": "[]",
            "topic_file": "",
        }
        knowledge_paths: list[Path] = []
        knowledge_path: Path | None = None
        topic_path: Path | None = None
        if action in {"knowledge", "both"}:
            knowledge_paths = create_knowledge_notes(archive_target, text, meta)
            if not knowledge_paths:
                path.write_text(
                    update_frontmatter(
                        text,
                        {
                            "action_error": (
                                "这张审核卡没有可沉淀的知识单元；"
                                "请改选写作、暂缓或清理，或重新生成审核卡"
                            )
                        },
                    ),
                    encoding="utf-8",
                )
                result["invalid"] += 1
                continue
            knowledge_path = knowledge_paths[0]
            updates["knowledge_file"] = rel(knowledge_path)
            updates["knowledge_files_json"] = json_meta(
                [rel(item) for item in knowledge_paths]
            )
            result["knowledge_notes"] += len(knowledge_paths)
        if action in {"topic", "both"}:
            topic_path = create_topic_candidate(
                archive_target,
                text,
                meta,
                knowledge_path=knowledge_path,
                knowledge_paths=knowledge_paths,
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


def review_attention(
    meta: dict[str, str], *, today: date | None = None
) -> tuple[str, str, str]:
    entered_at = (
        meta.get("attention_entered_at", "").strip()
        or meta.get("reviewed_at", "").strip()
    )
    entered_date = iso_date(entered_at)
    if not entered_date:
        return entered_at, "", "unknown"
    attention_until = entered_date + timedelta(days=REVIEW_ATTENTION_DAYS)
    current_date = today or datetime.now().astimezone().date()
    status_value = "fresh" if current_date < attention_until else "expired"
    return entered_at, attention_until.isoformat(), status_value


def review_knowledge_files(meta: dict[str, str]) -> list[Path]:
    rels = [
        str(value).strip()
        for value in meta_json_list(meta, "knowledge_files_json")
        if str(value).strip()
    ]
    legacy = meta.get("knowledge_file", "").strip().strip('"')
    if legacy and legacy not in rels:
        rels.insert(0, legacy)
    return [VAULT / item for item in rels if (VAULT / item).is_file()]


def maintain_review_attention(
    state: dict[str, Any] | None = None,
    *,
    today: date | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "scanned": 0,
        "updated": 0,
        "archived": 0,
        "knowledge": 0,
        "source_only": 0,
        "unresolved": 0,
        "would_archive": [],
    }
    if not REVIEW_DIR.exists():
        return result
    for path in sorted(REVIEW_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        if meta.get("status") != "待审核" or checked_review_actions(text):
            continue
        result["scanned"] += 1
        entered_at, attention_until, attention_status = review_attention(
            meta, today=today
        )
        if not attention_until:
            result["unresolved"] += 1
            continue
        updates = {
            "attention_entered_at": entered_at,
            "attention_until": attention_until,
            "attention_status": attention_status,
        }
        if attention_status == "fresh":
            if any(meta.get(key, "") != value for key, value in updates.items()):
                result["updated"] += 1
                if not dry_run:
                    path.write_text(update_frontmatter(text, updates), encoding="utf-8")
            continue

        knowledge_paths = (
            review_knowledge_files(meta) if meta.get("knowledge_auto_at") else []
        )
        outcome = "knowledge" if knowledge_paths else "source_only"
        updates.update(
            {
                "status": "已自动沉淀" if knowledge_paths else "已保留来源",
                "routing_outcome": (
                    "auto-knowledge" if knowledge_paths else "source-only"
                ),
                "action_processed_at": now_local(),
                "archived_at": now_local(),
                "archive_reason": (
                    f"进入待审核区 {REVIEW_ATTENTION_DAYS} 天内未人工判断；"
                    + (
                        "高把握初始知识已保留，未创建候选选题"
                        if knowledge_paths
                        else "退出当前队列，原始资料与审核记录保留"
                    )
                ),
                "result_file": (
                    rel(knowledge_paths[0])
                    if knowledge_paths
                    else meta.get("source_file", "")
                ),
            }
        )
        result["archived"] += 1
        result[outcome] += 1
        result["would_archive"].append(rel(path))
        if dry_run:
            continue
        mark_source_processed(meta)
        path.write_text(update_frontmatter(text, updates), encoding="utf-8")
        archive_review_card(path, {**meta, **updates}, state)
        log(f"review attention expired ({outcome}): {rel(path)}")
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


def active_review_for_capture(capture_id: str) -> Path | None:
    if not REVIEW_DIR.exists():
        return None
    for path in sorted(REVIEW_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if parse_frontmatter(text).get("capture_id") == capture_id:
            return path
    return None


def normalized_relevance_score(value: Any) -> int:
    score = max(0, min(100, int(value)))
    # Some models occasionally answer on a 1–10 scale despite the schema.
    return score * 10 if 0 < score <= 10 else score


def auto_knowledge_eligible(review: dict[str, Any]) -> bool:
    policy = AUTO_KNOWLEDGE_POLICY
    if not policy["enabled"]:
        return False
    if str(review.get("recommendation", "")).strip() != policy["recommendation"]:
        return False
    if str(review.get("confidence", "")).strip() != policy["confidence"]:
        return False
    raw_units = review.get("knowledge_units")
    if policy["require_explicit_complete_units"] and not isinstance(raw_units, list):
        return False
    if not normalized_knowledge_units(review, strict=True):
        return False
    knowledge_score = normalized_relevance_score(
        review.get("knowledge_value_score", review.get("relevance_score", 0))
    )
    writing_score = normalized_relevance_score(
        review.get("writing_value_score", review.get("relevance_score", 0))
    )
    return (
        knowledge_score >= policy["min_knowledge_value_score"]
        and knowledge_score - writing_score
        >= policy["min_knowledge_over_writing_gap"]
    )


def add_auto_knowledge_notice(text: str, knowledge_paths: list[Path]) -> str:
    links = "\n".join(f"- [[{rel(path)[:-3]}]]" for path in knowledge_paths)
    section = (
        "## 已自动沉淀的知识\n\n"
        "> 这份资料满足高把握知识边界，已经生成初始知识卡。"
        "你现在只需要决定：它是否还值得进入候选选题。\n\n"
        f"{links}"
    )
    pattern = r"^## 已自动沉淀的知识\s*\n.*?(?=^## |\Z)"
    if re.search(pattern, text, flags=re.M | re.S):
        return re.sub(pattern, section + "\n\n", text, count=1, flags=re.M | re.S)
    return text.replace("## 人工审核", section + "\n\n## 人工审核", 1)


def auto_extract_review_knowledge(
    path: Path, review: dict[str, Any]
) -> list[Path]:
    if not auto_knowledge_eligible(review):
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    meta = parse_frontmatter(text)
    source_file = meta.get("source_file", "").strip().strip('"')
    if not source_file or not (VAULT / source_file).is_file():
        return []
    try:
        knowledge_paths = create_knowledge_notes(path, text, meta)
    except Exception as exc:
        message = re.sub(r"\s+", " ", str(exc)).strip()[:500]
        path.write_text(
            update_frontmatter(text, {"knowledge_auto_error": message}),
            encoding="utf-8",
        )
        log(f"auto knowledge failed: {rel(path)}: {message}")
        return []
    if not knowledge_paths:
        return []
    updated = update_frontmatter(
        text,
        {
            "knowledge_status": "extracted",
            "knowledge_file": rel(knowledge_paths[0]),
            "knowledge_files_json": json_meta([rel(item) for item in knowledge_paths]),
            "knowledge_auto_at": now_local(),
            "knowledge_auto_policy": AUTO_KNOWLEDGE_POLICY["policy_id"],
            "knowledge_auto_error": "",
        },
    )
    updated = add_auto_knowledge_notice(updated, knowledge_paths)
    path.write_text(updated, encoding="utf-8")
    log(f"auto knowledge: {rel(path)} -> {len(knowledge_paths)} notes")
    return knowledge_paths


def write_review_card(
    review: dict[str, Any], item: dict[str, str], replacement_path: Path | None = None
) -> Path:
    path = (
        replacement_path
        or active_review_for_capture(item["id"])
        or card_path(review, item["id"])
    )
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
    personal_context = personal_context_from_source(item["content"])
    suggested_kind = review["suggested_kind"]
    knowledge_units = normalized_knowledge_units(review)
    entities = normalized_entities(review)
    if knowledge_units and suggested_kind not in VALID_KNOWLEDGE_KINDS:
        suggested_kind = str(knowledge_units[0]["kind"])
    suggested_destination = destination_map.get(suggested_kind, "")
    if suggested_kind == "人物与组织":
        primary_title = str(knowledge_units[0]["title"]) if knowledge_units else ""
        primary_entity = declared_entity_for_name(primary_title, entities)
        if primary_entity and primary_entity.get("entity_type") in {"产品", "模型"}:
            suggested_destination = "20-知识/25-产品与模型"
    reviewed_at = now_local()
    reviewed_date = iso_date(reviewed_at)
    attention_until = (
        reviewed_date + timedelta(days=REVIEW_ATTENTION_DAYS)
        if reviewed_date
        else None
    )
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
        f"suggested_destination: {yaml_string(suggested_destination)}",
        f"knowledge_unit_count: {len(knowledge_units)}",
        f"entity_count: {len(entities)}",
        f"personal_context_status: {'captured' if personal_context else 'missing'}",
        f"knowledge_units_json: {yaml_string(json_meta(knowledge_units))}",
        f"entities_json: {yaml_string(json_meta(entities))}",
        f"source_file: {yaml_string(item['file'])}",
        f"source_url: {yaml_string(source_url)}",
        f"source_published_at: {yaml_string(source_date)}",
        f"capture_id: {yaml_string(item['id'])}",
        f"reviewed_at: {yaml_string(reviewed_at)}",
        f"attention_entered_at: {yaml_string(reviewed_at)}",
        f"attention_until: {yaml_string(attention_until.isoformat() if attention_until else '')}",
        "attention_status: fresh",
        "topics:",
    ]
    lines.extend([f"  - {yaml_string(x)}" for x in review.get("topics", [])] or ["  - 待分类"])
    lines.extend([
        "---", "", f"# {review['title']}", "",
        "## 快速判断", "",
        f"**建议：{review['recommendation']}｜知识价值：{knowledge_value_score}/100｜写作价值：{writing_value_score}/100｜{time_sensitivity}｜置信度：{review['confidence']}**", "",
        review["reason"], "",
        "## 读者收益", "", review.get("reader_value", "待确认读者读完后能采取什么行动或形成什么判断。"), "",
    ])
    if personal_context:
        lines.extend(["## Ray 的经验", "", personal_context, ""])
    lines.extend(["## 摘要", "", review["summary"], "", "## 核心观点", ""])
    lines.extend([f"- {x}" for x in review.get("core_claims", [])] or ["- 无"])
    lines.extend(["", "## 支撑证据", ""])
    lines.extend([f"- {x}" for x in review.get("evidence", [])] or ["- 原文未提供足够证据"])
    lines.extend(["", "## 漏洞与反例", ""])
    lines.extend([f"- {x}" for x in review.get("counterpoints", [])] or ["- 暂无"])
    lines.extend(["", "## 与现有知识的连接", ""])
    lines.extend([f"- {x}" for x in review.get("connections", [])] or ["- 暂无明确连接"])
    lines.extend(["", "## 拟沉淀知识单元", ""])
    if knowledge_units:
        for unit in knowledge_units:
            lines.extend(
                [
                    f"### {unit['id']} · {unit['kind']} · {unit['title']}",
                    "",
                    unit["summary"] or "待补充摘要。",
                    "",
                    "**核心内容**",
                    "",
                ]
            )
            lines.extend(
                [f"- {value}" for value in unit["core_claims"]]
                or ["- 待补充"]
            )
            lines.extend(["", "**证据**", ""])
            lines.extend(
                [f"- {value}" for value in unit["evidence"]]
                or ["- 原文未提供足够证据"]
            )
            lines.extend(["", "**边界**", ""])
            lines.extend(
                [f"- {value}" for value in unit["counterpoints"]]
                or ["- 暂无"]
            )
            if unit["people"]:
                lines.extend(
                    [
                        "",
                        "**相关对象**",
                        "",
                        *[f"- {value}" for value in unit["people"]],
                    ]
                )
            lines.append("")
    else:
        lines.extend(["- 这份资料没有达到长期知识门槛。", ""])
    lines.extend(["## 对象档案", ""])
    if entities:
        for entity in entities:
            aliases = "、".join(entity["aliases"]) or "无"
            lines.extend(
                [
                    f"### {entity['name']} · {entity['entity_type']}",
                    "",
                    entity["description"] or "待补充。",
                    "",
                    f"- 别名：{aliases}",
                ]
            )
            lines.extend(
                [f"- 观点：{value}" for value in entity["viewpoints"]]
                or ["- 观点：原文未提供可复用观点"]
            )
            lines.append("")
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
        content = item["content"]
        if len(content) > max_item_chars:
            prefix = f"review source failed: {item['id']}:"
            message = (
                f"{prefix} 原文 {len(content)} 字符，超过单份完整处理上限 "
                f"{max_item_chars}；已留在队列，未截断"
            )
            if not any(
                error.get("message", "").startswith(prefix)
                and not error.get("resolved_at")
                for error in state["errors"]
            ):
                state["errors"].append({"at": now_local(), "message": message})
                log(message)
            continue
        if batch and (len(batch) >= max_items or used_chars + len(content) > max_batch_chars):
            break
        batch.append({**item, "content": content})
        used_chars += len(content)
    if not batch:
        return 0
    for error in state["errors"]:
        if error.get("message", "").startswith("review failed:") and not error.get("resolved_at"):
            error["resolved_at"] = now_local()
    count = 0
    for item in batch:
        prefix = f"review source failed: {item['id']}:"
        try:
            result = run_codex(build_prompt([item], config), config)
        except Exception as exc:
            message = f"{prefix} {exc}"
            state["errors"].append({"at": now_local(), "message": message})
            log(message)
            continue
        review = next(
            (
                candidate
                for candidate in result.get("reviews", [])
                if isinstance(candidate, dict)
                and str(candidate.get("id", "")) == item["id"]
            ),
            None,
        )
        if review is None:
            message = f"{prefix} 模型没有返回这份资料的审核结果"
            state["errors"].append({"at": now_local(), "message": message})
            log(message)
            continue
        if not normalized_knowledge_units(review, strict=True):
            message = f"{prefix} 审核结果缺少完整知识单元"
            state["errors"].append({"at": now_local(), "message": message})
            log(message)
            continue
        replacement = state["review_replacements"].get(review["id"])
        replacement_path = VAULT / replacement if replacement else None
        path = write_review_card(review, item, replacement_path)
        auto_knowledge_paths = auto_extract_review_knowledge(path, review)
        state["reviewed_sources"][review["id"]] = {
            "source": item["file"],
            "review": rel(path),
            "at": now_local(),
            "auto_knowledge_files": [rel(item) for item in auto_knowledge_paths],
        }
        state["review_replacements"].pop(review["id"], None)
        for error in state["errors"]:
            if error.get("message", "").startswith(prefix) and not error.get("resolved_at"):
                error["resolved_at"] = now_local()
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


def archived_reviews_needing_v2(
    state: dict[str, Any],
) -> list[tuple[Path, dict[str, str], dict[str, str]]]:
    pending: list[tuple[Path, dict[str, str], dict[str, str]]] = []
    for capture_id, record in state.get("reviewed_sources", {}).items():
        review_rel = str(record.get("review", ""))
        source_rel = str(record.get("source", ""))
        review_path = VAULT / review_rel
        source_path = VAULT / source_rel
        if not review_path.is_file() or not source_path.is_file():
            continue
        text = review_path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        action = REVIEW_ACTION_KEY_ALIASES.get(
            meta.get("review_action", ""), meta.get("review_action", "")
        )
        if action not in {"knowledge", "both"}:
            continue
        existing_units = meta_json_list(meta, "knowledge_units_json")
        if existing_units and normalized_knowledge_units(
            {
                "knowledge_units": existing_units,
                "entities": meta_json_list(meta, "entities_json"),
            },
            strict=True,
        ):
            continue
        pending.append(
            (
                review_path,
                meta,
                {
                    "id": capture_id,
                    "file": source_rel,
                    "content": source_path.read_text(encoding="utf-8", errors="replace"),
                },
            )
        )
    return sorted(pending, key=lambda item: rel(item[0]))


def quarantine_invalid_review_knowledge(
    meta: dict[str, str],
) -> list[tuple[Path, Path]]:
    """将不完整 v2 结果移入可恢复区，使同一单元可以重新生成。"""
    old_paths = [
        VAULT / str(value)
        for value in meta_json_list(meta, "knowledge_files_json")
        if str(value).strip()
    ]
    legacy = meta.get("knowledge_file", "").strip().strip('"')
    if legacy and VAULT / legacy not in old_paths:
        old_paths.append(VAULT / legacy)
    moved: list[tuple[Path, Path]] = []
    archive_root = (
        VAULT
        / "90-归档/90-待清理"
        / datetime.now().astimezone().date().isoformat()
        / "无效知识提炼"
    )
    for source in old_paths:
        try:
            in_knowledge = KNOWLEDGE_DIR.resolve() in source.resolve().parents
        except OSError:
            in_knowledge = False
        if not source.is_file() or not in_knowledge:
            continue
        target = archive_root / source.relative_to(KNOWLEDGE_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        suffix = 2
        while target.exists():
            target = target.with_name(f"{target.stem}-{suffix}{target.suffix}")
            suffix += 1
        shutil.move(str(source), str(target))
        moved.append((source, target))
    return moved


def replace_knowledge_wikilink(
    old_title: str, new_title: str, *, old_rel: str = ""
) -> int:
    if not old_title or old_title == new_title:
        return 0
    updated_files = 0
    targets = [old_title]
    if old_rel:
        targets.append(old_rel.removesuffix(".md"))
    pattern = re.compile(
        rf"\[\[(?:{'|'.join(re.escape(value) for value in targets)})(?P<tail>[|#\]])"
    )
    for path in KNOWLEDGE_DIR.rglob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        updated = pattern.sub(
            lambda match: f"[[{new_title}{match.group('tail')}",
            text,
        )
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            updated_files += 1
    return updated_files


def backfill_knowledge_v2(
    state: dict[str, Any],
    config: dict[str, Any],
    *,
    limit: int = 8,
    dry_run: bool = False,
) -> dict[str, Any]:
    candidates = archived_reviews_needing_v2(state)
    selected = candidates[: max(1, min(limit, 8))]
    if dry_run:
        return {
            "pending": len(candidates),
            "selected": [rel(path) for path, _, _ in selected],
            "processed": 0,
            "knowledge_notes": 0,
        }
    if not selected:
        return {
            "pending": 0,
            "selected": [],
            "processed": 0,
            "knowledge_notes": 0,
        }

    processed = 0
    knowledge_notes = 0
    updated_reviews: list[str] = []
    skipped: list[str] = []
    for review_path, previous_meta, item in selected:
        # 存量补齐逐份调用：一份输入必须对应一份输出，避免模型在批量响应中
        # 只返回部分条目，造成“命令成功但实际漏做”的假象。
        try:
            result = run_codex(build_prompt([item], config), config)
        except Exception as exc:
            log(f"knowledge v2 backfill failed: {rel(review_path)}: {exc}")
            skipped.append(rel(review_path))
            continue
        review = next(
            (
                candidate
                for candidate in result.get("reviews", [])
                if isinstance(candidate, dict)
                and str(candidate.get("id", "")) == item["id"]
            ),
            None,
        )
        if not review:
            skipped.append(rel(review_path))
            continue
        units = normalized_knowledge_units(review, strict=True)
        if not units:
            skipped.append(rel(review_path))
            continue
        previous_units = meta_json_list(previous_meta, "knowledge_units_json")
        invalid_previous = bool(previous_units) and not normalized_knowledge_units(
            {
                "knowledge_units": previous_units,
                "entities": meta_json_list(previous_meta, "entities_json"),
            },
            strict=True,
        )
        quarantined = (
            quarantine_invalid_review_knowledge(previous_meta)
            if invalid_previous
            else []
        )
        write_review_card(review, item, review_path)
        updated_text = review_path.read_text(encoding="utf-8", errors="replace")
        updated_meta = parse_frontmatter(updated_text)
        paths = create_knowledge_notes(review_path, updated_text, updated_meta)
        if not paths:
            skipped.append(rel(review_path))
            continue
        for index, (old_path, _) in enumerate(quarantined):
            replacement = paths[min(index, len(paths) - 1)]
            replace_knowledge_wikilink(
                old_path.stem,
                replacement.stem,
                old_rel=rel(old_path),
            )
        updates = {
            "knowledge_file": rel(paths[0]),
            "knowledge_files_json": json_meta([rel(path) for path in paths]),
            "knowledge_v2_backfilled_at": now_local(),
        }
        action = REVIEW_ACTION_KEY_ALIASES.get(
            previous_meta.get("review_action", ""),
            previous_meta.get("review_action", ""),
        )
        if action == "knowledge":
            updates["result_file"] = rel(paths[0])
        updated_text = update_frontmatter(updated_text, updates)
        review_path.write_text(updated_text, encoding="utf-8")
        topic_rel = previous_meta.get("topic_file", "")
        if action == "both" and topic_rel:
            topic_path = VAULT / topic_rel
            if quarantined:
                detach_knowledge_from_topic(
                    topic_path, [rel(old_path) for old_path, _ in quarantined]
                )
            attach_knowledge_to_topic(topic_path, paths)
        processed += 1
        knowledge_notes += len(paths)
        updated_reviews.append(rel(review_path))
        log(
            f"knowledge v2 backfill: {rel(review_path)} -> "
            f"{len(paths)} knowledge units"
        )
    save_state(state)
    return {
        "pending": max(0, len(candidates) - processed),
        "selected": [rel(path) for path, _, _ in selected],
        "processed": processed,
        "knowledge_notes": knowledge_notes,
        "updated_reviews": updated_reviews,
        "skipped": skipped,
    }


def sync_only(state: dict[str, Any], config: dict[str, Any]) -> dict[str, int]:
    result = {"x": 0, "mobile": 0, "web": 0, "weread": 0}
    try:
        result["x"] = sync_x(state, config)
        record_source_success(state, "x")
        for error in state["errors"]:
            if error.get("message", "").startswith("x sync failed:") and not error.get(
                "resolved_at"
            ):
                error["resolved_at"] = now_local()
    except Exception as exc:
        message = f"x sync failed: {exc}"
        state["errors"].append({"at": now_local(), "message": message})
        record_source_failure(state, "x", message)
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
        "source_health": state.get("source_health", {}),
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
            "maintain-topics",
            "maintain-reviews",
            "backfill-knowledge-v2",
            "migrate-entities",
            "reconcile-knowledge",
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
        help="支持的命令只打印会做什么，不写任何文件",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=8,
        help="backfill-knowledge-v2 单轮最多处理的审核卡数量（1–8）",
    )
    args = parser.parse_args()
    run_lock = None
    if args.command == "run":
        run_lock = acquire_run_lock()
        if run_lock is None:
            print(
                json.dumps(
                    {
                        "skipped": "already_running",
                        "message": "已有知识采集周期正在运行，本轮安全跳过",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
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
    elif args.command == "maintain-topics":
        result = maintain_topic_attention(dry_run=args.dry_run)
    elif args.command == "maintain-reviews":
        result = maintain_review_attention(
            state,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            save_state(state)
    elif args.command == "backfill-knowledge-v2":
        result = backfill_knowledge_v2(
            state,
            config,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    elif args.command == "migrate-entities":
        result = migrate_entity_layout(dry_run=args.dry_run)
    elif args.command == "reconcile-knowledge":
        result = reconcile_knowledge_links()
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
        result["topic_attention"] = maintain_topic_attention()
        result["review_attention"] = maintain_review_attention(state)
        result["knowledge_reconcile"] = reconcile_knowledge_links()
        save_state(state)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if run_lock is not None:
        run_lock.close()


if __name__ == "__main__":
    main()
