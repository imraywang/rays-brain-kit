#!/usr/bin/env python3
"""从现有长期知识中发现主题地图候选。

候选发现只读取 Markdown 中已经存在的主题、来源和知识连接。它不会自行
创建、拆分或合并地图；只有明确的人工动作才会建立新地图。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import tempfile
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


MAP_DIR_REL = Path("20-知识/70-知识地图")
MAP_INDEX_REL = MAP_DIR_REL / "知识地图索引.md"
CANDIDATE_NOTE_REL = Path("00-入口/10-工作台/主题地图候选.md")
REVIEW_ROOTS = (
    Path("10-创作/10-灵感/10-待评估/剪藏复核"),
    Path("90-归档/20-审核记录/剪藏复核"),
)
NON_TOPIC_KINDS = {"map", "guide", "entity"}
TERMINAL_STATUSES = {"archived", "已归档", "待清理", "cleanup"}
GENERIC_TOPICS = {
    "ai",
    "人工智能",
    "agent",
    "ai agent",
    "llm",
    "大模型",
    "模型",
    "产品",
    "方法",
    "工具",
    "科技",
    "商业",
    "创业",
    "组织",
}
GENERIC_TOPIC_PARTS = {
    "ai",
    "人工智能",
    "agent",
    "aiagent",
    "llm",
    "大模型",
    "模型",
    "产品",
    "系统",
    "工作流",
    "决策",
    "组织",
    "方法",
    "工具",
    "科技",
}
ACTION_LABELS = {
    "approve": "建立这张主题地图",
    "approve_merge": "确认需要人工合并",
    "watch": "继续观察 14 天",
    "dismiss": "忽略，除非证据明显增长",
}


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


def frontmatter_block(text: str) -> str:
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---\n", 4)
    return text[4:end] if end >= 0 else ""


def scalar_meta(text: str, key: str) -> str:
    block = frontmatter_block(text)
    match = re.search(
        rf"(?m)^{re.escape(key)}:\s*(.*?)\s*$",
        block,
    )
    if not match:
        return ""
    value = match.group(1).strip()
    if value in {"", "[]"}:
        return ""
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in {'"', "'"}
    ):
        value = value[1:-1]
    return value.strip()


def list_meta(text: str, key: str) -> list[str]:
    block = frontmatter_block(text)
    inline = re.search(
        rf"(?m)^{re.escape(key)}:\s*(\[[^\n]*\])\s*$",
        block,
    )
    if inline:
        try:
            values = json.loads(inline.group(1))
        except json.JSONDecodeError:
            values = []
        if isinstance(values, list):
            return [str(value).strip() for value in values if str(value).strip()]
    header = re.search(rf"(?m)^{re.escape(key)}:\s*$", block)
    if not header:
        return []
    values: list[str] = []
    for line in block[header.end():].splitlines():
        if line and not line[0].isspace():
            break
        match = re.match(r"^\s+-\s+(.*?)\s*$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if value:
            values.append(value)
    return values


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\s·•._\-—–，,。:：'\"()（）/@《》]+", "", value)


def clean_topic(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    value = re.sub(r"^[#\-\s]+|[#\-\s]+$", "", value)
    return re.sub(r"\s+", " ", value)


def wiki_targets(text: str) -> set[str]:
    targets: set[str] = set()
    for raw in re.findall(r"\[\[([^\]]+)\]\]", text):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if not target:
            continue
        target = target.removesuffix(".md")
        targets.add(Path(target).name)
    return targets


def section_text(text: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^##\s+{re.escape(heading)}\s*\n(.*?)(?=^##\s+|\Z)",
        text,
    )
    return match.group(1).strip() if match else ""


def is_theme_map(text: str) -> bool:
    if scalar_meta(text, "kind") != "map":
        return False
    role = scalar_meta(text, "map_role")
    if role:
        return role == "theme"
    return bool(re.search(r"(?m)^#\s+主题地图[：:]", text))


def review_topic_index(vault: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    by_rel: dict[str, list[str]] = {}
    by_stem: dict[str, list[str]] = {}
    for root_rel in REVIEW_ROOTS:
        root = vault / root_rel
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            text = path.read_text(encoding="utf-8", errors="replace")
            topics = [clean_topic(value) for value in list_meta(text, "topics")]
            topics = [value for value in topics if value]
            if not topics:
                continue
            rel = path.relative_to(vault).as_posix()
            by_rel[rel] = topics
            by_rel[rel.removesuffix(".md")] = topics
            by_stem.setdefault(path.stem, topics)
    return by_rel, by_stem


def source_identities(text: str) -> set[str]:
    values = {
        scalar_meta(text, "source_file"),
        scalar_meta(text, "source_url"),
        scalar_meta(text, "source_review"),
    }
    values.update(list_meta(text, "sources"))
    return {value for value in values if value}


def knowledge_inventory(vault: Path) -> dict[str, Any]:
    knowledge_root = vault / "20-知识"
    reviews_by_rel, reviews_by_stem = review_topic_index(vault)
    notes: dict[str, dict[str, Any]] = {}
    maps: dict[str, dict[str, Any]] = {}
    all_map_names: set[str] = set()
    object_keys: set[str] = set()
    for path in knowledge_root.rglob("*.md"):
        if path.name == "README.md" or "90-元数据" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        kind = scalar_meta(text, "kind")
        status = scalar_meta(text, "status")
        record = {
            "title": path.stem,
            "path": path.relative_to(vault).as_posix(),
            "kind": kind,
            "status": status,
            "text": text,
        }
        if kind == "map":
            all_map_names.add(path.stem)
            if is_theme_map(text):
                maps[path.stem] = {
                    **record,
                    "members": set(),
                    "new_count": len(wiki_targets(section_text(text, "新增知识"))),
                }
            continue
        if kind == "entity":
            object_keys.add(normalized(path.stem))
            for alias in list_meta(text, "aliases"):
                object_keys.add(normalized(alias))
            continue
        if kind in NON_TOPIC_KINDS or status in TERMINAL_STATUSES:
            continue
        source_review = scalar_meta(text, "source_review")
        topics = list_meta(text, "topics")
        if source_review:
            topics = reviews_by_rel.get(
                source_review,
                reviews_by_rel.get(
                    source_review.removesuffix(".md"),
                    reviews_by_stem.get(Path(source_review).stem, topics),
                ),
            )
        notes[path.stem] = {
            **record,
            "topics": [clean_topic(value) for value in topics if clean_topic(value)],
            "sources": source_identities(text),
            "links": wiki_targets(text),
            "memberships": set(),
            "topic_context": normalized(
                path.stem
                + "\n"
                + re.sub(r"(?s)^---\n.*?\n---\n", "", text)[:2400]
            ),
        }

    for map_name, record in maps.items():
        members = wiki_targets(str(record["text"])) & set(notes)
        record["members"] = members
        record["member_count"] = len(members)
        for title in members:
            notes[title]["memberships"].add(map_name)
    return {
        "notes": notes,
        "maps": maps,
        "all_map_names": all_map_names,
        "object_keys": object_keys,
    }


def theme_map_coverage_issues(vault: Path) -> list[dict[str, str]]:
    inventory = knowledge_inventory(vault)
    return [
        {
            "file": str(note["path"]),
            "issue": "没有进入任何真正的主题地图",
        }
        for note in inventory["notes"].values()
        if not note["memberships"]
    ]


def useful_topic(topic: str, map_names: set[str], object_keys: set[str]) -> bool:
    key = normalized(topic)
    if (
        len(key) < 2
        or len(key) > 32
        or key in {normalized(value) for value in GENERIC_TOPICS}
        or key in object_keys
    ):
        return False
    for map_name in map_names:
        map_key = normalized(map_name)
        if key == map_key:
            return False
        shorter, longer = sorted((key, map_key), key=len)
        if len(shorter) >= 5 and shorter in longer:
            return False
    return bool(topic_parts(topic))


def topic_parts(topic: str) -> list[str]:
    values = re.split(r"[\s、，,/|+]+|(?:与|和|及)", topic, flags=re.I)
    result: list[str] = []
    for value in values:
        key = normalized(value)
        if len(key) < 2 or key in {normalized(item) for item in GENERIC_TOPIC_PARTS}:
            continue
        result.append(key)
    return result


def topic_applies_to_note(topic: str, note: dict[str, Any]) -> bool:
    context = str(note.get("topic_context", ""))
    key = normalized(topic)
    if key and key in context:
        return True
    parts = topic_parts(topic)
    if not parts:
        return False
    matches = sum(part in context for part in parts)
    return matches >= max(1, (len(parts) + 1) // 2)


def candidate_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(normalized(value).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def topic_candidates_from_inventory(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    notes: dict[str, dict[str, Any]] = inventory["notes"]
    maps: dict[str, dict[str, Any]] = inventory["maps"]
    topic_notes: dict[str, set[str]] = defaultdict(set)
    labels: dict[str, Counter[str]] = defaultdict(Counter)
    for title, note in notes.items():
        for topic in note["topics"]:
            key = normalized(topic)
            if not useful_topic(
                topic,
                set(inventory.get("all_map_names", set())),
                set(inventory.get("object_keys", set())),
            ):
                continue
            if not topic_applies_to_note(topic, note):
                continue
            topic_notes[key].add(title)
            labels[key][topic] += 1

    rows: list[dict[str, Any]] = []
    for key, titles in topic_notes.items():
        sources = {
            source
            for title in titles
            for source in notes[title]["sources"]
        }
        if len(titles) < 3 or len(sources) < 2:
            continue
        label = sorted(
            labels[key],
            key=lambda value: (-labels[key][value], -len(value), value),
        )[0]
        distribution: Counter[str] = Counter()
        unmapped = 0
        for title in titles:
            memberships = notes[title]["memberships"]
            if not memberships:
                unmapped += 1
            for map_name in memberships:
                distribution[map_name] += 1
        dominant_map = distribution.most_common(1)[0][0] if distribution else ""
        dominant_count = distribution[dominant_map] if dominant_map else 0
        dominant_share = dominant_count / len(titles) if titles else 0
        overloaded = bool(
            dominant_map
            and (
                int(maps[dominant_map]["member_count"]) >= 60
                or int(maps[dominant_map]["new_count"]) >= 30
            )
        )
        ready = len(titles) >= 4 and len(sources) >= 2
        if not ready:
            proposal = "观察"
            reason = (
                f"已有 {len(titles)} 条知识和 {len(sources)} 份独立来源，"
                "接近成图门槛，先等待更多证据。"
            )
        elif dominant_map and overloaded and dominant_share >= 0.55:
            proposal = "拆分"
            reason = (
                f"{dominant_count} 条相关知识集中在“{dominant_map}”，"
                f"该地图已有 {maps[dominant_map]['member_count']} 条知识，"
                "继续放入会让边界更宽。"
            )
        else:
            proposal = "新建"
            if unmapped:
                reason = (
                    f"已有 {unmapped} 条知识没有真正的主题地图，"
                    "其余内容也分散在多个现有地图中。"
                )
            else:
                reason = (
                    f"相关知识分布在 {max(1, len(distribution))} 张地图中，"
                    "已经形成可以单独进入的稳定主题。"
                )
        score = min(
            100,
            28
            + min(len(titles), 12) * 4
            + min(len(sources), 8) * 3
            + min(unmapped, 5) * 2
            + min(len(distribution), 3) * 3
            + (8 if proposal == "拆分" else 0),
        )
        support = sorted(
            titles,
            key=lambda title: (
                not bool(notes[title]["memberships"]),
                str(notes[title]["path"]),
            ),
            reverse=True,
        )
        rows.append(
            {
                "id": candidate_id("topic", label),
                "name": label,
                "proposal": proposal,
                "score": score,
                "confidence": "高" if len(titles) >= 8 and len(sources) >= 3 else "中",
                "note_count": len(titles),
                "source_count": len(sources),
                "unmapped_count": unmapped,
                "dominant_map": dominant_map,
                "map_distribution": dict(distribution.most_common()),
                "reason": reason,
                "question": (
                    f"围绕“{label}”，现有知识正在形成哪些稳定判断、"
                    "关键分歧和可执行路径？"
                ),
                "supporting_notes": [
                    {
                        "title": title,
                        "path": str(notes[title]["path"]),
                    }
                    for title in support[:12]
                ],
            }
        )

    rows.sort(
        key=lambda item: (
            item["proposal"] == "观察",
            -int(item["score"]),
            -int(item["source_count"]),
            -int(item["note_count"]),
            str(item["name"]),
        )
    )
    deduped: list[dict[str, Any]] = []
    for row in rows:
        row_titles = {item["title"] for item in row["supporting_notes"]}
        duplicate = False
        for kept in deduped:
            kept_titles = {item["title"] for item in kept["supporting_notes"]}
            union = row_titles | kept_titles
            overlap = len(row_titles & kept_titles) / len(union) if union else 0
            if overlap >= 0.75 and row["dominant_map"] == kept["dominant_map"]:
                duplicate = True
                break
        if not duplicate:
            deduped.append(row)
    ready = [row for row in deduped if row["proposal"] != "观察"][:6]
    observing = [row for row in deduped if row["proposal"] == "观察"][:4]
    return ready + observing


def merge_candidates(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    maps = inventory["maps"]
    rows: list[dict[str, Any]] = []
    names = sorted(maps)
    for index, left in enumerate(names):
        left_members = set(maps[left]["members"])
        for right in names[index + 1:]:
            right_members = set(maps[right]["members"])
            smaller = min(len(left_members), len(right_members))
            overlap = left_members & right_members
            if smaller < 12 or len(overlap) < 12 or len(overlap) / smaller < 0.65:
                continue
            label = f"{left} + {right}"
            rows.append(
                {
                    "id": candidate_id("merge", label),
                    "name": label,
                    "proposal": "合并",
                    "score": min(100, 65 + len(overlap)),
                    "confidence": "中",
                    "note_count": len(overlap),
                    "source_count": 0,
                    "unmapped_count": 0,
                    "dominant_map": "",
                    "map_distribution": {left: len(left_members), right: len(right_members)},
                    "reason": (
                        f"两张地图共享 {len(overlap)} 条知识，"
                        f"占较小地图的 {len(overlap) / smaller:.0%}，边界可能已经重叠。"
                    ),
                    "question": "这两张地图是否仍在回答不同的长期问题？",
                    "supporting_notes": [
                        {"title": title, "path": ""}
                        for title in sorted(overlap)[:12]
                    ],
                }
            )
    return rows


def analyze_topic_map_candidates(vault: Path) -> dict[str, Any]:
    inventory = knowledge_inventory(vault)
    candidates = topic_candidates_from_inventory(inventory) + merge_candidates(inventory)
    candidates.sort(
        key=lambda item: (
            item["proposal"] == "观察",
            -int(item["score"]),
            str(item["name"]),
        )
    )
    maps = inventory["maps"]
    overloaded = [
        {
            "name": name,
            "member_count": int(record["member_count"]),
            "new_count": int(record["new_count"]),
        }
        for name, record in maps.items()
        if int(record["member_count"]) >= 60 or int(record["new_count"]) >= 30
    ]
    overloaded.sort(key=lambda item: (-item["member_count"], item["name"]))
    return {
        "candidates": candidates,
        "summary": {
            "theme_map_count": len(maps),
            "thematic_note_count": len(inventory["notes"]),
            "true_unmapped_count": sum(
                not note["memberships"] for note in inventory["notes"].values()
            ),
            "overloaded_maps": overloaded,
        },
    }


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "generated_at": "",
        "candidates": [],
        "observing": [],
        "decisions": {},
        "summary": {},
        "action_errors": [],
    }


def action_blocks(text: str) -> list[tuple[str, list[str]]]:
    rows: list[tuple[str, list[str]]] = []
    pattern = re.compile(
        r"<!--\s*topic-map-candidate:(?P<id>[a-z0-9\-]+)\s*-->"
        r"(?P<body>.*?)(?=<!--\s*topic-map-candidate:|\Z)",
        flags=re.S,
    )
    for match in pattern.finditer(text):
        checked = [
            action
            for action, label in ACTION_LABELS.items()
            if re.search(
                rf"(?m)^-\s+\[[xX]\]\s+{re.escape(label)}\s*$",
                match.group("body"),
            )
        ]
        rows.append((match.group("id"), checked))
    return rows


def safe_map_title(value: str) -> str:
    value = re.sub(r"[/\\:*?\"<>|]", "／", value).strip().strip(".")
    if not value or len(value) > 80:
        raise ValueError("候选主题名称不适合作为地图标题")
    return value


def add_map_to_index(index_text: str, title: str) -> str:
    link = f"- [[{title}]]"
    if f"[[{title}]]" in index_text or f"[[{title}|" in index_text:
        return index_text
    match = re.search(r"(?m)^## 现有地图\s*$", index_text)
    if not match:
        return index_text.rstrip() + f"\n\n## 现有地图\n\n{link}\n"
    section_end_match = re.search(r"(?m)^## ", index_text[match.end():])
    section_end = (
        match.end() + section_end_match.start()
        if section_end_match
        else len(index_text)
    )
    section = index_text[match.end():section_end]
    theme_heading = re.search(r"(?m)^### 主题地图\s*$", section)
    if theme_heading:
        theme_start = match.end() + theme_heading.end()
        next_subheading = re.search(
            r"(?m)^### ",
            index_text[theme_start:section_end],
        )
        insert_at = (
            theme_start + next_subheading.start()
            if next_subheading
            else section_end
        )
    else:
        insert_at = section_end
    before = index_text[:insert_at].rstrip()
    after = index_text[insert_at:].lstrip("\n")
    return before + f"\n{link}\n\n" + after


def create_theme_map(vault: Path, candidate: dict[str, Any], now: datetime) -> Path:
    title = safe_map_title(str(candidate["name"]))
    map_path = vault / MAP_DIR_REL / f"{title}.md"
    if map_path.exists():
        text = map_path.read_text(encoding="utf-8", errors="replace")
        if scalar_meta(text, "kind") != "map":
            raise ValueError(f"同名文件不是知识地图：{map_path.relative_to(vault)}")
        return map_path
    related = [
        name
        for name in candidate.get("map_distribution", {})
        if name and name != title
    ][:3]
    support = candidate.get("supporting_notes", [])[:12]
    date_value = now.date().isoformat()
    lines = [
        "---",
        "kind: map",
        "map_role: theme",
        "status: seed",
        "origin: topic-map-candidate",
        f'candidate_id: "{candidate["id"]}"',
        f"created_at: {date_value}",
        f"updated_at: {date_value}",
        "---",
        "",
        f"# 主题地图：{title}",
        "",
        "## 主题定义",
        "",
        str(candidate["question"]),
        "",
        "## 起始知识",
        "",
    ]
    lines.extend(
        f"- [[{Path(str(item['path'])).stem if item.get('path') else item['title']}]]"
        for item in support
    )
    lines.extend(
        [
            "",
            "## 使用路径",
            "",
            "1. 先从起始知识识别已经形成共识的判断。",
            "2. 再比较不同来源之间的分歧、边界和反例。",
            "3. 新资料进入后，持续更新这个主题仍未回答的问题。",
            "",
            "## 相关主题",
            "",
        ]
    )
    lines.extend(f"- [[{name}]]" for name in related)
    if not related:
        lines.append("- [[知识地图索引]]")
    lines.extend(["", "## 新增知识", ""])
    atomic_text(map_path, "\n".join(lines).rstrip() + "\n")
    index_path = vault / MAP_INDEX_REL
    if index_path.exists():
        index_text = index_path.read_text(encoding="utf-8", errors="replace")
        updated = add_map_to_index(index_text, title)
        updated = re.sub(
            r"(?m)^updated_at:\s*.*$",
            f"updated_at: {date_value}",
            updated,
            count=1,
        )
        if updated != index_text:
            atomic_text(index_path, updated)
    return map_path


def apply_decision(
    vault: Path,
    state: dict[str, Any],
    candidate: dict[str, Any],
    action: str,
    now: datetime,
) -> dict[str, Any]:
    decisions = state.setdefault("decisions", {})
    decision: dict[str, Any] = {
        "status": action,
        "decided_at": now.isoformat(timespec="seconds"),
        "name": candidate["name"],
        "note_count": candidate.get("note_count", 0),
        "source_count": candidate.get("source_count", 0),
    }
    result: dict[str, Any] = {"ok": True, "action": action, "candidate": candidate["id"]}
    if action == "approve":
        if candidate["proposal"] == "合并":
            decision["status"] = "merge-confirmed"
            result["message"] = "已确认合并建议；原地图没有被自动删除或改写"
        else:
            map_path = create_theme_map(vault, candidate, now)
            decision["status"] = "created"
            decision["map_file"] = map_path.relative_to(vault).as_posix()
            result["map_file"] = decision["map_file"]
            result["message"] = "主题地图已建立"
    elif action == "watch":
        decision["next_review_at"] = (now.date() + timedelta(days=14)).isoformat()
        result["message"] = "已进入 14 天观察期"
    elif action == "dismiss":
        result["message"] = "已忽略；知识或来源明显增长时会重新出现"
    else:
        raise ValueError("不支持的主题地图候选动作")
    decisions[str(candidate["id"])] = decision
    return result


def filter_with_decisions(
    candidates: list[dict[str, Any]],
    decisions: dict[str, Any],
    today: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    active: list[dict[str, Any]] = []
    observing: list[dict[str, Any]] = []
    for candidate in candidates:
        decision = decisions.get(candidate["id"])
        if not isinstance(decision, dict):
            if candidate["proposal"] == "观察":
                observing.append(candidate)
            else:
                active.append(candidate)
            continue
        status = decision.get("status")
        if status in {"created", "merge-confirmed"}:
            continue
        if status == "dismiss":
            note_growth = int(candidate.get("note_count", 0)) - int(
                decision.get("note_count", 0)
            )
            source_growth = int(candidate.get("source_count", 0)) - int(
                decision.get("source_count", 0)
            )
            if note_growth < 3 and source_growth < 2:
                continue
            decisions.pop(candidate["id"], None)
            active.append(candidate)
            continue
        if status == "watch":
            try:
                next_review = date.fromisoformat(str(decision["next_review_at"]))
            except (KeyError, ValueError):
                next_review = today
            if next_review > today:
                candidate = {**candidate, "next_review_at": next_review.isoformat()}
                observing.append(candidate)
                continue
            decisions.pop(candidate["id"], None)
            active.append(candidate)
            continue
        active.append(candidate)
    return active[:8], observing[:8]


def candidate_markdown(state: dict[str, Any]) -> str:
    generated = str(state.get("generated_at", ""))
    summary = state.get("summary", {})
    lines = [
        "---",
        "kind: topic-map-candidate-dashboard",
        "status: active",
        f'generated_at: "{generated}"',
        "---",
        "",
        "# 主题地图候选",
        "",
        "> 系统只发现知识结构变化，不会自行创建、拆分或合并地图。"
        "勾选后由下一轮半小时任务执行；也可以在浏览器知识工作台中确认。",
        "",
        "## 待判断",
        "",
    ]
    candidates = state.get("candidates", [])
    if not candidates:
        lines.append("- 当前没有达到人工判断门槛的主题。")
    for candidate in candidates:
        lines.extend(
            [
                f"<!-- topic-map-candidate:{candidate['id']} -->",
                f"### {candidate['name']}",
                "",
                f"**建议：{candidate['proposal']}｜置信度：{candidate['confidence']}｜"
                f"{candidate['note_count']} 条知识｜{candidate['source_count']} 份独立来源**",
                "",
                str(candidate["reason"]),
                "",
                f"- 核心问题：{candidate['question']}",
            ]
        )
        if candidate.get("dominant_map"):
            lines.append(f"- 主要来自：[[{candidate['dominant_map']}]]")
        lines.append("- 支持知识：" + "、".join(
            f"[[{Path(str(item['path'])).stem if item.get('path') else item['title']}]]"
            for item in candidate.get("supporting_notes", [])[:6]
        ))
        approve_label = (
            ACTION_LABELS["approve_merge"]
            if candidate["proposal"] == "合并"
            else ACTION_LABELS["approve"]
        )
        lines.extend(
            [
                "",
                f"- [ ] {approve_label}",
                f"- [ ] {ACTION_LABELS['watch']}",
                f"- [ ] {ACTION_LABELS['dismiss']}",
                "",
            ]
        )
    lines.extend(["## 观察中", ""])
    observing = state.get("observing", [])
    if not observing:
        lines.append("- 当前没有处于观察期的主题。")
    for candidate in observing:
        next_review = candidate.get("next_review_at", "达到更多证据后")
        lines.append(
            f"- **{candidate['name']}**：{candidate['note_count']} 条知识，"
            f"{candidate['source_count']} 份来源；下次复查 {next_review}。"
        )
    lines.extend(["", "## 当前地图压力", ""])
    overloaded = summary.get("overloaded_maps", [])
    if not overloaded:
        lines.append("- 现有主题地图规模正常。")
    for item in overloaded:
        lines.append(
            f"- [[{item['name']}]]：{item['member_count']} 条知识，"
            f"其中 {item['new_count']} 条仍在“新增知识”。"
        )
    lines.extend(
        [
            "",
            "## 口径",
            "",
            f"- 真正的主题地图：{summary.get('theme_map_count', 0)} 张。",
            f"- 尚无真正主题归属：{summary.get('true_unmapped_count', 0)} 条知识。",
            "- 人物索引、产品模型索引和地图总目录不计入主题覆盖。",
            "- 新建门槛：至少 4 条知识、2 份独立来源，并形成区别于现有地图的长期问题。",
            "",
        ]
    )
    errors = state.get("action_errors", [])
    if errors:
        lines.extend(["## 操作需要更正", ""])
        lines.extend(f"- {value}" for value in errors)
        lines.append("")
    return "\n".join(lines)


def refresh_topic_map_candidates(
    vault: Path,
    state_file: Path,
    candidate_note: Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or now_local()
    candidate_note = candidate_note or vault / CANDIDATE_NOTE_REL
    lock_path = state_file.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = load_json(state_file, default_state())
        if not isinstance(state, dict):
            state = default_state()
        state.setdefault("decisions", {})
        previous_candidates = {
            str(item.get("id")): item
            for item in list(state.get("candidates", [])) + list(state.get("observing", []))
            if isinstance(item, dict) and item.get("id")
        }
        action_errors: list[str] = []
        if candidate_note.exists():
            note_text = candidate_note.read_text(encoding="utf-8", errors="replace")
            for item_id, checked in action_blocks(note_text):
                if not checked:
                    continue
                if len(checked) > 1:
                    action_errors.append(
                        f"{previous_candidates.get(item_id, {}).get('name', item_id)} "
                        "同时勾选了多个动作，本轮没有执行。"
                    )
                    continue
                candidate = previous_candidates.get(item_id)
                if candidate is None:
                    continue
                action = "approve" if checked[0] == "approve_merge" else checked[0]
                apply_decision(vault, state, candidate, action, now)
        analysis = analyze_topic_map_candidates(vault)
        active, observing = filter_with_decisions(
            list(analysis["candidates"]),
            state["decisions"],
            now.date(),
        )
        state.update(
            {
                "version": 1,
                "generated_at": now.isoformat(timespec="seconds"),
                "candidates": active,
                "observing": observing,
                "summary": analysis["summary"],
                "action_errors": action_errors,
            }
        )
        atomic_json(state_file, state)
        atomic_text(candidate_note, candidate_markdown(state))
        return state


def apply_topic_map_candidate_action(
    vault: Path,
    state_file: Path,
    candidate_id_value: str,
    action: str,
    candidate_note: Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or now_local()
    candidate_note = candidate_note or vault / CANDIDATE_NOTE_REL
    lock_path = state_file.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = load_json(state_file, default_state())
        candidates = {
            str(item.get("id")): item
            for item in list(state.get("candidates", [])) + list(state.get("observing", []))
            if isinstance(item, dict) and item.get("id")
        }
        candidate = candidates.get(candidate_id_value)
        if candidate is None:
            raise ValueError("这个主题地图候选已经变化，请刷新后重试")
        result = apply_decision(vault, state, candidate, action, now)
        analysis = analyze_topic_map_candidates(vault)
        active, observing = filter_with_decisions(
            list(analysis["candidates"]),
            state.setdefault("decisions", {}),
            now.date(),
        )
        state.update(
            {
                "version": 1,
                "generated_at": now.isoformat(timespec="seconds"),
                "candidates": active,
                "observing": observing,
                "summary": analysis["summary"],
                "action_errors": [],
            }
        )
        atomic_json(state_file, state)
        atomic_text(candidate_note, candidate_markdown(state))
        result["state"] = state
        return result
