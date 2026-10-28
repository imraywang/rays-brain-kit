#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from topic_map_candidates import (
    refresh_topic_map_candidates,
    theme_map_coverage_issues,
)
from map_digest import refresh_weekly_map_digest

VAULT = Path(os.environ.get("RAYS_BRAIN", str(HERE.parents[2]))).resolve()
SYSTEM_DIR = HERE
REVIEW_PROTOCOL_FILE = HERE.parent / "review_protocol.json"
# 运行时状态放在 vault 外，与 knowledge_ingest.py 保持一致
STATE_HOME = Path(os.environ.get("RAYS_BRAIN_STATE", str(Path.home() / ".local/state/rays-brain")))
STATE_FILE = STATE_HOME / "state.json"
SNAPSHOT_DIR = STATE_HOME / "health-snapshots"
CONTENT_SNAPSHOT_SUCCESS_FILE = STATE_HOME / "content-snapshot-success.json"
CONTENT_SNAPSHOT_LOCK_DIR = STATE_HOME / "content-snapshot.lock"
CONFIG_FILE = SYSTEM_DIR / "config.json"
DAILY_DIR = VAULT / "00-入口/20-日报/知识库健康日报"
WEEKLY_DIR = VAULT / "00-入口/20-日报/知识库周报"
REPORT_ARCHIVE_DIR = VAULT / "90-归档/40-系统报告"
REVIEW_DIR = VAULT / "10-创作/10-灵感/10-待评估/剪藏复核"
LEGACY_REVIEW_DIR = VAULT / "10-创作/10-灵感/10-待评估/存量知识复核"
CANDIDATE_DIR = VAULT / "10-创作/10-灵感/20-候选选题"
TOPIC_RESERVE_DIR = VAULT / "10-创作/10-灵感/90-选题储备"
WRITING_TASK_DIR = VAULT / "10-创作/20-写作任务"
ARTICLE_DRAFT_DIR = VAULT / "10-创作/30-文章草稿"
ORAL_DRAFT_DIR = VAULT / "10-创作/25-口播草稿"
CONTENT_FEEDBACK_DIR = VAULT / "40-发布/00-内容反馈"
X_DIR = VAULT / "30-资料/30-X书签"
WEB_DIR = VAULT / "30-资料/40-网页剪藏"
KNOWLEDGE_DIR = VAULT / "20-知识"
TOPIC_MAP_CANDIDATE_STATE = STATE_HOME / "topic-map-candidates.json"
TOPIC_MAP_CANDIDATE_NOTE = VAULT / "00-入口/10-工作台/主题地图候选.md"
MAP_DIGEST_STATE = STATE_HOME / "map-digest.json"
MAP_DIGEST_NOTE_REL = Path("00-入口/10-工作台/知识地图整理.md")
MAP_DIGEST_WEEKLY_DIR_REL = Path("00-入口/20-日报/知识地图整理")
LINK_INBOX = VAULT / "30-资料/00-待抓取/链接收件箱.md"
INGEST_LABEL = "com.ray.knowledge-ingest"
AUTOMATION_LABELS = {
    "ingest": ("知识采集", INGEST_LABEL),
    "health_daily": ("每日健康检查", "com.ray.knowledge-health-daily"),
    "health_weekly": ("每周健康检查", "com.ray.knowledge-health-weekly"),
    "content_snapshot": ("内容快照", "com.ray.content-snapshot"),
    "feedback_snapshot": ("反馈复盘快照", "com.ray.feedback-snapshot"),
}
DEFAULT_DAILY_REPORT_KEEP_DAYS = 7
DEFAULT_WEEKLY_REPORT_KEEP_WEEKS = 8
DAILY_REPORT_NAME = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\.md$")
WEEKLY_REPORT_NAME = re.compile(r"^(?P<year>\d{4})-W(?P<week>\d{2})\.md$")

REQUIRED_FILES = [
    "HOME.md",
    "00-入口/10-工作台/知识工作台.md",
    "00-入口/10-工作台/创作工作台.md",
    "50-系统/50-视图/剪藏复核.base",
    "50-系统/40-自动化/review_protocol.json",
    "50-系统/40-自动化/内容快照/content_snapshot.py",
    "20-知识/README.md",
    "20-知识/20-人物与组织/README.md",
    "20-知识/25-产品与模型/README.md",
    "20-知识/70-知识地图/人物与思想索引.md",
    "20-知识/70-知识地图/产品与模型索引.md",
    "50-系统/40-自动化/知识采集/knowledge_ingest.py",
    "50-系统/40-自动化/知识采集/map_digest.py",
    "50-系统/40-自动化/知识采集/topic_map_candidates.py",
    "50-系统/40-自动化/知识采集/config.json",
    "50-系统/40-自动化/知识采集/entity_registry.json",
]


def load_review_protocol() -> tuple[dict[str, str], dict[str, str]]:
    try:
        data = json.loads(REVIEW_PROTOCOL_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"审核协议无法解析：{REVIEW_PROTOCOL_FILE}（{exc}）") from exc
    actions = data.get("actions", [])
    if not isinstance(actions, list) or not actions:
        raise RuntimeError(f"审核协议缺少 actions：{REVIEW_PROTOCOL_FILE}")
    aliases: dict[str, str] = {}
    labels: dict[str, str] = {}
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
    return labels, aliases


REVIEW_ACTION_LABELS, REVIEW_ACTION_ALIASES = load_review_protocol()


def load_topic_attention_days() -> int:
    try:
        data = json.loads(REVIEW_PROTOCOL_FILE.read_text(encoding="utf-8"))
        days = int(data.get("topic_attention", {}).get("candidate_days", 7))
    except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"无法读取候选注意力期限：{REVIEW_PROTOCOL_FILE}（{exc}）") from exc
    if not 1 <= days <= 30:
        raise RuntimeError("候选注意力期限必须在 1–30 天之间")
    return days


TOPIC_ATTENTION_DAYS = load_topic_attention_days()
_REVIEW_ACTION_PATTERN = re.compile(
    r"^- \[[xX]\]\s+("
    + "|".join(
        sorted(
            (re.escape(label) for label in REVIEW_ACTION_ALIASES),
            key=len,
            reverse=True,
        )
    )
    + r")\s*$",
    flags=re.M,
)


def checked_review_action_keys(text: str) -> list[str]:
    return list(
        dict.fromkeys(
            REVIEW_ACTION_ALIASES[match]
            for match in _REVIEW_ACTION_PATTERN.findall(text)
        )
    )


def now_local() -> datetime:
    return datetime.now().astimezone()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def atomic_json(path: Path, data: Any) -> None:
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def positive_int_setting(config: dict[str, Any], key: str, default: int) -> int:
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def report_retention_settings() -> tuple[int, int]:
    config = load_json(CONFIG_FILE, {})
    return (
        positive_int_setting(
            config, "daily_report_keep_days", DEFAULT_DAILY_REPORT_KEEP_DAYS
        ),
        positive_int_setting(
            config, "weekly_report_keep_weeks", DEFAULT_WEEKLY_REPORT_KEEP_WEEKS
        ),
    )


def move_report_to_archive(source: Path, archive_dir: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / source.name
    suffix = 2
    while target.exists():
        target = archive_dir / f"{source.stem}-{suffix}{source.suffix}"
        suffix += 1
    old_target = source.relative_to(VAULT).with_suffix("").as_posix()
    source.replace(target)
    new_target = target.relative_to(VAULT).with_suffix("").as_posix()
    rewrite_wikilink_target(old_target, new_target)
    return target


def rewrite_wikilink_target(old_target: str, new_target: str) -> int:
    """报告移动后同步更新精确指向它的 Obsidian 链接。"""
    pattern = re.compile(
        rf"(?<=\[\[){re.escape(old_target)}(?=(?:[|#]|\]\]))"
    )
    updated_files = 0
    for path in VAULT.rglob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        updated = pattern.sub(new_target, text)
        if updated == text:
            continue
        path.write_text(updated, encoding="utf-8")
        updated_files += 1
    return updated_files


def archive_report_history(
    reference_date: date | None = None,
    daily_keep_days: int | None = None,
    weekly_keep_weeks: int | None = None,
) -> dict[str, Any]:
    configured_daily, configured_weekly = report_retention_settings()
    daily_keep_days = daily_keep_days or configured_daily
    weekly_keep_weeks = weekly_keep_weeks or configured_weekly
    today = reference_date or now_local().date()
    daily_first_kept = today - timedelta(days=daily_keep_days - 1)
    current_week_start = today - timedelta(days=today.weekday())
    weekly_first_kept = current_week_start - timedelta(weeks=weekly_keep_weeks - 1)

    archived_daily: list[str] = []
    for path in sorted(DAILY_DIR.glob("*.md")):
        match = DAILY_REPORT_NAME.fullmatch(path.name)
        if not match:
            continue
        try:
            report_date = date.fromisoformat(match.group("date"))
        except ValueError:
            continue
        if report_date >= daily_first_kept:
            continue
        target = move_report_to_archive(
            path,
            REPORT_ARCHIVE_DIR / "知识库健康日报" / report_date.strftime("%Y-%m"),
        )
        archived_daily.append(target.relative_to(VAULT).as_posix())

    archived_weekly: list[str] = []
    for path in sorted(WEEKLY_DIR.glob("*.md")):
        match = WEEKLY_REPORT_NAME.fullmatch(path.name)
        if not match:
            continue
        try:
            week_start = date.fromisocalendar(
                int(match.group("year")), int(match.group("week")), 1
            )
        except ValueError:
            continue
        if week_start >= weekly_first_kept:
            continue
        target = move_report_to_archive(
            path,
            REPORT_ARCHIVE_DIR / "知识库周报" / match.group("year"),
        )
        archived_weekly.append(target.relative_to(VAULT).as_posix())

    archived_map_digest_weekly: list[str] = []
    map_digest_weekly_dir = VAULT / MAP_DIGEST_WEEKLY_DIR_REL
    for path in sorted(map_digest_weekly_dir.glob("*.md")):
        match = WEEKLY_REPORT_NAME.fullmatch(path.name)
        if not match:
            continue
        try:
            week_start = date.fromisocalendar(
                int(match.group("year")), int(match.group("week")), 1
            )
        except ValueError:
            continue
        if week_start >= weekly_first_kept:
            continue
        target = move_report_to_archive(
            path,
            REPORT_ARCHIVE_DIR / "知识地图整理" / match.group("year"),
        )
        archived_map_digest_weekly.append(target.relative_to(VAULT).as_posix())

    return {
        "daily_keep_days": daily_keep_days,
        "weekly_keep_weeks": weekly_keep_weeks,
        "daily_archived": archived_daily,
        "weekly_archived": archived_weekly,
        "map_digest_weekly_archived": archived_map_digest_weekly,
    }


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


def parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def safe_age_hours(value: str, now: datetime) -> float | None:
    parsed = parse_dt(value)
    if not parsed:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return max(0.0, (now - parsed.astimezone(now.tzinfo)).total_seconds() / 3600)


TERMINAL_STATUSES = {
    "archived", "cancelled", "canceled", "closed", "complete", "completed",
    "done", "published", "rejected", "已关闭", "已取消", "已完成", "已发布", "已归档",
}
CANDIDATE_INACTIVE_STATUSES = TERMINAL_STATUSES | {
    "expired", "parked", "paused", "reserve", "reserved",
    "储备", "已过期", "失效", "暂缓",
}
FEEDBACK_DONE_STATUSES = TERMINAL_STATUSES | {
    "not-needed", "not_needed", "reviewed", "无需复盘", "复盘完成", "已复盘",
}
PROCESSED_REVIEW_STATUSES = {
    "processed", "writing", "cleanup", "knowledge", "paused",
    "可写作", "已处理", "已沉淀", "已进入候选", "已进入储备",
    "已双向分流", "待清理", "暂缓",
}


def normalized_status(meta: dict[str, str]) -> str:
    return meta.get("status", "").strip().strip('"').lower()


def markdown_notes(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        path for path in directory.rglob("*.md")
        if path.name.lower() not in {"readme.md", "index.md"}
    )


def dated_value(meta: dict[str, str], keys: tuple[str, ...], now: datetime) -> datetime | None:
    for key in keys:
        parsed = parse_dt(meta.get(key, ""))
        if not parsed:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
        return parsed.astimezone(now.tzinfo)
    return None


def note_age_days(path: Path, meta: dict[str, str], now: datetime) -> float:
    progressed = dated_value(
        meta,
        (
            "last_progress_at", "updated_at", "promoted_at", "created_at",
            "reviewed_at", "created",
        ),
        now,
    )
    if not progressed:
        progressed = datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)
    return max(0.0, (now - progressed).total_seconds() / 86400)


def deadline_value(meta: dict[str, str], keys: tuple[str, ...], now: datetime) -> datetime | None:
    for key in keys:
        raw = meta.get(key, "").strip().strip('"')
        if not raw:
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            parsed_date = date.fromisoformat(raw)
            return datetime.combine(
                parsed_date,
                datetime.max.time().replace(microsecond=0),
                tzinfo=now.tzinfo,
            )
        parsed = parse_dt(raw)
        if not parsed:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
        return parsed.astimezone(now.tzinfo)
    return None


def knowledge_link_issues() -> list[dict[str, Any]]:
    notes: list[tuple[Path, str]] = []
    known_titles: set[str] = set()
    for path in KNOWLEDGE_DIR.rglob("*.md"):
        rel_path = path.relative_to(VAULT).as_posix()
        if path.name == "README.md" or rel_path.startswith("20-知识/90-元数据/"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if normalized_status(parse_frontmatter(text)) in {"archived", "已归档"}:
            continue
        notes.append((path, text))
        known_titles.add(unicodedata.normalize("NFC", path.stem))

    issues: list[dict[str, Any]] = []
    for path, text in notes:
        own_title = unicodedata.normalize("NFC", path.stem)
        linked_titles: set[str] = set()
        searchable = re.sub(r"```.*?```", "", text, flags=re.S)
        searchable = re.sub(r"`[^`\n]*`", "", searchable)
        for raw in re.findall(r"!?\[\[([^\]]+)\]\]", searchable):
            target = raw.split("|", 1)[0].split("#", 1)[0].strip()
            if not target:
                continue
            title = unicodedata.normalize("NFC", Path(target).stem)
            if title in known_titles and title != own_title:
                linked_titles.add(title)
        if len(linked_titles) < 2:
            issues.append({
                "file": path.relative_to(VAULT).as_posix(),
                "internal_links": len(linked_titles),
            })
    return issues


def entity_identity_issues() -> list[dict[str, str]]:
    roots = {
        "20-人物与组织": {"人物", "组织", "公司"},
        "25-产品与模型": {"产品", "模型"},
    }
    records: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []

    def key(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold()
        return re.sub(r"[\s·•._\-—–，,。:：'\"()（）/@]+", "", value)

    def family(entity_type: str) -> str:
        if entity_type == "人物":
            return "person"
        if entity_type in {"组织", "公司"}:
            return "organization"
        if entity_type in {"产品", "模型"}:
            return "offering"
        return "invalid"

    for folder, allowed_types in roots.items():
        root = KNOWLEDGE_DIR / folder
        if not root.exists():
            continue
        for path in root.glob("*.md"):
            if path.name == "README.md":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            meta = parse_frontmatter(text)
            rel_path = path.relative_to(VAULT).as_posix()
            entity_type = meta.get("entity_type", "").strip().strip('"')
            entity_id = meta.get("entity_id", "").strip().strip('"')
            if entity_type not in allowed_types:
                issues.append(
                    {
                        "file": rel_path,
                        "issue": f"类型 {entity_type or '缺失'} 与目录 {folder} 不一致",
                    }
                )
            if not entity_id:
                issues.append({"file": rel_path, "issue": "缺少稳定身份"})
            try:
                aliases = json.loads(meta.get("aliases", "[]"))
            except (TypeError, json.JSONDecodeError):
                aliases = []
            if not isinstance(aliases, list):
                aliases = []
            records.append(
                {
                    "file": rel_path,
                    "name": path.stem,
                    "entity_id": entity_id,
                    "family": family(entity_type),
                    "keys": {
                        key(str(value))
                        for value in [path.stem, *aliases]
                        if key(str(value))
                    },
                }
            )

    ids: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    names: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record["entity_id"]:
            ids[record["entity_id"]].append(record)
        for name_key in record["keys"]:
            names[(record["family"], name_key)].append(record)
    for entity_id, matches in ids.items():
        if len(matches) > 1:
            issues.append(
                {
                    "file": "；".join(item["file"] for item in matches),
                    "issue": f"稳定身份重复：{entity_id}",
                }
            )
    seen_groups: set[tuple[str, ...]] = set()
    for matches in names.values():
        files = tuple(sorted({item["file"] for item in matches}))
        if len(files) < 2 or files in seen_groups:
            continue
        seen_groups.add(files)
        issues.append(
            {
                "file": "；".join(files),
                "issue": "同类对象的名称或别名冲突",
            }
        )
    return issues


def frontmatter_block(text: str) -> str:
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---\n", 4)
    return text[4:end] if end >= 0 else ""


def declared_source_paths(text: str) -> list[str]:
    """Return local primary-source paths declared in frontmatter."""
    block = frontmatter_block(text)
    prefix = r"(?:10-创作|30-资料|40-发布)/"
    meta = parse_frontmatter(text)
    paths: list[str] = []
    source_file = meta.get("source_file", "").strip()
    if re.match(rf"^{prefix}", source_file):
        paths.append(source_file)
    remaining_lines = [
        line for line in block.splitlines() if not line.startswith("source_file:")
    ]
    remaining = "\n".join(remaining_lines)
    paths += re.findall(rf'"({prefix}[^"]+)"', remaining)
    paths += re.findall(rf"'({prefix}[^']+)'", remaining)
    for line in remaining_lines:
        if '"' in line or "'" in line:
            continue
        paths += re.findall(rf"({prefix}[^,\]]+)", line)
    return sorted({
        value.strip().strip('"').strip("'")
        for value in paths
        if value.strip()
    })


def knowledge_provenance_issues() -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for path in KNOWLEDGE_DIR.rglob("*.md"):
        rel_path = path.relative_to(VAULT).as_posix()
        if path.name == "README.md" or rel_path.startswith("20-知识/90-元数据/"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        if normalized_status(meta) in {"archived", "已归档"}:
            continue
        if meta.get("kind", "").strip().strip('"') in {"map", "guide"}:
            continue
        source_paths = declared_source_paths(text)
        has_url = bool(meta.get("source_url", "").strip().strip('"'))
        if not source_paths and not has_url:
            issues.append({"file": rel_path, "issue": "没有可追溯的原始资料"})
            continue
        for source in source_paths:
            if not (VAULT / source).exists():
                issues.append({
                    "file": rel_path,
                    "issue": f"原始资料不存在：{source}",
                })
    return issues


def review_source_issues() -> list[dict[str, Any]]:
    """待审核卡的来源文件必须存在；被移走时按 corpus_source 或 source_url 给出可找回的路径。"""
    issues: list[dict[str, Any]] = []
    if not REVIEW_DIR.exists():
        return issues
    corpus_dir = VAULT / "30-资料/70-人物语料"
    web_dir = VAULT / "30-资料/40-网页剪藏"
    url_index: dict[str, str] | None = None

    def lookup(url: str) -> str:
        nonlocal url_index
        if url_index is None:
            url_index = {}
            candidates = list(web_dir.glob("*.md")) if web_dir.exists() else []
            if corpus_dir.exists():
                candidates.extend(corpus_dir.rglob("*.md"))
            for candidate in sorted(candidates):
                candidate_meta = parse_frontmatter(
                    candidate.read_text(encoding="utf-8", errors="replace")
                )
                candidate_url = candidate_meta.get("source_url", "").strip().strip('"')
                if candidate_url:
                    url_index.setdefault(candidate_url, candidate.relative_to(VAULT).as_posix())
        return url_index.get(url, "")

    for path in sorted(REVIEW_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        if meta.get("kind", "").strip().strip('"') != "capture-review":
            continue
        if meta.get("status", "").strip().strip('"') != "待审核":
            continue
        source_file = meta.get("source_file", "").strip().strip('"')
        if source_file and (VAULT / source_file).is_file():
            continue
        corpus = meta.get("corpus_source", "").strip().strip('"')
        suggested = corpus if corpus and (VAULT / corpus).is_file() else ""
        if not suggested:
            suggested = lookup(meta.get("source_url", "").strip().strip('"'))
        issues.append({
            "file": path.relative_to(VAULT).as_posix(),
            "issue": f"来源文件不存在：{source_file or '未声明'}",
            "suggested_source": suggested,
        })
    return issues


def knowledge_backfill_issues() -> list[dict[str, Any]]:
    active_knowledge = [
        (path, path.read_text(encoding="utf-8", errors="replace"))
        for path in KNOWLEDGE_DIR.rglob("*.md")
        if path.name != "README.md" and "90-元数据" not in path.parts
    ]
    issues: list[dict[str, Any]] = []
    published_dir = VAULT / "40-发布"
    if not published_dir.exists():
        return issues
    for path in published_dir.rglob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        if meta.get("knowledge_backfill_status", "").strip().strip('"') != "complete":
            continue
        published_rel = path.relative_to(VAULT).as_posix()
        target = published_rel[:-3]
        linked = any(
            f"[[{target}" in knowledge_text
            or f'source_file: "{published_rel}"' in knowledge_text
            for _, knowledge_text in active_knowledge
        )
        if not linked:
            issues.append({
                "file": published_rel,
                "issue": "标记为已回写，但没有长期知识引用这篇发布稿",
            })
    return issues


def writing_knowledge_handoff_issues() -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for task in markdown_notes(WRITING_TASK_DIR):
        task_text = task.read_text(encoding="utf-8", errors="replace")
        task_meta = parse_frontmatter(task_text)
        if normalized_status(task_meta) in TERMINAL_STATUSES:
            continue
        candidate_raw = task_meta.get("candidate", "")
        candidate_match = re.search(r"\[\[([^\]|#]+)", candidate_raw)
        if not candidate_match:
            continue
        candidate_path = VAULT / (candidate_match.group(1) + ".md")
        if not candidate_path.exists():
            continue
        candidate_text = candidate_path.read_text(encoding="utf-8", errors="replace")
        callable_section = re.search(
            r"^## 可调用知识\s*\n(.*?)(?=^## |\Z)",
            candidate_text,
            flags=re.M | re.S,
        )
        if not callable_section:
            continue
        expected = {
            Path(raw.split("|", 1)[0].split("#", 1)[0].strip()).stem
            for raw in re.findall(r"\[\[([^\]]+)\]\]", callable_section.group(1))
        }
        missing = sorted(
            title for title in expected
            if f"[[{title}" not in task_text and f"/{title}]]" not in task_text
        )
        if missing:
            issues.append({
                "file": task.relative_to(VAULT).as_posix(),
                "issue": "写作任务没有继承候选知识",
                "missing": missing,
            })
    return issues


def topic_knowledge_reference_issues() -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for root in (CANDIDATE_DIR, TOPIC_RESERVE_DIR):
        for path in markdown_notes(root):
            meta = parse_frontmatter(
                path.read_text(encoding="utf-8", errors="replace")
            )
            if meta.get("kind") != "topic-candidate":
                continue
            knowledge_status = meta.get("knowledge_status", "").strip()
            if knowledge_status != "extracted":
                continue
            knowledge_files: list[str] = []
            raw_files = meta.get("knowledge_files_json", "")
            if raw_files:
                try:
                    parsed_files = json.loads(raw_files)
                except json.JSONDecodeError:
                    parsed_files = []
                if isinstance(parsed_files, list):
                    knowledge_files.extend(
                        str(item).strip() for item in parsed_files if str(item).strip()
                    )
            legacy_file = meta.get("knowledge_file", "").strip().strip('"')
            if legacy_file and legacy_file not in knowledge_files:
                knowledge_files.insert(0, legacy_file)
            if not knowledge_files:
                issues.append({
                    "file": path.relative_to(VAULT).as_posix(),
                    "issue": "标记为已沉淀，但没有 knowledge_file",
                })
                continue
            for raw_file in knowledge_files:
                knowledge_file = raw_file
                wiki_match = re.fullmatch(
                    r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]", knowledge_file
                )
                if wiki_match:
                    knowledge_file = wiki_match.group(1)
                if knowledge_file and not Path(knowledge_file).suffix:
                    knowledge_file += ".md"
                if knowledge_file.startswith("90-归档/90-待清理/"):
                    issues.append({
                        "file": path.relative_to(VAULT).as_posix(),
                        "issue": f"knowledge_file 指向待清理区：{knowledge_file}",
                    })
                    continue
                knowledge_path = VAULT / knowledge_file
                try:
                    in_knowledge = (
                        KNOWLEDGE_DIR.resolve() in knowledge_path.resolve().parents
                    )
                except OSError:
                    in_knowledge = False
                if not knowledge_path.is_file():
                    issues.append({
                        "file": path.relative_to(VAULT).as_posix(),
                        "issue": f"knowledge_file 不存在：{knowledge_file}",
                    })
                elif (
                    not in_knowledge
                    or knowledge_path.name == "README.md"
                    or "90-元数据" in knowledge_path.parts
                ):
                    issues.append({
                        "file": path.relative_to(VAULT).as_posix(),
                        "issue": f"knowledge_file 不在活跃长期知识中：{knowledge_file}",
                    })
                else:
                    knowledge_meta = parse_frontmatter(
                        knowledge_path.read_text(encoding="utf-8", errors="replace")
                    )
                    knowledge_status = normalized_status(knowledge_meta)
                    if knowledge_status in TERMINAL_STATUSES or knowledge_status in {
                        "cleanup",
                        "deleted",
                        "deprecated",
                        "inactive",
                        "superseded",
                        "待清理",
                        "已废弃",
                    }:
                        issues.append({
                            "file": path.relative_to(VAULT).as_posix(),
                            "issue": f"knowledge_file 指向非活跃知识：{knowledge_file}",
                        })
                    elif knowledge_meta.get("kind") not in {
                        "concept",
                        "entity",
                        "question",
                        "viewpoint",
                        "case",
                        "playbook",
                        "map",
                        "guide",
                    }:
                        issues.append({
                            "file": path.relative_to(VAULT).as_posix(),
                            "issue": f"knowledge_file 不是规范长期知识：{knowledge_file}",
                        })
    return issues


def knowledge_map_coverage_issues() -> list[dict[str, str]]:
    return theme_map_coverage_issues(VAULT)


# 正式稿的两种形态：图文走 article-published，口播视频走 video-published。
# 字段名的真源在 50-系统/20-流程/个人知识库工作流「最小属性」那张表里。
PUBLISHED_KINDS = ("article-published", "video-published")
VIDEO_PLATFORM_LABELS = {
    "shipinhao": "视频号",
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "youtube": "YouTube",
    "bilibili": "B 站",
}
# 填了创作者后台地址，这篇就永远显得既已发布又信息齐全。
BACKEND_URL_MARKS = {
    "shipinhao": "channels.weixin.qq.com/platform",
    "douyin": "creator.douyin.com",
    "xiaohongshu": "creator.xiaohongshu.com",
    "youtube": "studio.youtube.com",
    "bilibili": "member.bilibili.com",
}


def published_metadata_issues() -> list[dict[str, Any]]:
    """平台名称必须确认；链接和发布时间可选，但已填写的资料必须合法。"""
    issues: list[dict[str, Any]] = []
    published_dir = VAULT / "40-发布"
    if not published_dir.exists():
        return issues
    for path in published_dir.rglob("*.md"):
        if CONTENT_FEEDBACK_DIR in path.parents:
            continue
        meta = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        if meta.get("kind") not in PUBLISHED_KINDS:
            continue
        problems: list[str] = []
        published_status_keys = (
            "x_article_status", "wechat_status", "shipinhao_status",
            "douyin_status", "xiaohongshu_status", "youtube_status", "bilibili_status",
        )
        if meta.get("status", "").strip() == "published" and not any(
            meta.get(key, "").strip() == "published" for key in published_status_keys
        ):
            problems.append("尚未确认具体发布平台")
        # 发布事实由各平台 status 决定。链接和时间是可选的追溯资料；留空不影响
        # 发布状态，但一旦填写成草稿编辑器或创作者后台地址，仍要指出。
        if meta.get("x_article_status") == "published":
            x_url = meta.get("x_article_url", "").strip()
            if x_url and "compose/articles/edit" in x_url:
                # 草稿编辑器地址填在这里，这篇就永远显得既已发布又信息齐全。
                problems.append("X Article 链接当前填的是草稿编辑器地址")
        for prefix, label in VIDEO_PLATFORM_LABELS.items():
            if meta.get(f"{prefix}_status") != "published":
                continue
            url = meta.get(f"{prefix}_url", "").strip()
            if url and BACKEND_URL_MARKS.get(prefix, "") and BACKEND_URL_MARKS[prefix] in url:
                problems.append(f"{label}链接当前填的是创作者后台地址")
        if problems:
            issues.append({
                "file": path.relative_to(VAULT).as_posix(),
                "issues": problems,
            })
    return issues


def published_active_residue_issues() -> list[dict[str, str]]:
    """找出已经发布、却仍占在三个活跃生产目录里的文件。"""
    active_roots = (WRITING_TASK_DIR, ARTICLE_DRAFT_DIR, ORAL_DRAFT_DIR)
    found: dict[str, dict[str, str]] = {}

    def active_file_from_link(value: str) -> Path | None:
        match = re.match(r"\s*\[\[([^\]|#]+)", value or "")
        if not match:
            return None
        raw = match.group(1).strip()
        path = VAULT / (raw if raw.endswith(".md") else raw + ".md")
        if path.is_file() and any(root in path.parents for root in active_roots):
            return path
        return None

    # 正式稿反向指向活跃稿件，说明发布登记后没有完成移动。即使旧稿状态没来得及回写，
    # 这条关系本身也足够确定，不能让它继续被工作台隐藏。
    published_root = VAULT / "40-发布"
    if published_root.exists():
        for formal in published_root.rglob("*.md"):
            if CONTENT_FEEDBACK_DIR in formal.parents:
                continue
            meta = parse_frontmatter(formal.read_text(encoding="utf-8", errors="replace"))
            if meta.get("kind") not in PUBLISHED_KINDS:
                continue
            for key in ("draft_source", "oral_script", "content_pack"):
                path = active_file_from_link(meta.get(key, ""))
                if path is None:
                    continue
                relative_path = path.relative_to(VAULT).as_posix()
                found[relative_path] = {
                    "file": relative_path,
                    "published_file": formal.relative_to(VAULT).as_posix(),
                    "issue": "正式稿仍指向活跃生产目录",
                }

    # 再从活跃目录正向检查，覆盖更早的历史稿：它可能已经写了 published_file，
    # 但正式稿还没补 draft_source/content_pack。
    for root in active_roots:
        for path in markdown_notes(root):
            meta = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            if normalized_status(meta) not in {"published", "已发布"} and not meta.get(
                "published_file", ""
            ).strip():
                continue
            relative_path = path.relative_to(VAULT).as_posix()
            found.setdefault(
                relative_path,
                {
                    "file": relative_path,
                    "published_file": meta.get("published_file", ""),
                    "issue": "已发布文件仍留在活跃生产目录",
                },
            )
    return [found[key] for key in sorted(found)]


WIKILINK_TARGET = re.compile(r"\[\[([^\]|#]+)")
VIDEO_STATUS_VALUES = {"published", "pending", "reviewing", "restricted", "private", "removed", "not-needed"}
ACCOUNT_REQUIRED_STATUSES = {"published", "restricted", "private", "removed"}
LOCAL_PATH_FIELDS = (
    "video_project", "video_file", "video_file_douyin", "video_cover",
    "video_cover_3x4", "video_cover_16x9",
)
STALE_DRAFT_DAYS = 7


def link_targets(value: str) -> list[str]:
    return [match.strip() for match in WIKILINK_TARGET.findall(value or "")]


def note_by_target(target: str) -> Path | None:
    path = VAULT / (target if target.endswith(".md") else target + ".md")
    return path if path.is_file() else None


def published_notes() -> list[tuple[Path, dict[str, str]]]:
    root = VAULT / "40-发布"
    notes: list[tuple[Path, dict[str, str]]] = []
    if not root.exists():
        return notes
    for path in sorted(root.rglob("*.md")):
        if CONTENT_FEEDBACK_DIR in path.parents:
            continue
        meta = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        if meta.get("kind") in PUBLISHED_KINDS:
            notes.append((path, meta))
    return notes


def publication_closeout_issues(now: datetime) -> dict[str, list[dict[str, str]]]:
    """发布之后还开着的候选、被同一选题替代的旧稿，以及停在草稿区太久的稿子。"""
    published = published_notes()
    published_rels = {path.relative_to(VAULT).as_posix()[:-3] for path, _ in published}
    candidate_to_published: dict[str, str] = {}
    for path, meta in published:
        for key in ("topic_candidate", "topic_card"):
            for target in link_targets(meta.get(key, "")):
                candidate_to_published.setdefault(target, path.relative_to(VAULT).as_posix())

    open_candidates: list[dict[str, str]] = []
    for target, published_file in sorted(candidate_to_published.items()):
        candidate = note_by_target(target)
        if candidate is None or CANDIDATE_DIR not in candidate.parents:
            continue
        status = normalized_status(
            parse_frontmatter(candidate.read_text(encoding="utf-8", errors="replace"))
        )
        if status in {"candidate", "active", "selected", "writing"}:
            open_candidates.append({
                "file": candidate.relative_to(VAULT).as_posix(),
                "published_file": published_file,
                "issue": f"已发布，候选仍是 {status}",
            })

    sibling_drafts: list[dict[str, str]] = []
    stale_drafts: list[dict[str, str]] = []
    missing_precheck: list[dict[str, str]] = []
    precheck_since = str(health_config().get("knowledge_precheck_since", "")).strip()
    for root in (ARTICLE_DRAFT_DIR, ORAL_DRAFT_DIR):
        for path in markdown_notes(root):
            text = path.read_text(encoding="utf-8", errors="replace")
            meta = parse_frontmatter(text)
            relative_path = path.relative_to(VAULT).as_posix()
            created = meta.get("created_at", "").strip()[:10]
            if root == ORAL_DRAFT_DIR and precheck_since and created >= precheck_since \
                    and not has_knowledge_precheck(text):
                missing_precheck.append({
                    "file": relative_path,
                    "issue": "调研前没做知识库预检（knowledge_search.py）",
                })
            reason = related = ""
            for target in link_targets(meta.get("supersedes", "")):
                # 新稿替代的是已经发布的正式稿时，新稿本身也多半已经发出去
                if target in published_rels or target.startswith("90-归档/"):
                    reason = "替代的旧版已发布或已归档，确认这一版是否也已发布"
                    related = target + ".md"
            for target in link_targets(meta.get("topic_candidate", "")):
                if target in candidate_to_published and not reason:
                    reason = "同一个候选已经发布过另一版"
                    related = candidate_to_published[target]
            if reason:
                sibling_drafts.append({
                    "file": relative_path,
                    "published_file": related,
                    "issue": reason,
                })
                continue
            if meta.get("recording_status", "").strip() == "recorded":
                continue
            age = note_age_days(path, meta, now)
            if age >= STALE_DRAFT_DAYS:
                stale_drafts.append({
                    "file": relative_path,
                    "issue": f"{int(age)} 天没有推进（{normalized_status(meta) or '无状态'}），决定做还是不做",
                })
    return {
        "open_candidates": open_candidates,
        "sibling_drafts": sibling_drafts,
        "stale_drafts": stale_drafts,
        "missing_precheck": missing_precheck,
    }


def has_knowledge_precheck(text: str) -> bool:
    """「知识库预检」小节里要么有知识链接，要么明确写了「无相关」；模板里的说明不算。"""
    match = re.search(r"^###\s*知识库预检.*?$(.*?)(?=^#{1,3}\s|\Z)", text, flags=re.M | re.S)
    if not match:
        return False
    section = match.group(1)
    return "[[20-知识/" in section or "无相关" in section


def health_config() -> dict[str, Any]:
    config = load_json(CONFIG_FILE, {})
    return config if isinstance(config, dict) else {}


def media_roots() -> list[Path]:
    roots = health_config().get("media_roots", [])
    return [Path(os.path.expanduser(str(root))) for root in roots if str(root).strip()]


def resolve_media_path(value: str) -> tuple[str, Path | None]:
    """返回 (ok|missing|unmounted, 实际找到的路径)。只写项目名时在 media_roots 里找。"""
    raw = os.path.expanduser(value.strip().strip('"'))
    path = Path(raw)
    if not path.is_absolute():
        for root in media_roots():
            if (root / raw).exists():
                return "ok", root / raw
        return "missing", None
    if path.exists():
        return "ok", path
    parts = path.parts
    if len(parts) > 2 and parts[1] == "Volumes" and not Path("/", *parts[:3]).exists():
        return "unmounted", None
    # 项目整体搬家后，在其他根目录下找同名目录；项目还在原处说明只是文件没了
    project_index = next(
        (index for index, part in enumerate(parts) if re.match(r"\d{4}-\d{2}-\d{2}-", part)), -1
    )
    if project_index < 0 or Path(*parts[:project_index + 1]).exists():
        return "missing", None
    for root in media_roots():
        if (root / parts[project_index]).exists():
            return "missing", root / parts[project_index]
    return "missing", None


def local_path_issues() -> tuple[list[dict[str, str]], int]:
    """已发布内容里填写的本地路径要能打开；外置盘没接上时只计数，不算错。"""
    issues: list[dict[str, str]] = []
    unmounted = 0
    has_media_dir = (VAULT / "60-素材").is_dir()
    for path, meta in published_notes():
        problems: list[str] = []
        for key in LOCAL_PATH_FIELDS:
            value = meta.get(key, "").strip()
            if not value or value.startswith(("http://", "https://")):
                continue
            status, found = resolve_media_path(value)
            if status == "unmounted":
                unmounted += 1
            elif status == "missing":
                hint = f"，同名项目在 {found}" if found else ""
                problems.append(f"{key} 打不开{hint}")
        cover = meta.get("cover_dir", "")
        if has_media_dir:
            for target in link_targets(cover):
                if not (VAULT / target).exists():
                    problems.append("cover_dir 指向的封面目录不存在")
        if problems:
            issues.append({
                "file": path.relative_to(VAULT).as_posix(),
                "issue": "；".join(problems),
            })
    return issues, unmounted


def platform_field_issues() -> list[dict[str, str]]:
    """视频平台状态用固定取值；config 里配了多个抖音号时，已经发出的必须写明是哪个号。"""
    accounts = {str(item) for item in health_config().get("douyin_accounts", [])}
    issues: list[dict[str, str]] = []
    for path, meta in published_notes():
        if meta.get("kind") != "video-published":
            continue
        problems: list[str] = []
        for prefix in VIDEO_PLATFORM_LABELS:
            value = meta.get(f"{prefix}_status", "").strip()
            if value and value not in VIDEO_STATUS_VALUES:
                problems.append(f"{prefix}_status 取值 {value} 不在约定里")
        # 只有配置了多个抖音号时才要求写明账号；单号的库不需要这个字段
        if accounts and meta.get("douyin_status", "").strip() in ACCOUNT_REQUIRED_STATUSES:
            account = meta.get("douyin_account", "").strip()
            if not account:
                problems.append("抖音没写是哪个号（douyin_account）")
            elif account not in accounts:
                problems.append(f"douyin_account「{account}」不在 config 的 douyin_accounts 里")
        if problems:
            issues.append({
                "file": path.relative_to(VAULT).as_posix(),
                "issue": "；".join(problems),
            })
    return issues


def backfill_pending_issues(now: datetime) -> list[dict[str, str]]:
    """发布满 3 天仍没判断知识回写和剩余角度。只看 backfill_check_since 之后的发布。"""
    since = parse_dt(str(health_config().get("backfill_check_since", "")))
    if since is None:
        return []
    if since.tzinfo is None:
        since = since.replace(tzinfo=now.tzinfo)
    issues: list[dict[str, str]] = []
    for path, meta in published_notes():
        published_at = dated_value(meta, ("publication_confirmed_at", "created_at"), now)
        if published_at is None or published_at < since or now - published_at < timedelta(days=3):
            continue
        pending = [
            label for key, label in (
                ("knowledge_backfill_status", "知识回写"),
                ("remaining_angles_status", "剩余角度"),
            )
            if meta.get(key, "pending").strip() in {"", "pending"}
        ]
        problems = ["、".join(pending) + "还没判断（写 complete 或 not-needed）"] if pending else []
        unanswered = unanswered_backflow(path.read_text(encoding="utf-8", errors="replace"))
        if unanswered:
            problems.append("「回流」没答：" + "、".join(unanswered))
        if problems:
            issues.append({
                "file": path.relative_to(VAULT).as_posix(),
                "issue": "；".join(problems),
            })
    return issues


BACKFLOW_QUESTIONS = ("机制与类比", "带日期的事实", "讲法与反馈")


def unanswered_backflow(text: str) -> list[str]:
    """正式稿「回流」三问：机制进长期知识、带日期的事实进对象时间线、讲法留在本篇。每行写链接或「无」。"""
    match = re.search(r"^##\s*回流\s*$(.*?)(?=^##\s|\Z)", text, flags=re.M | re.S)
    if not match:
        return list(BACKFLOW_QUESTIONS)
    answers: dict[str, str] = {}
    for line in match.group(1).splitlines():
        for question in BACKFLOW_QUESTIONS:
            if line.lstrip("-* ").startswith(question):
                answers[question] = re.split(r"[：:]", line, maxsplit=1)[-1].strip() if re.search(r"[：:]", line) else ""
    return [question for question in BACKFLOW_QUESTIONS if not answers.get(question)]


def has_topic_freshness_anchor(meta: dict[str, str]) -> bool:
    return bool(
        meta.get("source_published_at", "").strip()
        or (
            meta.get("refreshed_at", "").strip()
            and meta.get("refresh_source", "").strip()
        )
    )


def topic_source_is_fresh(meta: dict[str, str], now: datetime) -> bool:
    fresh_until = deadline_value(meta, ("fresh_until",), now)
    return bool(
        has_topic_freshness_anchor(meta)
        and meta.get("freshness_status", "").strip().lower() == "fresh"
        and fresh_until
        and fresh_until >= now
    )


def topic_attention_until(meta: dict[str, str]) -> date | None:
    raw_until = meta.get("attention_until", "").strip().strip('"')
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_until):
        return date.fromisoformat(raw_until)
    entered = parse_dt(
        meta.get("attention_entered_at", "")
        or meta.get("created_at", "")
        or meta.get("last_progress_at", "")
    )
    if not entered:
        return None
    return entered.date() + timedelta(days=TOPIC_ATTENTION_DAYS)


def topic_attention_is_fresh(meta: dict[str, str], now: datetime) -> bool:
    attention_until = topic_attention_until(meta)
    return bool(
        attention_until
        and meta.get("attention_status", "fresh").strip().lower() != "expired"
        and now.date() < attention_until
    )


def collect_creation_metrics(now: datetime) -> dict[str, Any]:
    active_candidates = 0
    continuations = 0
    expired_candidates: list[dict[str, Any]] = []
    source_stale_candidates: list[dict[str, Any]] = []
    source_date_missing_candidates: list[dict[str, Any]] = []
    topic_freshness_missing: list[dict[str, Any]] = []
    review_overdue_candidates: list[dict[str, Any]] = []
    stale_candidates: list[dict[str, Any]] = []
    needs_refresh_candidates: list[dict[str, Any]] = []
    for path in markdown_notes(CANDIDATE_DIR):
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        status = normalized_status(meta)
        if (
            meta.get("kind") == "topic-candidate"
            and status not in TERMINAL_STATUSES
            and (
                not has_topic_freshness_anchor(meta)
                or not meta.get("fresh_until", "").strip()
                or not meta.get("freshness_status", "").strip()
            )
        ):
            topic_freshness_missing.append({
                "file": path.relative_to(VAULT).as_posix(),
                "status": status,
            })
        if status == "partially-published":
            if topic_source_is_fresh(meta, now):
                continuations += 1
            else:
                needs_refresh_candidates.append({
                    "file": path.relative_to(VAULT).as_posix(),
                    "source_published_at": meta.get("source_published_at", ""),
                    "fresh_until": meta.get("fresh_until", ""),
                })
            continue
        if status != "candidate":
            continue
        freshness_status = meta.get("freshness_status", "").strip().lower()
        age_days = note_age_days(path, meta, now)
        item = {
            "file": path.relative_to(VAULT).as_posix(),
            "age_days": round(age_days, 1),
        }
        attention_fresh = topic_attention_is_fresh(meta, now)
        if not attention_fresh:
            expired_candidates.append(item)
        if not has_topic_freshness_anchor(meta) or not meta.get("fresh_until", "").strip():
            source_date_missing_candidates.append(item)
        if attention_fresh:
            active_candidates += 1
            source_expiry = deadline_value(meta, ("fresh_until",), now)
            if freshness_status == "stale" or bool(source_expiry and source_expiry < now):
                source_stale_candidates.append({
                    **item,
                    "source_published_at": meta.get("source_published_at", ""),
                    "fresh_until": meta.get("fresh_until", ""),
                })

    reserve_candidates = 0
    for path in markdown_notes(TOPIC_RESERVE_DIR):
        meta = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        status = normalized_status(meta)
        if status in TERMINAL_STATUSES:
            continue
        reserve_candidates += 1
        if (
            not has_topic_freshness_anchor(meta)
            or not meta.get("fresh_until", "").strip()
            or not meta.get("freshness_status", "").strip()
        ):
            topic_freshness_missing.append({
                "file": path.relative_to(VAULT).as_posix(),
                "status": status,
            })
        fresh_until = deadline_value(meta, ("fresh_until",), now)
        freshness_status = meta.get("freshness_status", "").strip().lower()
        if freshness_status in {"stale", "unknown"} or (
            fresh_until and fresh_until < now
        ):
            needs_refresh_candidates.append({
                "file": path.relative_to(VAULT).as_posix(),
                "source_published_at": meta.get("source_published_at", ""),
                "fresh_until": meta.get("fresh_until", ""),
            })

    active_writing_tasks = sum(
        normalized_status(
            parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        ) not in TERMINAL_STATUSES
        for path in markdown_notes(WRITING_TASK_DIR)
    )

    pending_feedback = 0
    overdue_feedback: list[dict[str, Any]] = []
    feedback_schedule_issues: list[dict[str, str]] = []
    for path in markdown_notes(CONTENT_FEEDBACK_DIR):
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        if meta.get("kind") == "data-snapshot":
            continue  # 后台数据快照是一次性记录，不是待复盘的反馈卡
        status = normalized_status(meta)
        completed = status in FEEDBACK_DONE_STATUSES or any(
            meta.get(key, "").strip()
            for key in ("reviewed_at", "completed_at", "feedback_completed_at")
        )
        if completed:
            continue
        pending_feedback += 1
        due = deadline_value(
            meta,
            ("due_at", "due_date", "feedback_due_at", "review_due_at", "feedback_due"),
            now,
        )
        if not meta.get("platform", "").strip():
            feedback_schedule_issues.append({
                "file": path.relative_to(VAULT).as_posix(),
                "issue": "发布平台待确认",
            })
        elif not due:
            feedback_schedule_issues.append({
                "file": path.relative_to(VAULT).as_posix(),
                "issue": "缺少下一次复盘日期",
            })
        if status in {"overdue", "已逾期", "逾期"} or (due and due < now):
            overdue_feedback.append({
                "file": path.relative_to(VAULT).as_posix(),
                "due_at": due.isoformat(timespec="seconds") if due else "",
            })

    processed_reviews_in_active: list[dict[str, str]] = []
    for path in markdown_notes(REVIEW_DIR):
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        status = normalized_status(meta)
        if meta.get("action_processed_at", "").strip() or status in PROCESSED_REVIEW_STATUSES:
            processed_reviews_in_active.append({
                "file": path.relative_to(VAULT).as_posix(),
                "status": meta.get("status", ""),
            })

    underlinked_knowledge = knowledge_link_issues()
    entity_issues = entity_identity_issues()
    provenance_issues = knowledge_provenance_issues()
    review_source = review_source_issues()
    backfill_issues = knowledge_backfill_issues()
    handoff_issues = writing_knowledge_handoff_issues()
    topic_knowledge_issues = topic_knowledge_reference_issues()
    topic_map_state = refresh_topic_map_candidates(
        VAULT,
        TOPIC_MAP_CANDIDATE_STATE,
        TOPIC_MAP_CANDIDATE_NOTE,
        now=now,
    )
    map_coverage_issues = knowledge_map_coverage_issues()
    publication_metadata = published_metadata_issues()
    publication_residue = published_active_residue_issues()
    closeout = publication_closeout_issues(now)
    path_issues, media_unmounted = local_path_issues()
    platform_issues = platform_field_issues()
    backfill_pending = backfill_pending_issues(now)
    overdue_candidates = {
        item["file"]: item
        for item in expired_candidates + review_overdue_candidates + stale_candidates
    }
    return {
        "active_candidate_count": active_candidates,
        "fresh_candidate_count": active_candidates,
        "continuation_count": continuations,
        "reserve_candidate_count": reserve_candidates,
        "needs_refresh_candidate_count": len(needs_refresh_candidates),
        "needs_refresh_candidates": needs_refresh_candidates,
        "expired_candidate_count": len(expired_candidates),
        "source_stale_candidate_count": len(source_stale_candidates),
        "source_stale_candidates": source_stale_candidates,
        "source_date_missing_candidate_count": len(source_date_missing_candidates),
        "source_date_missing_candidates": source_date_missing_candidates,
        "topic_freshness_missing_count": len(topic_freshness_missing),
        "topic_freshness_missing": topic_freshness_missing,
        "review_overdue_candidate_count": len(review_overdue_candidates),
        "stale_candidate_count": len(stale_candidates),
        "overdue_candidate_count": len(overdue_candidates),
        "expired_candidates": expired_candidates,
        "review_overdue_candidates": review_overdue_candidates,
        "stale_candidates": stale_candidates,
        "overdue_candidates": list(overdue_candidates.values()),
        "active_writing_task_count": active_writing_tasks,
        "pending_feedback_count": pending_feedback,
        "overdue_feedback_count": len(overdue_feedback),
        "overdue_feedback": overdue_feedback,
        "feedback_schedule_issue_count": len(feedback_schedule_issues),
        "feedback_schedule_issues": feedback_schedule_issues,
        "processed_review_in_active_count": len(processed_reviews_in_active),
        "processed_reviews_in_active": processed_reviews_in_active,
        "underlinked_knowledge_count": len(underlinked_knowledge),
        "underlinked_knowledge": underlinked_knowledge,
        "entity_identity_issue_count": len(entity_issues),
        "entity_identity_issues": entity_issues,
        "knowledge_provenance_issue_count": len(provenance_issues),
        "knowledge_provenance_issues": provenance_issues,
        "review_source_issue_count": len(review_source),
        "review_source_issues": review_source,
        "knowledge_backfill_issue_count": len(backfill_issues),
        "knowledge_backfill_issues": backfill_issues,
        "writing_knowledge_handoff_issue_count": len(handoff_issues),
        "writing_knowledge_handoff_issues": handoff_issues,
        "topic_knowledge_reference_issue_count": len(topic_knowledge_issues),
        "topic_knowledge_reference_issues": topic_knowledge_issues,
        "knowledge_map_coverage_issue_count": len(map_coverage_issues),
        "knowledge_map_coverage_issues": map_coverage_issues,
        "topic_map_candidate_count": len(topic_map_state.get("candidates", [])),
        "topic_map_candidates": topic_map_state.get("candidates", []),
        "topic_map_observing_count": len(topic_map_state.get("observing", [])),
        "topic_map_observing": topic_map_state.get("observing", []),
        "topic_map_summary": topic_map_state.get("summary", {}),
        "published_metadata_issue_count": len(publication_metadata),
        "published_metadata_issues": publication_metadata,
        "published_active_residue_count": len(publication_residue),
        "published_active_residue_issues": publication_residue,
        "published_open_candidate_count": len(closeout["open_candidates"]),
        "published_open_candidates": closeout["open_candidates"],
        "sibling_draft_count": len(closeout["sibling_drafts"]),
        "sibling_drafts": closeout["sibling_drafts"],
        "stale_draft_count": len(closeout["stale_drafts"]),
        "stale_drafts": closeout["stale_drafts"],
        "missing_precheck_count": len(closeout["missing_precheck"]),
        "missing_precheck": closeout["missing_precheck"],
        "local_path_issue_count": len(path_issues),
        "local_path_issues": path_issues,
        "media_unmounted_count": media_unmounted,
        "platform_field_issue_count": len(platform_issues),
        "platform_field_issues": platform_issues,
        "backfill_pending_count": len(backfill_pending),
        "backfill_pending_issues": backfill_pending,
    }


# 长期积压：只有比上一份日报变多时才影响状态，否则放进「长期积压」，留给周报处理
CHRONIC_ALERTS = {
    "knowledge_provenance_issue_count": "篇长期知识来源缺失或失效",
    "underlinked_knowledge_count": "篇长期知识少于 2 条有效连接",
    "entity_identity_issue_count": "个对象档案存在重复、错位或身份缺失",
    "knowledge_map_coverage_issue_count": "篇长期知识尚未进入真正的主题地图",
}


def chronic_backlog(metrics: dict[str, Any], previous: dict[str, Any] | None) -> list[str]:
    if not previous:
        return []
    notes: list[str] = []
    for key, text in CHRONIC_ALERTS.items():
        count = metrics.get(key, 0)
        if count and count <= previous.get(key, count):
            notes.append(f"{count} {text}（未增加）")
    return notes


def chronic_unchanged(metrics: dict[str, Any], previous: dict[str, Any] | None, key: str) -> bool:
    return bool(previous) and metrics.get(key, 0) <= previous.get(key, metrics.get(key, 0))


def chronic_delta(metrics: dict[str, Any], previous: dict[str, Any] | None, key: str) -> str:
    if not previous or key not in previous:
        return ""
    return f"（较上次 +{metrics.get(key, 0) - previous[key]}）"


def creation_alerts(
    metrics: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> list[str]:
    reasons: list[str] = []
    if metrics["active_candidate_count"] > 7:
        reasons.append(
            f"今日候选有 {metrics['active_candidate_count']} 条，超过 7 条上限"
        )
    if metrics["stale_candidate_count"]:
        reasons.append(
            f"有 {metrics['stale_candidate_count']} 条候选超过 {TOPIC_ATTENTION_DAYS} 天未推进"
        )
    if metrics.get("source_stale_candidate_count", 0):
        reasons.append(
            f"有 {metrics['source_stale_candidate_count']} 条今日候选的原始资料已过时"
        )
    if metrics.get("source_date_missing_candidate_count", 0):
        reasons.append(
            f"有 {metrics['source_date_missing_candidate_count']} 条今日候选缺少原始资料日期、刷新依据或有效期"
        )
    reserve_or_continuation_missing = max(
        0,
        metrics.get("topic_freshness_missing_count", 0)
        - metrics.get("source_date_missing_candidate_count", 0),
    )
    if reserve_or_continuation_missing:
        reasons.append(
            f"有 {reserve_or_continuation_missing} 条储备或续写选题缺少原始资料日期、刷新依据或有效期"
        )
    if metrics["active_writing_task_count"] > 4:
        reasons.append(
            f"活跃写作任务有 {metrics['active_writing_task_count']} 个，超过 4 个上限"
        )
    if metrics["overdue_feedback_count"]:
        reasons.append(
            f"有 {metrics['overdue_feedback_count']} 条到期反馈尚未复盘"
        )
    if metrics.get("feedback_schedule_issue_count", 0):
        reasons.append(
            f"有 {metrics['feedback_schedule_issue_count']} 条待反馈内容缺少平台或复盘日期"
        )
    if metrics["processed_review_in_active_count"]:
        reasons.append(
            "活跃审核目录混入 "
            f"{metrics['processed_review_in_active_count']} 张已处理卡"
        )
    if metrics.get("knowledge_provenance_issue_count", 0) and not chronic_unchanged(metrics, previous, "knowledge_provenance_issue_count"):
        reasons.append(
            f"有 {metrics['knowledge_provenance_issue_count']} 篇长期知识来源缺失或失效" + chronic_delta(metrics, previous, "knowledge_provenance_issue_count")
        )
    if metrics.get("review_source_issue_count", 0):
        reasons.append(
            f"有 {metrics['review_source_issue_count']} 张待审核卡的来源文件不存在"
        )
    if metrics.get("knowledge_backfill_issue_count", 0):
        reasons.append(
            f"有 {metrics['knowledge_backfill_issue_count']} 篇发布稿回写状态与实际知识不一致"
        )
    if metrics.get("writing_knowledge_handoff_issue_count", 0):
        reasons.append(
            f"有 {metrics['writing_knowledge_handoff_issue_count']} 个写作任务没有继承候选知识"
        )
    if metrics.get("underlinked_knowledge_count", 0) and not chronic_unchanged(metrics, previous, "underlinked_knowledge_count"):
        reasons.append(
            f"有 {metrics['underlinked_knowledge_count']} 篇长期知识少于 2 条有效连接" + chronic_delta(metrics, previous, "underlinked_knowledge_count")
        )
    if metrics.get("entity_identity_issue_count", 0) and not chronic_unchanged(metrics, previous, "entity_identity_issue_count"):
        reasons.append(
            f"有 {metrics['entity_identity_issue_count']} 个对象档案存在重复、错位或身份缺失" + chronic_delta(metrics, previous, "entity_identity_issue_count")
        )
    if metrics.get("topic_knowledge_reference_issue_count", 0):
        reasons.append(
            f"有 {metrics['topic_knowledge_reference_issue_count']} 条候选的知识去向失效"
        )
    if metrics.get("knowledge_map_coverage_issue_count", 0) and not chronic_unchanged(metrics, previous, "knowledge_map_coverage_issue_count"):
        reasons.append(
            f"有 {metrics['knowledge_map_coverage_issue_count']} 篇长期知识尚未进入真正的主题地图" + chronic_delta(metrics, previous, "knowledge_map_coverage_issue_count")
        )
    if metrics.get("published_metadata_issue_count", 0):
        reasons.append(
            f"有 {metrics['published_metadata_issue_count']} 篇已发布内容的平台资料待确认或填写不正确"
        )
    if metrics.get("published_open_candidate_count", 0):
        reasons.append(
            f"有 {metrics['published_open_candidate_count']} 条候选对应的内容已经发布，候选还没关"
        )
    if metrics.get("sibling_draft_count", 0):
        reasons.append(
            f"有 {metrics['sibling_draft_count']} 份草稿的同一选题已经发布过，确认是已发还是作废"
        )
    if metrics.get("stale_draft_count", 0):
        reasons.append(
            f"有 {metrics['stale_draft_count']} 份草稿超过 {STALE_DRAFT_DAYS} 天没有推进"
        )
    if metrics.get("missing_precheck_count", 0):
        reasons.append(
            f"有 {metrics['missing_precheck_count']} 份口播草稿调研前没做知识库预检"
        )
    if metrics.get("local_path_issue_count", 0):
        reasons.append(
            f"有 {metrics['local_path_issue_count']} 篇已发布内容的本地文件路径打不开"
        )
    if metrics.get("platform_field_issue_count", 0):
        reasons.append(
            f"有 {metrics['platform_field_issue_count']} 篇视频的平台状态或抖音账号没写对"
        )
    if metrics.get("backfill_pending_count", 0):
        reasons.append(
            f"有 {metrics['backfill_pending_count']} 篇发布满 3 天还没答完回流三问或判断回写"
        )
    if metrics.get("published_active_residue_count", 0):
        reasons.append(
            f"有 {metrics['published_active_residue_count']} 个已发布文件仍留在草稿或写作任务区"
        )
    return reasons


def launch_status(label: str = INGEST_LABEL) -> dict[str, Any]:
    uid = os.getuid()
    try:
        proc = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{uid}/{label}"],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except Exception as exc:
        return {"loaded": False, "error": str(exc)}
    if proc.returncode != 0:
        return {"loaded": False, "error": proc.stderr.strip() or proc.stdout.strip()}
    output = proc.stdout
    runs = re.search(r"\bruns = (\d+)", output)
    exit_code = re.search(r"\blast exit code = (-?\d+)", output)
    interval = re.search(r"\brun interval = (\d+) seconds", output)
    return {
        "label": label,
        "loaded": True,
        "runs": int(runs.group(1)) if runs else None,
        "last_exit_code": int(exit_code.group(1)) if exit_code else None,
        "interval_seconds": int(interval.group(1)) if interval else None,
    }


def source_health_findings(
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    source_health = state.get("source_health", {})
    degraded_sources = [
        {
            "source": str(key),
            "label": str(value.get("label", key)),
            "consecutive_failures": int(value.get("consecutive_failures", 0) or 0),
            "last_failure_at": str(value.get("last_failure_at", "")),
            "message": str(value.get("message", "")),
        }
        for key, value in source_health.items()
        if isinstance(value, dict) and value.get("status") == "degraded"
    ]
    red: list[str] = []
    yellow: list[str] = []
    for source in degraded_sources:
        reason = (
            f"{source['label']}已连续 {source['consecutive_failures']} 轮读取失败"
        )
        if source["consecutive_failures"] >= 3:
            red.append(reason)
        else:
            yellow.append(reason)
    return degraded_sources, red, yellow


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def content_snapshot_runtime(now: datetime) -> dict[str, Any]:
    result: dict[str, Any] = {
        "snapshot_state": "scheduled",
        "last_success_at": "",
        "runtime_error": "",
    }
    success = load_json(CONTENT_SNAPSHOT_SUCCESS_FILE, {})
    completed_at = parse_dt(str(success.get("completed_at", "")))
    if completed_at:
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=now.tzinfo)
        completed_at = completed_at.astimezone(now.tzinfo)
        result["last_success_at"] = completed_at.isoformat(timespec="seconds")

    if CONTENT_SNAPSHOT_LOCK_DIR.exists():
        try:
            lock_age = max(
                0.0,
                now.timestamp() - CONTENT_SNAPSHOT_LOCK_DIR.stat().st_mtime,
            )
        except OSError:
            lock_age = 0.0
        lock_info = load_json(CONTENT_SNAPSHOT_LOCK_DIR / "owner.json", {})
        try:
            lock_pid = int(lock_info.get("pid", 0))
        except (TypeError, ValueError):
            lock_pid = 0
        if lock_pid and process_is_running(lock_pid):
            result["snapshot_state"] = "running"
            if lock_age > 7200:
                result["runtime_error"] = "内容快照已经连续运行超过 2 小时"
            return result
        elif not lock_pid and lock_age < 60:
            result["snapshot_state"] = "starting"
            return result
        else:
            result["snapshot_state"] = "stale-lock"
            result["runtime_error"] = "内容快照存在失效的运行锁"
            return result

    due_at = now.replace(hour=18, minute=35, second=0, microsecond=0)
    if completed_at and completed_at.date() == now.date():
        result["snapshot_state"] = "ok"
    elif now < due_at:
        result["snapshot_state"] = (
            result["snapshot_state"]
            if result["snapshot_state"] in {"running", "starting"}
            else "scheduled"
        )
    else:
        result["snapshot_state"] = "overdue"
        result["runtime_error"] = (
            "内容快照今天尚未成功"
            if completed_at
            else "内容快照尚无成功记录"
        )
    return result


def icloud_status() -> str:
    try:
        proc = subprocess.run(
            ["/usr/bin/brctl", "status"],
            text=True,
            capture_output=True,
            timeout=20,
        )
    except Exception:
        return "unknown"
    output = proc.stdout + proc.stderr
    if "caught-up" in output and "client:idle" in output:
        return "caught_up"
    if output.strip():
        return "syncing"
    return "unknown"


def resolve_wikilinks() -> tuple[
    int,
    list[dict[str, str]],
    list[dict[str, str]],
]:
    # 隐藏目录（.git、.claude 会话工作副本、.obsidian、.trash）不属于知识库正文
    rel_files: dict[str, Path] = {}
    rel_dirs: set[str] = set()
    for root, dirnames, filenames in os.walk(VAULT, followlinks=True):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        root_path = Path(root)
        for name in dirnames:
            rel_dirs.add((root_path / name).relative_to(VAULT).as_posix())
        for name in filenames:
            path = root_path / name
            rel_files[path.relative_to(VAULT).as_posix()] = path
    cleanup_prefix = "90-归档/90-待清理/"
    stems: dict[str, list[str]] = defaultdict(list)
    names: dict[str, list[str]] = defaultdict(list)
    for rel, path in rel_files.items():
        stems[path.stem].append(rel)
        names[path.name].append(rel)

    def classify(matches: list[str]) -> str:
        if any(not match.startswith(cleanup_prefix) for match in matches):
            return "active"
        if matches:
            return "cleanup"
        return "missing"

    def resolve(source: Path, raw: str) -> str:
        if source.suffix == ".json":
            # JSON 字符串里的 \\ 和 \" 是转义，先还原成笔记里的原文
            raw = raw.replace("\\\\", "\\").replace('\\"', '"')
        # 表格里的别名要写成 [[目标\|别名]]，反斜杠不属于目标
        target = re.split(r"\\?\|", raw, maxsplit=1)[0].split("#", 1)[0].strip()
        # [[^块编号]] 是块引用，X 书签每次重抓都会还原原文里的这种写法
        if not target or "://" in target or target == "wikilinks" or target.startswith("^"):
            return "active"
        suffix = PurePosixPath(target).suffix.lower()
        has_file_suffix = suffix in {".md", ".base", ".json"}
        candidates = [target] if has_file_suffix else [target, target + ".md", target + ".base"]
        exact = [candidate for candidate in candidates if candidate in rel_files]
        if exact:
            return classify(exact)
        folder = target.rstrip("/")
        if folder in rel_dirs:
            return classify([folder + "/"])
        source_rel = source.relative_to(VAULT).parent
        relative_matches = [
            (source_rel / candidate).as_posix()
            for candidate in candidates
            if (source_rel / candidate).as_posix() in rel_files
        ]
        if relative_matches:
            return classify(relative_matches)
        if "/" not in target:
            if suffix in {".md", ".base"}:
                return classify(stems.get(Path(target).stem, []))
            # 图片等附件按完整文件名找，笔记按文件名（不带 .md）找
            return classify(stems.get(target, []) + names.get(target, []))
        return "missing"

    checked = 0
    broken: list[dict[str, str]] = []
    cleanup_only: list[dict[str, str]] = []
    pattern = re.compile(r"!?\[\[([^\]]+)\]\]")
    for rel, path in sorted(rel_files.items()):
        if path.suffix not in {".md", ".base", ".json"}:
            continue
        # 模板里的 [[XX-style.png]]、[[YYYY-MM-DD-…]] 是占位符，套用后才有目标
        if rel.startswith("50-系统/30-模板/") or "_模板" in PurePosixPath(rel).parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        searchable = re.sub(r"```.*?```", "", text, flags=re.S)
        searchable = re.sub(r"`[^`\n]*`", "", searchable)
        for match in pattern.finditer(searchable):
            checked += 1
            resolution = resolve(path, match.group(1))
            if resolution == "missing":
                broken.append({"source": rel, "target": match.group(1)})
            elif resolution == "cleanup" and not rel.startswith("90-归档/"):
                cleanup_only.append({"source": rel, "target": match.group(1)})
    return checked, broken, cleanup_only


def previous_snapshot(today: str) -> dict[str, Any] | None:
    candidates = sorted(path for path in SNAPSHOT_DIR.glob("*.json") if path.stem < today)
    return load_json(candidates[-1], {}) if candidates else None


def recent_snapshots(days: int = 7) -> list[dict[str, Any]]:
    paths = sorted(SNAPSHOT_DIR.glob("*.json"))[-days:]
    return [load_json(path, {}) for path in paths]


def collect_metrics() -> dict[str, Any]:
    now = now_local()
    today = now.date().isoformat()
    state = load_json(STATE_FILE, {})
    captures_today: Counter[str] = Counter()
    failed_articles: list[str] = []
    failed_web: list[str] = []
    capture_files = 0
    for base in (X_DIR, WEB_DIR):
        for path in base.rglob("*.md"):
            capture_files += 1
            text = path.read_text(encoding="utf-8", errors="replace")
            meta = parse_frontmatter(text)
            captured_at = meta.get("created_at", "") or meta.get("created", "")
            if captured_at.startswith(today):
                captures_today[meta.get("capture_source", "legacy")] += 1
            if meta.get("article_fetch_status") in {"failed", "unavailable"}:
                failed_articles.append(path.relative_to(VAULT).as_posix())
            if meta.get("fetch_status") == "failed":
                failed_web.append(path.relative_to(VAULT).as_posix())

    review_statuses: Counter[str] = Counter()
    recommendations: Counter[str] = Counter()
    reviewed_today = 0
    stale_reviews: list[str] = []
    invalid_review_actions: list[dict[str, str]] = []
    pending_cards: list[dict[str, Any]] = []
    seven_days_ago = now - timedelta(days=7)
    weekly_card_titles: list[dict[str, Any]] = []
    for path in REVIEW_DIR.glob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        status = meta.get("status", "未标记")
        recommendation = meta.get("recommendation", "未标记")
        review_statuses[status] += 1
        recommendations[recommendation] += 1
        checked_actions = checked_review_action_keys(text)
        if len(checked_actions) > 1 or meta.get("action_error"):
            invalid_review_actions.append({
                "file": path.relative_to(VAULT).as_posix(),
                "error": meta.get("action_error") or "同时勾选了多个审核选项",
            })
        reviewed_at = meta.get("reviewed_at", "")
        reviewed_dt = parse_dt(reviewed_at)
        if reviewed_at.startswith(today):
            reviewed_today += 1
        if status == "待审核":
            age = safe_age_hours(reviewed_at, now)
            score = int(meta.get("relevance_score", "0") or 0)
            pending_cards.append({
                "file": path.relative_to(VAULT).as_posix(),
                "title": path.stem,
                "score": score,
                "recommendation": recommendation,
                "age_hours": round(age, 1) if age is not None else None,
            })
            if age is not None and age >= 72:
                stale_reviews.append(path.relative_to(VAULT).as_posix())
        if reviewed_dt:
            if reviewed_dt.tzinfo is None:
                reviewed_dt = reviewed_dt.replace(tzinfo=now.tzinfo)
            if reviewed_dt.astimezone(now.tzinfo) >= seven_days_ago:
                heading = re.search(r"^# (.+)$", text, re.M)
                weekly_card_titles.append({
                    "title": heading.group(1) if heading else path.stem,
                    "score": int(meta.get("relevance_score", "0") or 0),
                    "recommendation": recommendation,
                    "kind": meta.get("suggested_kind", ""),
                })

    pending_cards.sort(key=lambda item: item["score"], reverse=True)

    legacy_statuses: Counter[str] = Counter()
    legacy_waiting: list[str] = []
    legacy_errors: list[dict[str, str]] = []
    legacy_action_labels = (
        "保留为长期知识",
        "合并到建议目标",
        "降级为参考资料",
        "标记为可恢复的待清理项",
    )
    for path in LEGACY_REVIEW_DIR.glob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        legacy_statuses[meta.get("status", "未标记")] += 1
        checked = [
            label for label in legacy_action_labels
            if re.search(rf"^- \[[xX]\]\s+{re.escape(label)}\s*$", text, flags=re.M)
        ]
        if len(checked) > 1 or meta.get("action_error"):
            legacy_errors.append({
                "file": path.relative_to(VAULT).as_posix(),
                "error": meta.get("action_error") or "同时勾选了多个复核选项",
            })
        if checked and not meta.get("action_processed_at"):
            legacy_waiting.append(path.relative_to(VAULT).as_posix())
    knowledge_files = sorted(path.relative_to(VAULT).as_posix() for path in KNOWLEDGE_DIR.rglob("*.md"))

    # 属性 schema 一致性：分类、来源和维护日期必须完整，且不残留迁移前的旧字段
    allowed_kinds = {"concept", "entity", "question", "viewpoint", "case", "playbook", "map", "guide"}
    legacy_fields = ("type", "created", "updated", "knowledge_type")
    schema_issues: list[dict[str, str]] = []
    for path in KNOWLEDGE_DIR.rglob("*.md"):
        rel_path = path.relative_to(VAULT).as_posix()
        if rel_path.startswith("20-知识/90-元数据/"):
            continue
        meta = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        problems: list[str] = []
        kind_value = meta.get("kind", "").strip().strip('"')
        if kind_value not in allowed_kinds:
            problems.append(f"kind={kind_value or '缺失'}")
        missing_maintenance = [
            field for field in ("origin", "created_at", "updated_at")
            if not meta.get(field, "").strip()
        ]
        if missing_maintenance:
            problems.append("缺少维护字段 " + "/".join(missing_maintenance))
        present_legacy = [field for field in legacy_fields if field in meta]
        if present_legacy:
            problems.append("遗留字段 " + "/".join(present_legacy))
        if problems:
            schema_issues.append({"file": rel_path, "issue": "；".join(problems)})

    # 待清理保留策略：按日期命名的目录超过保留期后提醒人工确认删除
    cleanup_dir = VAULT / "90-归档/90-待清理"
    retention_days = 90
    overdue_cleanup: list[dict[str, Any]] = []
    if cleanup_dir.exists():
        for folder in sorted(cleanup_dir.iterdir()):
            if not folder.is_dir():
                continue
            try:
                folder_date = date.fromisoformat(folder.name)
            except ValueError:
                continue
            age_days = (now.date() - folder_date).days
            file_count = sum(1 for p in folder.rglob("*") if p.is_file() and p.name != ".DS_Store")
            if file_count and age_days >= retention_days:
                overdue_cleanup.append({
                    "folder": folder.relative_to(VAULT).as_posix(),
                    "age_days": age_days,
                    "files": file_count,
                })
    previous = previous_snapshot(today)
    previous_files = set(previous.get("knowledge_files", [])) if previous else set()
    knowledge_added = sorted(set(knowledge_files) - previous_files) if previous else []
    knowledge_removed = sorted(previous_files - set(knowledge_files)) if previous else []

    bookmark_ids: Counter[str] = Counter()
    for path in X_DIR.rglob("*.md"):
        # 与 knowledge_ingest.x_id_of 同口径：frontmatter tweet_id → 正文 H1 → 文件名
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        tweet_id = str(meta.get("tweet_id", "")).strip().strip('"')
        if not re.fullmatch(r"\d{18,20}", tweet_id or " "):
            head = "\n".join(text.split("\n")[:40])
            match = re.search(r"—\s*(\d{18,20})\s*$", head, flags=re.M)
            if not match:
                match = re.search(r"(\d{18,20})", path.stem)
            tweet_id = match.group(1) if match else ""
        if tweet_id:
            bookmark_ids[tweet_id] += 1
    duplicate_ids = {key: value for key, value in bookmark_ids.items() if value > 1}
    checked_links, broken_links, links_to_cleanup = resolve_wikilinks()
    missing_required = [rel for rel in REQUIRED_FILES if not (VAULT / rel).exists()]
    unresolved_errors = [error for error in state.get("errors", []) if not error.get("resolved_at")]
    source_health = state.get("source_health", {})
    degraded_sources, source_red_reasons, source_yellow_reasons = (
        source_health_findings(state)
    )
    state_age = safe_age_hours(state.get("updated_at", ""), now)
    automation_jobs = {
        key: {
            "name": name,
            **launch_status(label),
        }
        for key, (name, label) in AUTOMATION_LABELS.items()
    }
    automation_jobs["content_snapshot"].update(content_snapshot_runtime(now))
    launch = automation_jobs["ingest"]
    icloud = icloud_status()
    creation = collect_creation_metrics(now)

    red_reasons: list[str] = []
    yellow_reasons: list[str] = []
    for job in automation_jobs.values():
        if not job.get("loaded"):
            red_reasons.append(f"{job['name']}定时任务未加载")
        elif job.get("last_exit_code") not in {None, 0}:
            red_reasons.append(
                f"{job['name']}上次退出码为 {job['last_exit_code']}"
            )
        if job.get("runtime_error"):
            red_reasons.append(str(job["runtime_error"]))
    if state_age is not None and state_age > 3:
        red_reasons.append(f"采集状态已 {state_age:.1f} 小时没有更新")
    elif state_age is not None and state_age > 1:
        yellow_reasons.append(f"采集状态已 {state_age:.1f} 小时没有更新")
    if unresolved_errors:
        red_reasons.append(f"存在 {len(unresolved_errors)} 个未解决错误")
    red_reasons.extend(source_red_reasons)
    yellow_reasons.extend(source_yellow_reasons)
    if failed_articles or failed_web:
        red_reasons.append(f"存在 {len(failed_articles) + len(failed_web)} 条正文抓取失败")
    if icloud == "syncing":
        yellow_reasons.append("iCloud 尚未同步完成")
    if broken_links:
        yellow_reasons.append(f"发现 {len(broken_links)} 个内部链接无法解析")
    if links_to_cleanup:
        yellow_reasons.append(
            f"发现 {len(links_to_cleanup)} 个活跃链接只指向待清理区"
        )
    if duplicate_ids:
        yellow_reasons.append(f"发现 {len(duplicate_ids)} 组重复 X 书签")
    if missing_required:
        yellow_reasons.append(f"缺少 {len(missing_required)} 个关键入口文件")
    pipeline_pending = len(state.get("pending_sources", []))
    if pipeline_pending >= 5:
        yellow_reasons.append(f"有 {pipeline_pending} 条资料等待系统提炼")
    human_pending = review_statuses.get("待审核", 0)
    if human_pending >= 10:
        yellow_reasons.append(f"有 {human_pending} 张审核卡等待人工处理")
    if stale_reviews:
        yellow_reasons.append(f"有 {len(stale_reviews)} 张审核卡超过 3 天未处理")
    if invalid_review_actions:
        yellow_reasons.append(f"有 {len(invalid_review_actions)} 张审核卡勾选冲突，需要人工更正")
    if legacy_waiting:
        yellow_reasons.append(f"有 {len(legacy_waiting)} 张历史复核卡等待执行")
    if legacy_errors:
        yellow_reasons.append(f"有 {len(legacy_errors)} 张历史复核卡需要补充合并目标或更正选择")
    if schema_issues:
        yellow_reasons.append(f"有 {len(schema_issues)} 篇长期知识属性不符合 schema")
    if overdue_cleanup:
        yellow_reasons.append(
            f"有 {len(overdue_cleanup)} 个待清理目录已超过 {retention_days} 天，可确认后删除"
        )
    yellow_reasons.extend(creation_alerts(creation, previous))

    health = "red" if red_reasons else "yellow" if yellow_reasons else "green"
    metrics = {
        "version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "date": today,
        "health": health,
        "red_reasons": red_reasons,
        "yellow_reasons": yellow_reasons,
        "backlog_notes": chronic_backlog(creation, previous),
        "launch": launch,
        "automation_jobs": automation_jobs,
        "icloud": icloud,
        "state_updated_at": state.get("updated_at", ""),
        "state_age_hours": round(state_age, 2) if state_age is not None else None,
        "captures_today": dict(captures_today),
        "captured_today_total": sum(captures_today.values()),
        "capture_files_total": capture_files,
        "reviewed_today": reviewed_today,
        "review_statuses": dict(review_statuses),
        "recommendations": dict(recommendations),
        "pipeline_pending": pipeline_pending,
        "human_pending": human_pending,
        "stale_reviews": stale_reviews,
        "invalid_review_actions": invalid_review_actions,
        "legacy_statuses": dict(legacy_statuses),
        "legacy_waiting": legacy_waiting,
        "legacy_errors": legacy_errors,
        "top_pending": pending_cards[:5],
        "knowledge_total": len(knowledge_files),
        "knowledge_baseline_established": previous is not None,
        "knowledge_added": knowledge_added,
        "knowledge_removed": knowledge_removed,
        "knowledge_files": knowledge_files,
        "writable_cards": review_statuses.get("可写作", 0),
        "failed_articles": failed_articles,
        "failed_web": failed_web,
        "unresolved_errors": unresolved_errors[-10:],
        "source_health": source_health,
        "degraded_sources": degraded_sources,
        "links_checked": checked_links,
        "broken_link_count": len(broken_links),
        "broken_links": broken_links[:20],
        "links_to_cleanup_count": len(links_to_cleanup),
        "links_to_cleanup": links_to_cleanup[:20],
        "duplicate_x_ids": duplicate_ids,
        "missing_required": missing_required,
        "weekly_card_titles": weekly_card_titles,
        "schema_issues": schema_issues[:20],
        "schema_issue_count": len(schema_issues),
        "overdue_cleanup": overdue_cleanup,
        "cleanup_retention_days": retention_days,
        **creation,
    }
    return metrics


def icon(health: str) -> str:
    return {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(health, "⚪️")


def label(health: str) -> str:
    return {"green": "健康", "yellow": "需要关注", "red": "异常"}.get(health, "未知")


def fmt_icloud(value: str) -> str:
    return {"caught_up": "已同步", "syncing": "同步中", "unknown": "无法确认"}.get(value, value)


def fmt_automation_job(job: dict[str, Any]) -> str:
    if (
        not job.get("loaded")
        or job.get("last_exit_code") not in {None, 0}
        or job.get("runtime_error")
    ):
        return "异常"
    snapshot_state = job.get("snapshot_state")
    if snapshot_state == "scheduled":
        return "已加载，等待今日 18:05"
    if snapshot_state in {"running", "starting"}:
        return "正在运行"
    if snapshot_state == "ok" and job.get("last_success_at"):
        return f"正常（最近 {job['last_success_at']}）"
    return "正常"


def daily_report(metrics: dict[str, Any]) -> str:
    health = metrics["health"]
    captures = metrics["captures_today"]
    knowledge_delta = (
        str(len(metrics["knowledge_added"]))
        if metrics["knowledge_baseline_established"]
        else "基线建立中"
    )
    reasons = metrics["red_reasons"] + metrics["yellow_reasons"]
    summary = "；".join(reasons) if reasons else "采集、同步、提炼和知识库结构均正常。"
    automation_jobs = metrics.get("automation_jobs") or {
        "ingest": {"name": "知识采集", **metrics["launch"]}
    }
    lines = [
        "---",
        "kind: knowledge-health-daily",
        f"date: {metrics['date']}",
        f"health: {health}",
        f"generated_at: {json.dumps(metrics['generated_at'], ensure_ascii=False)}",
        "---",
        "",
        f"# 知识库健康日报 · {metrics['date']}",
        "",
        f"> {icon(health)} **{label(health)}**｜{summary}",
        "",
        "## 今日流量",
        "",
        "| 指标 | 数量 |",
        "|---|---:|",
        f"| 新收集 | {metrics['captured_today_total']} |",
        f"| └ X 书签 | {captures.get('x-bookmark', 0)} |",
        f"| └ 手机分享 | {captures.get('mobile-share', 0)} |",
        f"| └ Web Clipper | {captures.get('web-clipper', 0)} |",
        f"| 今日已提炼 | {metrics['reviewed_today']} |",
        f"| 新增长期知识 | {knowledge_delta} |",
        f"| 可写作 | {metrics['writable_cards']} |",
        "",
        "## 当前积压",
        "",
        "| 阶段 | 数量 | 说明 |",
        "|---|---:|---|",
        f"| 待系统提炼 | {metrics['pipeline_pending']} | 尚未生成审核卡 |",
        f"| 待人工审核 | {metrics['human_pending']} | 已提炼，等待你的判断 |",
        f"| 超过 3 天未处理 | {len(metrics['stale_reviews'])} | 建议优先清理 |",
        f"| 可写作 | {metrics['writable_cards']} | 已确认可进入写作 |",
        f"| 审核勾选冲突 | {len(metrics['invalid_review_actions'])} | 同一张卡只能勾选一项 |",
        f"| 历史复核待执行 | {len(metrics['legacy_waiting'])} | 已选择但尚未完成移动或合并 |",
        f"| 历史复核需补充 | {len(metrics['legacy_errors'])} | 缺少合并目标或存在选择冲突 |",
        "",
        "## 创作健康",
        "",
        "| 检查项 | 数量 | 说明 |",
        "|---|---:|---|",
        f"| 今日可选 | {metrics.get('fresh_candidate_count', metrics.get('active_candidate_count', 0))} | 进入候选池未满 {TOPIC_ATTENTION_DAYS} 天 |",
        f"| 可续写 | {metrics.get('continuation_count', 0)} | 已发布但还有明确剩余角度 |",
        f"| 选题储备 | {metrics.get('reserve_candidate_count', 0)} | 有价值但近期不写 |",
        f"| 待新信号唤醒 | {metrics.get('needs_refresh_candidate_count', 0)} | 需要新进展或本人新实践才能回到今日候选 |",
        f"| 新鲜度资料不全 | {metrics.get('topic_freshness_missing_count', 0)} | 候选、续写和储备都应保留原始资料日期与有效期 |",
        f"| 过期或超期候选 | {metrics.get('overdue_candidate_count', 0)} | 进入候选池已满 {TOPIC_ATTENTION_DAYS} 天仍未立项 |",
        f"| 活跃写作任务 | {metrics.get('active_writing_task_count', 0)} | 建议同时不超过 4 个 |",
        f"| 待反馈 | {metrics.get('pending_feedback_count', 0)} | 等待发布后复盘 |",
        f"| 已到期未复盘 | {metrics.get('overdue_feedback_count', 0)} | 应优先补齐反馈 |",
        f"| 活跃审核目录中的已处理卡 | {metrics.get('processed_review_in_active_count', 0)} | 应退出活跃入口 |",
        f"| 对象重复、错位或身份缺失 | {metrics.get('entity_identity_issue_count', 0)} | 人物、组织、产品和模型应各有唯一稳定档案 |",
        f"| 少于 2 条知识连接 | {metrics.get('underlinked_knowledge_count', 0)} | 长期知识缺少足够内部连接 |",
        f"| 来源缺失或失效 | {metrics.get('knowledge_provenance_issue_count', 0)} | 长期知识必须能回到原始资料 |",
        f"| 审核卡来源失效 | {metrics.get('review_source_issue_count', 0)} | 待审核卡必须能打开原文；被移走时按 source_url 找回 |",
        f"| 发布回写不一致 | {metrics.get('knowledge_backfill_issue_count', 0)} | 已标记回写却没有知识引用 |",
        f"| 写作知识漏接 | {metrics.get('writing_knowledge_handoff_issue_count', 0)} | 写作任务应继承候选中的可调用知识 |",
        f"| 候选知识去向失效 | {metrics.get('topic_knowledge_reference_issue_count', 0)} | 已沉淀候选必须指向活跃知识 |",
        f"| 主题地图候选 | {metrics.get('topic_map_candidate_count', 0)} | 只提醒，不自动建图 |",
        f"| 观察中的主题 | {metrics.get('topic_map_observing_count', 0)} | 等待更多来源或知识增长 |",
        f"| 未进入主题地图 | {metrics.get('knowledge_map_coverage_issue_count', 0)} | 对象索引和总目录不计入主题覆盖 |",
        f"| 发布资料异常 | {metrics.get('published_metadata_issue_count', 0)} | 链接和时间可选；只检查已填写资料是否明显错误 |",
        f"| 发布后未收尾 | {metrics.get('published_active_residue_count', 0)} | 已发布的母稿、口播稿和成稿包应退出活跃目录 |",
        f"| 已发布但候选未关 | {metrics.get('published_open_candidate_count', 0)} | 正式稿的 topic_candidate 指向的候选应关闭 |",
        f"| 同一选题已发布的草稿 | {metrics.get('sibling_draft_count', 0)} | 被替代的旧版或另一版，确认已发还是作废 |",
        f"| 停滞草稿 | {metrics.get('stale_draft_count', 0)} | 草稿区超过 {STALE_DRAFT_DAYS} 天没推进 |",
        f"| 没做知识库预检 | {metrics.get('missing_precheck_count', 0)} | 口播草稿调研前要先查 20-知识 |",
        f"| 本地路径打不开 | {metrics.get('local_path_issue_count', 0)} | 外置盘未接上的 {metrics.get('media_unmounted_count', 0)} 处不计 |",
        f"| 平台字段待补 | {metrics.get('platform_field_issue_count', 0)} | 状态取值和抖音账号 |",
        f"| 发布后待判断回写 | {metrics.get('backfill_pending_count', 0)} | 发布满 3 天应答完「回流」三问并写 complete 或 not-needed |",
        "",
        "## 主题地图候选",
        "",
    ]
    topic_map_candidates = metrics.get("topic_map_candidates", [])
    if topic_map_candidates:
        for candidate in topic_map_candidates[:5]:
            lines.extend(
                [
                    f"### {candidate['name']}",
                    "",
                    f"- 建议：{candidate['proposal']}；置信度 {candidate['confidence']}",
                    f"- 证据：{candidate['note_count']} 条知识，{candidate['source_count']} 份独立来源",
                    f"- 原因：{candidate['reason']}",
                    f"- 判断入口：[[00-入口/10-工作台/主题地图候选#{candidate['name']}|打开主题地图候选]]",
                    "",
                ]
            )
        if len(topic_map_candidates) > 5:
            lines.extend(
                [
                    f"- 另有 {len(topic_map_candidates) - 5} 个候选，"
                    "[[00-入口/10-工作台/主题地图候选|打开完整候选列表]]。",
                    "",
                ]
            )
    else:
        lines.extend(["- 当前没有达到人工判断门槛的主题。", ""])
    lines.extend([
        "## 运行健康",
        "",
        "| 检查项 | 结果 |",
        "|---|---|",
        *[
            f"| {job['name']} | {fmt_automation_job(job)} |"
            for job in automation_jobs.values()
        ],
        f"| 最近状态更新 | {metrics['state_updated_at'] or '未知'} |",
        f"| iCloud | {fmt_icloud(metrics['icloud'])} |",
        f"| 正文抓取失败 | {len(metrics['failed_articles']) + len(metrics['failed_web'])} |",
        f"| 未解决错误 | {len(metrics['unresolved_errors'])} |",
        f"| 异常来源 | {len(metrics.get('degraded_sources', []))} |",
        f"| 内部链接 | 检查 {metrics['links_checked']} 个，异常 {metrics.get('broken_link_count', len(metrics['broken_links']))} 个 |",
        f"| 只指向待清理区 | {metrics.get('links_to_cleanup_count', 0)} 个 |",
        f"| 重复 X 书签 | {len(metrics['duplicate_x_ids'])} 组 |",
        f"| 属性 schema | {'正常' if not metrics.get('schema_issue_count') else str(metrics['schema_issue_count']) + ' 篇待修'} |",
        f"| 待清理超期 | {'无' if not metrics.get('overdue_cleanup') else str(len(metrics['overdue_cleanup'])) + ' 个目录'} |",
        f"| 长期知识总量 | {metrics['knowledge_total']} |",
        "",
        "## 建议优先处理",
        "",
    ])
    if metrics["top_pending"]:
        for item in metrics["top_pending"][:3]:
            note = item["file"][:-3]
            lines.append(f"- [[{note}|{item['title']}]] · {item['score']}/100 · {item['recommendation']}")
    else:
        lines.append("- 当前没有待人工审核的卡片。")
    if reasons:
        lines.extend(["", "## 异常与提醒", ""])
        lines.extend(f"- {reason}" for reason in reasons)
    if metrics.get("backlog_notes"):
        lines.extend(["", "## 长期积压（没有变多，不影响状态，周报处理）", ""])
        lines.extend(f"- {note}" for note in metrics["backlog_notes"])
    detail_groups = (
        ("过期或超期候选", metrics.get("overdue_candidates", [])),
        ("新鲜度资料不全", metrics.get("topic_freshness_missing", [])),
        ("到期未复盘", metrics.get("overdue_feedback", [])),
        ("反馈安排缺项", metrics.get("feedback_schedule_issues", [])),
        ("活跃审核目录中的已处理卡", metrics.get("processed_reviews_in_active", [])),
        ("对象档案异常", metrics.get("entity_identity_issues", [])),
        ("连接不足的长期知识", metrics.get("underlinked_knowledge", [])),
        ("来源缺失或失效的长期知识", metrics.get("knowledge_provenance_issues", [])),
        ("来源失效的待审核卡", metrics.get("review_source_issues", [])),
        ("发布回写状态不一致", metrics.get("knowledge_backfill_issues", [])),
        ("写作任务知识漏接", metrics.get("writing_knowledge_handoff_issues", [])),
        ("候选知识去向失效", metrics.get("topic_knowledge_reference_issues", [])),
        ("未进入主题地图", metrics.get("knowledge_map_coverage_issues", [])),
        ("发布资料异常", metrics.get("published_metadata_issues", [])),
        ("发布后未收尾", metrics.get("published_active_residue_issues", [])),
        ("已发布但候选未关", metrics.get("published_open_candidates", [])),
        ("同一选题已发布的草稿", metrics.get("sibling_drafts", [])),
        ("停滞草稿", metrics.get("stale_drafts", [])),
        ("没做知识库预检", metrics.get("missing_precheck", [])),
        ("本地路径打不开", metrics.get("local_path_issues", [])),
        ("平台字段待补", metrics.get("platform_field_issues", [])),
        ("发布后待判断回写", metrics.get("backfill_pending_issues", [])),
    )
    details = [(title, items) for title, items in detail_groups if items]
    if details:
        lines.extend(["", "## 创作健康待办", ""])
        for title, items in details:
            lines.extend([f"### {title}", ""])
            for item in items[:5]:
                notes = []
                for raw_note in str(item["file"]).split("；"):
                    note = raw_note.strip()
                    note = note[:-3] if note.endswith(".md") else note
                    if note:
                        notes.append(f"[[{note}]]")
                lines.append(f"- {'；'.join(notes)}")
            if len(items) > 5:
                lines.append(f"- 另有 {len(items) - 5} 条，请在对应工作台查看。")
            lines.append("")
    lines.extend(["", "## 口径说明", ""])
    lines.extend([
        "- 已提炼：已经生成审核卡的资料。",
        "- 新增长期知识：相较上一份日报，`20-知识` 新出现的笔记。首次运行只建立基线。",
        "- 待系统提炼：采集完成但还没有审核卡。",
        "- 待人工审核：AI 已完成提炼，但仍需你决定是否保留、写作或清理。",
        f"- 今日可选：进入候选池未满 {TOPIC_ATTENTION_DAYS} 天、且尚未立项的选题；资料时效另行提醒。",
        "- 新鲜度资料不全：候选、续写和储备中缺少原始资料日期、有效期或新鲜度状态的项目。",
        f"- 过期或超期候选：进入候选池已满 {TOPIC_ATTENTION_DAYS} 天仍未立项的选题，系统会关闭候选但保留资料与知识。",
        "- 待反馈：尚未标记完成的发布反馈；“已到期未复盘”是其中超过约定复盘日期的部分。",
        "- 活跃审核目录中的已处理卡：已执行分流但仍留在活跃审核入口的卡片。",
        "- 少于 2 条知识连接：长期知识正文中指向其他长期知识的有效内部链接不足 2 条。",
        "- 来源缺失或失效：长期知识没有声明原始资料，或声明的本地资料已经不存在。",
        "- 审核卡来源失效：待审核卡的 source_file 已不存在，多半是剪藏副本被移走或改名；沉淀时脚本会按 corpus_source 或 source_url 找回，快照里附带建议路径。",
        "- 发布回写不一致：发布稿标记为已经回写知识，但没有任何有效长期知识引用该发布稿。",
        "- 写作知识漏接：候选中已列出的可调用知识，没有随选定角度进入当前写作任务。",
        "- 候选知识去向失效：候选标记为已沉淀，但对应知识为空、不存在或只在待清理区。",
        "- 主题地图候选：多份来源和多条知识已经形成稳定聚集，但仍需人工确认边界；候选不会自动建图。",
        "- 未进入主题地图：长期知识还没有被任何 `map_role: theme` 地图引用；人物索引、产品模型索引、综合入口和地图总目录都不计入。",
        "- 发布资料异常：平台链接和发布时间可以留空；只有已经填写却明显是草稿编辑器或创作者后台地址时才提醒。",
        "- 发布后未收尾：正式稿已经存在，但对应母稿、口播稿或成稿包仍留在活跃生产目录。",
        "- 已发布但候选未关：正式稿的 topic_candidate / topic_card 指向的候选仍是 candidate 或 active。",
        "- 同一选题已发布的草稿：草稿的 supersedes 指向已发布或已归档的稿子，或与某篇正式稿共用 topic_candidate。",
        f"- 停滞草稿：口播草稿和文章草稿里 {STALE_DRAFT_DAYS} 天没有更新、也没有录完的稿子。",
        "- 没做知识库预检：knowledge_precheck_since 之后新建的口播草稿，「素材与来源」里没有「知识库预检」小节，或小节里既没有知识链接也没写「无相关」。",
        "- 本地路径打不开：正式稿里 video_project、video_file、封面等路径不存在；只写项目名时到 config 的 media_roots 里找；外置盘没接上时不算错。",
        "- 平台字段待补：视频平台状态只用 published / pending / reviewing / restricted / private / removed / not-needed；抖音已发布必须写 douyin_account。",
        "- 发布后待判断回写：backfill_check_since 之后发布、满 3 天仍没写知识回写和剩余角度判断，或「回流」三问（机制与类比、带日期的事实、讲法与反馈）没答完的正式稿。每行写链接或「无」。",
        "- 长期积压：来源缺失、连接不足等存量问题，比上一份日报没有变多时不影响状态，只列出数量。",
        "- 属性 schema：`20-知识` 中 kind 不在枚举内或残留 type/created/updated/knowledge_type 旧字段的笔记数。",
        f"- 待清理超期：`90-归档/90-待清理` 中放置超过 {metrics.get('cleanup_retention_days', 90)} 天的目录，确认无误后可人工删除。",
        "",
    ])
    return "\n".join(lines)


def write_daily() -> Path:
    metrics = collect_metrics()
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = SNAPSHOT_DIR / f"{metrics['date']}.json"
    report_path = DAILY_DIR / f"{metrics['date']}.md"
    atomic_json(snapshot_path, metrics)
    atomic_text(report_path, daily_report(metrics))
    atomic_text(
        DAILY_DIR / "最新健康日报.md",
        "---\nkind: dashboard\n---\n\n# 最新知识库健康日报\n\n"
        f"![[00-入口/20-日报/知识库健康日报/{metrics['date']}]]\n",
    )
    retention = archive_report_history(reference_date=date.fromisoformat(metrics["date"]))
    print(json.dumps({
        "report": report_path.relative_to(VAULT).as_posix(),
        "health": metrics["health"],
        "captured_today": metrics["captured_today_total"],
        "reviewed_today": metrics["reviewed_today"],
        "pipeline_pending": metrics["pipeline_pending"],
        "human_pending": metrics["human_pending"],
        "active_candidates": metrics["active_candidate_count"],
        "active_writing_tasks": metrics["active_writing_task_count"],
        "pending_feedback": metrics["pending_feedback_count"],
        "report_retention": retention,
    }, ensure_ascii=False, indent=2))
    return report_path


def weekly_context() -> dict[str, Any]:
    snapshots = recent_snapshots(7)
    live = collect_metrics()
    if not snapshots or snapshots[-1].get("date") != live["date"]:
        snapshots.append(live)
    compact = []
    for item in snapshots[-7:]:
        compact.append({
            "date": item.get("date"),
            "health": item.get("health"),
            "captured": item.get("captured_today_total", 0),
            "reviewed": item.get("reviewed_today", 0),
            "knowledge_added": (
                len(item.get("knowledge_added", []))
                if item.get("knowledge_baseline_established", False)
                else None
            ),
            "knowledge_baseline_established": item.get("knowledge_baseline_established", False),
            "pipeline_pending": item.get("pipeline_pending", 0),
            "human_pending": item.get("human_pending", 0),
            "writable": item.get("writable_cards", 0),
            "active_candidates": item.get("active_candidate_count", 0),
            "continuations": item.get("continuation_count", 0),
            "reserve_candidates": item.get("reserve_candidate_count", 0),
            "needs_refresh_candidates": item.get("needs_refresh_candidate_count", 0),
            "overdue_candidates": item.get("overdue_candidate_count", 0),
            "active_writing_tasks": item.get("active_writing_task_count", 0),
            "pending_feedback": item.get("pending_feedback_count", 0),
            "overdue_feedback": item.get("overdue_feedback_count", 0),
            "processed_review_residue": item.get("processed_review_in_active_count", 0),
            "underlinked_knowledge": item.get("underlinked_knowledge_count", 0),
            "knowledge_provenance_issues": item.get("knowledge_provenance_issue_count", 0),
            "knowledge_backfill_issues": item.get("knowledge_backfill_issue_count", 0),
            "writing_knowledge_handoff_issues": item.get("writing_knowledge_handoff_issue_count", 0),
            "errors": len(item.get("unresolved_errors", [])),
            "failed_fetches": len(item.get("failed_articles", [])) + len(item.get("failed_web", [])),
            "health_reasons": item.get("red_reasons", []) + item.get("yellow_reasons", []),
        })
    cards = live.get("weekly_card_titles", [])
    return {"days": compact, "reviewed_cards": cards, "current": compact[-1]}


def fallback_weekly(context: dict[str, Any], period: str) -> str:
    days = context["days"]
    captured = sum(item["captured"] for item in days)
    reviewed = sum(item["reviewed"] for item in days)
    knowledge_values = [item["knowledge_added"] for item in days if item["knowledge_added"] is not None]
    knowledge_text = str(sum(knowledge_values)) if knowledge_values else "基线建立中，暂无法判断"
    current = context["current"]
    return "\n".join([
        f"# 知识库周报 · {period}", "",
        "## 本周结论", "",
        f"本周收集 {captured} 条、提炼 {reviewed} 条；新增长期知识：{knowledge_text}。",
        "", "## 当前积压", "",
        f"- 待系统提炼：{current['pipeline_pending']} 条",
        f"- 待人工审核：{current['human_pending']} 条",
        f"- 可写作：{current['writable']} 条",
        f"- 今日可选：{current['active_candidates']} 条",
        f"- 选题储备：{current['reserve_candidates']} 条",
        f"- 过期或超期候选：{current['overdue_candidates']} 条",
        f"- 活跃写作任务：{current['active_writing_tasks']} 个",
        f"- 待反馈：{current['pending_feedback']} 条，其中到期未复盘 {current['overdue_feedback']} 条",
        "", "## 下周优先事项", "",
        "1. 优先处理高相关度的待审核卡片。",
        "2. 从保留资料中至少沉淀一条长期知识。",
        "3. 将一条高价值资料推进到可写作。", "",
    ])


def generate_weekly_with_codex(context: dict[str, Any], period: str) -> str:
    config = load_json(CONFIG_FILE, {})
    prompt = f"""你是 Ray 个人知识库的周报编辑。根据下面的统计与审核卡元数据，写一份简洁、具体的中文周报。

要求：
1. 不虚构数据，不把收集量等同于知识产出。
2. 必须区分：新收集、已提炼、新增长期知识、待系统处理、待人工审核、今日可选、选题储备、活跃写作任务、待反馈。
3. 数据中的 knowledge_added 为 null 表示首次建立基线，不能写成新增 0 条，必须写“基线建立中，暂无法判断增量”。成文时不要出现 knowledge_added、null、JSON 等字段或数据格式名称。
4. 找出本周主要主题，只能依据标题和元数据。
5. 必须解释红色或黄色状态的具体原因，包括正文抓取失败。
6. 结合候选是否超期、同时写作任务数、反馈是否到期、已处理审核卡是否退出活跃入口，指出一个最值得改善的流程问题。
7. 给出下周三个明确、可执行的优先事项。
8. 使用 Markdown，依次包含：本周结论、关键数据、主要主题、流程问题、下周优先事项。
9. 不要输出 YAML frontmatter，不要添加代码块。

周期：{period}
数据：
{json.dumps(context, ensure_ascii=False, indent=2)}
"""
    codex = config.get("codex_path", "/opt/homebrew/bin/codex")
    with tempfile.NamedTemporaryFile(prefix="knowledge-weekly-", suffix=".md", delete=False) as file:
        output = Path(file.name)
    cmd = [
        codex, "exec", "--ephemeral", "--sandbox", "read-only",
        "--skip-git-repo-check", "-C", str(VAULT),
        "-o", str(output), "-",
    ]
    # 没配置就跟随 Codex 默认模型；写死的型号会随账号支持范围变化而失效
    model = config.get("codex_model")
    if model:
        cmd[2:2] = ["--model", model]
    try:
        proc = subprocess.run(cmd, input=prompt, text=True, capture_output=True, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[-3000:] or proc.stdout[-3000:] or "Codex failed")
        body = output.read_text(encoding="utf-8").strip()
        if len(body) < 200:
            raise RuntimeError("Codex returned an incomplete weekly report")
        return body + "\n"
    finally:
        output.unlink(missing_ok=True)


def write_weekly() -> Path:
    now = now_local()
    iso_year, iso_week, _ = now.isocalendar()
    week_start = (now.date() - timedelta(days=now.weekday())).isoformat()
    week_end = (now.date() + timedelta(days=6 - now.weekday())).isoformat()
    period = f"{week_start} 至 {week_end}"
    digest_status = "success"
    try:
        digest_result = refresh_weekly_map_digest(
            VAULT,
            MAP_DIGEST_STATE,
            VAULT / MAP_DIGEST_NOTE_REL,
            VAULT / MAP_DIGEST_WEEKLY_DIR_REL,
            TOPIC_MAP_CANDIDATE_STATE,
            now=now,
        )
    except Exception as exc:
        digest_status = "failed"
        digest_result = {"summary": {}}
        print(f"weekly map digest failed: {exc}")
    context = weekly_context()
    ai_status = "success"
    try:
        body = generate_weekly_with_codex(context, period)
    except Exception as exc:
        ai_status = "fallback"
        body = fallback_weekly(context, period)
        print(f"weekly AI summary failed, fallback used: {exc}")
    digest_summary = digest_result.get("summary", {})
    if digest_status == "success":
        digest_line = (
            f"本周有 {digest_summary.get('maps_with_new_knowledge', 0)} 张地图需要整理，"
            f"共 {digest_summary.get('new_knowledge_count', 0)} 条暂存知识；"
            f"其中 {digest_summary.get('pending_action_count', 0)} 条已有可确认建议。"
        )
    else:
        digest_line = "本周地图整理没有成功生成，请查看每周任务日志。"
    body = (
        body.rstrip()
        + "\n\n## 知识地图整理\n\n"
        + digest_line
        + "\n\n- [[00-入口/10-工作台/知识地图整理|打开本周整理建议]]\n"
    )
    frontmatter = "\n".join([
        "---",
        "kind: knowledge-health-weekly",
        f"week: {iso_year}-W{iso_week:02d}",
        f"period_start: {week_start}",
        f"period_end: {week_end}",
        f"generated_at: {json.dumps(now.isoformat(timespec='seconds'), ensure_ascii=False)}",
        f"summary_mode: {ai_status}",
        f"map_digest_status: {digest_status}",
        "---", "",
    ])
    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    report_path = WEEKLY_DIR / f"{iso_year}-W{iso_week:02d}.md"
    atomic_text(report_path, frontmatter + body)
    atomic_text(
        WEEKLY_DIR / "最新知识库周报.md",
        "---\nkind: dashboard\n---\n\n# 最新知识库周报\n\n"
        f"![[00-入口/20-日报/知识库周报/{iso_year}-W{iso_week:02d}]]\n",
    )
    retention = archive_report_history(reference_date=now.date())
    print(json.dumps({
        "report": report_path.relative_to(VAULT).as_posix(),
        "summary_mode": ai_status,
        "days_included": len(context["days"]),
        "map_digest_status": digest_status,
        "map_digest": digest_summary,
        "report_retention": retention,
    }, ensure_ascii=False, indent=2))
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Ray's Brain health reports")
    parser.add_argument("command", choices=["daily", "weekly", "archive-reports"])
    args = parser.parse_args()
    if args.command == "daily":
        write_daily()
    elif args.command == "weekly":
        write_weekly()
    else:
        print(json.dumps(archive_report_history(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
