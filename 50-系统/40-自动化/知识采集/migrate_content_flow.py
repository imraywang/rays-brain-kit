#!/usr/bin/env python3
"""把旧的“审核卡直接生成成稿包”迁移为轻量选题流程。

默认只输出计划；传入 apply 才执行。执行前会把涉及的审核卡、旧成稿包和
运行时状态压缩备份。所有清理项都进入可恢复归档，不永久删除。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("rays_knowledge_ingest", HERE / "knowledge_ingest.py")
assert SPEC and SPEC.loader
flow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(flow)

VAULT = flow.VAULT
OLD_PACK_DIR = VAULT / "10-创作/10-灵感/20-成稿包"
LEGACY_REVIEW_DIR = VAULT / "10-创作/10-灵感/10-待评估/存量知识复核"
MIGRATION_ARCHIVE = VAULT / "90-归档/30-创作记录"
BACKUP_DIR = VAULT / "90-归档/10-迁移记录"
ACTIVE_CANDIDATE_LIMIT = 10


def meta(path: Path) -> dict[str, str]:
    return flow.parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))


def review_action(path: Path) -> str:
    data = meta(path)
    action = data.get("review_action", "")
    if action == "writing":
        return "topic"
    if action:
        return action
    checked = flow.checked_review_actions(path.read_text(encoding="utf-8", errors="replace"))
    return "topic" if checked == ["writing"] else checked[0] if len(checked) == 1 else ""


def processed_reviews() -> list[Path]:
    result = []
    for path in sorted(flow.REVIEW_DIR.glob("*.md")):
        data = meta(path)
        if data.get("action_processed_at") or data.get("status") != "待审核":
            result.append(path)
    return result


def old_packs() -> list[Path]:
    return sorted(OLD_PACK_DIR.glob("*.md")) if OLD_PACK_DIR.exists() else []


def legacy_processed() -> list[Path]:
    if not LEGACY_REVIEW_DIR.exists():
        return []
    return [
        path
        for path in sorted(LEGACY_REVIEW_DIR.glob("*.md"))
        if meta(path).get("action_processed_at") or meta(path).get("status") == "已处理"
    ]


def pack_by_capture() -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in old_packs():
        capture_id = meta(path).get("capture_id", "")
        if capture_id:
            result[capture_id] = path
    return result


def plan() -> dict[str, Any]:
    reviews = processed_reviews()
    packs = old_packs()
    actions: dict[str, int] = {}
    for path in reviews:
        action = review_action(path) or "unknown"
        actions[action] = actions.get(action, 0) + 1
    pack_statuses: dict[str, int] = {}
    for path in packs:
        status = meta(path).get("status", "未标记")
        pack_statuses[status] = pack_statuses.get(status, 0) + 1
    return {
        "processed_reviews_to_archive": len(reviews),
        "review_actions": actions,
        "old_content_packs": len(packs),
        "content_pack_statuses": pack_statuses,
        "legacy_reviews_to_archive": len(legacy_processed()),
        "candidate_limit": ACTIVE_CANDIDATE_LIMIT,
        "pending_reviews_left_in_place": len(list(flow.REVIEW_DIR.glob("*.md"))) - len(reviews),
    }


def backup(paths: list[Path]) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    output = BACKUP_DIR / f"{stamp}-创作流程改造前备份.zip"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(paths)):
            if path.exists() and path.is_file():
                archive.write(path, path.relative_to(VAULT).as_posix())
        if flow.STATE_FILE.exists():
            archive.write(flow.STATE_FILE, "_runtime/state.json")
    return output


def move_note(path: Path, destination_root: Path) -> Path:
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = flow.unique_destination(destination_root, path.name)
    old_rel = flow.rel(path)
    shutil.move(path, destination)
    flow.replace_markdown_references(old_rel, flow.rel(destination))
    return destination


def update_topic(path: Path, updates: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    path.write_text(flow.update_frontmatter(text, updates), encoding="utf-8")


def create_topics(reviews: list[Path], packs: dict[str, Path]) -> dict[str, Path]:
    topics: dict[str, Path] = {}
    for card in reviews:
        action = review_action(card)
        if action not in {"topic", "both"}:
            continue
        text = card.read_text(encoding="utf-8", errors="replace")
        card_meta = flow.parse_frontmatter(text)
        capture_id = card_meta.get("capture_id", "")
        pack = packs.get(capture_id)
        pack_status = meta(pack).get("status", "seed") if pack else "seed"
        status = (
            "partially-published"
            if pack_status == "published"
            else "active"
            if pack_status not in {"seed", ""}
            else "candidate"
        )
        topic = flow.create_topic_candidate(card, text, card_meta, status=status)
        topics[capture_id or flow.rel(card)] = topic
        topic_status = meta(topic).get("status", "")
        updates = {
            "topic_file": flow.rel(topic),
            "result_file": flow.rel(topic),
            "status": (
                "已双向分流"
                if action == "both"
                else "已进入储备"
                if topic_status == "parked"
                else "已进入候选"
            ),
        }
        card.write_text(flow.update_frontmatter(text, updates), encoding="utf-8")
    return topics


def migrate_packs(packs: list[Path], topics: dict[str, Path]) -> dict[str, int]:
    result = {"seed_archived": 0, "active_moved": 0, "published_archived": 0}
    for pack in packs:
        pack_meta = meta(pack)
        status = pack_meta.get("status", "seed")
        capture_id = pack_meta.get("capture_id", "")
        topic = topics.get(capture_id)
        month = (pack_meta.get("created_at") or flow.now_local())[:7]
        if status in {"seed", ""}:
            moved = move_note(pack, MIGRATION_ARCHIVE / "旧成稿包" / month)
            result["seed_archived"] += 1
            if topic:
                update_topic(topic, {"legacy_content_pack": flow.rel(moved)})
            continue
        if status == "published":
            moved = move_note(pack, MIGRATION_ARCHIVE / "写作任务" / month)
            result["published_archived"] += 1
        else:
            moved = move_note(pack, flow.WRITING_TASK_DIR)
            result["active_moved"] += 1
        if topic:
            updates = {
                "content_pack": f"[[{flow.rel(moved)[:-3]}]]",
                "last_progress_at": flow.now_local(),
            }
            published = (
                pack_meta.get("published_file", "")
                or pack_meta.get("published", "")
            )
            if published:
                updates["published_file"] = published
            update_topic(topic, updates)
            moved_text = moved.read_text(encoding="utf-8", errors="replace")
            moved.write_text(
                flow.update_frontmatter(
                    moved_text,
                    {
                        "candidate": f"[[{flow.rel(topic)[:-3]}]]",
                        "topic_id": meta(topic).get("topic_id", ""),
                    },
                ),
                encoding="utf-8",
            )
    return result


def trim_active_candidates() -> dict[str, int]:
    candidates: list[tuple[str, int, Path]] = []
    committed = 0
    parked = 0
    for path in flow.TOPIC_DIR.glob("*.md"):
        data = meta(path)
        if data.get("kind") != "topic-candidate":
            continue
        if data.get("status") == "active":
            committed += 1
            continue
        if data.get("status") == "partially-published":
            # 已发布但仍可续写的主题有独立视图，不占“今天选什么”的名额。
            continue
        if data.get("status") != "candidate":
            continue
        time_sensitivity = data.get("time_sensitivity", "")
        source_date, fresh_until, freshness_status = flow.topic_freshness(
            data,
            time_sensitivity if time_sensitivity in flow.TOPIC_FRESHNESS_DAYS else "中时效",
        )
        if freshness_status != "fresh":
            text = path.read_text(encoding="utf-8", errors="replace")
            path.write_text(
                flow.update_frontmatter(
                    text,
                    {
                        "status": "parked",
                        "priority_score": "0",
                        "source_published_at": source_date,
                        "fresh_until": fresh_until,
                        "freshness_status": freshness_status,
                        "updated_at": flow.now_local(),
                        "parked_reason": (
                            "原始资料已超过当前时效窗口，需有新进展或本人新实践后再唤醒"
                            if freshness_status == "stale"
                            else "无法确认原始资料日期，需补齐来源时间后再进入今日候选"
                        ),
                    },
                ),
                encoding="utf-8",
            )
            move_note(path, flow.TOPIC_RESERVE_DIR)
            parked += 1
            continue
        score = flow.int_score(data, "priority_score", flow.int_score(data, "writing_value_score"))
        candidates.append((source_date, score, path))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    open_slots = max(0, ACTIVE_CANDIDATE_LIMIT - committed)
    for _, _, path in candidates[open_slots:]:
        text = path.read_text(encoding="utf-8", errors="replace")
        path.write_text(
            flow.update_frontmatter(
                text,
                {
                    "status": "parked",
                    "updated_at": flow.now_local(),
                    "parked_reason": "超出日常候选池容量，保留供检索和后续唤醒",
                },
            ),
            encoding="utf-8",
        )
        move_note(path, flow.TOPIC_RESERVE_DIR)
        parked += 1
    return {
        "active_candidates": committed + min(len(candidates), open_slots),
        "committed_topics": committed,
        "parked": parked,
    }


def archive_reviews(reviews: list[Path], state: dict[str, Any]) -> dict[str, int]:
    result = {"records": 0, "cleanup": 0, "sources_archived": 0}
    for card in reviews:
        if not card.exists():
            continue
        text = card.read_text(encoding="utf-8", errors="replace")
        card_meta = flow.parse_frontmatter(text)
        action = review_action(card)
        cleanup = action == "cleanup"
        if cleanup:
            old_source = card_meta.get("source_file", "")
            new_source = flow.archive_cleanup_source(card_meta)
            if new_source != old_source:
                # archive_cleanup_source 已同步正文中的旧链接；重新读取，避免
                # 用迁移前缓存的正文把链接改动覆盖掉。
                text = card.read_text(encoding="utf-8", errors="replace")
                text = flow.update_frontmatter(
                    text,
                    {"original_source_file": old_source, "source_file": new_source},
                )
                card.write_text(text, encoding="utf-8")
                card_meta = flow.parse_frontmatter(text)
                result["sources_archived"] += 1
        flow.archive_review_card(card, card_meta, state, cleanup=cleanup)
        result["cleanup" if cleanup else "records"] += 1
    return result


def repair_archived_cleanup_links() -> int:
    """修复旧迁移中被缓存正文覆盖的待清理来源链接。"""
    cleanup_root = VAULT / "90-归档/90-待清理"
    if not cleanup_root.exists():
        return 0
    changed = 0
    for card in cleanup_root.rglob("*.md"):
        if "审核记录" not in card.parts:
            continue
        text = card.read_text(encoding="utf-8", errors="replace")
        data = flow.parse_frontmatter(text)
        old_rel = data.get("original_source_file", "")
        new_rel = data.get("source_file", "")
        if not old_rel or not new_rel or old_rel == new_rel:
            continue
        end = text.find("\n---\n", 4)
        if end < 0:
            continue
        body_start = end + len("\n---\n")
        old_note = old_rel[:-3] if old_rel.endswith(".md") else old_rel
        new_note = new_rel[:-3] if new_rel.endswith(".md") else new_rel
        body = text[body_start:]
        updated_body = body.replace(old_rel, new_rel).replace(old_note, new_note)
        if updated_body != body:
            card.write_text(text[:body_start] + updated_body, encoding="utf-8")
            changed += 1
    return changed


def archive_legacy_cards(cards: list[Path]) -> int:
    moved = 0
    for card in cards:
        data = meta(card)
        month = (data.get("action_processed_at") or data.get("audited_at") or flow.now_local())[:7]
        move_note(card, VAULT / "90-归档/20-审核记录/存量知识复核" / month)
        moved += 1
    return moved


def apply() -> dict[str, Any]:
    reviews = processed_reviews()
    packs = old_packs()
    legacy = legacy_processed()
    backup_path = backup(reviews + packs + legacy)
    state = flow.load_state()
    packs_by_capture = pack_by_capture()
    topics = create_topics(reviews, packs_by_capture)
    pack_result = migrate_packs(packs, topics)
    candidate_result = trim_active_candidates()
    review_result = archive_reviews(reviews, state)
    repaired_cleanup_links = repair_archived_cleanup_links()
    legacy_result = archive_legacy_cards(legacy)
    flow.save_state(state)
    return {
        "backup": flow.rel(backup_path),
        "topics_created_or_reused": len(topics),
        "packs": pack_result,
        "candidates": candidate_result,
        "reviews": review_result,
        "cleanup_links_repaired": repaired_cleanup_links,
        "legacy_reviews_archived": legacy_result,
        "pending_reviews_left": len(list(flow.REVIEW_DIR.glob("*.md"))),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Ray's Brain content workflow")
    parser.add_argument("command", choices=["plan", "apply"], nargs="?", default="plan")
    args = parser.parse_args()
    result = apply() if args.command == "apply" else plan()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
