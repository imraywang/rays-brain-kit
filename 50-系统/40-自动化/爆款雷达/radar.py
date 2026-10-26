#!/usr/bin/env python3
"""爆款雷达：从 TikHub 拉抖音低粉爆款榜和小红书笔记搜索，筛出「粉丝少却爆了」的选题。

用法：
    python3 radar.py fetch            # 拉一轮数据，合并进状态目录
    python3 radar.py fetch --dry-run  # 只打印将调用的接口，不计费
    python3 radar.py status           # 看最近一轮结果

数据不进 vault：全部放在 $RAYS_BRAIN_STATE/radar/（默认 ~/.local/state/rays-brain/radar/）。
只有在工作台点「加入选题」的条目，才会在候选选题目录生成笔记。
凭据与对标拆解共用 $RAYS_BRAIN_STATE/tikhub.json。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "radar_config.json"
STATE_HOME = Path(os.environ.get("RAYS_BRAIN_STATE", str(Path.home() / ".local/state/rays-brain")))
TIKHUB_CONFIG = STATE_HOME / "tikhub.json"
RADAR_DIR = STATE_HOME / "radar"
ITEMS_FILE = RADAR_DIR / "items.json"
FEEDBACK_FILE = RADAR_DIR / "feedback.json"
AUTHORS_FILE = RADAR_DIR / "xhs_authors.json"
RUNS_FILE = RADAR_DIR / "runs.jsonl"
COVER_DIR = RADAR_DIR / "covers"

DOUYIN_LOW_FAN = "/api/v1/douyin/billboard/fetch_hot_total_low_fan_list"
XHS_SEARCH = "/api/v1/xiaohongshu/app_v2/search_notes"
XHS_USER = "/api/v1/xiaohongshu/app_v2/get_user_info"
ENDPOINT_COST = {DOUYIN_LOW_FAN: 0.001, XHS_SEARCH: 0.01, XHS_USER: 0.01}
COVER_HOSTS = ("douyinpic.com", "xhscdn.com")
ACTIONS = {"adopt", "reject", "seen", "restore"}

_STATE_LOCK = threading.Lock()


# ---------- 基础读写 ----------

def now_local() -> datetime:
    return datetime.now().astimezone()


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_config(path: Path = CONFIG_FILE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def to_int(value: Any) -> int | None:
    """接口里的数字可能是 int、"1234"、"1.2万"，统一成 int；认不出返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([万wWkK千]?)\+?", text)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2)
    if unit in {"万", "w", "W"}:
        number *= 10_000
    elif unit in {"千", "k", "K"}:
        number *= 1_000
    return int(number)


def clean_title(text: str, limit: int = 60) -> str:
    text = re.sub(r"#\S+", " ", text or "")
    text = re.sub(r"@\S+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip() or "（无标题）"


# ---------- TikHub 调用 ----------

class Budget:
    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        self.used = 0
        self.cost = 0.0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.max_calls

    def spend(self, endpoint: str) -> None:
        if self.used >= self.max_calls:
            raise BudgetExceeded(f"已达本次运行调用上限 {self.max_calls}")
        self.used += 1
        self.cost += ENDPOINT_COST.get(endpoint, 0.01)


class BudgetExceeded(RuntimeError):
    pass


def tikhub_call(
    endpoint: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    config: dict[str, Any],
    budget: Budget,
) -> dict[str, Any]:
    """用 curl 子进程调用（系统证书链），和对标拆解一致。返回响应里的 data。"""
    budget.spend(endpoint)
    url = config.get("base_url", "https://api.tikhub.io") + endpoint
    if params:
        url += "?" + urllib.parse.urlencode(params)
    cmd = [
        "curl", "-sS", "--max-time", "60",
        "-H", f"Authorization: Bearer {config['api_key']}",
        "-H", "User-Agent: rays-brain-radar",
    ]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "--data-raw", json.dumps(body, ensure_ascii=False)]
    proc = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=75)
    if proc.returncode != 0:
        raise RuntimeError(f"{endpoint} curl 失败: {proc.stderr.strip()[:300]}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"{endpoint} 非 JSON 响应: {proc.stdout[:300]}")
    if isinstance(payload.get("detail"), dict):
        payload = payload["detail"]
    if payload.get("code") not in (200, 0):
        raise RuntimeError(f"{endpoint} 返回异常: {json.dumps(payload, ensure_ascii=False)[:300]}")
    return payload.get("data") or {}


# ---------- 归一化 ----------

def douyin_item(obj: dict[str, Any], keyword: str, seen_at: str) -> dict[str, Any] | None:
    item_id = str(obj.get("item_id") or "")
    if not item_id:
        return None
    published = obj.get("publish_time")
    return {
        "id": f"douyin:{item_id}",
        "platform": "douyin",
        "item_id": item_id,
        "title": clean_title(obj.get("item_title", "")),
        "desc": re.sub(r"\s+", " ", obj.get("item_title", "")).strip()[:500],
        "author": obj.get("nick_name", ""),
        "author_id": "",
        "fans": to_int(obj.get("fans_cnt")),
        "likes": to_int(obj.get("like_cnt")) or 0,
        "collects": None,
        "comments": None,
        "shares": None,
        "plays": to_int(obj.get("play_cnt")),
        "follow_rate": obj.get("follow_rate"),
        "published_at": iso(datetime.fromtimestamp(int(published)).astimezone()) if published else "",
        "url": f"https://www.douyin.com/video/{item_id}",
        "cover": obj.get("item_cover_url", ""),
        "keywords": [keyword],
        "first_seen": seen_at,
        "last_seen": seen_at,
    }


def xhs_published_at(note: dict[str, Any]) -> str:
    ts = note.get("timestamp")
    if not ts:
        # 笔记 id 前 8 位十六进制就是创建时间戳（探针已核对）
        note_id = str(note.get("id", ""))
        try:
            ts = int(note_id[:8], 16)
        except ValueError:
            return ""
    return iso(datetime.fromtimestamp(int(ts)).astimezone())


def xhs_cover(note: dict[str, Any]) -> str:
    video = note.get("video_info_v2") or {}
    thumb = (video.get("image") or {}).get("thumbnail_dim")
    if thumb:
        return thumb
    images = note.get("images_list") or []
    if images:
        return images[0].get("url_size_large") or images[0].get("url") or ""
    return ""


def xhs_item(note: dict[str, Any], keyword: str, seen_at: str) -> dict[str, Any] | None:
    note_id = str(note.get("id") or "")
    if not note_id:
        return None
    user = note.get("user") or {}
    token = note.get("xsec_token", "")
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if token:
        url += "?" + urllib.parse.urlencode({"xsec_token": token, "xsec_source": "pc_search"})
    title = note.get("title") or note.get("desc") or ""
    return {
        "id": f"xiaohongshu:{note_id}",
        "platform": "xiaohongshu",
        "item_id": note_id,
        "title": clean_title(title),
        "desc": re.sub(r"\s+", " ", note.get("desc", "")).strip()[:500],
        "author": user.get("nickname", ""),
        "author_id": user.get("userid", ""),
        "fans": None,
        "likes": to_int(note.get("liked_count")) or 0,
        "collects": to_int(note.get("collected_count")) or 0,
        "comments": to_int(note.get("comments_count")),
        "shares": to_int(note.get("shared_count")),
        "plays": None,
        "follow_rate": None,
        "published_at": xhs_published_at(note),
        "url": url,
        "cover": xhs_cover(note),
        "keywords": [keyword],
        "first_seen": seen_at,
        "last_seen": seen_at,
    }


# ---------- 打分 ----------

def interactions(item: dict[str, Any]) -> int:
    if item["platform"] == "xiaohongshu":
        return (item.get("likes") or 0) + (item.get("collects") or 0)
    return item.get("likes") or 0


def age_days(item: dict[str, Any], now: datetime) -> float | None:
    if not item.get("published_at"):
        return None
    try:
        published = datetime.fromisoformat(item["published_at"])
    except ValueError:
        return None
    return (now - published).total_seconds() / 86400


def score_item(
    item: dict[str, Any],
    config: dict[str, Any],
    feedback: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """在条目上写 interactions / multiplier / passed / listicle / excluded；不改原始指标。"""
    rules = config[item["platform"]]
    total = interactions(item)
    fans = item.get("fans")
    item["interactions"] = total
    item["multiplier"] = round(total / max(fans, 100), 1) if fans is not None else None
    age = age_days(item, now)
    item["age_days"] = round(age, 1) if age is not None else None
    text = f"{item.get('title', '')} {item.get('desc', '')}"
    item["listicle"] = bool(re.search(config.get("listicle_pattern", "$^"), item.get("title", "")))
    blocked_words = [*config.get("exclude_words", []), *feedback.get("blocked_words", [])]
    lowered = text.lower()
    item["excluded"] = (
        any(word and word.lower() in lowered for word in blocked_words)
        or (item.get("author") and item["author"] in feedback.get("blocked_authors", []))
    )
    base_ok = (
        total >= rules["min_interactions"]
        and (age is None or age <= rules["max_age_days"])
    )
    if fans is None:
        # 小红书粉丝没补查到时先算「待定」，工作台照样展示
        item["passed"] = None if base_ok else False
    else:
        item["passed"] = bool(
            base_ok and fans <= rules["max_fans"] and item["multiplier"] >= rules["min_multiplier"]
        )
    return item


# ---------- 采集 ----------

def fetch_douyin(
    config: dict[str, Any], tikhub: dict[str, Any], budget: Budget, seen_at: str,
    call: Callable[..., dict[str, Any]] = tikhub_call,
) -> tuple[list[dict[str, Any]], list[str]]:
    rules = config["douyin"]
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for keyword in rules["keywords"]:
        for page in range(1, rules.get("pages", 1) + 1):
            body = {
                "page": page,
                "page_size": rules.get("page_size", 50),
                "date_window": rules.get("date_window_hours", 168),
                "keyword": keyword,
            }
            try:
                data = call(DOUYIN_LOW_FAN, body=body, config=tikhub, budget=budget)
            except BudgetExceeded as exc:
                errors.append(str(exc))
                return items, errors
            except Exception as exc:
                errors.append(f"抖音「{keyword}」第 {page} 页：{exc}")
                break
            objs = (data.get("data") or {}).get("objs") or []
            items.extend(filter(None, (douyin_item(obj, keyword, seen_at) for obj in objs)))
            if len(objs) < body["page_size"] * 0.8:
                break
    return items, errors


def fetch_xhs(
    config: dict[str, Any], tikhub: dict[str, Any], budget: Budget, seen_at: str,
    call: Callable[..., dict[str, Any]] = tikhub_call,
) -> tuple[list[dict[str, Any]], list[str]]:
    rules = config["xiaohongshu"]
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for keyword in rules["keywords"]:
        params = {
            "keyword": keyword,
            "page": 1,
            "sort_type": rules.get("sort_type", "general"),
            "time_filter": rules.get("time_filter", "一周内"),
        }
        try:
            data = call(XHS_SEARCH, params=params, config=tikhub, budget=budget)
        except BudgetExceeded as exc:
            errors.append(str(exc))
            return items, errors
        except Exception as exc:
            errors.append(f"小红书「{keyword}」：{exc}")
            continue
        for entry in (data.get("data") or {}).get("items") or []:
            note = entry.get("note")
            if note:
                item = xhs_item(note, keyword, seen_at)
                if item:
                    items.append(item)
    return items, errors


def enrich_xhs_fans(
    items: list[dict[str, Any]],
    config: dict[str, Any],
    tikhub: dict[str, Any],
    budget: Budget,
    now: datetime,
    call: Callable[..., dict[str, Any]] = tikhub_call,
) -> list[str]:
    """只给互动过门槛、时效合格的作者补查粉丝，按 userid 缓存。"""
    rules = config["xiaohongshu"]
    cache: dict[str, Any] = load_json(AUTHORS_FILE, {})
    ttl = timedelta(days=config.get("author_cache_days", 30))
    errors: list[str] = []
    for item in items:
        cached = cache.get(item.get("author_id", ""))
        if cached and now - datetime.fromisoformat(cached["at"]) < ttl:
            item["fans"] = cached["fans"]
    pending = [
        item for item in items
        if item["platform"] == "xiaohongshu" and item.get("fans") is None and item.get("author_id")
        and interactions(item) >= rules["min_interactions"]
        and (age_days(item, now) or 0) <= rules["max_age_days"]
    ]
    pending.sort(key=interactions, reverse=True)
    looked_up: set[str] = set()
    for item in pending:
        author_id = item["author_id"]
        if author_id in looked_up:
            continue
        if len(looked_up) >= rules.get("enrich_limit", 10):
            break
        looked_up.add(author_id)
        try:
            data = call(XHS_USER, params={"user_id": author_id}, config=tikhub, budget=budget)
        except BudgetExceeded as exc:
            errors.append(str(exc))
            break
        except Exception as exc:
            errors.append(f"小红书作者 {item.get('author')}：{exc}")
            continue
        fans = to_int((data.get("data") or {}).get("fans"))
        if fans is not None:
            cache[author_id] = {"fans": fans, "nickname": item.get("author", ""), "at": iso(now)}
    for item in items:
        cached = cache.get(item.get("author_id", ""))
        if cached and item.get("fans") is None:
            item["fans"] = cached["fans"]
    write_json(AUTHORS_FILE, cache)
    return errors


def merge_items(
    store: dict[str, Any], fresh: list[dict[str, Any]], adopted: set[str], now: datetime, retention_days: int
) -> dict[str, Any]:
    for item in fresh:
        old = store.get(item["id"])
        if old:
            item["first_seen"] = old.get("first_seen", item["first_seen"])
            item["keywords"] = sorted(set(old.get("keywords", [])) | set(item["keywords"]))
            if item.get("fans") is None and old.get("fans") is not None:
                item["fans"] = old["fans"]
        store[item["id"]] = item
    cutoff = now - timedelta(days=retention_days)
    return {
        key: value for key, value in store.items()
        if key in adopted or datetime.fromisoformat(value["last_seen"]) >= cutoff
    }


def run_fetch(
    config: dict[str, Any] | None = None,
    *,
    tikhub: dict[str, Any] | None = None,
    call: Callable[..., dict[str, Any]] = tikhub_call,
    now: datetime | None = None,
) -> dict[str, Any]:
    config = config or load_config()
    if tikhub is None:
        if not TIKHUB_CONFIG.exists():
            raise SystemExit(f"缺少凭据文件 {TIKHUB_CONFIG}")
        tikhub = load_json(TIKHUB_CONFIG, {})
    now = now or now_local()
    seen_at = iso(now)
    budget = Budget(config.get("max_calls", 40))
    fresh: list[dict[str, Any]] = []
    errors: list[str] = []
    items, errs = fetch_douyin(config, tikhub, budget, seen_at, call)
    fresh += items
    errors += errs
    if config.get("xiaohongshu", {}).get("enabled", True) and not budget.exhausted:
        items, errs = fetch_xhs(config, tikhub, budget, seen_at, call)
        errors += errs
        if not budget.exhausted:
            errors += enrich_xhs_fans(items, config, tikhub, budget, now, call)
        fresh += items
    if budget.exhausted and not any("调用上限" in error for error in errors):
        errors.append(f"已达本次运行调用上限 {budget.max_calls}，后面的查询本轮跳过")
    with _STATE_LOCK:
        feedback = load_json(FEEDBACK_FILE, {})
        adopted = {key for key, mark in feedback.get("items", {}).items() if mark.get("status") == "adopted"}
        store = merge_items(load_json(ITEMS_FILE, {}), fresh, adopted, now, config.get("retention_days", 30))
        write_json(ITEMS_FILE, store)
    scored = [score_item(dict(item), config, feedback, now) for item in fresh]
    record = {
        "at": seen_at,
        "calls": budget.used,
        "cost_usd": round(budget.cost, 3),
        "fetched": len({item["id"] for item in fresh}),
        "passed": len({item["id"] for item in scored if item["passed"] and not item["excluded"]}),
        "errors": errors,
    }
    RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with RUNS_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def last_run() -> dict[str, Any]:
    try:
        lines = RUNS_FILE.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return {}
    for line in reversed(lines):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {}


# ---------- 工作台读取与动作 ----------

def radar_view(config: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    config = config or load_config()
    now = now or now_local()
    with _STATE_LOCK:
        store = load_json(ITEMS_FILE, {})
        feedback = load_json(FEEDBACK_FILE, {})
    marks = feedback.get("items", {})
    rows = []
    for item in store.values():
        scored = score_item(dict(item), config, feedback, now)
        mark = marks.get(scored["id"], {})
        scored["status"] = mark.get("status", "new")
        scored["candidate"] = mark.get("candidate", "")
        if scored["status"] == "new" and (scored["excluded"] or scored["passed"] is False):
            continue
        rows.append(scored)
    rows.sort(key=lambda row: (row["multiplier"] is None, -(row["multiplier"] or 0), -row["interactions"]))
    return {
        "items": rows,
        "last_run": last_run(),
        "blocked_authors": feedback.get("blocked_authors", []),
        "thresholds": {
            platform: {key: config[platform][key] for key in ("max_fans", "min_interactions", "min_multiplier", "max_age_days")}
            for platform in ("douyin", "xiaohongshu")
        },
    }


def safe_filename(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|#^\[\]\n\r\t]+', " ", text)
    return re.sub(r"\s+", " ", text).strip()[:50] or "爆款选题"


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def candidate_note(item: dict[str, Any], angle: str, now: datetime, attention_days: int) -> str:
    platform_name = {"douyin": "抖音", "xiaohongshu": "小红书"}[item["platform"]]
    today = now.date()
    published = item.get("published_at", "")[:10]
    try:
        fresh_until = (date.fromisoformat(published) + timedelta(days=14)).isoformat()
    except ValueError:
        fresh_until = ""
    attention_until = (today + timedelta(days=attention_days)).isoformat()
    multiplier_value = item["multiplier"] if item.get("multiplier") is not None else '""'
    topic_id = "topic-" + hashlib.sha256(item["id"].encode("utf-8")).hexdigest()[:12]
    metrics = [
        f"- 平台：{platform_name}",
        f"- 作者：{item.get('author') or '未知'}（粉丝 {item['fans']:,}）" if item.get("fans") is not None else f"- 作者：{item.get('author') or '未知'}（粉丝未知）",
        f"- 点赞：{item.get('likes', 0):,}",
    ]
    if item.get("collects") is not None:
        metrics.append(f"- 收藏：{item['collects']:,}")
    if item.get("plays"):
        metrics.append(f"- 播放：{item['plays']:,}")
    if item.get("multiplier") is not None:
        metrics.append(f"- 爆款倍数（互动 ÷ 粉丝）：{item['multiplier']}")
    metrics += [
        f"- 发布：{published or '未知'}",
        f"- 命中关键词：{'、'.join(item.get('keywords', []))}",
        f"- 雷达抓取：{item.get('last_seen', '')[:16].replace('T', ' ')}",
    ]
    angle_line = f"- [ ] A1 · {angle}" if angle else "- [ ] A1 · 待确认：这个题我能讲出什么不一样的东西"
    lines = [
        "---",
        "kind: topic-candidate",
        "status: candidate",
        "origin: radar",
        f"topic_id: {topic_id}",
        "priority_score: 60",
        "knowledge_value_score: 40",
        "writing_value_score: 60",
        "time_sensitivity: 中时效",
        f"source_published_at: {yaml_string(published)}",
        f"fresh_until: {yaml_string(fresh_until)}",
        f"freshness_status: {'fresh' if fresh_until and today.isoformat() <= fresh_until else 'expired'}",
        f"attention_entered_at: {yaml_string(iso(now))}",
        f"attention_until: {yaml_string(attention_until)}",
        "attention_status: fresh",
        f"review_after: {attention_until}",
        'parked_reason: ""',
        "knowledge_status: pending",
        "personal_context_status: missing",
        f"created_at: {yaml_string(iso(now))}",
        f"updated_at: {yaml_string(iso(now))}",
        f"last_progress_at: {yaml_string(iso(now))}",
        f"capture_id: {yaml_string(item['id'])}",
        'source_review: ""',
        'source_file: ""',
        f"source_url: {yaml_string(item['url'])}",
        f"radar_platform: {item['platform']}",
        f"radar_multiplier: {multiplier_value}",
        'knowledge_file: ""',
        'knowledge_files_json: "[]"',
        'content_pack: ""',
        'draft: ""',
        'published_file: ""',
        'performance_file: ""',
        "---",
        "",
        f"# {item['title']}",
        "",
        "## 选题判断",
        "",
        f"爆款雷达从{platform_name}低粉高赞内容里挑出来的：别人已经验证过这个题能爆，我们重新讲一遍，不搬文案和画面。",
        "",
        "## 待用角度",
        "",
        angle_line,
        "",
        "## 已用角度",
        "",
        "- 暂无",
        "",
        "## 原帖数据",
        "",
        *metrics,
        "",
        f"原帖：{item['url']}",
        "",
        "## 原帖描述",
        "",
        item.get("desc") or "（无）",
        "",
    ]
    return "\n".join(lines)


def adopt_item(
    item_id: str, angle: str, *, vault: Path, topics_dir: str, attention_days: int, now: datetime | None = None
) -> dict[str, Any]:
    now = now or now_local()
    angle = " ".join(angle.split())
    if len(angle) > 120:
        raise ValueError("角度请控制在 120 字以内")
    config = load_config()
    with _STATE_LOCK:
        store = load_json(ITEMS_FILE, {})
        feedback = load_json(FEEDBACK_FILE, {})
        item = store.get(item_id)
        if not item:
            raise ValueError("这条素材已不在雷达里，请刷新")
        mark = feedback.setdefault("items", {}).get(item_id, {})
        if mark.get("status") == "adopted" and mark.get("candidate"):
            return {"ok": True, "candidate": mark["candidate"], "existing": True}
        item = score_item(dict(item), config, feedback, now)
        folder = vault / topics_dir
        folder.mkdir(parents=True, exist_ok=True)
        base = f"{now.date().isoformat()}-{safe_filename(item['title'])}"
        path = folder / f"{base}.md"
        index = 2
        while path.exists():
            path = folder / f"{base}-{index}.md"
            index += 1
        path.write_text(candidate_note(item, angle, now, attention_days), encoding="utf-8")
        rel = path.relative_to(vault).as_posix()
        feedback["items"][item_id] = {"status": "adopted", "at": iso(now), "candidate": rel}
        write_json(FEEDBACK_FILE, feedback)
    return {"ok": True, "candidate": rel, "existing": False}


def mark_item(item_id: str, action: str, *, block_author: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """reject / seen / restore。加入选题走 adopt_item。"""
    if action not in {"reject", "seen", "restore"}:
        raise ValueError("操作不存在")
    now = now or now_local()
    with _STATE_LOCK:
        store = load_json(ITEMS_FILE, {})
        feedback = load_json(FEEDBACK_FILE, {})
        marks = feedback.setdefault("items", {})
        item = store.get(item_id)
        if not item and item_id not in marks:
            raise ValueError("这条素材已不在雷达里，请刷新")
        current = marks.get(item_id, {})
        if current.get("status") == "adopted":
            raise ValueError("已经加入选题，请到候选选题里处理")
        blocked = feedback.setdefault("blocked_authors", [])
        if action == "restore":
            marks.pop(item_id, None)
            author = current.get("blocked_author")
            if author and author in blocked:
                blocked.remove(author)
        else:
            entry = {"status": "rejected" if action == "reject" else "seen", "at": iso(now)}
            author = (item or {}).get("author", "")
            if action == "reject" and block_author and author:
                if author not in blocked:
                    blocked.append(author)
                entry["blocked_author"] = author
            marks[item_id] = entry
        write_json(FEEDBACK_FILE, feedback)
    return {"ok": True, "id": item_id, "status": marks.get(item_id, {}).get("status", "new")}


def cover_bytes(item_id: str, fetch: Callable[[str], tuple[bytes, str]] | None = None) -> tuple[bytes, str]:
    """封面走本机代理：页面 CSP 不允许外链图片，而且抖音、小红书的图有防盗链。"""
    store = load_json(ITEMS_FILE, {})
    item = store.get(item_id)
    if not item or not item.get("cover"):
        raise ValueError("没有封面")
    url = item["cover"]
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if urllib.parse.urlparse(url).scheme not in {"http", "https"} or not host.endswith(COVER_HOSTS):
        raise ValueError("封面地址不在允许的域名里")
    cache = COVER_DIR / (hashlib.sha256(item_id.encode()).hexdigest()[:24] + ".img")
    if cache.exists():
        data = cache.read_bytes()
        return data, sniff_image(data) or "image/jpeg"
    data, content_type = (fetch or curl_fetch)(url)
    if not content_type.startswith("image/"):
        content_type = sniff_image(data)
        if not content_type:
            raise ValueError("封面不是图片")
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return data, content_type


def sniff_image(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in {b"ftypavif", b"ftypheic"}:
        return "image/avif"
    return ""


def curl_fetch(url: str) -> tuple[bytes, str]:
    proc = subprocess.run(
        ["curl", "-sS", "-L", "--max-time", "20", "--max-filesize", "3000000",
         "-A", "Mozilla/5.0", "-w", "\n%{content_type}", url],
        capture_output=True, timeout=30,
    )
    if proc.returncode != 0:
        raise ValueError("封面下载失败")
    body, _, content_type = proc.stdout.rpartition(b"\n")
    return body, content_type.decode("ascii", "ignore").split(";")[0].strip()


# ---------- 命令行 ----------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="爆款雷达")
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch", help="拉一轮数据")
    fetch.add_argument("--dry-run", action="store_true", help="只打印计划调用，不计费")
    sub.add_parser("status", help="看最近一轮结果")
    args = parser.parse_args(argv)
    if args.command == "status":
        print(json.dumps(last_run(), ensure_ascii=False, indent=2))
        return 0
    config = load_config()
    if args.dry_run:
        dy = config["douyin"]
        xhs = config["xiaohongshu"]
        calls = len(dy["keywords"]) * dy.get("pages", 1)
        print(f"抖音低粉榜：{len(dy['keywords'])} 词 × 最多 {dy.get('pages', 1)} 页 = 最多 {calls} 次（${calls * 0.001:.3f}）")
        if xhs.get("enabled", True):
            n = len(xhs["keywords"]) + xhs.get("enrich_limit", 10)
            print(f"小红书：{len(xhs['keywords'])} 词 + 最多补查 {xhs.get('enrich_limit', 10)} 位作者 = 最多 {n} 次（${n * 0.01:.2f}）")
        print(f"单次调用上限 {config.get('max_calls', 40)}")
        return 0
    record = run_fetch(config)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 1 if record["errors"] and not record["fetched"] else 0


if __name__ == "__main__":
    sys.exit(main())
