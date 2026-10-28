#!/usr/bin/env python3
"""把平台后台的作品列表对到 40-发布/50-口播视频 的发布笔记上。

后台数据由人或 Claude 在已登录的浏览器里只读取出（取法见 README「平台核对」），
存成 JSON 交给这个脚本。脚本只做两件事：匹配、回写。默认只打印报告，加 --write 才落盘。

输入格式：
{
  "platform": "douyin",              # douyin / xiaohongshu / shipinhao / youtube / bilibili
  "account": "解说号",                # 只有抖音需要
  "fetched_at": "2026-10-27T10:00:00-07:00",
  "complete": true,                  # 列表是否覆盖了这段时间的全部作品，决定能不能判「后台已没有」
  "items": [
    {"id": "7700845544964439887", "title": "…", "published_at": "2026-10-25T22:33:45-07:00",
     "duration_s": 349.9, "views": 3395, "status": "published"}
  ]
}
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import record_published as rp  # noqa: E402

PUBLIC_URL = {
    "douyin": "https://www.douyin.com/video/{id}",
    "xiaohongshu": "https://www.xiaohongshu.com/explore/{id}",
    "youtube": "https://youtu.be/{id}",
    "bilibili": "https://www.bilibili.com/video/{id}",
    "shipinhao": "",  # 视频号没有公开网页链接
}
LABELS = {**rp.PLATFORM_LABEL, "youtube": "YouTube", "bilibili": "B站"}
STATUSES = {"published", "reviewing", "restricted", "private", "removed"}
DURATION_TOLERANCE_S = 3.0
DATE_WINDOW = timedelta(days=4)


def parse_moment(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def video_notes() -> list[tuple[Path, dict[str, str]]]:
    root = rp.VAULT / rp.VIDEO_DIR
    notes = []
    for path in sorted(root.glob("*.md")):
        meta = rp.parse_frontmatter(path.read_text(encoding="utf-8"))
        if meta.get("kind") == "video-published":
            notes.append((path, meta))
    return notes


def note_moment(meta: dict[str, str]) -> datetime | None:
    """优先用已知平台的发布时间，其次是确认时间，最后是创建日期。"""
    for key in sorted(meta):
        if key.endswith("_published_at"):
            moment = parse_moment(meta[key])
            if moment:
                return moment
    for key in ("publication_confirmed_at", "created_at"):
        raw = meta.get(key, "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            raw += "T12:00:00+08:00"
        moment = parse_moment(raw)
        if moment:
            return moment
    return None


def title_tokens(text: str) -> set[str]:
    text = re.sub(r"#\S+", " ", text or "")
    chars = [char for char in text if "一" <= char <= "鿿" or char.isalnum()]
    return {"".join(chars[index:index + 2]) for index in range(len(chars) - 1)}


def title_score(item_title: str, meta: dict[str, str], path: Path) -> float:
    item = title_tokens(item_title)
    note = title_tokens(meta.get("title", "") + path.stem)
    for key, value in meta.items():
        if key.endswith("_title"):
            note |= title_tokens(value)
    if not item or not note:
        return 0.0
    return len(item & note) / len(item)


def item_id_in(meta: dict[str, str], platform: str, item_id: str) -> bool:
    return bool(item_id) and item_id in meta.get(f"{platform}_url", "")


def match_items(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    platform = payload["platform"]
    account = payload.get("account", "")
    notes = video_notes()
    report: dict[str, list[dict[str, Any]]] = {
        "linked": [], "proposed": [], "ambiguous": [], "unmatched": [], "missing": [],
    }
    claimed: set[Path] = set()
    for item in payload.get("items", []):
        item_id = str(item.get("id", "")).strip()
        linked = [(path, meta) for path, meta in notes if item_id_in(meta, platform, item_id)]
        if linked:
            path, meta = linked[0]
            claimed.add(path)
            report["linked"].append({"item": item, "file": path, "meta": meta})
            continue
        moment = parse_moment(item.get("published_at", ""))
        duration = item.get("duration_s")
        candidates = []
        for path, meta in notes:
            if account and meta.get("douyin_account") and meta["douyin_account"] != account:
                continue
            if meta.get(f"{platform}_url", "").strip():
                continue  # 已经记过另一个地址，不在这里改；重发交给 missing 一起看
            note_at = note_moment(meta)
            if moment and note_at and abs(moment - note_at) > DATE_WINDOW:
                continue
            note_duration = meta.get("duration_s", "").strip()
            duration_ok = None
            if duration and note_duration:
                try:
                    duration_ok = abs(float(note_duration) - float(duration)) <= DURATION_TOLERANCE_S
                except ValueError:
                    duration_ok = None
            if duration_ok is False:
                continue
            score = title_score(item.get("title", ""), meta, path) + (0.5 if duration_ok else 0)
            if score >= 0.3:
                candidates.append((score, path, meta))
        candidates.sort(key=lambda entry: -entry[0])
        if not candidates:
            report["unmatched"].append({"item": item})
        elif len(candidates) == 1 or candidates[0][0] - candidates[1][0] >= 0.3:
            _, path, meta = candidates[0]
            claimed.add(path)
            report["proposed"].append({"item": item, "file": path, "meta": meta})
        else:
            report["ambiguous"].append({
                "item": item, "files": [entry[1] for entry in candidates[:3]],
            })
    # 后台列表完整时，笔记记过的地址在列表里找不到，说明作品被删或换了重发
    if payload.get("complete"):
        ids = {str(item.get("id", "")) for item in payload.get("items", [])}
        dates = [parse_moment(item.get("published_at", "")) for item in payload.get("items", [])]
        dates = [moment for moment in dates if moment]
        earliest = min(dates) if dates else None
        for path, meta in notes:
            url = meta.get(f"{platform}_url", "").strip()
            if not url or path in claimed:
                continue
            if account and meta.get("douyin_account", account) != account:
                continue
            if any(item_id and item_id in url for item_id in ids):
                continue
            note_at = note_moment(meta)
            if earliest and note_at and note_at < earliest - DATE_WINDOW:
                continue  # 比这次取到的最早作品还早，不能下结论
            report["missing"].append({"file": path, "url": url})
    return report


def updates_for(platform: str, account: str, item: dict[str, Any], meta: dict[str, str],
                fetched_at: str, *, fill_link: bool) -> dict[str, str]:
    updates: dict[str, str] = {}
    status = item.get("status", "published")
    if status not in STATUSES:
        raise SystemExit(f"未知状态 {status}：只认 {sorted(STATUSES)}")
    updates[f"{platform}_status"] = status
    if fill_link:
        template = PUBLIC_URL.get(platform, "")
        if template and item.get("id"):
            updates[f"{platform}_url"] = template.format(id=item["id"])
        if item.get("published_at") and not meta.get(f"{platform}_published_at"):
            updates[f"{platform}_published_at"] = item["published_at"]
    if platform == "douyin" and account:
        updates["douyin_account"] = account
    if item.get("views") is not None:
        updates[f"{platform}_views"] = str(item["views"])
        updates["stats_synced_at"] = fetched_at
    label = LABELS.get(platform, platform)
    if status == "published":
        merged = rp.merge_platform_labels(meta.get("platform", ""), label)
        if merged != meta.get("platform", ""):
            updates["platform"] = merged
    return {key: value for key, value in updates.items() if meta.get(key, "") != value}


def describe(entry: dict[str, Any]) -> str:
    item = entry.get("item", {})
    return f"{item.get('id', '')} {item.get('published_at', '')[:10]} {item.get('title', '')[:40]}"


def main() -> int:
    parser = argparse.ArgumentParser(description="平台后台作品列表 → 发布笔记")
    parser.add_argument("--input", required=True, help="后台作品列表 JSON")
    parser.add_argument("--write", action="store_true", help="写回笔记；默认只打印")
    parser.add_argument("--accept-proposed", action="store_true",
                        help="连没有链接、按时长和标题对上的也一起写（先看一遍报告）")
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    platform = payload.get("platform", "")
    if platform not in PUBLIC_URL:
        raise SystemExit(f"不认识的平台 {platform}")
    if platform == "douyin" and not payload.get("account"):
        raise SystemExit("抖音分两个号，输入里必须写 account")
    fetched_at = payload.get("fetched_at") or datetime.now().astimezone().isoformat(timespec="seconds")
    account = payload.get("account", "")
    report = match_items(payload)

    writes: list[tuple[Path, dict[str, str]]] = []
    for kind, fill in (("linked", False), ("proposed", True)):
        for entry in report[kind]:
            updates = updates_for(platform, account, entry["item"], entry["meta"], fetched_at,
                                  fill_link=fill)
            if kind == "proposed" and not args.accept_proposed:
                updates = {}
            if updates:
                writes.append((entry["file"], updates))

    print(f"# {LABELS.get(platform, platform)} {account} · {len(payload.get('items', []))} 条作品")
    print(f"- 已有链接、只更新数据：{len(report['linked'])}")
    print(f"- 按时长和标题新对上：{len(report['proposed'])}"
          + ("" if args.accept_proposed else "（加 --accept-proposed 才写）"))
    for entry in report["proposed"]:
        print(f"  - {describe(entry)} → {rp.rel(entry['file'])}")
    for title, key in (("对不准，人工判断", "ambiguous"), ("后台有、笔记里没有", "unmatched")):
        if report[key]:
            print(f"- {title}：{len(report[key])}")
            for entry in report[key]:
                extra = " / ".join(rp.rel(path) for path in entry.get("files", []))
                print(f"  - {describe(entry)}" + (f" → {extra}" if extra else ""))
    if report["missing"]:
        print(f"- 笔记记过、后台已经找不到（删了或重发）：{len(report['missing'])}")
        for entry in report["missing"]:
            print(f"  - {rp.rel(entry['file'])} · {entry['url']}")
    print(f"- 要改的笔记：{len(writes)}" + ("" if args.write else "（预览，加 --write 落盘）"))
    for path, updates in writes:
        print(f"  - {rp.rel(path)}：{', '.join(f'{k}={v}' for k, v in updates.items())}")
    if args.write:
        for path, updates in writes:
            text = path.read_text(encoding="utf-8")
            path.write_text(rp.update_frontmatter(text, updates), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
