#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
VAULT = Path(os.environ.get("RAYS_BRAIN", str(HERE.parents[2]))).resolve()
REVIEW_DIR = VAULT / "10-创作/10-灵感/10-待评估/存量知识复核"
REFERENCE_DIR = VAULT / "30-资料/20-参考资料/历史知识降级"
ARCHIVE_DIR = VAULT / "90-归档/90-待清理" / datetime.now().astimezone().date().isoformat()
BACKUP_DIR = VAULT / "90-归档/10-迁移记录"

ACTIONS = {
    "保留为长期知识": "keep",
    "合并到建议目标": "merge",
    "降级为参考资料": "reference",
    "标记为可恢复的待清理项": "cleanup",
}


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    out: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line or line.startswith((" ", "\t", "-")):
            continue
        key, value = line.split(":", 1)
        out[key.strip()] = value.strip().strip('"')
    return out


def yaml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(str(value), ensure_ascii=False)


def update_frontmatter(text: str, updates: dict[str, Any]) -> str:
    if not text.startswith("---\n"):
        lines = ["---"] + [f"{k}: {yaml_value(v)}" for k, v in updates.items()] + ["---", "", text]
        return "\n".join(lines)
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("invalid frontmatter")
    lines = text[4:end].splitlines()
    positions = {}
    for i, line in enumerate(lines):
        if ":" in line and not line.startswith((" ", "\t", "-")):
            positions[line.split(":", 1)[0].strip()] = i
    for key, value in updates.items():
        rendered = f"{key}: {yaml_value(value)}"
        if key in positions:
            lines[positions[key]] = rendered
        else:
            lines.append(rendered)
    return "---\n" + "\n".join(lines) + text[end:]


def checked_action(text: str) -> tuple[str | None, str | None]:
    found = []
    for label, action in ACTIONS.items():
        if re.search(rf"^- \[[xX]\]\s+{re.escape(label)}\s*$", text, re.M):
            found.append(action)
    if len(found) == 1:
        return found[0], None
    if not found:
        return None, None
    return None, "同时勾选了多个复核选项"


def safe_rel(raw: str, prefix: str | None = None) -> Path:
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe path: {raw}")
    if prefix and not path.as_posix().startswith(prefix):
        raise ValueError(f"path outside {prefix}: {raw}")
    return path


def unique_destination(base: Path, name: str) -> Path:
    candidate = base / name
    i = 2
    while candidate.exists():
        candidate = base / f"{Path(name).stem}（{i}）{Path(name).suffix}"
        i += 1
    return candidate


def replace_active_links(old_rel: str, replacement_rel: str) -> int:
    old_note = old_rel[:-3] if old_rel.endswith(".md") else old_rel
    new_note = replacement_rel[:-3] if replacement_rel.endswith(".md") else replacement_rel
    old_stem = Path(old_note).name
    new_stem = Path(new_note).name
    changed = 0
    for path in VAULT.rglob("*.md"):
        rel = path.relative_to(VAULT).as_posix()
        if rel.startswith("90-归档/") or rel.startswith("10-创作/10-灵感/10-待评估/存量知识复核/"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        updated = text.replace(f"[[{old_note}]]", f"[[{new_note}]]")
        updated = updated.replace(f"[[{old_note}|", f"[[{new_note}|")
        updated = re.sub(rf"\[\[{re.escape(old_stem)}(?=(?:\||#|\]\]))", f"[[{new_stem}", updated)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed += 1
    return changed


def backup(paths: list[Path]) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    output = BACKUP_DIR / f"{stamp}-存量知识执行前备份.zip"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(paths)):
            if path.exists() and path.is_file():
                archive.write(path, path.relative_to(VAULT).as_posix())
    return output


def collect_ready() -> list[tuple[Path, str, dict[str, str]]]:
    ready = []
    for card in sorted(REVIEW_DIR.glob("*.md")):
        text = card.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        if meta.get("action_processed_at") or meta.get("status") == "已处理":
            continue
        action, error = checked_action(text)
        if error:
            card.write_text(update_frontmatter(text, {"action_error": error}), encoding="utf-8")
            continue
        if action:
            ready.append((card, action, meta))
    return ready


def apply() -> dict[str, Any]:
    result: dict[str, Any] = {"processed": 0, "keep": 0, "merge": 0, "reference": 0, "cleanup": 0, "waiting": 0, "links_updated": 0, "backup": ""}
    ready = collect_ready()
    backup_paths = [card for card, _, _ in ready]
    for _, _, meta in ready:
        source = meta.get("source_file", "")
        if source:
            backup_paths.append(VAULT / safe_rel(source))
        target = meta.get("merge_target", "")
        if target:
            backup_paths.append(VAULT / safe_rel(target))
    if ready:
        result["backup"] = backup(backup_paths).relative_to(VAULT).as_posix()

    for card, action, meta in ready:
        card_text = card.read_text(encoding="utf-8", errors="replace")
        source_rel = meta.get("source_file", "")
        if not source_rel:
            card.write_text(update_frontmatter(card_text, {"action_error": "缺少原始知识路径"}), encoding="utf-8")
            result["waiting"] += 1
            continue
        source = VAULT / safe_rel(source_rel, "20-知识/")
        if not source.exists():
            card.write_text(update_frontmatter(card_text, {"action_error": "原始知识文件不存在"}), encoding="utf-8")
            result["waiting"] += 1
            continue
        target_rel = source_rel
        relocated_rel = source_rel
        if action == "keep":
            source_text = source.read_text(encoding="utf-8", errors="replace")
            source.write_text(update_frontmatter(source_text, {"status": "evergreen", "legacy_review_action": "keep", "legacy_reviewed_at": now(), "legacy_review_card": card.relative_to(VAULT).as_posix()}), encoding="utf-8")
        elif action == "merge":
            target_rel = meta.get("merge_target", "")
            merge_ready = meta.get("merge_ready", "").lower() == "true"
            if not target_rel or not merge_ready:
                card.write_text(update_frontmatter(card_text, {"action_error": "合并目标尚未准备完成"}), encoding="utf-8")
                result["waiting"] += 1
                continue
            target = VAULT / safe_rel(target_rel, "20-知识/")
            if not target.exists() or target.stat().st_size < 300:
                card.write_text(update_frontmatter(card_text, {"action_error": "合并目标不存在或内容不完整"}), encoding="utf-8")
                result["waiting"] += 1
                continue
            destination = unique_destination(ARCHIVE_DIR / "已合并历史知识", source.name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(source, destination)
            relocated_rel = destination.relative_to(VAULT).as_posix()
            result["links_updated"] += replace_active_links(source_rel, target_rel)
        elif action == "reference":
            REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
            source_text = source.read_text(encoding="utf-8", errors="replace")
            original_kind = parse_frontmatter(source_text).get("kind", "")
            source.write_text(update_frontmatter(source_text, {"kind": "source", "status": "reference", "original_kind": original_kind, "legacy_review_action": "reference", "legacy_reviewed_at": now()}), encoding="utf-8")
            destination = unique_destination(REFERENCE_DIR, source.name)
            shutil.move(source, destination)
            target_rel = destination.relative_to(VAULT).as_posix()
            relocated_rel = target_rel
            replacement = meta.get("replacement_target", "") or target_rel
            result["links_updated"] += replace_active_links(source_rel, replacement)
        else:
            destination = unique_destination(ARCHIVE_DIR / "人工确认待清理", source.name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(source, destination)
            target_rel = destination.relative_to(VAULT).as_posix()
            relocated_rel = target_rel
        card_updates = {
            "status": "已处理",
            "review_action": action,
            "action_processed_at": now(),
            "action_error": "",
            "original_source_file": source_rel,
            "source_file": relocated_rel,
            "result_file": target_rel,
        }
        card.write_text(update_frontmatter(card_text, card_updates), encoding="utf-8")
        result["processed"] += 1
        result[action] += 1
    return result


def status() -> dict[str, int]:
    counts = {"checked": 0, "processed": 0, "waiting": 0}
    for card in REVIEW_DIR.glob("*.md"):
        text = card.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        action, error = checked_action(text)
        if action:
            counts["checked"] += 1
        if meta.get("action_processed_at") or meta.get("status") == "已处理":
            counts["processed"] += 1
        elif action or error:
            counts["waiting"] += 1
    return counts


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Apply legacy knowledge review decisions")
    parser.add_argument("command", choices=["apply", "status"])
    args = parser.parse_args()
    print(json.dumps(apply() if args.command == "apply" else status(), ensure_ascii=False, indent=2))
