#!/usr/bin/env python3
"""每周整理主题地图的“新增知识”暂存区。

本模块只生成可审核建议。地图中的知识只有在工作台明确勾选后才会移动；
移动时先确认目标位置已经保留关系，再从“新增知识”暂存区移除。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from topic_map_candidates import (
    is_theme_map,
    knowledge_inventory,
    list_meta,
    normalized,
    scalar_meta,
    section_text,
    wiki_targets,
)


MAP_DIR_REL = Path("20-知识/70-知识地图")
WORKBENCH_REL = Path("00-入口/10-工作台/知识地图整理.md")
WEEKLY_DIR_REL = Path("00-入口/20-日报/知识地图整理")
LATEST_REPORT_NAME = "最新知识地图整理.md"
ACTION_LABELS = {
    "route-child": "确认转入已有子地图",
    "promote-core": "确认升级为核心知识",
    "supplement": "确认归入补充知识",
}
CATEGORY_LABELS = {
    "route_child": "建议转入已有子地图",
    "promote_core": "建议升级为核心知识",
    "supplement": "建议归入补充知识",
    "new_map_candidate": "等待新地图判断",
    "observe": "本周继续观察",
}
ACTION_MARKER = re.compile(
    r"<!--\s*map-digest-action:(?P<id>digest-[a-f0-9]{16})\s*-->"
    r"(?P<body>.*?)(?=<!--\s*map-digest-action:|\Z)",
    flags=re.S,
)


def now_local() -> datetime:
    return datetime.now().astimezone()


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    temporary.replace(path)


def atomic_json(path: Path, data: Any) -> None:
    atomic_text(
        path,
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
    )


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def replace_section(text: str, heading: str, body: str) -> str:
    block = f"## {heading}\n\n{body.strip()}".rstrip()
    pattern = re.compile(
        rf"(?ms)^##\s+{re.escape(heading)}\s*\n.*?(?=^##\s+|\Z)",
    )
    if pattern.search(text):
        return pattern.sub(block + "\n\n", text, count=1).rstrip() + "\n"
    return text.rstrip() + "\n\n" + block + "\n"


def ordered_wiki_targets(text: str) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"\[\[([^\]]+)\]\]", text):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip().removesuffix(".md")
        title = Path(target).name
        if title and title not in seen:
            seen.add(title)
            targets.append(title)
    return targets


def section_has_link(text: str, heading: str, title: str) -> bool:
    return title in wiki_targets(section_text(text, heading))


def add_section_link(text: str, heading: str, title: str) -> str:
    current = section_text(text, heading)
    if title in wiki_targets(current):
        return text
    lines = current.splitlines() if current else []
    lines.append(f"- [[{title}]]")
    return replace_section(text, heading, "\n".join(lines))


def remove_section_link(text: str, heading: str, title: str) -> str:
    current = section_text(text, heading)
    if not current:
        return text
    kept: list[str] = []
    for line in current.splitlines():
        if (
            re.match(r"^\s*[-*]\s+", line)
            and title in wiki_targets(line)
        ):
            continue
        kept.append(line)
    return replace_section(text, heading, "\n".join(kept))


def touch_map(text: str, today: date) -> str:
    replacement = f"updated_at: {today.isoformat()}"
    if re.search(r"(?m)^updated_at:\s*.*$", text):
        return re.sub(
            r"(?m)^updated_at:\s*.*$",
            replacement,
            text,
            count=1,
        )
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            return text[:end] + "\n" + replacement + text[end:]
    return text


def note_date(text: str) -> date | None:
    value = scalar_meta(text, "created_at") or scalar_meta(text, "updated_at")
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def action_id(
    source_map: str,
    note_title: str,
    action: str,
    target_map: str = "",
) -> str:
    raw = "\n".join((source_map, note_title, action, target_map))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"digest-{digest}"


def topic_candidate_members(topic_state: dict[str, Any]) -> dict[str, list[str]]:
    by_note: dict[str, list[str]] = defaultdict(list)
    for candidate in topic_state.get("candidates", []):
        if not isinstance(candidate, dict):
            continue
        name = str(candidate.get("name", "")).strip()
        if not name:
            continue
        for item in candidate.get("supporting_notes", []):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            if title and name not in by_note[title]:
                by_note[title].append(name)
    return by_note


def related_theme_maps(inventory: dict[str, Any]) -> dict[str, set[str]]:
    maps: dict[str, dict[str, Any]] = inventory["maps"]
    related: dict[str, set[str]] = {}
    for name, record in maps.items():
        related[name] = wiki_targets(section_text(str(record["text"]), "相关主题")) & set(maps)
    return related


def choose_child_map(
    source_map: str,
    note: dict[str, Any],
    inventory: dict[str, Any],
    related: dict[str, set[str]],
) -> tuple[str, str] | None:
    maps: dict[str, dict[str, Any]] = inventory["maps"]
    choices: list[tuple[int, int, str, str]] = []
    note_topics = {normalized(value) for value in note.get("topics", [])}
    for child_name, child in maps.items():
        if (
            child_name == source_map
            or source_map not in related.get(child_name, set())
            or scalar_meta(str(child["text"]), "origin") != "topic-map-candidate"
        ):
            continue
        if note["title"] in child["members"]:
            choices.append(
                (
                    0,
                    int(child.get("member_count", 0)),
                    child_name,
                    "这条知识已经出现在该子地图中",
                )
            )
        elif normalized(child_name) in note_topics:
            choices.append(
                (
                    1,
                    int(child.get("member_count", 0)),
                    child_name,
                    "资料主题与该子地图名称一致",
                )
            )
    if not choices:
        return None
    _, _, child_name, reason = sorted(choices)[0]
    return child_name, reason


def make_action(
    source_map: str,
    note: dict[str, Any],
    action: str,
    reason: str,
    *,
    target_map: str = "",
    target_section: str = "",
    score: int = 0,
    confidence: str = "需判断",
    auto_selected: bool = False,
) -> dict[str, Any]:
    return {
        "id": action_id(source_map, str(note["title"]), action, target_map),
        "action": action,
        "source_map": source_map,
        "note_title": str(note["title"]),
        "note_path": str(note["path"]),
        "target_map": target_map,
        "target_section": target_section,
        "reason": reason,
        "score": score,
        "confidence": confidence,
        "auto_selected": auto_selected,
    }


def analyze_map_digest(
    vault: Path,
    topic_map_state: dict[str, Any] | None = None,
    *,
    today: date | None = None,
    first_seen: dict[str, str] | None = None,
) -> dict[str, Any]:
    today = today or now_local().date()
    topic_map_state = topic_map_state or {}
    first_seen = dict(first_seen or {})
    active_seen: dict[str, str] = {}
    inventory = knowledge_inventory(vault)
    notes: dict[str, dict[str, Any]] = inventory["notes"]
    maps: dict[str, dict[str, Any]] = inventory["maps"]
    related = related_theme_maps(inventory)
    candidate_members = topic_candidate_members(topic_map_state)
    for title, note in notes.items():
        for candidate in topic_map_state.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            name = str(candidate.get("name", "")).strip()
            if normalized(name) in {
                normalized(value) for value in note.get("topics", [])
            } and name not in candidate_members[title]:
                candidate_members[title].append(name)

    backlinks: Counter[str] = Counter()
    for note in notes.values():
        backlinks.update(set(note.get("links", set())) & set(notes))

    map_rows: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    category_totals: Counter[str] = Counter()
    unresolved_total = 0
    for map_name in sorted(maps):
        record = maps[map_name]
        staged_titles = ordered_wiki_targets(
            section_text(str(record["text"]), "新增知识")
        )
        if not staged_titles:
            continue
        row: dict[str, Any] = {
            "name": map_name,
            "new_count": len(staged_titles),
            "route_child": [],
            "promote_core": [],
            "supplement": [],
            "new_map_candidate": [],
            "observe": [],
            "unresolved": [],
        }
        core_options: list[tuple[int, str, dict[str, Any]]] = []
        deferred: list[dict[str, Any]] = []
        has_core_section = bool(
            re.search(r"(?m)^##\s+核心知识\s*$", str(record["text"]))
        )
        for title in staged_titles:
            note = notes.get(title)
            if note is None:
                row["unresolved"].append(title)
                unresolved_total += 1
                continue
            seen_key = f"{map_name}\n{title}"
            seen_value = first_seen.get(seen_key)
            if not seen_value:
                initial_date = note_date(str(note["text"])) or today
                seen_value = initial_date.isoformat()
            active_seen[seen_key] = seen_value
            child = choose_child_map(map_name, note, inventory, related)
            if child:
                child_name, reason = child
                high_confidence = reason == "这条知识已经出现在该子地图中"
                action = make_action(
                    map_name,
                    note,
                    "route-child",
                    reason,
                    target_map=child_name,
                    target_section="新增知识",
                    confidence="高" if high_confidence else "中",
                    auto_selected=high_confidence,
                )
                row["route_child"].append(action)
                actions.append(action)
                category_totals["route_child"] += 1
                continue
            candidates = candidate_members.get(title, [])
            if candidates:
                item = {
                    "note_title": title,
                    "note_path": str(note["path"]),
                    "candidates": candidates,
                }
                row["new_map_candidate"].append(item)
                category_totals["new_map_candidate"] += 1
                continue
            try:
                first_seen_date = date.fromisoformat(seen_value[:10])
            except ValueError:
                first_seen_date = today
                active_seen[seen_key] = today.isoformat()
            if first_seen_date >= today - timedelta(days=6):
                row["observe"].append(
                    {
                        "note_title": title,
                        "note_path": str(note["path"]),
                        "reason": "进入暂存区不足 7 天，先等本周的连接和反馈长出来",
                    }
                )
                category_totals["observe"] += 1
                continue
            score = (
                backlinks[title] * 2
                + max(0, len(note.get("memberships", set())) - 1)
                + min(3, len(note.get("sources", set())))
            )
            if has_core_section and score >= 5:
                core_options.append((score, title, note))
            else:
                deferred.append(note)

        promoted_titles: set[str] = set()
        for score, title, note in sorted(
            core_options,
            key=lambda item: (-item[0], item[1]),
        )[:5]:
            action = make_action(
                map_name,
                note,
                "promote-core",
                f"在知识库中有 {backlinks[title]} 条知识引用，且已进入 "
                f"{len(note.get('memberships', set()))} 张主题地图",
                target_section="核心知识",
                score=score,
            )
            row["promote_core"].append(action)
            actions.append(action)
            promoted_titles.add(title)
            category_totals["promote_core"] += 1
        deferred.extend(
            note
            for _, title, note in core_options
            if title not in promoted_titles
        )
        for note in sorted(deferred, key=lambda item: str(item["title"])):
            action = make_action(
                map_name,
                note,
                "supplement",
                "已经属于这张地图，但目前没有足够证据进入核心或独立子地图",
                target_section="补充知识",
            )
            row["supplement"].append(action)
            actions.append(action)
            category_totals["supplement"] += 1
        map_rows.append(row)

    summary = {
        "theme_map_count": len(maps),
        "maps_with_new_knowledge": len(map_rows),
        "new_knowledge_count": sum(row["new_count"] for row in map_rows),
        "pending_action_count": len(actions),
        "route_child_count": category_totals["route_child"],
        "promote_core_count": category_totals["promote_core"],
        "supplement_count": category_totals["supplement"],
        "new_map_candidate_count": category_totals["new_map_candidate"],
        "observe_count": category_totals["observe"],
        "unresolved_count": unresolved_total,
        "auto_selected_count": sum(
            bool(item.get("auto_selected")) for item in actions
        ),
        "human_decision_count": sum(
            not bool(item.get("auto_selected")) for item in actions
        ),
    }
    return {
        "actions": actions,
        "maps": map_rows,
        "summary": summary,
        "first_seen": active_seen,
    }


def checked_action_ids(text: str) -> list[str]:
    checked: list[str] = []
    for match in ACTION_MARKER.finditer(text):
        body = match.group("body")
        if any(
            re.search(
                rf"(?m)^\s*-\s+\[[xX]\]\s+{re.escape(label)}\s*$",
                body,
            )
            for label in ACTION_LABELS.values()
        ):
            checked.append(match.group("id"))
    return list(dict.fromkeys(checked))


def apply_action(vault: Path, action: dict[str, Any], today: date) -> str:
    source_path = vault / MAP_DIR_REL / f"{action['source_map']}.md"
    if not source_path.is_file():
        raise ValueError(f"找不到来源地图：{action['source_map']}")
    source_text = source_path.read_text(encoding="utf-8", errors="replace")
    if not is_theme_map(source_text):
        raise ValueError(f"来源不是主题地图：{action['source_map']}")
    title = str(action["note_title"])
    if not section_has_link(source_text, "新增知识", title):
        return "already-applied"

    action_name = str(action["action"])
    if action_name == "route-child":
        target_name = str(action["target_map"])
        target_path = vault / MAP_DIR_REL / f"{target_name}.md"
        if not target_path.is_file():
            raise ValueError(f"找不到目标子地图：{target_name}")
        target_text = target_path.read_text(encoding="utf-8", errors="replace")
        if not is_theme_map(target_text):
            raise ValueError(f"目标不是主题地图：{target_name}")
        if title not in wiki_targets(target_text):
            target_text = add_section_link(target_text, "新增知识", title)
            target_text = touch_map(target_text, today)
            atomic_text(target_path, target_text)
        verified = target_path.read_text(encoding="utf-8", errors="replace")
        if title not in wiki_targets(verified):
            raise ValueError(f"目标子地图没有保留知识关系：{target_name} / {title}")
        source_text = remove_section_link(source_text, "新增知识", title)
        source_text = add_section_link(source_text, "相关主题", target_name)
        if not section_has_link(source_text, "相关主题", target_name):
            raise ValueError(f"父地图没有保留子地图关系：{action['source_map']} / {target_name}")
    elif action_name == "promote-core":
        if not re.search(r"(?m)^##\s+核心知识\s*$", source_text):
            raise ValueError(f"地图没有核心知识区：{action['source_map']}")
        source_text = remove_section_link(source_text, "新增知识", title)
        source_text = add_section_link(source_text, "核心知识", title)
        if not section_has_link(source_text, "核心知识", title):
            raise ValueError(f"核心知识区没有保留关系：{action['source_map']} / {title}")
    elif action_name == "supplement":
        source_text = remove_section_link(source_text, "新增知识", title)
        source_text = add_section_link(source_text, "补充知识", title)
        if not section_has_link(source_text, "补充知识", title):
            raise ValueError(f"补充知识区没有保留关系：{action['source_map']} / {title}")
    else:
        raise ValueError("不支持的地图整理动作")

    source_text = touch_map(source_text, today)
    atomic_text(source_path, source_text)
    return "applied"


def dashboard_markdown(state: dict[str, Any]) -> str:
    summary = state.get("summary", {})
    generated = str(state.get("generated_at", ""))
    lines = [
        "---",
        "kind: map-digest-dashboard",
        "status: active",
        f'generated_at: "{generated}"',
        "---",
        "",
        "# 知识地图整理",
        "",
        "> 每周日生成一次建议。已经真实存在于子地图中的高置信度关系会自动勾选，"
        "下一轮半小时任务再执行；其余建议留给你在 Obsidian 中判断。"
        "浏览器工作台用于阅读，未勾选的内容保持原样。",
        "",
        "## 本周总览",
        "",
        f"- 有 {summary.get('maps_with_new_knowledge', 0)} 张地图包含“新增知识”，"
        f"共 {summary.get('new_knowledge_count', 0)} 条。",
        f"- 可确认整理：{summary.get('pending_action_count', 0)} 条；"
        f"转入已有子地图 {summary.get('route_child_count', 0)} 条，"
        f"升级为核心 {summary.get('promote_core_count', 0)} 条，"
        f"归入补充 {summary.get('supplement_count', 0)} 条。",
        f"- 等待新地图判断 {summary.get('new_map_candidate_count', 0)} 条；"
        f"本周继续观察 {summary.get('observe_count', 0)} 条。",
        f"- 高置信度已自动勾选 {summary.get('auto_selected_count', 0)} 条；"
        f"仍需你逐条判断 {summary.get('human_decision_count', 0)} 条。",
        "",
    ]
    if not state.get("maps"):
        lines.extend(["- 当前没有需要整理的“新增知识”。", ""])
    for map_row in state.get("maps", []):
        lines.extend(
            [
                f"## {map_row['name']}",
                "",
                f"当前暂存 {map_row['new_count']} 条。",
                "",
            ]
        )
        for key in (
            "route_child",
            "promote_core",
            "supplement",
            "new_map_candidate",
            "observe",
        ):
            items = map_row.get(key, [])
            if not items:
                continue
            lines.extend([f"### {CATEGORY_LABELS[key]}", ""])
            if key in {"route_child", "promote_core", "supplement"}:
                for item in items:
                    target = (
                        f" → [[{item['target_map']}]]"
                        if item.get("target_map")
                        else ""
                    )
                    checked = "x" if item.get("auto_selected") else " "
                    confidence = (
                        "高置信度，已自动勾选"
                        if item.get("auto_selected")
                        else f"{item.get('confidence', '需判断')}置信度，等待你的判断"
                    )
                    lines.extend(
                        [
                            f"<!-- map-digest-action:{item['id']} -->",
                            f"- [[{item['note_title']}]]{target}",
                            f"  - 原因：{item['reason']}",
                            f"  - 判断：{confidence}",
                            f"  - [{checked}] {ACTION_LABELS[item['action']]}",
                            "",
                        ]
                    )
            elif key == "new_map_candidate":
                for item in items:
                    candidates = "、".join(
                        f"[[00-入口/10-工作台/主题地图候选#{name}|{name}]]"
                        for name in item["candidates"]
                    )
                    lines.append(f"- [[{item['note_title']}]]：{candidates}")
                lines.append("")
            else:
                for item in items:
                    lines.append(f"- [[{item['note_title']}]]：{item['reason']}")
                lines.append("")
        unresolved = map_row.get("unresolved", [])
        if unresolved:
            lines.extend(
                [
                    "### 需要修复的链接",
                    "",
                    *[f"- [[{title}]]：找不到对应长期知识。" for title in unresolved],
                    "",
                ]
            )
    errors = state.get("action_errors", [])
    if errors:
        lines.extend(["## 本轮未执行", "", *[f"- {value}" for value in errors], ""])
    lines.extend(
        [
            "## 处理口径",
            "",
            "- “新增知识”只是地图的暂存区，不代表知识没有被收录。",
            "- 已有子地图能承接的内容，确认后转入子地图；父地图仍保留对子地图的关系。",
            "- 引用和跨地图连接较强的内容，才会建议进入核心知识；每张地图每周最多建议 5 条。",
            "- 其余稳定归属但尚未形成核心或子地图的内容，确认后进入补充知识。",
            "- 进入暂存区不足 7 天的内容先观察；正在形成新地图的内容回到主题地图候选判断。",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def weekly_report_markdown(state: dict[str, Any], week: str) -> str:
    summary = state.get("summary", {})
    lines = [
        "---",
        "kind: map-digest-weekly",
        f"week: {week}",
        f'generated_at: "{state.get("generated_at", "")}"',
        "---",
        "",
        f"# 知识地图整理周报 · {week}",
        "",
        "## 结论",
        "",
        f"本周检查了 {summary.get('theme_map_count', 0)} 张真正的主题地图；"
        f"{summary.get('maps_with_new_knowledge', 0)} 张存在待整理内容，"
        f"合计 {summary.get('new_knowledge_count', 0)} 条。",
        "",
        f"- 建议转入已有子地图：{summary.get('route_child_count', 0)} 条",
        f"- 建议升级为核心：{summary.get('promote_core_count', 0)} 条",
        f"- 建议归入补充：{summary.get('supplement_count', 0)} 条",
        f"- 等待新地图判断：{summary.get('new_map_candidate_count', 0)} 条",
        f"- 本周继续观察：{summary.get('observe_count', 0)} 条",
        f"- 已自动勾选的高置信度建议：{summary.get('auto_selected_count', 0)} 条",
        f"- 仍需人工逐条判断：{summary.get('human_decision_count', 0)} 条",
        "",
        "## 各地图",
        "",
    ]
    for row in state.get("maps", []):
        lines.append(
            f"- [[{row['name']}]]：暂存 {row['new_count']} 条；"
            f"转子地图 {len(row['route_child'])}，升核心 {len(row['promote_core'])}，"
            f"归补充 {len(row['supplement'])}，等新地图 {len(row['new_map_candidate'])}，"
            f"观察 {len(row['observe'])}。"
        )
    lines.extend(
        [
            "",
            "## 下一步",
            "",
            "- 在 [[00-入口/10-工作台/知识地图整理|知识地图整理]] 判断尚未勾选的建议。",
            "- 高置信度建议已经自动勾选，和人工勾选项一起由下一轮半小时任务执行。",
            "",
        ]
    )
    return "\n".join(lines)


def write_outputs(
    vault: Path,
    state_file: Path,
    dashboard_note: Path,
    weekly_dir: Path,
    state: dict[str, Any],
    now: datetime,
) -> None:
    atomic_json(state_file, state)
    atomic_text(dashboard_note, dashboard_markdown(state))
    iso_year, iso_week, _ = now.isocalendar()
    week = f"{iso_year}-W{iso_week:02d}"
    report_path = weekly_dir / f"{week}.md"
    atomic_text(report_path, weekly_report_markdown(state, week))
    atomic_text(
        weekly_dir / LATEST_REPORT_NAME,
        "---\nkind: dashboard\n---\n\n# 最新知识地图整理\n\n"
        f"![[00-入口/20-日报/知识地图整理/{week}]]\n",
    )


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "generated_at": "",
        "last_weekly_generated_at": "",
        "actions": [],
        "maps": [],
        "summary": {},
        "first_seen": {},
        "decisions": {},
        "action_errors": [],
    }


def apply_checked_actions(
    vault: Path,
    state: dict[str, Any],
    dashboard_note: Path,
    now: datetime,
) -> dict[str, Any]:
    if not dashboard_note.exists():
        return {"applied": 0, "already_applied": 0, "errors": []}
    checked = checked_action_ids(
        dashboard_note.read_text(encoding="utf-8", errors="replace")
    )
    actions = {
        str(item.get("id")): item
        for item in state.get("actions", [])
        if isinstance(item, dict) and item.get("id")
    }
    decisions = state.setdefault("decisions", {})
    applied = 0
    already_applied = 0
    errors: list[str] = []
    for item_id in checked:
        action = actions.get(item_id)
        if action is None or item_id in decisions:
            continue
        try:
            result = apply_action(vault, action, now.date())
        except (OSError, ValueError) as exc:
            errors.append(f"{action.get('note_title', item_id)}：{exc}")
            continue
        decisions[item_id] = {
            "status": result,
            "decided_at": now.isoformat(timespec="seconds"),
            "action": action.get("action"),
            "source_map": action.get("source_map"),
            "target_map": action.get("target_map"),
            "note_title": action.get("note_title"),
        }
        if result == "applied":
            applied += 1
        else:
            already_applied += 1
    return {
        "applied": applied,
        "already_applied": already_applied,
        "errors": errors,
    }


def rebuild_state(
    vault: Path,
    state: dict[str, Any],
    topic_map_state_file: Path,
    now: datetime,
    *,
    weekly: bool,
    action_errors: list[str] | None = None,
) -> dict[str, Any]:
    analysis = analyze_map_digest(
        vault,
        load_json(topic_map_state_file, {}),
        today=now.date(),
        first_seen=state.get("first_seen", {}),
    )
    state.update(
        {
            "version": 1,
            "generated_at": now.isoformat(timespec="seconds"),
            "actions": analysis["actions"],
            "maps": analysis["maps"],
            "summary": analysis["summary"],
            "first_seen": analysis["first_seen"],
            "action_errors": action_errors or [],
        }
    )
    if weekly:
        state["last_weekly_generated_at"] = now.isoformat(timespec="seconds")
    return state


def refresh_weekly_map_digest(
    vault: Path,
    state_file: Path,
    dashboard_note: Path | None = None,
    weekly_dir: Path | None = None,
    topic_map_state_file: Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or now_local()
    dashboard_note = dashboard_note or vault / WORKBENCH_REL
    weekly_dir = weekly_dir or vault / WEEKLY_DIR_REL
    topic_map_state_file = topic_map_state_file or state_file.with_name(
        "topic-map-candidates.json"
    )
    lock_path = state_file.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = load_json(state_file, default_state())
        if not isinstance(state, dict):
            state = default_state()
        result = apply_checked_actions(vault, state, dashboard_note, now)
        state = rebuild_state(
            vault,
            state,
            topic_map_state_file,
            now,
            weekly=True,
            action_errors=result["errors"],
        )
        write_outputs(vault, state_file, dashboard_note, weekly_dir, state, now)
        return {**result, "state": state, "summary": state["summary"]}


def process_map_digest_actions(
    vault: Path,
    state_file: Path,
    dashboard_note: Path | None = None,
    weekly_dir: Path | None = None,
    topic_map_state_file: Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or now_local()
    dashboard_note = dashboard_note or vault / WORKBENCH_REL
    weekly_dir = weekly_dir or vault / WEEKLY_DIR_REL
    topic_map_state_file = topic_map_state_file or state_file.with_name(
        "topic-map-candidates.json"
    )
    lock_path = state_file.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = load_json(state_file, default_state())
        if not isinstance(state, dict):
            state = default_state()
        result = apply_checked_actions(vault, state, dashboard_note, now)
        if not result["applied"] and not result["already_applied"] and not result["errors"]:
            return {**result, "summary": state.get("summary", {})}
        state = rebuild_state(
            vault,
            state,
            topic_map_state_file,
            now,
            weekly=False,
            action_errors=result["errors"],
        )
        write_outputs(vault, state_file, dashboard_note, weekly_dir, state, now)
        return {**result, "state": state, "summary": state["summary"]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Ray's Brain 每周知识地图整理")
    parser.add_argument("command", choices=["weekly", "apply"])
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    vault = Path(os.environ.get("RAYS_BRAIN", str(here.parents[2]))).resolve()
    state_home = Path(
        os.environ.get(
            "RAYS_BRAIN_STATE",
            str(Path.home() / ".local/state/rays-brain"),
        )
    )
    values = (
        vault,
        state_home / "map-digest.json",
        vault / WORKBENCH_REL,
        vault / WEEKLY_DIR_REL,
        state_home / "topic-map-candidates.json",
    )
    if args.command == "weekly":
        result = refresh_weekly_map_digest(*values)
    else:
        result = process_map_digest_actions(*values)
    print(
        json.dumps(
            {
                "applied": result.get("applied", 0),
                "already_applied": result.get("already_applied", 0),
                "errors": result.get("errors", []),
                "summary": result.get("summary", {}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
