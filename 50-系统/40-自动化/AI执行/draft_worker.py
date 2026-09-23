#!/usr/bin/env python3
"""Codex 起草执行器：读成稿包上下文 → codex 只读沙箱起草 → 落盘待审草稿。

安全边界：Codex 永远不直接写 vault。它在只读沙箱里读材料、产出结构化 JSON；
草稿落盘、任务状态回写和 AI 任务队列勾销都由本脚本完成，全程可审计。
产出物 status 固定为 ai-draft——它是待人工审阅的初稿，不是定稿。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
VAULT = Path(os.environ.get("RAYS_BRAIN", str(HERE.parents[2]))).resolve()
INGEST_CONFIG = HERE.parent / "知识采集" / "config.json"
PROMPT_FILE = HERE / "draft-prompt.md"
SCHEMA_FILE = HERE / "draft.schema.json"
DRAFTS_DIR = VAULT / "10-创作/30-文章草稿"
TASKS_PREFIX = "10-创作/20-写作任务/"
QUEUE_FILE = VAULT / "50-系统/40-自动化/AI任务队列.md"
TASK_KINDS = {"writing-task", "content-task", "content-pack"}
INACTIVE_STATUSES = {"published", "cancelled", "completed", "archived", "closed"}
CONTEXT_CHAR_LIMIT = 24_000
CODEX_TIMEOUT = 1_200
# 写作规范直接读 ray-writer skill（安装位是指向仓库的符号链接，永远最新），
# 无头引擎与 /ray 深链共享同一套语气、原型和质量门，只是执行者不同。
WRITER_SKILL_DIR = Path(
    os.environ.get("RAY_WRITER_SKILL_DIR", str(Path.home() / ".claude/skills/ray-writer"))
).expanduser()
WRITER_SPEC_FILES = (
    ("voice.md（表达规则与样本清单，最高优先级）", "references/voice.md"),
    ("quality-gates.md（交稿前自检）", "references/quality-gates.md"),
)


def log(message: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def now_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def rel(path: Path) -> str:
    return path.relative_to(VAULT).as_posix()


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


def yaml_value(value: str) -> str:
    value = str(value)
    if not value or value != value.strip() or re.search(r'[:#"\'\n\[\]{}]', value):
        return json.dumps(value, ensure_ascii=False)
    return value


def update_frontmatter(text: str, updates: dict[str, str]) -> str:
    if not text.startswith("---\n"):
        raise ValueError("目标笔记缺少页首属性")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("目标笔记页首属性不完整")
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


def wikilink_body(value: str) -> str:
    match = re.match(r"\s*\[\[([^\]|#]+)", value or "")
    return match.group(1).strip() if match else ""


def resolve_wikilink(target: str) -> Path | None:
    target = target.strip().strip("/")
    if not target:
        return None
    for suffix in ("", ".md"):
        candidate = VAULT / (target + suffix)
        if candidate.is_file():
            return candidate
    stem = target.split("/")[-1].casefold()
    matches = [
        path
        for path in VAULT.rglob("*.md")
        if path.stem.casefold() == stem
        and not any(part in {".git", ".obsidian", "node_modules"} for part in path.parts)
    ]
    return min(matches, key=lambda p: len(rel(p))) if matches else None


def sanitize_filename(value: str, limit: int = 48) -> str:
    value = re.sub(r"[\\/:*?\"<>|\n\r]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:limit].strip(" .") or "未命名草稿"


def load_config() -> dict:
    try:
        return json.loads(INGEST_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def clip(text: str, limit: int = CONTEXT_CHAR_LIMIT) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n（材料超长，此处截断）"


def gather_context(task_path: Path) -> tuple[dict[str, str], str]:
    """返回 (task_meta, 材料区块文本)。材料只来自成稿包和它指向的链接。"""
    task_text = task_path.read_text(encoding="utf-8", errors="replace")
    meta = parse_frontmatter(task_text)
    blocks = [f"## 成稿包（本次写作的唯一任务书）\n路径：{rel(task_path)}\n\n{clip(task_text)}"]
    seen = {task_path}
    for label, key in (
        ("候选选题", "candidate"),
        ("调研 / 可调用知识", "research"),
        ("源头材料", "source_file"),
    ):
        target = wikilink_body(meta.get(key, ""))
        if not target:
            continue
        resolved = resolve_wikilink(target)
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        blocks.append(f"## {label}\n路径：{rel(resolved)}\n\n{clip(text)}")
    return meta, "\n\n---\n\n".join(blocks)


def writer_specs() -> str:
    """从 ray-writer skill 取真实写作规范：固定原则 + 表达规则 + 交稿前自检。

    skill 未安装时返回空串，提示词退回自身的底线纪律——功能降级但不失败。
    """
    blocks: list[str] = []
    skill_md = WRITER_SKILL_DIR / "SKILL.md"
    try:
        text = skill_md.read_text(encoding="utf-8")
        match = re.search(r"^## 固定原则\n(.*?)(?=^## )", text, flags=re.M | re.S)
        if match:
            blocks.append("### 固定原则（ray-writer）\n\n" + match.group(1).strip())
    except OSError:
        pass
    for label, relative_path in WRITER_SPEC_FILES:
        try:
            content = (WRITER_SKILL_DIR / relative_path).read_text(encoding="utf-8")
        except OSError:
            continue
        blocks.append(f"### {label}\n\n{clip(content, 9_000)}")
    return "\n\n".join(blocks)


def style_sample() -> str:
    """取最近一篇已发布长文当语气锚点。规则描述语气，样本才定得住语域。"""
    published = VAULT / "40-发布/10-X长文"

    def mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    try:
        articles = sorted(published.glob("*.md"), key=mtime, reverse=True)
    except OSError:
        articles = []
    for path in articles:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        body = text
        if body.startswith("---\n"):
            end = body.find("\n---\n", 4)
            if end >= 0:
                body = body[end + 5 :]
        body = body.strip()
        # 太短的不是完整长文（索引页或存根），锚不住语域
        if len(body) < 2_000:
            continue
        return f"样本：{rel(path)}\n\n{clip(body, 6_000)}"
    return "（暂无已发布文章可作样本，按写作规范里的语气要求执行）"


def build_prompt(task_path: Path, meta: dict[str, str], materials: str) -> str:
    template = PROMPT_FILE.read_text(encoding="utf-8")
    specs = writer_specs()
    if not specs:
        specs = "（ray-writer 规范文件不可用，仅按上方硬性纪律执行）"
    return (
        template.replace("{{task_rel}}", rel(task_path))
        .replace("{{selected_angle}}", meta.get("selected_angle", "见成稿包"))
        .replace("{{platform}}", meta.get("platform", "X Article + 微信公众号"))
        .replace("{{writer_specs}}", specs)
        .replace("{{style_sample}}", style_sample())
        .replace("{{materials}}", materials)
    )


def run_codex(prompt: str, config: dict) -> dict:
    # RAY_CODEX_PATH/RAY_CODEX_MODEL 供测试与演示库注入桩实现，生产走知识采集 config.json
    codex = os.environ.get("RAY_CODEX_PATH") or config.get("codex_path", "codex")
    with tempfile.NamedTemporaryFile(prefix="ai-draft-", suffix=".json", delete=False) as f:
        output_path = Path(f.name)
    cmd = [
        codex, "exec", "--ephemeral", "--sandbox", "read-only",
        "--skip-git-repo-check", "-C", str(VAULT),
        "--output-schema", str(SCHEMA_FILE),
        "-o", str(output_path), "-",
    ]
    model = os.environ.get("RAY_CODEX_MODEL") or config.get("codex_model")
    if model:
        cmd[2:2] = ["--model", model]
    try:
        proc = subprocess.run(
            cmd, input=prompt, text=True, capture_output=True, timeout=CODEX_TIMEOUT
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[-4000:] or proc.stdout[-4000:] or "codex failed")
        return json.loads(output_path.read_text(encoding="utf-8"))
    finally:
        output_path.unlink(missing_ok=True)


def unique_path(directory: Path, name: str) -> Path:
    path = directory / name
    counter = 2
    while path.exists():
        path = directory / f"{Path(name).stem}-{counter}.md"
        counter += 1
    return path


def write_draft(task_path: Path, meta: dict[str, str], result: dict, model: str) -> Path:
    title = str(result.get("title", "")).strip() or task_path.stem
    body = str(result.get("body_markdown", "")).strip()
    if not body:
        raise RuntimeError("codex 没有产出正文")
    summary = str(result.get("summary", "")).strip()
    open_questions = [str(q).strip() for q in result.get("open_questions", []) if str(q).strip()]
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    path = unique_path(
        DRAFTS_DIR, f"{datetime.now().strftime('%Y-%m-%d')}-{sanitize_filename(title)}.md"
    )
    lines = [
        "---",
        "kind: draft",
        "status: ai-draft",
        f"title: {yaml_value(title)}",
        f"created_at: {yaml_value(now_local())}",
        f"generated_by: {yaml_value('codex-' + (model or 'default'))}",
        f"content_pack: {yaml_value('[[' + rel(task_path)[:-3] + ']]')}",
        "publish_exclude_sections: 待确认",
    ]
    for key in ("topic_id", "content_id", "candidate", "research", "source_url", "platform"):
        if meta.get(key, "").strip():
            lines.append(f"{key}: {yaml_value(meta[key])}")
    if summary:
        lines.append(f"ai_summary: {yaml_value(summary[:300])}")
    lines.append("---")
    content = "\n".join(lines) + f"\n\n# {title}\n\n{body}\n"
    if open_questions:
        content += "\n## 待确认\n\n" + "\n".join(f"- [ ] {q}" for q in open_questions) + "\n"
    path.write_text(content, encoding="utf-8")
    return path


def update_task(task_path: Path, draft_path: Path) -> None:
    text = task_path.read_text(encoding="utf-8", errors="replace")
    meta = parse_frontmatter(text)
    updates: dict[str, str] = {"updated_at": now_local()}
    if meta.get("status", "").strip() in {"active", "researching", "drafting"}:
        updates["status"] = "drafted"
    if not meta.get("draft", "").strip():
        updates["draft"] = f"[[{rel(draft_path)[:-3]}]]"
    task_path.write_text(update_frontmatter(text, updates), encoding="utf-8")


def tick_queue(task_path: Path, draft_path: Path) -> bool:
    if not QUEUE_FILE.exists():
        return False
    marker = f"[[{rel(task_path)[:-3]}]]"
    lines = QUEUE_FILE.read_text(encoding="utf-8").splitlines()
    changed = False
    for index, line in enumerate(lines):
        if line.startswith("- [ ] ") and marker in line:
            lines[index] = "- [x]" + line[5:] + f" → [[{rel(draft_path)[:-3]}]]"
            changed = True
            break
    if changed:
        QUEUE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="用 Codex 为一个写作任务起草待审初稿")
    parser.add_argument("task", help="写作任务的 vault 相对路径")
    args = parser.parse_args()
    task_path = (VAULT / args.task).resolve()
    try:
        task_rel = rel(task_path)
    except ValueError:
        raise SystemExit("任务路径必须在 vault 内")
    if not task_rel.startswith(TASKS_PREFIX) or not task_path.is_file():
        raise SystemExit("目标必须是写作任务目录里的 Markdown 文件")
    meta = parse_frontmatter(task_path.read_text(encoding="utf-8", errors="replace"))
    if meta.get("kind") not in TASK_KINDS:
        raise SystemExit("目标不是写作任务")
    if meta.get("status", "").strip() in INACTIVE_STATUSES:
        raise SystemExit(f"任务状态是 {meta.get('status')}，不需要起草")

    config = load_config()
    log(f"起草开始：{task_rel}")
    meta, materials = gather_context(task_path)
    prompt = build_prompt(task_path, meta, materials)
    log(f"上下文就绪（{len(materials)} 字符），调用 codex …")
    result = run_codex(prompt, config)
    draft = write_draft(task_path, meta, result, str(config.get("codex_model", "")))
    update_task(task_path, draft)
    ticked = tick_queue(task_path, draft)
    log(f"草稿已落盘：{rel(draft)}（队列勾销：{'是' if ticked else '无对应项'}）")
    print(json.dumps({"task": task_rel, "draft": rel(draft)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
