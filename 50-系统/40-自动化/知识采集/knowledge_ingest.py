#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
VAULT = Path(os.environ.get("RAYS_BRAIN", str(HERE.parents[2]))).resolve()
SYSTEM_DIR = HERE
# 运行时状态与日志放在 vault 外，避免 iCloud 高频同步与冲突副本破坏去重状态
STATE_HOME = Path(os.environ.get("RAYS_BRAIN_STATE", str(Path.home() / ".local/state/rays-brain")))
STATE_FILE = STATE_HOME / "state.json"
LOG_DIR = STATE_HOME / "logs"
CONFIG_FILE = SYSTEM_DIR / "config.json"
SCHEMA_FILE = SYSTEM_DIR / "review.schema.json"
PROMPT_FILE = SYSTEM_DIR / "review-prompt.md"
X_DIR = VAULT / "30-资料/30-X书签"
WEB_DIR = VAULT / "30-资料/40-网页剪藏"
LINK_INBOX = VAULT / "30-资料/00-待抓取/链接收件箱.md"
REVIEW_DIR = VAULT / "10-创作/10-灵感/10-待评估/剪藏复核"
CONTENT_PACK_DIR = VAULT / "10-创作/10-灵感/20-成稿包"
KNOWLEDGE_DIR = VAULT / "20-知识"
LEGACY_ACTIONS_SCRIPT = SYSTEM_DIR / "legacy_actions.py"


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


def initialize() -> dict[str, int]:
    for path in (SYSTEM_DIR, LOG_DIR, X_DIR, WEB_DIR, REVIEW_DIR, CONTENT_PACK_DIR, LINK_INBOX.parent):
        path.mkdir(parents=True, exist_ok=True)
    state = load_state()
    if not STATE_FILE.exists():
        state["known_web_files"] = sorted(rel(p) for p in WEB_DIR.glob("*.md"))
        state["known_x_ids"] = sorted(existing_x_ids())
    save_state(state)
    return {
        "known_web_files": len(state["known_web_files"]),
        "known_x_ids": len(state["known_x_ids"]),
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


def fetch_x_article(tweet_id: str, config: dict[str, Any]) -> dict[str, Any]:
    endpoint = (
        f"/2/tweets/{tweet_id}"
        "?tweet.fields=article,author_id,created_at,entities,public_metrics"
        "&expansions=article.cover_media,article.media_entities,author_id"
    )
    payload = run_xurl_request(config, [endpoint])
    article = payload.get("data", {}).get("article", {})
    article["plain_text"] = clean_x_article_text(str(article.get("plain_text", "")))
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
    if meta.get("capture_version") != "2" or not meta.get("source"):
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
    lines = [
        "---",
        f"title: {yaml_string(display_title)}",
        f"source: {yaml_string(x_source_url(tweet_id, username))}",
        f"author: {yaml_string('@' + username)}",
        f"published: {yaml_string(created)}",
        f"created: {yaml_string(captured_at or now_local())}",
        "tags:",
        "  - x-bookmark",
    ]
    if has_article:
        lines.append("  - x-article")
    lines.extend([
        "capture_source: x-bookmark",
        f"capture_id: {yaml_string('x:' + tweet_id)}",
        "processing_status: unprocessed",
        "capture_version: 2",
        "capture_status: complete",
        f"attachment_count: {len(media)}",
        f"tweet_id: {yaml_string(tweet_id)}",
    ])
    if has_article:
        lines.extend([
            "content_type: x-article",
            f"article_title: {yaml_string(article_title)}",
            f"article_url: {yaml_string(article_url)}",
            f"article_fetch_status: {article_status}",
            f"article_chars: {len(article_body)}",
        ])
    path = existing_x_path(tweet_id)
    existing_text = path.read_text(encoding="utf-8", errors="replace") if path else ""
    notes = preserved_x_notes(existing_text) if notes_override is None else notes_override
    lines.extend(["---", "", f"# {display_title}"])
    if notes:
        lines.extend(["", notes])
    lines.extend(["", "## 推文", "", full_text])
    if article_body:
        lines.extend(["", "## Article 原文", "", article_body])
    elif has_article:
        lines.extend(["", "## Article 原文", "", "正文暂未获取，系统会在下次同步时重试。"])
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
    known = set(state["known_x_ids"]) | existing_x_ids()
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
                if not x_bookmark_needs_refresh(existing_text, tweet, article, media_by_key):
                    continue
                captured_at = parse_frontmatter(existing_text).get("created", "")
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
                article = article_hint if article_hint.get("plain_text") else fetch_x_article(tweet_id, config)
            except Exception as exc:
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
        if article_hint and not article:
            try:
                article = fetch_x_article(tweet_id, config)
            except Exception as exc:
                article_error = str(exc)
        author = users.get(str(tweet.get("author_id")), {})
        username = str(author.get("username") or tweet.get("author_id") or "unknown")
        captured_at = parse_frontmatter(existing_text).get("created", "")
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
            "source": x_source_url(tweet_id),
            "capture_source": "x-bookmark",
            "capture_id": "x:" + tweet_id,
            "processing_status": "unprocessed",
            "capture_version": "2",
            "capture_status": "source-unavailable",
        }
        path.write_text(update_frontmatter(text, updates), encoding="utf-8")
        unavailable += 1
    state["known_x_ids"] = sorted(existing_x_ids())
    save_state(state)
    return {"repaired": repaired, "unavailable": unavailable}


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
            lines.extend(mobile_file.read_text(encoding="utf-8", errors="replace").splitlines())
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
        path = WEB_DIR / f"{day}-{sanitize_filename(title)}-{short_hash}.md"
        lines = [
            "---",
            f"title: {yaml_string(title)}",
            f"source: {yaml_string(url)}",
            f"created: {yaml_string(fetched_at)}",
            "tags:",
            "  - mobile-share",
            "capture_source: mobile-share",
            f"capture_id: {yaml_string('mobile:' + line_hash)}",
            "processing_status: unprocessed",
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
        source_rel = rel(path)
        if source_rel in known:
            continue
        known.add(source_rel)
        if source_rel not in state["pending_sources"]:
            state["pending_sources"].append(source_rel)
        discovered += 1
    state["known_web_files"] = sorted(known)
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


def update_frontmatter(text: str, updates: dict[str, str]) -> str:
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
        rendered = f"{key}: {yaml_string(value)}"
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


REVIEW_ACTIONS = {
    "批准进入长期知识库": "knowledge",
    "仅保留为写作素材": "writing",
    "暂缓": "paused",
    "移入可恢复的待清理区": "cleanup",
    "标记为可恢复的待清理项": "cleanup",
}


def checked_review_actions(text: str) -> list[str]:
    checked: list[str] = []
    for label, action in REVIEW_ACTIONS.items():
        if re.search(rf"^- \[[xX]\]\s+{re.escape(label)}\s*$", text, flags=re.M):
            checked.append(action)
    return list(dict.fromkeys(checked))


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
    # kind 以目标文件夹推导，保持与《个人知识库工作流》的枚举一致（kind 是分类的唯一真源）
    kind = KIND_BY_FOLDER.get(destination.name, "concept")
    if "30-X书签" in source_file:
        origin = "x-bookmark"
    elif "40-网页剪藏" in source_file:
        origin = "web-clip"
    else:
        origin = "capture"
    lines = [
        "---",
        f"kind: {kind}",
        "status: seed",
        f"origin: {origin}",
        f"created_at: {yaml_string(now_local())}",
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
    """把人工批准的写作素材装配成独立成稿包，不改动原始资料和审核正文。"""
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


def process_review_actions() -> dict[str, int]:
    result = {"processed": 0, "knowledge": 0, "writing": 0, "paused": 0, "cleanup": 0, "invalid": 0}
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
        action = actions[0]
        updates = {
            "review_action": action,
            "action_processed_at": now_local(),
            "action_error": "",
            "result_file": "",
        }
        if action == "knowledge":
            knowledge_path = create_knowledge_note(path, text, meta)
            updates["status"] = "已沉淀"
            updates["result_file"] = rel(knowledge_path)
        elif action == "writing":
            content_pack_path = create_content_pack(path, text, meta)
            updates["status"] = "可写作"
            updates["result_file"] = rel(content_pack_path)
        elif action == "paused":
            updates["status"] = "暂缓"
        else:
            updates["status"] = "待清理"
        path.write_text(update_frontmatter(text, updates), encoding="utf-8")
        result["processed"] += 1
        result[action] += 1
        log(f"review action {action}: {rel(path)}")
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
    return meta.get("capture_id") or "file:" + rel(path)


def existing_knowledge_titles(limit: int = 300) -> list[str]:
    titles = sorted(
        unicodedata.normalize("NFC", path.stem)
        for path in KNOWLEDGE_DIR.rglob("*.md")
        if "90-元数据" not in path.parts and path.name != "README.md"
    )
    return titles[:limit]


def build_prompt(items: list[dict[str, str]], config: dict[str, Any]) -> str:
    instruction = PROMPT_FILE.read_text(encoding="utf-8")
    topics = "、".join(config.get("focus_topics", []))
    titles = "、".join(existing_knowledge_titles())
    chunks = [
        instruction,
        f"\n当前重点主题：{topics}\n",
        f"现有长期知识笔记标题（connections 中凡能对应上的，必须原样使用这些标题）：{titles}\n",
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
    destination_map = {
        "概念": "20-知识/10-概念",
        "观点": "20-知识/40-观点",
        "案例": "20-知识/50-案例",
        "方法": "20-知识/60-方法",
        "选题": "10-创作/10-灵感/10-待评估",
        "无": "90-归档/90-待清理",
    }
    source_meta = parse_frontmatter(item["content"])
    suggested_kind = review["suggested_kind"]
    relevance_score = normalized_relevance_score(review["relevance_score"])
    lines = [
        "---",
        "kind: capture-review",
        "status: 待审核",
        f"recommendation: {review['recommendation']}",
        f"relevance_score: {relevance_score}",
        f"confidence: {review['confidence']}",
        f"suggested_kind: {suggested_kind}",
        f"suggested_destination: {yaml_string(destination_map.get(suggested_kind, ''))}",
        f"source_file: {yaml_string(item['file'])}",
        f"source_url: {yaml_string(source_meta.get('source', ''))}",
        f"capture_id: {yaml_string(item['id'])}",
        f"reviewed_at: {yaml_string(now_local())}",
        "topics:",
    ]
    lines.extend([f"  - {yaml_string(x)}" for x in review.get("topics", [])] or ["  - 待分类"])
    lines.extend([
        "---", "", f"# {review['title']}", "",
        "## 快速判断", "",
        f"**建议：{review['recommendation']}｜相关度：{relevance_score}/100｜置信度：{review['confidence']}**", "",
        review["reason"], "",
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
        f"- 链接：{source_meta.get('source', '') or '未提供'}",
        "", "## 人工审核", "",
        "- [ ] 批准进入长期知识库",
        "- [ ] 仅保留为写作素材",
        "- [ ] 暂缓",
        "- [ ] 标记为可恢复的待清理项",
        "", "> 只勾选一项。系统会自动处理，但不会自动删除原文。", "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")
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
    result = {"x": 0, "mobile": 0, "web": 0}
    try:
        result["x"] = sync_x(state, config)
    except Exception as exc:
        message = f"x sync failed: {exc}"
        state["errors"].append({"at": now_local(), "message": message})
        log(message)
    result["mobile"] = sync_mobile(state, config)
    result["web"] = discover_web_clips(state)
    save_state(state)
    return result


def status(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "known_x_ids": len(state["known_x_ids"]),
        "known_web_files": len(state["known_web_files"]),
        "mobile_lines": len(state["mobile_line_hashes"]),
        "pending": len(state["pending_sources"]),
        "reviewed": len(state["reviewed_sources"]),
        "recent_errors": [error for error in state["errors"] if not error.get("resolved_at")][-5:],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Ray's Brain unified capture and review pipeline")
    parser.add_argument(
        "command", choices=["init", "sync", "repair-x", "review", "actions", "run", "status"]
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
    elif args.command == "sync":
        result = sync_only(state, config)
    elif args.command == "repair-x":
        result = repair_x(state, config)
    elif args.command == "review":
        result = {
            "actions": process_review_actions(),
            "legacy_actions": process_legacy_actions(),
            "reviewed": review_pending(state, config),
        }
        save_state(state)
    elif args.command == "actions":
        result = {
            "actions": process_review_actions(),
            "legacy_actions": process_legacy_actions(),
        }
        save_state(state)
    elif args.command == "run":
        actions_before = process_review_actions()
        legacy_actions_before = process_legacy_actions()
        result = sync_only(state, config)
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
        actions_after = process_review_actions()
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
