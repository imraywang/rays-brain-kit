#!/usr/bin/env python3
"""Ray's Brain 本地知识仪表盘。

只监听本机回环地址。Markdown 仍是唯一数据源；仪表盘提供读取、灵感追加、
审核卡勾选，以及按 board_protocol.json 声明的可撤回状态流转与候选立项。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from collections import Counter
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse


HERE = Path(__file__).resolve().parent
DASHBOARD_SCHEMA_VERSION = 5
SERVER_STARTED_AT = datetime.now().astimezone().isoformat(timespec="seconds")
DEFAULT_VAULT = HERE.parents[2]
VAULT = Path(os.environ.get("RAYS_BRAIN", str(DEFAULT_VAULT))).resolve()
STATIC_DIR = HERE / "static"
REVIEW_PROTOCOL_FILE = HERE.parent / "review_protocol.json"
BOARD_PROTOCOL_FILE = HERE.parent / "board_protocol.json"
INGEST_SCRIPT = HERE.parent / "知识采集" / "knowledge_ingest.py"
STATE_HOME = Path(
    os.environ.get("RAYS_BRAIN_STATE", str(Path.home() / ".local/state/rays-brain"))
)
DEFAULT_LAYOUT = {
    "review_dir": "10-创作/10-灵感/10-待评估/剪藏复核",
    "topics_dir": "10-创作/10-灵感/20-候选选题",
    "topic_reserve_dir": "10-创作/10-灵感/90-选题储备",
    "inbox_file": "10-创作/10-灵感/inbox.md",
    "link_inbox_file": "30-资料/00-待抓取/链接收件箱.md",
    "create_dir": "10-创作",
    "writing_tasks_dir": "10-创作/20-写作任务",
    "drafts_dir": "10-创作/30-文章草稿",
    "oral_scripts_dir": "10-创作/20-口播草稿",
    "knowledge_dir": "20-知识",
    "sources_dir": "30-资料",
    "published_dir": "40-发布",
    "feedback_dir": "40-发布/00-内容反馈",
    "archive_dir": "90-归档",
}


def load_layout() -> dict[str, str]:
    """vault 目录布局，可用 config.json（或 RAYS_BRAIN_CONFIG 指定的文件）覆盖。
    审核卡的状态值与五个选项是管线协议，不属于布局配置。"""
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
LINK_INBOX_FILE = VAULT / LAYOUT["link_inbox_file"]
# 锚定在 VAULT（而非代码目录）：指向别的 vault 运行时，队列跟着那个 vault 走
INTENT_QUEUE_FILE = VAULT / "50-系统/40-自动化/AI任务队列.md"
PIPELINE_LOG_FILE_NAME = "pipeline.log"


def area_prefix(key: str) -> str:
    return LAYOUT[key] + "/"


STATUS_PENDING = "待审核"


def load_review_protocol() -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, str],
    list[dict[str, object]],
]:
    """审核动作协议。文件缺失时降级为审核功能整体关闭，其余功能照常；
    文件存在但内容不合法仍然响亮退出，避免带着坏协议改写审核卡。"""
    if not REVIEW_PROTOCOL_FILE.exists():
        return {}, {}, {}, []
    try:
        data = json.loads(REVIEW_PROTOCOL_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"审核协议无法解析：{REVIEW_PROTOCOL_FILE}（{exc}）")
    actions = data.get("actions", [])
    if not isinstance(actions, list) or not actions:
        raise SystemExit("审核协议必须包含非空 actions")
    labels: dict[str, str] = {}
    aliases: dict[str, str] = {}
    ui_actions: list[dict[str, object]] = []
    shortcuts: set[int] = set()
    for item in actions:
        key = str(item.get("key", "")).strip()
        label = str(item.get("label", "")).strip()
        ui_label = str(item.get("ui_label", "")).strip()
        try:
            shortcut = int(item.get("shortcut", 0))
        except (TypeError, ValueError):
            shortcut = 0
        if (
            not key
            or not label
            or not ui_label
            or shortcut < 1
            or shortcut > 9
            or shortcut in shortcuts
            or key in labels
            or label in aliases
        ):
            raise SystemExit("审核协议包含空值或重复的 key/label")
        labels[key] = label
        aliases[label] = key
        shortcuts.add(shortcut)
        ui_actions.append(
            {
                "key": key,
                "label": label,
                "ui_label": ui_label,
                "shortcut": shortcut,
            }
        )
        for alias in item.get("aliases", []):
            alias = str(alias).strip()
            if not alias or alias in aliases:
                raise SystemExit(f"审核协议包含空值或重复别名：{alias!r}")
            aliases[alias] = key
    key_aliases = {
        str(key).strip(): str(value).strip()
        for key, value in data.get("key_aliases", {}).items()
    }
    if any(target not in labels for target in key_aliases.values()):
        raise SystemExit("审核协议 key_aliases 指向不存在的动作")
    ui_actions.sort(key=lambda item: int(item["shortcut"]))
    return labels, aliases, key_aliases, ui_actions


ACTION_LABELS, ACTION_ALIASES, ACTION_KEY_ALIASES, UI_ACTIONS = load_review_protocol()
# 协议缺失（降级模式）时用一个永不匹配的分支占位，勾选解析自然全部落空
_ACTION_LABEL_PATTERN = "|".join(
    sorted((re.escape(label) for label in ACTION_ALIASES), key=len, reverse=True)
) or r"(?!x)x"
ACTION_PATTERN = re.compile(
    rf"^- \[(?P<checked>[ xX])\]\s+(?P<label>{_ACTION_LABEL_PATTERN})\s*$",
    flags=re.M,
)


def load_board_protocol() -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    """看板状态流转协议：哪些 kind、在哪些目录、允许哪些状态变化。

    sequence 会展开为相邻状态的「推进/退回」；from 为 "*" 表示任一已声明状态。
    这里只做声明校验，真正执行前 apply_transition 还会重新核对文件现状。
    """
    if not BOARD_PROTOCOL_FILE.exists():
        return [], {}
    try:
        data = json.loads(BOARD_PROTOCOL_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"看板协议无法解析：{BOARD_PROTOCOL_FILE}（{exc}）")
    groups: list[dict[str, object]] = []
    kind_map: dict[str, dict[str, object]] = {}
    for raw in data.get("groups", []):
        kinds = [str(kind).strip() for kind in raw.get("kinds", []) if str(kind).strip()]
        labels = {
            str(key).strip(): str(value).strip()
            for key, value in raw.get("status_labels", {}).items()
        }
        dir_keys = [str(key).strip() for key in raw.get("dirs", [])]
        if not kinds or not labels or not dir_keys:
            raise SystemExit("看板协议的每一组都必须包含 kinds、status_labels 和 dirs")
        unknown_dirs = [key for key in dir_keys if key not in LAYOUT]
        if unknown_dirs:
            raise SystemExit(f"看板协议 dirs 引用了未知布局键：{'、'.join(unknown_dirs)}")
        transitions: list[dict[str, object]] = []

        def add_transition(source: str, target: str, label: str, confirm: str = "", extra: dict | None = None) -> None:
            if source == target or source not in labels or target not in labels:
                raise SystemExit(f"看板协议包含无效流转：{source!r} → {target!r}")
            if any(t["from"] == source and t["to"] == target for t in transitions):
                return
            transitions.append(
                {
                    "from": source,
                    "to": target,
                    "label": label,
                    "confirm": confirm,
                    "set": {str(k): str(v) for k, v in (extra or {}).items()},
                }
            )

        sequence = [str(item).strip() for item in raw.get("sequence", [])]
        for item in raw.get("transitions", []):
            source = str(item.get("from", "")).strip()
            target = str(item.get("to", "")).strip()
            label = str(item.get("label", "")).strip()
            confirm = str(item.get("confirm", "")).strip()
            extra = item.get("set", {})
            if not label:
                raise SystemExit("看板协议的每条流转都必须有 label")
            sources = [s for s in labels if s != target] if source == "*" else [source]
            for one in sources:
                add_transition(one, target, label, confirm, extra)
        for left, right in zip(sequence, sequence[1:]):
            add_transition(left, right, f"推进到「{labels.get(right, right)}」")
            add_transition(right, left, f"退回「{labels.get(left, left)}」")
        group = {
            "kinds": kinds,
            "prefixes": tuple(LAYOUT[key] + "/" for key in dir_keys),
            "status_labels": labels,
            "set_on_change": {
                str(k): str(v) for k, v in raw.get("set_on_change", {}).items()
            },
            "transitions": transitions,
        }
        groups.append(group)
        for kind in kinds:
            if kind in kind_map:
                raise SystemExit(f"看板协议中 kind 重复声明：{kind}")
            kind_map[kind] = group
    return groups, kind_map


BOARD_GROUPS, BOARD_KIND_MAP = load_board_protocol()


def client_board_protocol() -> dict[str, object]:
    """给前端的流转描述：按 kind 展平，不含目录约束等服务端细节。"""
    result: dict[str, object] = {}
    for kind, group in BOARD_KIND_MAP.items():
        result[kind] = {
            "status_labels": group["status_labels"],
            "transitions": [
                {
                    "from": t["from"],
                    "to": t["to"],
                    "label": t["label"],
                    "confirm": t["confirm"],
                }
                for t in group["transitions"]
            ],
        }
    return result


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


def yaml_value(value: str) -> str:
    """尽量写无引号的简单值：vault 里既有的 Obsidian 查询按 `^status: active$` 匹配。"""
    value = str(value)
    if not value or value != value.strip() or re.search(r'[:#"\'\n\[\]{}]', value):
        return json.dumps(value, ensure_ascii=False)
    return value


def update_frontmatter_text(text: str, updates: dict[str, str]) -> str:
    """定点改写页首属性：只动指定键，其余行原样保留；缺失键补在块尾。"""
    if not text.startswith("---\n"):
        raise ValueError("这篇笔记缺少页首属性，无法在工作台改状态")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("这篇笔记页首属性不完整，请先在 Obsidian 中检查")
    lines = text[4:end].splitlines()
    positions: dict[str, int] = {}
    for index, line in enumerate(lines):
        if ":" not in line or line.startswith((" ", "\t", "-")):
            continue
        positions[line.split(":", 1)[0].strip()] = index
    for key, value in updates.items():
        rendered = f"{key}: {yaml_value(value)}"
        if key in positions:
            lines[positions[key]] = rendered
        else:
            lines.append(rendered)
    return "---\n" + "\n".join(lines) + text[end:]


def resolve_set_value(value: str) -> str:
    now = datetime.now().astimezone()
    if value == "$date":
        return now.date().isoformat()
    if value == "$datetime":
        return now.strftime("%Y-%m-%d %H:%M")
    return value


_LOG_LOCK = threading.Lock()


def log_operation(entry: dict[str, object]) -> None:
    """操作日志只是审计线索，写失败不能影响请求本身。"""
    record = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), **entry}
    try:
        log_dir = STATE_HOME / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK, (log_dir / "dashboard-actions.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


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


def first_value(meta: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = meta.get(key, "").strip()
        if value:
            return value
    return ""


def score_value(value: object) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def content_values(meta: dict[str, str]) -> dict[str, object]:
    """兼容管线新旧字段名，向页面提供稳定的三项判断值。"""
    return {
        "knowledge_value": first_value(
            meta, "knowledge_value_score", "knowledge_value", "long_term_value_score"
        ),
        "writing_value": first_value(
            meta, "writing_value_score", "writing_value", "content_value_score"
        ),
        "timeliness": first_value(
            meta, "timeliness", "freshness", "freshness_status", "time_sensitivity"
        ),
    }


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
    score = score_value(meta.get("priority_score") or meta.get("relevance_score", "0"))
    summary = section_of(text, "摘要") or section_of(text, "快速判断")
    reviewed_at = meta.get("reviewed_at", "")
    card = {
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
    card.update(content_values(meta))
    return card


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
                    "red_reasons": item.get("red_reasons", []),
                    "yellow_reasons": item.get("yellow_reasons", []),
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


def note_record(path: Path, text: str, mtime: float) -> dict[str, object]:
    meta = parse_frontmatter(text)
    summary = (
        section_of(text, "一句话判断")
        or section_of(text, "核心判断")
        or section_of(text, "摘要")
    )
    item: dict[str, object] = {
        "path": relative(path),
        "title": title_of(path, text),
        "summary": plain_excerpt(summary),
        "kind": meta.get("kind", ""),
        "status": meta.get("status", ""),
        "priority_score": score_value(meta.get("priority_score", "0")),
        "writing_value_score": score_value(
            meta.get("writing_value_score") or meta.get("writing_value", "0")
        ),
        "platform": meta.get("platform", ""),
        "modified": datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="minutes"),
        "obsidian_uri": obsidian_uri(path),
    }
    item.update(content_values(meta))
    return item


def notes_under(
    files: list[Path],
    prefix: str,
    limit: int,
    *,
    exclude_prefixes: tuple[str, ...] = (),
    predicate=None,
    sort_key=None,
) -> list[dict[str, object]]:
    rows: list[tuple[float, Path]] = []
    for path in files:
        rel = relative(path)
        if not rel.startswith(prefix) or any(rel.startswith(item) for item in exclude_prefixes):
            continue
        try:
            rows.append((path.stat().st_mtime, path))
        except OSError:
            continue
    rows.sort(key=lambda row: row[0], reverse=True)
    result: list[dict[str, object]] = []
    for mtime, path in rows:
        text = try_read(path)
        if text is None:
            continue
        item = note_record(path, text, mtime)
        if predicate is not None and not predicate(item, parse_frontmatter(text)):
            continue
        result.append(item)
    if sort_key is not None:
        result.sort(key=sort_key, reverse=True)
    return result[:limit]


def has_topic_freshness_anchor(meta: dict[str, str]) -> bool:
    return bool(
        meta.get("source_published_at", "").strip()
        or (
            meta.get("refreshed_at", "").strip()
            and meta.get("refresh_source", "").strip()
        )
    )


def load_topic_candidates(files: list[Path], limit: int = 40) -> list[dict[str, object]]:
    today = date.today().isoformat()
    return notes_under(
        files,
        area_prefix("topics_dir"),
        limit,
        predicate=lambda item, meta: (
            meta.get("kind") == "topic-candidate"
            and meta.get("status") == "candidate"
            and has_topic_freshness_anchor(meta)
            and meta.get("freshness_status") == "fresh"
            and meta.get("fresh_until", "") >= today
        ),
        sort_key=lambda item: (
            int(item["priority_score"]),
            int(item["writing_value_score"]),
            str(item["modified"]),
        ),
    )


def load_topic_continuations(files: list[Path], limit: int = 40) -> list[dict[str, object]]:
    today = date.today().isoformat()
    return notes_under(
        files,
        area_prefix("topics_dir"),
        limit,
        predicate=lambda item, meta: (
            meta.get("kind") == "topic-candidate"
            and meta.get("status") == "partially-published"
            and has_topic_freshness_anchor(meta)
            and meta.get("freshness_status") == "fresh"
            and meta.get("fresh_until", "") >= today
        ),
        sort_key=lambda item: (
            int(item["priority_score"]),
            int(item["writing_value_score"]),
            str(item["modified"]),
        ),
    )


INACTIVE_TASK_STATUSES = {
    "published",
    "completed",
    "archived",
    "cancelled",
    "closed",
    "done",
    "已发布",
    "已完成",
    "已归档",
    "已取消",
}


def load_writing_tasks(files: list[Path], limit: int = 40) -> list[dict[str, object]]:
    return notes_under(
        files,
        area_prefix("writing_tasks_dir"),
        limit,
        predicate=lambda item, meta: (
            meta.get("kind") in {"writing-task", "content-task", "content-pack"}
            and meta.get("status", "").strip().lower() not in INACTIVE_TASK_STATUSES
        ),
        sort_key=lambda item: (
            int(item["priority_score"]),
            int(item["writing_value_score"]),
            str(item["modified"]),
        ),
    )


INACTIVE_DRAFT_STATUSES = INACTIVE_TASK_STATUSES | {
    "alternative-draft",
    "superseded",
}


def load_drafts(files: list[Path], limit: int = 40) -> list[dict[str, object]]:
    """母稿看板：两处草稿，按目录分，不是按 kind 分。

    从长文改出来的口播稿放在 drafts_dir，跟着母稿走，不在这里单独占位——这是原
    来就有的规矩。但以口播起稿、根本没有图文母稿的内容（FDE 系列这种）放在
    oral_scripts_dir，它自己就是母稿；不收进来，这类内容在看板上一次都不会出现。
    """
    def active(kinds: set[str]):
        return lambda item, meta: (
            meta.get("kind") in kinds
            and meta.get("status", "").strip().lower() not in INACTIVE_DRAFT_STATUSES
        )

    merged = notes_under(
        files, area_prefix("drafts_dir"), limit, predicate=active({"draft", "article-draft"})
    ) + notes_under(
        files, area_prefix("oral_scripts_dir"), limit, predicate=active({"oral-script"})
    )
    merged.sort(key=lambda item: str(item.get("modified", "")), reverse=True)
    return merged[:limit]


COMPLETED_FEEDBACK_STATUSES = {
    "reviewed",
    "complete",
    "completed",
    "closed",
    "done",
    "已复盘",
    "已完成",
    "已关闭",
}


def load_feedback(files: list[Path], limit: int = 80) -> list[dict[str, object]]:
    completion_fields = (
        "reviewed_at",
        "completed_at",
        "feedback_completed_at",
    )

    def is_feedback(item: dict[str, object], meta: dict[str, str]) -> bool:
        return meta.get("kind") in {
            "content-feedback",
            "publication-feedback",
            "feedback",
        }

    rows = notes_under(
        files,
        area_prefix("feedback_dir"),
        limit,
        predicate=is_feedback,
    )
    for item in rows:
        path = VAULT / str(item["path"])
        text = try_read(path) or ""
        meta = parse_frontmatter(text)
        status = meta.get("status", "").strip().lower()
        completed = status in COMPLETED_FEEDBACK_STATUSES or any(
            meta.get(key, "").strip() for key in completion_fields
        )
        item["pending"] = not completed
        item["due_at"] = first_value(
            meta,
            "due_at",
            "due_date",
            "feedback_due_at",
            "review_due_at",
            "feedback_due",
        )
    rows.sort(
        key=lambda item: (
            bool(item.get("pending")),
            str(item.get("due_at", "")),
            str(item["modified"]),
        ),
        reverse=True,
    )
    return rows


def dashboard_payload() -> dict[str, object]:
    files = markdown_files()
    reviews = load_reviews()
    pending = [card for card in reviews if card["status"] == STATUS_PENDING]
    decision_pending = [card for card in pending if not card["selected_action"]]
    queued = [card for card in pending if card["selected_action"]]
    topic_candidates = load_topic_candidates(files, len(files))
    topic_continuations = load_topic_continuations(files, len(files))
    writing_tasks = load_writing_tasks(files, len(files))
    feedback = load_feedback(files, len(files))
    feedback_pending = [item for item in feedback if item["pending"]]
    drafts = load_drafts(files, len(files))
    published = notes_under(
        files,
        area_prefix("published_dir"),
        30,
        exclude_prefixes=(area_prefix("feedback_dir"),),
    )
    for row in published:
        if row["platform"]:
            continue
        parts = str(row["path"]).split("/")
        row["platform"] = re.sub(r"^\d+-", "", parts[1]) if len(parts) > 2 else ""

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
    try:
        # state.json 每轮采集都会保存，它的 mtime 就是管线最近一次活动时间
        pipeline_last_activity = (
            datetime.fromtimestamp((STATE_HOME / "state.json").stat().st_mtime)
            .astimezone()
            .isoformat(timespec="minutes")
        )
    except OSError:
        pipeline_last_activity = ""
    reports = [
        entry
        for entry in (
            {"title": "健康日报", "path": "00-入口/20-日报/知识库健康日报/最新健康日报.md"},
            {"title": "知识库周报", "path": "00-入口/20-日报/知识库周报/最新知识库周报.md"},
        )
        if (VAULT / entry["path"]).is_file()
    ]
    health = str(latest_snapshot.get("health", "unknown"))
    health_reasons = [
        str(reason)
        for reason in (
            list(latest_snapshot.get("red_reasons", []))
            + list(latest_snapshot.get("yellow_reasons", []))
        )
    ]
    if unresolved_errors:
        health = "red"
        health_reasons.append(f"有 {len(unresolved_errors)} 个采集错误待处理")
    elif len(decision_pending) >= 10:
        health = "yellow"
        health_reasons.append(f"有 {len(decision_pending)} 张卡片等你判断")
    elif health == "unknown":
        health = "green"
    if not ACTION_LABELS and pending:
        # 降级模式：没有审核协议时卡片只读，提醒去 Obsidian 处理或补协议
        if health == "green":
            health = "yellow"
        health_reasons.append(
            f"审核协议未安装（review_protocol.json），{len(pending)} 张审核卡暂时只读"
        )

    high_value = [card for card in decision_pending if int(card["score"]) >= 90]
    low_value = [
        card for card in decision_pending
        if int(card["score"]) < 65 or card["recommendation"] == "清理"
    ]
    focus = (
        f"先判断 {len(high_value)} 条高价值资料"
        if high_value
        else f"从 {len(writing_tasks)} 个写作任务里推进一篇"
        if writing_tasks
        else f"从 {len(topic_candidates)} 个候选选题里挑一个立项"
        if topic_candidates
        else f"从 {len(topic_continuations)} 个可续写角度里挑一个推进"
        if topic_continuations
        else f"复盘 {len(feedback_pending)} 篇已发布内容"
        if feedback_pending
        else "当前没有紧急积压，适合整理旧知识"
    )

    def count_prefix(prefix: str, exclude_prefixes: tuple[str, ...] = ()) -> int:
        return sum(
            1
            for path in files
            if relative(path).startswith(prefix)
            and not any(relative(path).startswith(item) for item in exclude_prefixes)
        )

    return {
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "server_started_at": SERVER_STARTED_AT,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "vault": VAULT.name,
        "health": health,
        "health_reasons": health_reasons,
        "review_actions": UI_ACTIONS,
        "board_protocol": client_board_protocol(),
        "pipeline": {
            "last_activity": pipeline_last_activity,
            "unresolved_errors": len(unresolved_errors),
            "pending": pipeline_pending,
        },
        "reports": reports,
        "focus": focus,
        "counts": {
            "all_notes": len(files),
            "captured": count_prefix(area_prefix("sources_dir")),
            "pipeline_pending": pipeline_pending,
            "decision_pending": len(decision_pending),
            "queued": len(queued),
            "topic_candidates": len(topic_candidates),
            "topic_continuations": len(topic_continuations),
            "writing_tasks": len(writing_tasks),
            "knowledge": len(knowledge_files),
            "drafts": len(drafts),
            "published": count_prefix(
                area_prefix("published_dir"),
                (area_prefix("feedback_dir"),),
            ),
            "feedback": len(feedback),
            "feedback_pending": len(feedback_pending),
            "high_value": len(high_value),
            "low_value": len(low_value),
        },
        "reviews": decision_pending,
        "queued_reviews": queued,
        "topic_candidates": topic_candidates,
        "topic_continuations": topic_continuations,
        "writing_tasks": writing_tasks,
        "drafts": drafts,
        "published": published,
        "feedback": feedback,
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
    add(VAULT / LAYOUT["topics_dir"], "*.md", recursive=True)
    add(VAULT / LAYOUT["topic_reserve_dir"], "*.md", recursive=True)
    add(VAULT / LAYOUT["writing_tasks_dir"], "*.md", recursive=True)
    add(VAULT / LAYOUT["drafts_dir"], "*.md", recursive=True)
    add(VAULT / LAYOUT["oral_scripts_dir"], "*.md", recursive=True)
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
    if not ACTION_LABELS:
        raise ValueError(
            f"审核功能未启用：缺少协议文件 {REVIEW_PROTOCOL_FILE}，补上后重启工作台"
        )
    candidate = (VAULT / rel_path).resolve()
    review_root = REVIEW_DIR.resolve()
    if candidate.parent != review_root or candidate.suffix != ".md" or not candidate.exists():
        raise ValueError("找不到这张审核卡")
    text = read_text(candidate)
    meta = parse_frontmatter(text)
    if meta.get("status") != STATUS_PENDING:
        raise ValueError("这张卡已经处理，刷新后再试")
    if action is not None:
        action = ACTION_KEY_ALIASES.get(action, action)
    if action is not None and action not in ACTION_LABELS:
        raise ValueError("不支持这个审核选择")

    matches = list(ACTION_PATTERN.finditer(text))
    if len(matches) < 4:
        raise ValueError("这张审核卡格式不完整，请在 Obsidian 中检查")
    choices = "\n".join(
        f"- [{'x' if action == key else ' '}] {label}"
        for key, label in ACTION_LABELS.items()
    )
    updated = text[: matches[0].start()] + choices + text[matches[-1].end() :]
    atomic_write(candidate, updated)
    return {
        "ok": True,
        "action": action,
        "label": ACTION_LABELS.get(action or "", "已撤销选择"),
        "path": rel_path,
    }


CAPTURE_URL_PATTERN = re.compile(r"https?://[^\s<>\"）)】]+")


def append_capture(text: str) -> dict[str, object]:
    text = text.strip()
    if not text:
        raise ValueError("先写下一句话")
    if len(text) > 2000:
        raise ValueError("灵感太长了，请控制在 2000 字以内")
    now = datetime.now().astimezone()
    lines = text.splitlines()
    match = CAPTURE_URL_PATTERN.search(lines[0])
    if match:
        # 首行带链接的速记直接投链接收件箱，走采集管线抓取，而不是躺在灵感箱里
        if not LINK_INBOX_FILE.exists():
            raise ValueError("找不到链接收件箱")
        url = match.group(0).rstrip(".,;，。；")
        note = (lines[0][: match.start()] + lines[0][match.end():]).strip(" -[]\t·")
        extra = " ".join(line.strip() for line in lines[1:] if line.strip())
        note = " ".join(part for part in (note, extra) if part)[:200]
        entry = f"- [ ] {url}" + (f" {note}" if note else "")
        current = read_text(LINK_INBOX_FILE).rstrip()
        atomic_write(LINK_INBOX_FILE, current + "\n" + entry + "\n")
        log_operation({"op": "capture-link", "url": url})
        return {
            "ok": True,
            "target": "link-inbox",
            "captured_at": now.isoformat(timespec="minutes"),
            "url": url,
        }
    if not INBOX_FILE.exists():
        raise ValueError("找不到灵感收件箱")
    entry = f"- {now.strftime('%Y-%m-%d %H:%M')} · {lines[0].strip()}"
    if len(lines) > 1:
        entry += "\n" + "\n".join(f"  {line.rstrip()}" for line in lines[1:])
    current = read_text(INBOX_FILE).rstrip()
    atomic_write(INBOX_FILE, current + "\n\n" + entry + "\n")
    return {
        "ok": True,
        "target": "inbox",
        "captured_at": now.isoformat(timespec="minutes"),
        "text": text,
    }


IGNORED_PARTS = {".git", ".obsidian", "node_modules", "__pycache__"}


def contain_in_vault(rel_path: str) -> Path:
    """把请求路径钉死在 vault 内：按 resolve 后的根做越界判断，返回以 VAULT 为基准的路径。"""
    if not rel_path or rel_path.startswith(("/", "~")):
        raise ValueError("找不到这篇笔记")
    try:
        rel = (VAULT / rel_path).resolve().relative_to(VAULT.resolve())
    except (ValueError, OSError):
        raise ValueError("找不到这篇笔记")
    if any(part in IGNORED_PARTS for part in rel.parts):
        raise ValueError("找不到这篇笔记")
    return VAULT / rel


def vault_note_path(rel_path: str) -> Path:
    candidate = contain_in_vault(rel_path)
    if candidate.suffix != ".md":
        raise ValueError("找不到这篇笔记")
    if not candidate.is_file():
        raise ValueError("这篇笔记不存在，可能刚被移动；请刷新后再试")
    return candidate


def resolve_wikilink(target: str) -> Path | None:
    """按 Obsidian 习惯解析双链：优先当相对路径，其次按文件名全库匹配。"""
    target = target.split("|", 1)[0].split("#", 1)[0].strip().strip("/")
    if not target:
        return None
    if "/" in target:
        for suffix in ("", ".md"):
            try:
                return vault_note_path(target + suffix)
            except ValueError:
                continue
    stem = target[:-3] if target.lower().endswith(".md") else target
    stem = stem.casefold()
    matches = [path for path in markdown_files() if path.stem.casefold() == stem]
    if not matches:
        return None
    return min(matches, key=lambda path: len(relative(path)))


def note_payload(rel_path: str = "", link: str = "") -> dict[str, object]:
    if rel_path:
        path = vault_note_path(rel_path)
    else:
        resolved = resolve_wikilink(link)
        if resolved is None:
            raise ValueError(f"没有找到「{link.strip()}」对应的笔记")
        path = resolved
    text = read_text(path)
    meta = parse_frontmatter(text)
    return {
        "ok": True,
        "path": relative(path),
        "title": title_of(path, text),
        "frontmatter": meta,
        "body": without_frontmatter(text),
        "kind": meta.get("kind", ""),
        "status": meta.get("status", ""),
        # 字符串透传：mtime_ns 超出 JS 安全整数范围，数字形式会在浏览器里丢精度
        "mtime_ns": str(path.stat().st_mtime_ns),
        "obsidian_uri": obsidian_uri(path),
    }


ASSET_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
}
ASSET_SIZE_LIMIT = 26_214_400
_ASSET_INDEX: dict[str, object] = {"at": 0.0, "names": {}}


def asset_index() -> dict[str, Path]:
    """文件名 → 附件路径的索引；vault 很大，缓存两分钟避免每张图都全库扫描。"""
    now = time.monotonic()
    if now - float(_ASSET_INDEX["at"]) > 120 or not _ASSET_INDEX["names"]:
        names: dict[str, Path] = {}
        for path in VAULT.rglob("*"):
            if path.suffix.lower() not in ASSET_TYPES or not path.is_file():
                continue
            rel_parts = path.relative_to(VAULT).parts
            if any(part in IGNORED_PARTS for part in rel_parts):
                continue
            key = path.name.casefold()
            if key not in names or len(relative(path)) < len(relative(names[key])):
                names[key] = path
        _ASSET_INDEX["names"] = names
        _ASSET_INDEX["at"] = now
    return _ASSET_INDEX["names"]  # type: ignore[return-value]


def find_asset(rel_path: str = "", link: str = "") -> Path:
    candidate: Path | None = None
    if rel_path:
        try:
            direct = contain_in_vault(rel_path)
            candidate = direct if direct.is_file() else None
        except ValueError:
            candidate = None
    elif link:
        link = link.split("|", 1)[0].strip().strip("/")
        if "/" in link:
            try:
                direct = contain_in_vault(link)
                candidate = direct if direct.is_file() else None
            except ValueError:
                candidate = None
        if candidate is None and link:
            candidate = asset_index().get(Path(link).name.casefold())
    if candidate is None or candidate.suffix.lower() not in ASSET_TYPES or not candidate.is_file():
        raise ValueError("找不到这个附件")
    if candidate.stat().st_size > ASSET_SIZE_LIMIT:
        raise ValueError("附件太大，请在 Obsidian 中查看")
    return candidate


def apply_transition(
    rel_path: str, to_status: str, expected_mtime_ns: int | None
) -> dict[str, object]:
    path = vault_note_path(rel_path)
    rel = relative(path)
    text = read_text(path)
    meta = parse_frontmatter(text)
    group = BOARD_KIND_MAP.get(meta.get("kind", ""))
    if group is None:
        raise ValueError("这类笔记不支持在工作台改状态")
    if not any(rel.startswith(prefix) for prefix in group["prefixes"]):
        raise ValueError("这篇笔记不在对应的工作目录里，请在 Obsidian 中处理")
    from_status = meta.get("status", "").strip()
    transition = next(
        (
            t
            for t in group["transitions"]
            if t["from"] == from_status and t["to"] == to_status
        ),
        None,
    )
    if transition is None:
        label = group["status_labels"].get(from_status, from_status or "未标记")
        raise ValueError(f"当前状态「{label}」不支持这个流转，可能刚被其他流程更新；请刷新后再试")
    if expected_mtime_ns is not None:
        try:
            expected = int(expected_mtime_ns)
        except (TypeError, ValueError):
            raise ValueError("请求格式不正确")
        if path.stat().st_mtime_ns != expected:
            raise ValueError("这篇笔记刚被其他程序修改过，请刷新后再试")
    updates: dict[str, str] = {"status": to_status}
    for key, value in {**group["set_on_change"], **transition["set"]}.items():
        updates[key] = resolve_set_value(value)
    atomic_write(path, update_frontmatter_text(text, updates))
    log_operation({"op": "transition", "path": rel, "from": from_status, "to": to_status})
    return {
        "ok": True,
        "path": rel,
        "from": from_status,
        "to": to_status,
        "label": str(transition["label"]),
        "to_label": group["status_labels"].get(to_status, to_status),
        "mtime_ns": str(path.stat().st_mtime_ns),
    }


def promote_candidate(rel_path: str, angle: str) -> dict[str, object]:
    """立项走既有管线命令，工作台只负责校验入参和转述结果。"""
    angle = " ".join(angle.split())
    if not angle:
        raise ValueError("先用一句话写清本次角度")
    if len(angle) > 120:
        raise ValueError("角度请控制在 120 字以内")
    path = vault_note_path(rel_path)
    rel = relative(path)
    topic_prefixes = (area_prefix("topics_dir"), area_prefix("topic_reserve_dir"))
    if not rel.startswith(topic_prefixes):
        raise ValueError("只能对候选选题或选题储备立项")
    if parse_frontmatter(read_text(path)).get("kind") != "topic-candidate":
        raise ValueError("这篇笔记不是候选选题")
    if not INGEST_SCRIPT.is_file():
        raise ValueError("找不到知识采集脚本，无法在工作台立项")
    env = {**os.environ, "RAYS_BRAIN": str(VAULT), "RAYS_BRAIN_STATE": str(STATE_HOME)}
    try:
        completed = subprocess.run(
            [sys.executable, str(INGEST_SCRIPT), "promote", rel, "--angle", angle],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            cwd=str(INGEST_SCRIPT.parent),
        )
    except subprocess.TimeoutExpired:
        raise ValueError("立项超时，请稍后在终端重试")
    if completed.returncode != 0:
        stderr_lines = [line.strip() for line in completed.stderr.splitlines() if line.strip()]
        message = stderr_lines[-1] if stderr_lines else "未知错误"
        message = message.split("ValueError:", 1)[-1].strip()
        raise ValueError(f"立项失败：{message[:300]}")
    stdout = completed.stdout
    brace = stdout.find("{")
    try:
        data = json.loads(stdout[brace:]) if brace >= 0 else {}
    except json.JSONDecodeError:
        data = {}
    task_rel = str(data.get("writing_task", ""))
    log_operation({"op": "promote", "path": rel, "angle": angle, "writing_task": task_rel})
    return {
        "ok": True,
        "promoted": str(data.get("promoted", rel)),
        "writing_task": task_rel,
        "angle": angle,
    }


def save_note_body(
    rel_path: str, body: str, expected_mtime_ns: object
) -> dict[str, object]:
    """全文编辑只替换正文，页首属性原样保留——属性由流转和管线负责。"""
    if len(body) > 512_000:
        raise ValueError("正文太长，请回 Obsidian 编辑这一篇")
    path = vault_note_path(rel_path)
    if expected_mtime_ns is None:
        raise ValueError("请求格式不正确")
    try:
        expected = int(expected_mtime_ns)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("请求格式不正确")
    if path.stat().st_mtime_ns != expected:
        raise ValueError("这篇笔记刚被其他程序修改过；请复制你的改动，刷新后再编辑")
    text = read_text(path)
    head = ""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            head = text[: end + 5]
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    if body and not body.endswith("\n"):
        body += "\n"
    atomic_write(path, head + body)
    log_operation({"op": "edit", "path": relative(path), "chars": len(body)})
    return {
        "ok": True,
        "path": relative(path),
        "mtime_ns": str(path.stat().st_mtime_ns),
    }


_MANUAL_RUN: dict[str, object] = {"proc": None, "started_at": ""}


def manual_run_running() -> bool:
    proc = _MANUAL_RUN["proc"]
    return proc is not None and proc.poll() is None  # type: ignore[union-attr]


def start_manual_run() -> dict[str, object]:
    if manual_run_running():
        raise ValueError("已有一次手动采集在进行中，请等它跑完")
    if not INGEST_SCRIPT.is_file():
        raise ValueError("找不到知识采集脚本")
    env = {**os.environ, "RAYS_BRAIN": str(VAULT), "RAYS_BRAIN_STATE": str(STATE_HOME)}
    log_dir = STATE_HOME / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    with (log_dir / "manual-run.log").open("a", encoding="utf-8") as handle:
        handle.write(f"\n===== 工作台手动采集 {started_at} =====\n")
        handle.flush()
        proc = subprocess.Popen(
            [sys.executable, str(INGEST_SCRIPT), "run"],
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(INGEST_SCRIPT.parent),
        )
    _MANUAL_RUN["proc"] = proc
    _MANUAL_RUN["started_at"] = started_at
    log_operation({"op": "manual-run", "started_at": started_at})
    return {"ok": True, "started_at": started_at}


def resolve_pipeline_error(at: str, message: str) -> dict[str, object]:
    """在管线状态里标记一条错误已解决。与 30 分钟任务并发写有极小竞态，原子替换兜底。"""
    state_file = STATE_HOME / "state.json"
    try:
        data = json.loads(read_text(state_file))
    except (OSError, json.JSONDecodeError):
        raise ValueError("找不到管线状态文件")
    for item in data.get("errors", []):
        if (
            not item.get("resolved_at")
            and str(item.get("at", "")) == at
            and str(item.get("message", "")) == message
        ):
            item["resolved_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            item["resolved_by"] = "dashboard"
            atomic_write(state_file, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            log_operation({"op": "resolve-error", "at": at, "message": message[:120]})
            return {"ok": True}
    raise ValueError("这条错误已经处理过了，刷新看看")


INTENT_ACTIONS = {"draft": "起草"}
INTENT_QUEUE_HEADER = """---
kind: ai-task-queue
---

# AI 任务队列

工作台排入的 AI 协作请求。用 Claude Code（/ray）处理：完成一条勾掉一条，并把产物链接补在行尾。此文件由工作台追加，人工可随时编辑。

## 待处理
"""


def pending_intents(limit: int = 20) -> list[str]:
    text = try_read(INTENT_QUEUE_FILE)
    if not text:
        return []
    items = [
        line[6:].strip()
        for line in text.splitlines()
        if line.startswith("- [ ] ")
    ]
    return items[-limit:]


def queue_intent(rel_path: str, action: str) -> dict[str, object]:
    label = INTENT_ACTIONS.get(action)
    if not label:
        raise ValueError("不支持这类 AI 请求")
    path = vault_note_path(rel_path)
    rel = relative(path)
    if not rel.startswith(area_prefix("writing_tasks_dir")):
        raise ValueError("目前只支持给写作任务排队起草")
    marker = f"[[{rel[:-3]}]]"
    text = try_read(INTENT_QUEUE_FILE) or ""
    for line in text.splitlines():
        if line.startswith("- [ ] ") and marker in line and label in line:
            raise ValueError("这篇已经在队列里等着了")
    if not text.strip():
        text = INTENT_QUEUE_HEADER
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    entry = f"- [ ] {stamp} · {label} · {marker}"
    atomic_write(INTENT_QUEUE_FILE, text.rstrip() + "\n" + entry + "\n")
    log_operation({"op": "intent", "path": rel, "action": action})
    return {"ok": True, "queued": entry, "queue_path": relative(INTENT_QUEUE_FILE)}


def pipeline_status() -> dict[str, object]:
    runtime = load_runtime_state()
    errors = [
        {
            "at": str(item.get("at", "")),
            "message": str(item.get("message", "")),
        }
        for item in runtime.get("errors", [])
        if not item.get("resolved_at")
    ]
    try:
        last_activity = (
            datetime.fromtimestamp((STATE_HOME / "state.json").stat().st_mtime)
            .astimezone()
            .isoformat(timespec="minutes")
        )
    except OSError:
        last_activity = ""
    tail: list[str] = []
    try:
        tail = (
            (STATE_HOME / "logs" / PIPELINE_LOG_FILE_NAME)
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()[-40:]
        )
    except OSError:
        pass
    return {
        "ok": True,
        "last_activity": last_activity,
        "pending_sources": len(runtime.get("pending_sources", [])),
        "errors": errors[-20:],
        "log_tail": tail,
        "manual_run_running": manual_run_running(),
        "manual_run_started_at": str(_MANUAL_RUN["started_at"]),
        "intents": pending_intents(),
        "intent_queue_path": relative(INTENT_QUEUE_FILE) if INTENT_QUEUE_FILE.exists() else "",
    }


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
            self.send_json(
                {"ok": True, "vault": VAULT.name, "review_enabled": bool(ACTION_LABELS)}
            )
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
        if parsed.path == "/api/note":
            params = parse_qs(parsed.query)
            try:
                self.send_json(
                    note_payload(
                        params.get("path", [""])[0], params.get("link", [""])[0]
                    )
                )
            except ValueError as exc:
                self.error_json(str(exc), HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/pipeline":
            self.send_json(pipeline_status())
            return
        if parsed.path == "/api/asset":
            params = parse_qs(parsed.query)
            try:
                asset = find_asset(
                    params.get("path", [""])[0], params.get("link", [""])[0]
                )
            except ValueError as exc:
                self.error_json(str(exc), HTTPStatus.NOT_FOUND)
                return
            self.send_bytes(asset.read_bytes(), ASSET_TYPES[asset.suffix.lower()])
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
        if length <= 0 or length > 1_048_576:
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
            elif parsed.path == "/api/note/transition":
                expected = payload.get("expected_mtime_ns")
                if expected is not None:
                    try:
                        expected = int(expected)
                    except (TypeError, ValueError):
                        raise ValueError("请求格式不正确")
                self.send_json(
                    apply_transition(
                        str(payload.get("path", "")), str(payload.get("to", "")), expected
                    )
                )
            elif parsed.path == "/api/promote":
                self.send_json(
                    promote_candidate(
                        str(payload.get("path", "")), str(payload.get("angle", ""))
                    )
                )
            elif parsed.path == "/api/note/save":
                self.send_json(
                    save_note_body(
                        str(payload.get("path", "")),
                        str(payload.get("body", "")),
                        payload.get("expected_mtime_ns"),
                    )
                )
            elif parsed.path == "/api/pipeline/resolve-error":
                self.send_json(
                    resolve_pipeline_error(
                        str(payload.get("at", "")), str(payload.get("message", ""))
                    )
                )
            elif parsed.path == "/api/pipeline/run":
                self.send_json(start_manual_run())
            elif parsed.path == "/api/intent":
                self.send_json(
                    queue_intent(
                        str(payload.get("path", "")), str(payload.get("action", ""))
                    )
                )
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
