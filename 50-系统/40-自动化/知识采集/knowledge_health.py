#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any


HERE = Path(__file__).resolve().parent
VAULT = Path(os.environ.get("RAYS_BRAIN", str(HERE.parents[2]))).resolve()
SYSTEM_DIR = HERE
# 运行时状态放在 vault 外，与 knowledge_ingest.py 保持一致
STATE_HOME = Path(os.environ.get("RAYS_BRAIN_STATE", str(Path.home() / ".local/state/rays-brain")))
STATE_FILE = STATE_HOME / "state.json"
SNAPSHOT_DIR = STATE_HOME / "health-snapshots"
CONFIG_FILE = SYSTEM_DIR / "config.json"
DAILY_DIR = VAULT / "00-入口/20-日报/知识库健康日报"
WEEKLY_DIR = VAULT / "00-入口/20-日报/知识库周报"
REVIEW_DIR = VAULT / "10-创作/10-灵感/10-待评估/剪藏复核"
LEGACY_REVIEW_DIR = VAULT / "10-创作/10-灵感/10-待评估/存量知识复核"
X_DIR = VAULT / "30-资料/30-X书签"
WEB_DIR = VAULT / "30-资料/40-网页剪藏"
KNOWLEDGE_DIR = VAULT / "20-知识"
LINK_INBOX = VAULT / "30-资料/00-待抓取/链接收件箱.md"
INGEST_LABEL = "com.ray.knowledge-ingest"

REQUIRED_FILES = [
    "HOME.md",
    "00-入口/10-工作台/知识收件台.md",
    "00-入口/10-工作台/剪藏复核.base",
    "20-知识/README.md",
    "50-系统/40-自动化/知识采集/knowledge_ingest.py",
    "50-系统/40-自动化/知识采集/config.json",
]


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


def launch_status() -> dict[str, Any]:
    uid = os.getuid()
    try:
        proc = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{uid}/{INGEST_LABEL}"],
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
        "loaded": True,
        "runs": int(runs.group(1)) if runs else None,
        "last_exit_code": int(exit_code.group(1)) if exit_code else None,
        "interval_seconds": int(interval.group(1)) if interval else None,
    }


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


def resolve_wikilinks() -> tuple[int, list[dict[str, str]]]:
    all_files = [p for p in VAULT.rglob("*") if p.is_file() and ".git" not in p.parts]
    rel_files = {p.relative_to(VAULT).as_posix(): p for p in all_files}
    stems: dict[str, list[str]] = defaultdict(list)
    for rel, path in rel_files.items():
        stems[path.stem].append(rel)

    def resolve(source: Path, raw: str) -> bool:
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if not target or "://" in target or target.startswith("@") or target == "wikilinks":
            return True
        suffix = PurePosixPath(target).suffix.lower()
        has_file_suffix = suffix in {".md", ".base", ".json"}
        candidates = [target] if has_file_suffix else [target, target + ".md", target + ".base"]
        if any(candidate in rel_files for candidate in candidates):
            return True
        source_rel = source.relative_to(VAULT).parent
        if any((source_rel / candidate).as_posix() in rel_files for candidate in candidates):
            return True
        if "/" not in target:
            lookup = Path(target).stem if suffix in {".md", ".base"} else target
            return bool(stems.get(lookup))
        return False

    checked = 0
    broken: list[dict[str, str]] = []
    pattern = re.compile(r"!?\[\[([^\]]+)\]\]")
    for rel, path in sorted(rel_files.items()):
        if path.suffix not in {".md", ".base", ".json"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        searchable = re.sub(r"```.*?```", "", text, flags=re.S)
        searchable = re.sub(r"`[^`\n]*`", "", searchable)
        for match in pattern.finditer(searchable):
            checked += 1
            if not resolve(path, match.group(1)):
                broken.append({"source": rel, "target": match.group(1)})
    return checked, broken


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
            if meta.get("created", "").startswith(today):
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
        checked_actions = re.findall(
            r"^- \[[xX]\]\s+(批准进入长期知识库|仅保留为写作素材|暂缓|移入可恢复的待清理区|标记为可恢复的待清理项)\s*$",
            text,
            flags=re.M,
        )
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

    # 属性 schema 一致性：kind 必须落在工作流定义的枚举内，且不残留迁移前的旧字段
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
    checked_links, broken_links = resolve_wikilinks()
    missing_required = [rel for rel in REQUIRED_FILES if not (VAULT / rel).exists()]
    unresolved_errors = [error for error in state.get("errors", []) if not error.get("resolved_at")]
    state_age = safe_age_hours(state.get("updated_at", ""), now)
    launch = launch_status()
    icloud = icloud_status()

    red_reasons: list[str] = []
    yellow_reasons: list[str] = []
    if not launch.get("loaded"):
        red_reasons.append("知识采集定时任务未加载")
    elif launch.get("last_exit_code") not in {None, 0}:
        red_reasons.append(f"知识采集上次退出码为 {launch['last_exit_code']}")
    if state_age is not None and state_age > 3:
        red_reasons.append(f"采集状态已 {state_age:.1f} 小时没有更新")
    elif state_age is not None and state_age > 1:
        yellow_reasons.append(f"采集状态已 {state_age:.1f} 小时没有更新")
    if unresolved_errors:
        red_reasons.append(f"存在 {len(unresolved_errors)} 个未解决错误")
    if failed_articles or failed_web:
        red_reasons.append(f"存在 {len(failed_articles) + len(failed_web)} 条正文抓取失败")
    if icloud == "syncing":
        yellow_reasons.append("iCloud 尚未同步完成")
    if broken_links:
        yellow_reasons.append(f"发现 {len(broken_links)} 个内部链接无法解析")
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

    health = "red" if red_reasons else "yellow" if yellow_reasons else "green"
    metrics = {
        "version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "date": today,
        "health": health,
        "red_reasons": red_reasons,
        "yellow_reasons": yellow_reasons,
        "launch": launch,
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
        "links_checked": checked_links,
        "broken_links": broken_links[:20],
        "duplicate_x_ids": duplicate_ids,
        "missing_required": missing_required,
        "weekly_card_titles": weekly_card_titles,
        "schema_issues": schema_issues[:20],
        "schema_issue_count": len(schema_issues),
        "overdue_cleanup": overdue_cleanup,
        "cleanup_retention_days": retention_days,
    }
    return metrics


def icon(health: str) -> str:
    return {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(health, "⚪️")


def label(health: str) -> str:
    return {"green": "健康", "yellow": "需要关注", "red": "异常"}.get(health, "未知")


def fmt_icloud(value: str) -> str:
    return {"caught_up": "已同步", "syncing": "同步中", "unknown": "无法确认"}.get(value, value)


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
        "## 运行健康",
        "",
        "| 检查项 | 结果 |",
        "|---|---|",
        f"| 定时采集 | {'正常' if metrics['launch'].get('loaded') and metrics['launch'].get('last_exit_code') in {None, 0} else '异常'} |",
        f"| 最近状态更新 | {metrics['state_updated_at'] or '未知'} |",
        f"| iCloud | {fmt_icloud(metrics['icloud'])} |",
        f"| 正文抓取失败 | {len(metrics['failed_articles']) + len(metrics['failed_web'])} |",
        f"| 未解决错误 | {len(metrics['unresolved_errors'])} |",
        f"| 内部链接 | 检查 {metrics['links_checked']} 个，异常 {len(metrics['broken_links'])} 个 |",
        f"| 重复 X 书签 | {len(metrics['duplicate_x_ids'])} 组 |",
        f"| 属性 schema | {'正常' if not metrics.get('schema_issue_count') else str(metrics['schema_issue_count']) + ' 篇待修'} |",
        f"| 待清理超期 | {'无' if not metrics.get('overdue_cleanup') else str(len(metrics['overdue_cleanup'])) + ' 个目录'} |",
        f"| 长期知识总量 | {metrics['knowledge_total']} |",
        "",
        "## 建议优先处理",
        "",
    ]
    if metrics["top_pending"]:
        for item in metrics["top_pending"][:3]:
            note = item["file"][:-3]
            lines.append(f"- [[{note}|{item['title']}]] · {item['score']}/100 · {item['recommendation']}")
    else:
        lines.append("- 当前没有待人工审核的卡片。")
    if reasons:
        lines.extend(["", "## 异常与提醒", ""])
        lines.extend(f"- {reason}" for reason in reasons)
    lines.extend(["", "## 口径说明", ""])
    lines.extend([
        "- 已提炼：已经生成审核卡的资料。",
        "- 新增长期知识：相较上一份日报，`20-知识` 新出现的笔记。首次运行只建立基线。",
        "- 待系统提炼：采集完成但还没有审核卡。",
        "- 待人工审核：AI 已完成提炼，但仍需你决定是否保留、写作或清理。",
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
    print(json.dumps({
        "report": report_path.relative_to(VAULT).as_posix(),
        "health": metrics["health"],
        "captured_today": metrics["captured_today_total"],
        "reviewed_today": metrics["reviewed_today"],
        "pipeline_pending": metrics["pipeline_pending"],
        "human_pending": metrics["human_pending"],
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
2. 必须区分：新收集、已提炼、新增长期知识、待系统处理、待人工审核、可写作。
3. 数据中的 knowledge_added 为 null 表示首次建立基线，不能写成新增 0 条，必须写“基线建立中，暂无法判断增量”。成文时不要出现 knowledge_added、null、JSON 等字段或数据格式名称。
4. 找出本周主要主题，只能依据标题和元数据。
5. 必须解释红色或黄色状态的具体原因，包括正文抓取失败。
6. 指出一个最值得改善的流程问题。
7. 给出下周三个明确、可执行的优先事项。
8. 使用 Markdown，依次包含：本周结论、关键数据、主要主题、流程问题、下周优先事项。
9. 不要输出 YAML frontmatter，不要添加代码块。

周期：{period}
数据：
{json.dumps(context, ensure_ascii=False, indent=2)}
"""
    codex = config.get("codex_path", "/opt/homebrew/bin/codex")
    model = config.get("codex_model", "gpt-5.5")
    with tempfile.NamedTemporaryFile(prefix="knowledge-weekly-", suffix=".md", delete=False) as file:
        output = Path(file.name)
    cmd = [
        codex, "exec", "--ephemeral", "--sandbox", "read-only",
        "--skip-git-repo-check", "-C", str(VAULT), "--model", model,
        "-o", str(output), "-",
    ]
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
    context = weekly_context()
    ai_status = "success"
    try:
        body = generate_weekly_with_codex(context, period)
    except Exception as exc:
        ai_status = "fallback"
        body = fallback_weekly(context, period)
        print(f"weekly AI summary failed, fallback used: {exc}")
    frontmatter = "\n".join([
        "---",
        "kind: knowledge-health-weekly",
        f"week: {iso_year}-W{iso_week:02d}",
        f"period_start: {week_start}",
        f"period_end: {week_end}",
        f"generated_at: {json.dumps(now.isoformat(timespec='seconds'), ensure_ascii=False)}",
        f"summary_mode: {ai_status}",
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
    print(json.dumps({
        "report": report_path.relative_to(VAULT).as_posix(),
        "summary_mode": ai_status,
        "days_included": len(context["days"]),
    }, ensure_ascii=False, indent=2))
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Ray's Brain health reports")
    parser.add_argument("command", choices=["daily", "weekly"])
    args = parser.parse_args()
    if args.command == "daily":
        write_daily()
    else:
        write_weekly()


if __name__ == "__main__":
    main()
