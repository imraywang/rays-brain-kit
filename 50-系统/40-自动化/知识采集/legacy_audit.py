#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
VAULT = Path(os.environ.get("RAYS_BRAIN", str(HERE.parents[2]))).resolve()
SYSTEM_DIR = HERE
CONFIG_FILE = SYSTEM_DIR / "config.json"
PROMPT_FILE = SYSTEM_DIR / "legacy-audit-prompt.md"
SCHEMA_FILE = SYSTEM_DIR / "legacy-audit.schema.json"
KNOWLEDGE_DIR = VAULT / "20-知识"
AUDIT_DIR = VAULT / "10-创作/10-灵感/10-待评估/存量知识复核"


def now_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


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


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def rel(path: Path) -> str:
    return path.relative_to(VAULT).as_posix()


def target_notes() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for path in sorted(KNOWLEDGE_DIR.rglob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        if meta.get("status") != "待核对":
            continue
        items.append({
            "id": rel(path),
            "title": meta.get("title") or path.stem,
            "content": text,
        })
    return items


def existing_sources() -> set[str]:
    result: set[str] = set()
    if not AUDIT_DIR.exists():
        return result
    for path in AUDIT_DIR.glob("*.md"):
        meta = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        if meta.get("source_file"):
            result.add(meta["source_file"])
    return result


def knowledge_inventory() -> list[str]:
    return [rel(path) for path in sorted(KNOWLEDGE_DIR.rglob("*.md"))]


def build_prompt(items: list[dict[str, str]]) -> str:
    instructions = PROMPT_FILE.read_text(encoding="utf-8")
    inventory = "\n".join(f"- {path}" for path in knowledge_inventory())
    chunks = [instructions, "\n## 现有长期知识目录\n", inventory, "\n## 待复核全文\n"]
    for item in items:
        chunks.extend([
            "\n--- ITEM ---",
            f"id: {item['id']}",
            f"title: {item['title']}",
            item["content"],
        ])
    return "\n".join(chunks)


def run_codex(prompt: str) -> dict[str, Any]:
    config = load_json(CONFIG_FILE, {})
    codex = config.get("codex_path", "/opt/homebrew/bin/codex")
    model = config.get("codex_model", "gpt-5.5")
    with tempfile.NamedTemporaryFile(prefix="legacy-audit-", suffix=".json", delete=False) as file:
        output = Path(file.name)
    cmd = [
        codex, "exec", "--model", model, "--ephemeral", "--sandbox", "read-only",
        "--skip-git-repo-check", "-C", str(VAULT),
        "--output-schema", str(SCHEMA_FILE), "-o", str(output), "-",
    ]
    try:
        proc = subprocess.run(cmd, input=prompt, text=True, capture_output=True, timeout=900)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[-5000:] or proc.stdout[-5000:] or "Codex failed")
        return json.loads(output.read_text(encoding="utf-8"))
    finally:
        output.unlink(missing_ok=True)


def note_link(path: str) -> str:
    return path[:-3] if path.endswith(".md") else path


def write_card(review: dict[str, Any]) -> Path:
    source_file = review["id"]
    source_path = VAULT / source_file
    title = parse_frontmatter(source_path.read_text(encoding="utf-8", errors="replace")).get("title") or source_path.stem
    path = AUDIT_DIR / f"{title}.md"
    if path.exists():
        short = hashlib.sha256(source_file.encode()).hexdigest()[:8]
        path = AUDIT_DIR / f"{title}（{short}）.md"
    duplicate_candidates = [item for item in review.get("duplicate_candidates", []) if (VAULT / item).exists()]
    merge_target = review.get("merge_target", "")
    if merge_target and not (VAULT / merge_target).exists():
        merge_target = ""
    lines = [
        "---",
        "kind: legacy-knowledge-review",
        "status: 待审核",
        f"source_file: {yaml_string(source_file)}",
        f"quality_score: {int(review['quality_score'])}",
        f"recommendation: {yaml_string(review['recommendation'])}",
        f"confidence: {yaml_string(review['confidence'])}",
        f"evidence_status: {yaml_string(review['evidence_status'])}",
        f"merge_target: {yaml_string(merge_target)}",
        f"audited_at: {yaml_string(now_local())}",
        "---",
        "",
        f"# {title}",
        "",
        "## 快速判断",
        "",
        f"**建议：{review['recommendation']}｜质量：{int(review['quality_score'])}/100｜证据：{review['evidence_status']}｜置信度：{review['confidence']}**",
        "",
        review["reason"],
        "",
        "## 保留价值",
        "",
    ]
    lines.extend([f"- {item}" for item in review.get("strengths", [])] or ["- 暂无明确保留价值"])
    lines.extend(["", "## 质量问题", ""])
    lines.extend([f"- {item}" for item in review.get("weaknesses", [])] or ["- 未发现明显问题"])
    lines.extend(["", "## 来源与证据", "", review["evidence_notes"]])
    lines.extend(["", "## 独立复用价值", "", review["reusable_value"]])
    lines.extend(["", "## 疑似重复或可合并内容", ""])
    if duplicate_candidates:
        lines.extend(f"- [[{note_link(item)}]]" for item in duplicate_candidates)
    else:
        lines.append("- 未发现明确重复")
    if merge_target:
        lines.extend(["", f"**建议合并到：** [[{note_link(merge_target)}]]"])
    lines.extend(["", "## 改善建议", ""])
    lines.extend([f"- {item}" for item in review.get("suggested_improvements", [])] or ["- 暂无"])
    lines.extend([
        "", "## 原始知识", "", f"- [[{note_link(source_file)}]]",
        "", "## 人工审核", "",
        "- [ ] 保留为长期知识",
        "- [ ] 合并到建议目标",
        "- [ ] 降级为参考资料",
        "- [ ] 标记为可恢复的待清理项",
        "", "> 只勾选一项。本轮只记录判断，不会自动修改或删除原始知识。", "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run_audit() -> dict[str, Any]:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    existing = existing_sources()
    items = [item for item in target_notes() if item["id"] not in existing]
    if not items:
        return {"created": 0, "total_cards": len(existing), "pending_sources": 0}
    result = run_codex(build_prompt(items))
    by_id = {item["id"] for item in items}
    created = 0
    for review in result.get("reviews", []):
        if review.get("id") not in by_id:
            continue
        write_card(review)
        created += 1
    return {
        "created": created,
        "requested": len(items),
        "total_cards": len(existing) + created,
        "missing_results": len(items) - created,
    }


def status() -> dict[str, Any]:
    targets = target_notes()
    existing = existing_sources()
    return {
        "target_notes": len(targets),
        "audit_cards": len(existing),
        "remaining": len([item for item in targets if item["id"] not in existing]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Legacy knowledge quality audit")
    parser.add_argument("command", choices=["run", "status"])
    args = parser.parse_args()
    print(json.dumps(run_audit() if args.command == "run" else status(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
