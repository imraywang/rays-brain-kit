#!/usr/bin/env python3
"""Ray's Brain 本地知识仪表盘。

只监听本机回环地址。Markdown 仍是唯一数据源；仪表盘只提供读取、灵感追加，
以及对现有审核卡勾选一个可恢复动作。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import threading
import time
import traceback
import webbrowser
from collections import Counter
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse


HERE = Path(__file__).resolve().parent
DEFAULT_VAULT = HERE.parents[2]
VAULT = Path(os.environ.get("RAYS_BRAIN", str(DEFAULT_VAULT))).resolve()
STATIC_DIR = HERE / "static"
STATE_HOME = Path(
    os.environ.get("RAYS_BRAIN_STATE", str(Path.home() / ".local/state/rays-brain"))
)
DEFAULT_LAYOUT = {
    "review_dir": "10-创作/10-灵感/10-待评估/剪藏复核",
    "inbox_file": "10-创作/10-灵感/inbox.md",
    "create_dir": "10-创作",
    "drafts_dir": "10-创作/20-草稿",
    "knowledge_dir": "20-知识",
    "sources_dir": "30-资料",
    "published_dir": "40-发布",
    "archive_dir": "90-归档",
}


def load_layout() -> dict[str, str]:
    """vault 目录布局，可用 config.json（或 RAYS_BRAIN_CONFIG 指定的文件）覆盖。
    审核卡的状态值与四个选项是管线协议，不属于布局配置。"""
    path = Path(os.environ.get("RAYS_BRAIN_CONFIG", str(HERE / "config.json")))
    layout = dict(DEFAULT_LAYOUT)
    if not path.exists():
        return layout
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"配置文件无法解析：{path}（{exc}）")
    unknown = sorted(set(loaded) - set(DEFAULT_LAYOUT))
    if unknown:
        raise SystemExit(
            f"配置文件包含未知键：{'、'.join(unknown)}。可用键：{'、'.join(sorted(DEFAULT_LAYOUT))}"
        )
    for key, value in loaded.items():
        raw = str(value).strip()
        if not raw or raw.startswith(("/", "~")) or ".." in Path(raw).parts:
            raise SystemExit(f"配置 {key} 必须是 vault 内的相对路径，现在是：{raw!r}")
        layout[key] = raw.strip("/")
    return layout


LAYOUT = load_layout()
REVIEW_DIR = VAULT / LAYOUT["review_dir"]
INBOX_FILE = VAULT / LAYOUT["inbox_file"]


def area_prefix(key: str) -> str:
    return LAYOUT[key] + "/"


STATUS_PENDING = "待审核"
STATUS_WRITABLE = "可写作"

ACTION_LABELS = {
    "knowledge": "批准进入长期知识库",
    "writing": "仅保留为写作素材",
    "later": "暂缓",
    "cleanup": "标记为可恢复的待清理项",
}
ACTION_ALIASES = {
    "移入可恢复的待清理区": "cleanup",
    **{label: key for key, label in ACTION_LABELS.items()},
}
ACTION_PATTERN = re.compile(
    r"^- \[(?P<checked>[ xX])\]\s+"
    r"(?P<label>批准进入长期知识库|仅保留为写作素材|暂缓|"
    r"移入可恢复的待清理区|标记为可恢复的待清理项)\s*$",
    flags=re.M,
)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def try_read(path: Path) -> str | None:
    """扫描途中文件可能被采集流程移走，或还没从 iCloud 下载；坏一个不拖垮整页。"""
    try:
        return read_text(path)
    except OSError:
        return None


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else None
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    if mode is not None:
        temporary.chmod(mode)
    temporary.replace(path)


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
            except (json.JSONDecodeError, TypeError):
                pass
        result[key.strip()] = str(value)
    return result


def without_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    return text[end + 5 :] if end >= 0 else text


def title_of(path: Path, text: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", text, flags=re.M)
    return match.group(1).strip() if match else path.stem


def section_of(text: str, heading: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$\n(?P<body>.*?)(?=^##\s|\Z)",
        text,
        flags=re.M | re.S,
    )
    return match.group("body").strip() if match else ""


def plain_excerpt(value: str, limit: int = 220) -> str:
    value = re.sub(r"^---.*?^---\s*", "", value, flags=re.M | re.S)
    value = re.sub(r"!?(\[\[)([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m.group(3) or m.group(2), value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"https?://\S+", "", value)
    value = re.sub(r"^[#>*`\-]+\s*", "", value, flags=re.M)
    value = re.sub(r"[*_`~]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def relative(path: Path) -> str:
    return path.relative_to(VAULT).as_posix()


def obsidian_uri(path: Path) -> str:
    return "obsidian://open?path=" + quote(str(path.resolve()), safe="")


def selected_action(text: str) -> str | None:
    selected = [
        ACTION_ALIASES.get(match.group("label"))
        for match in ACTION_PATTERN.finditer(text)
        if match.group("checked").lower() == "x"
    ]
    selected = [item for item in selected if item]
    return selected[0] if len(selected) == 1 else None


def review_card(path: Path) -> dict[str, object]:
    text = read_text(path)
    meta = parse_frontmatter(text)
    score_text = meta.get("relevance_score", "0")
    try:
        score = int(float(score_text))
    except ValueError:
        score = 0
    summary = section_of(text, "摘要") or section_of(text, "快速判断")
    reviewed_at = meta.get("reviewed_at", "")
    return {
        "path": relative(path),
        "title": title_of(path, text),
        "summary": plain_excerpt(summary or text),
        "score": score,
        "recommendation": meta.get("recommendation", "待判断"),
        "confidence": meta.get("confidence", ""),
        "kind": meta.get("suggested_kind", ""),
        "status": meta.get("status", "未标记"),
        "source_url": meta.get("source_url", ""),
        "reviewed_at": reviewed_at,
        "selected_action": selected_action(text),
        "obsidian_uri": obsidian_uri(path),
    }


def load_reviews() -> list[dict[str, object]]:
    if not REVIEW_DIR.exists():
        return []
    cards = []
    for path in REVIEW_DIR.glob("*.md"):
        try:
            cards.append(review_card(path))
        except OSError:
            continue
    cards.sort(key=lambda item: (int(item["score"]), str(item["reviewed_at"])), reverse=True)
    return cards


def markdown_files() -> list[Path]:
    ignored_parts = {".git", ".obsidian", "node_modules", "__pycache__"}
    try:
        dashboard_rel = HERE.relative_to(VAULT).parts
    except ValueError:
        dashboard_rel = ()
    files: list[Path] = []
    for path in VAULT.rglob("*.md"):
        rel_parts = path.relative_to(VAULT).parts
        if any(part in ignored_parts for part in rel_parts):
            continue
        if dashboard_rel and rel_parts[: len(dashboard_rel)] == dashboard_rel:
            continue
        files.append(path)
    return files


def load_snapshots(limit: int = 10) -> list[dict[str, object]]:
    snapshot_dir = STATE_HOME / "health-snapshots"
    points: list[dict[str, object]] = []
    if snapshot_dir.exists():
        for path in sorted(snapshot_dir.glob("*.json"))[-limit:]:
            try:
                item = json.loads(read_text(path))
            except (json.JSONDecodeError, OSError):
                continue
            points.append(
                {
                    "date": item.get("date", path.stem),
                    "health": item.get("health", "unknown"),
                    "pending": item.get("human_pending", 0),
                    "writable": item.get("writable_cards", 0),
                    "captured": item.get("captured_today_total", 0),
                    "knowledge_added": len(item.get("knowledge_added", [])),
                    "generated_at": item.get("generated_at", ""),
                }
            )
    return points


def load_runtime_state() -> dict[str, object]:
    state_file = STATE_HOME / "state.json"
    if not state_file.exists():
        return {}
    try:
        return json.loads(read_text(state_file))
    except (json.JSONDecodeError, OSError):
        return {}


def recent_notes(files: list[Path], limit: int = 9) -> list[dict[str, object]]:
    candidates: list[tuple[float, Path]] = []
    for path in files:
        if relative(path).startswith(area_prefix("archive_dir")):
            continue
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    candidates.sort(key=lambda row: row[0], reverse=True)
    result: list[dict[str, object]] = []
    for mtime, path in candidates:
        if len(result) >= limit:
            break
        text = try_read(path)
        if text is None:
            continue
        meta = parse_frontmatter(text)
        top = relative(path).split("/", 1)[0]
        result.append(
            {
                "path": relative(path),
                "title": title_of(path, text),
                "kind": meta.get("kind", ""),
                "area": top,
                "modified": datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="minutes"),
                "obsidian_uri": obsidian_uri(path),
            }
        )
    return result


def notes_under(files: list[Path], prefix: str, limit: int) -> list[dict[str, object]]:
    rows: list[tuple[float, Path]] = []
    for path in files:
        if not relative(path).startswith(prefix):
            continue
        try:
            rows.append((path.stat().st_mtime, path))
        except OSError:
            continue
    rows.sort(key=lambda row: row[0], reverse=True)
    result: list[dict[str, object]] = []
    for mtime, path in rows:
        if len(result) >= limit:
            break
        text = try_read(path)
        if text is None:
            continue
        result.append(
            {
                "path": relative(path),
                "title": title_of(path, text),
                "modified": datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="minutes"),
                "obsidian_uri": obsidian_uri(path),
            }
        )
    return result


def dashboard_payload() -> dict[str, object]:
    files = markdown_files()
    reviews = load_reviews()
    pending = [card for card in reviews if card["status"] == STATUS_PENDING]
    decision_pending = [card for card in pending if not card["selected_action"]]
    queued = [card for card in pending if card["selected_action"]]
    writable = [card for card in reviews if card["status"] == STATUS_WRITABLE]

    knowledge_files = [path for path in files if relative(path).startswith(area_prefix("knowledge_dir"))]
    knowledge_kinds: Counter[str] = Counter()
    for path in knowledge_files:
        text = try_read(path)
        if text is None:
            continue
        kind = parse_frontmatter(text).get("kind", "未标记") or "未标记"
        knowledge_kinds[kind] += 1

    trend = load_snapshots()
    latest_snapshot = trend[-1] if trend else {}
    runtime = load_runtime_state()
    unresolved_errors = [item for item in runtime.get("errors", []) if not item.get("resolved_at")]
    pipeline_pending = len(runtime.get("pending_sources", []))
    health = str(latest_snapshot.get("health", "unknown"))
    health_reasons: list[str] = []
    if unresolved_errors:
        health = "red"
        health_reasons.append(f"有 {len(unresolved_errors)} 个采集错误待处理")
    elif len(decision_pending) >= 10:
        health = "yellow"
        health_reasons.append(f"有 {len(decision_pending)} 张卡片等你判断")
    elif health == "unknown":
        health = "green"

    high_value = [card for card in decision_pending if int(card["score"]) >= 90]
    low_value = [
        card for card in decision_pending
        if int(card["score"]) < 65 or card["recommendation"] == "清理"
    ]
    focus = (
        f"先判断 {len(high_value)} 条高价值资料"
        if high_value
        else f"从 {len(writable)} 条可写材料里推进一篇"
        if writable
        else "当前没有紧急积压，适合整理旧知识"
    )

    drafts = notes_under(files, area_prefix("drafts_dir"), 30)
    published = notes_under(files, area_prefix("published_dir"), 12)
    for row in published:
        parts = str(row["path"]).split("/")
        row["platform"] = re.sub(r"^\d+-", "", parts[1]) if len(parts) > 2 else ""

    def count_prefix(prefix: str) -> int:
        return sum(1 for path in files if relative(path).startswith(prefix))

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "vault": VAULT.name,
        "health": health,
        "health_reasons": health_reasons,
        "focus": focus,
        "counts": {
            "all_notes": len(files),
            "captured": count_prefix(area_prefix("sources_dir")),
            "pipeline_pending": pipeline_pending,
            "decision_pending": len(decision_pending),
            "queued": len(queued),
            "writable": len(writable),
            "knowledge": len(knowledge_files),
            "drafts": count_prefix(area_prefix("drafts_dir")),
            "published": count_prefix(area_prefix("published_dir")),
            "high_value": len(high_value),
            "low_value": len(low_value),
        },
        "reviews": decision_pending,
        "queued_reviews": queued,
        "writable": writable,
        "drafts": drafts,
        "published": published,
        "recent": recent_notes(files),
        "knowledge_kinds": dict(knowledge_kinds.most_common()),
        "trend": trend,
        "latest_health_at": latest_snapshot.get("generated_at", ""),
        "inbox_uri": obsidian_uri(INBOX_FILE),
    }


def watch_signature() -> tuple:
    """高频变化位置的 mtime 指纹。只盯少量目录，避免每 2 秒扫全库。"""
    parts: list[tuple[str, int]] = []

    def add(root: Path, pattern: str, recursive: bool = False) -> None:
        if not root.exists():
            return
        for item in (root.rglob(pattern) if recursive else root.glob(pattern)):
            try:
                parts.append((str(item), item.stat().st_mtime_ns))
            except OSError:
                continue

    add(REVIEW_DIR, "*.md")
    add(VAULT / LAYOUT["drafts_dir"], "*.md", recursive=True)
    add(VAULT / LAYOUT["published_dir"], "*.md", recursive=True)
    add(STATE_HOME / "health-snapshots", "*.json")
    for single in (INBOX_FILE, STATE_HOME / "state.json"):
        try:
            parts.append((str(single), single.stat().st_mtime_ns))
        except OSError:
            continue
    parts.sort()
    return tuple(parts)


def search_notes(query: str, scope: str = "all", limit: int = 30) -> list[dict[str, object]]:
    query = query.strip().casefold()
    if len(query) < 2:
        return []
    scope_prefixes = {
        "all": "",
        "create": area_prefix("create_dir"),
        "knowledge": area_prefix("knowledge_dir"),
        "sources": area_prefix("sources_dir"),
        "published": area_prefix("published_dir"),
    }
    prefix = scope_prefixes.get(scope, "")
    scored: list[tuple[int, float, dict[str, object]]] = []
    for path in markdown_files():
        rel = relative(path)
        if prefix and not rel.startswith(prefix):
            continue
        text = try_read(path)
        if text is None:
            continue
        title = title_of(path, text)
        title_folded = title.casefold()
        content = without_frontmatter(text)
        body_folded = content.casefold()
        meta_folded = " ".join(parse_frontmatter(text).values()).casefold()
        if query not in title_folded and query not in body_folded and query not in meta_folded and query not in rel.casefold():
            continue
        score = 3 if query in title_folded else 2 if query in rel.casefold() else 1
        index = body_folded.find(query)
        start = max(0, index - 90) if index >= 0 else 0
        excerpt = plain_excerpt(content[start : start + 420], 190)
        meta = parse_frontmatter(text)
        item = {
            "path": rel,
            "title": title,
            "excerpt": excerpt,
            "kind": meta.get("kind", ""),
            "status": meta.get("status", ""),
            "obsidian_uri": obsidian_uri(path),
        }
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        scored.append((score, mtime, item))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [row[2] for row in scored[:limit]]


def choose_review_action(rel_path: str, action: str | None) -> dict[str, object]:
    candidate = (VAULT / rel_path).resolve()
    review_root = REVIEW_DIR.resolve()
    if candidate.parent != review_root or candidate.suffix != ".md" or not candidate.exists():
        raise ValueError("找不到这张审核卡")
    text = read_text(candidate)
    meta = parse_frontmatter(text)
    if meta.get("status") != STATUS_PENDING:
        raise ValueError("这张卡已经处理，刷新后再试")
    if action is not None and action not in ACTION_LABELS:
        raise ValueError("不支持这个审核选择")

    seen = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal seen
        seen += 1
        key = ACTION_ALIASES.get(match.group("label"))
        checked = "x" if action is not None and key == action else " "
        label = ACTION_LABELS.get(key or "", match.group("label"))
        return f"- [{checked}] {label}"

    updated = ACTION_PATTERN.sub(replace, text)
    if seen < 4:
        raise ValueError("这张审核卡格式不完整，请在 Obsidian 中检查")
    atomic_write(candidate, updated)
    return {
        "ok": True,
        "action": action,
        "label": ACTION_LABELS.get(action or "", "已撤销选择"),
        "path": rel_path,
    }


def append_capture(text: str) -> dict[str, object]:
    text = text.strip()
    if not text:
        raise ValueError("先写下一句话")
    if len(text) > 2000:
        raise ValueError("灵感太长了，请控制在 2000 字以内")
    if not INBOX_FILE.exists():
        raise ValueError("找不到灵感收件箱")
    now = datetime.now().astimezone()
    lines = text.splitlines()
    entry = f"- {now.strftime('%Y-%m-%d %H:%M')} · {lines[0].strip()}"
    if len(lines) > 1:
        entry += "\n" + "\n".join(f"  {line.rstrip()}" for line in lines[1:])
    current = read_text(INBOX_FILE).rstrip()
    atomic_write(INBOX_FILE, current + "\n\n" + entry + "\n")
    return {"ok": True, "captured_at": now.isoformat(timespec="minutes"), "text": text}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "RaysBrainDashboard/1.0"

    def log_message(self, format: str, *args: object) -> None:
        if getattr(self.server, "quiet", False):
            return
        super().log_message(format, *args)

    def allowed_host(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        return host in {"127.0.0.1", "localhost", "::1"}

    def local_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.scheme == "http" and (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}

    def send_common_headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        )

    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_common_headers(content_type, len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_bytes(body, "application/json; charset=utf-8", status)

    def error_json(self, message: str, status: int = 400) -> None:
        self.send_json({"ok": False, "error": message}, status)

    def safe_handle(self, handler) -> None:
        """任何一个请求出错都只影响自己：返回 JSON 错误，不中断服务。"""
        try:
            handler()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.error_json("读取知识库时出错，请刷新重试", HTTPStatus.INTERNAL_SERVER_ERROR)
            except OSError:
                pass

    def do_GET(self) -> None:
        self.safe_handle(self.handle_get)

    def do_POST(self) -> None:
        self.safe_handle(self.handle_post)

    def handle_get(self) -> None:
        if not self.allowed_host():
            self.error_json("仅允许从本机访问", HTTPStatus.FORBIDDEN)
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/healthz":
            self.send_json({"ok": True, "vault": VAULT.name})
            return
        if parsed.path == "/api/dashboard":
            self.send_json(dashboard_payload())
            return
        if parsed.path == "/api/search":
            params = parse_qs(parsed.query)
            query = params.get("q", [""])[0]
            scope = params.get("scope", ["all"])[0]
            self.send_json({"query": query, "results": search_notes(query, scope)})
            return
        if parsed.path == "/api/events":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.stream_events()
            return
        static_map = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/favicon.svg": ("favicon.svg", "image/svg+xml"),
            "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
        }
        target = static_map.get(parsed.path)
        if not target:
            self.error_json("页面不存在", HTTPStatus.NOT_FOUND)
            return
        path = STATIC_DIR / target[0]
        self.send_bytes(path.read_bytes(), target[1])

    def stream_events(self) -> None:
        """SSE：轮询关键位置的 mtime 指纹，变化时通知页面刷新。客户端断开即退出。"""
        self.wfile.write(b"retry: 3000\n\n")
        self.wfile.flush()
        last = watch_signature()
        while True:
            time.sleep(2)
            current = watch_signature()
            if current != last:
                time.sleep(1)  # 管线常一次写一批文件，等落定后只通知一次
                last = watch_signature()
                self.wfile.write(b"event: change\ndata: {}\n\n")
            else:
                self.wfile.write(b": keep-alive\n\n")
            self.wfile.flush()

    def handle_post(self) -> None:
        if not self.allowed_host() or not self.local_origin():
            self.error_json("仅允许从本机页面操作", HTTPStatus.FORBIDDEN)
            return
        if self.headers.get_content_type() != "application/json":
            self.error_json("请求格式不正确", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.error_json("请求长度不正确")
            return
        if length <= 0 or length > 16_384:
            self.error_json("请求内容过长")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.error_json("请求内容无法读取")
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/capture":
                self.send_json(append_capture(str(payload.get("text", ""))))
            elif parsed.path == "/api/reviews/action":
                action = payload.get("action")
                if action is not None:
                    action = str(action)
                self.send_json(choose_review_action(str(payload.get("path", "")), action))
            else:
                self.error_json("操作不存在", HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.error_json(str(exc))
        except OSError:
            self.error_json("写入失败，请确认 iCloud 文件已经下载到本机", HTTPStatus.INTERNAL_SERVER_ERROR)


def run_server(host: str, port: int, open_browser: bool, quiet: bool = False) -> None:
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    server.quiet = quiet
    url = f"http://{host}:{port}"
    print(f"Ray's Brain 知识仪表盘：{url}")
    print("按 Control+C 停止。")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n仪表盘已停止。")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ray's Brain 本地知识仪表盘")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("为保护私人笔记，仪表盘只允许监听本机地址。")
    run_server(args.host, args.port, not args.no_open, args.quiet)


if __name__ == "__main__":
    main()
