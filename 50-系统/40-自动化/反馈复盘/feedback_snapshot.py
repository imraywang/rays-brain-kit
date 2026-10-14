#!/usr/bin/env python3
"""反馈复盘自动化。

机械层：到期反馈自动抓取 X 公开指标填入快照，微信等拿不到的口径显式降级。
判断层：生成「反馈复盘」工作台，预填信号解读与建议结论，人只勾选确认。
止损层：到期超过 30 天仍无复盘的反馈自动关闭为样本不足。

运行顺序（每次 run）：执行上一轮已勾选的结论 → 抓快照 → 止损 → 重建工作台。
判断永远归人：脚本只写数据和建议，结论必须在工作台勾选后才回写。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

DEFAULT_VAULT = Path(__file__).resolve().parents[3]
FEEDBACK_DIR_REL = "40-发布/00-内容反馈"
WORKBENCH_REL = "00-入口/10-工作台/反馈复盘.md"
INGEST_CONFIG_REL = "50-系统/40-自动化/知识采集/config.json"
FEEDBACK_CONFIG_REL = "50-系统/40-自动化/反馈复盘/config.json"
VIDEO_ARTICLE_DIR_REL = "40-发布/50-口播视频"

STALE_DAYS = 30
DONE_STATUSES = {
    "archived", "cancelled", "canceled", "closed", "complete", "completed",
    "done", "published", "rejected", "已关闭", "已取消", "已完成", "已发布", "已归档",
    "not-needed", "not_needed", "reviewed", "无需复盘", "复盘完成", "已复盘",
}
LEGACY_COMPLETE_STATUSES = {
    "reviewed", "复盘完成", "已复盘", "completed", "done", "closed", "已完成", "已关闭",
}
LEGACY_NOT_NEEDED_STATUSES = {"无需复盘", "not_needed"}
VERDICTS = [
    ("continue", "继续深挖"),
    ("stop", "停止此角度"),
    ("pivot", "换角度重写"),
    ("archive", "归档"),
]
VERDICT_LABELS = dict(VERDICTS)
# X public_metrics -> 快照条目标签；转发口径为转推+引用。
# 兼容两代模板：旧版（浏览/评论）与精简版（曝光/评论与私信）。
METRIC_BULLETS = [
    ("浏览", "impression_count"),
    ("曝光", "impression_count"),
    ("点赞", "like_count"),
    ("收藏", "bookmark_count"),
    ("转发", "_retweet_total"),
    ("评论", "reply_count"),
    ("评论与私信", "reply_count"),
]
STATUS_ID_RE = re.compile(r"(?:x|twitter)\.com/[^/]+/status/(\d+)")
UNFETCHABLE_STATUS_KEYS = (
    "wechat_data_status", "shipinhao_data_status",
    "douyin_data_status", "xiaohongshu_data_status",
)
ITEM_MARK_RE = re.compile(r"<!-- feedback-review:(?P<stem>.+?) -->")


# ---------- frontmatter ----------

def parse_frontmatter(text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return meta
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^([A-Za-z0-9_\-]+):\s*(.*)$", line)
        if m:
            meta[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return meta


def set_frontmatter_keys(text: str, updates: dict[str, str]) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
    if end is None:
        return text
    pending = dict(updates)
    for i in range(1, end):
        m = re.match(r"^([A-Za-z0-9_\-]+):", lines[i])
        if m and m.group(1) in pending:
            lines[i] = f"{m.group(1)}: {pending.pop(m.group(1))}"
    for key, value in pending.items():
        lines.insert(end, f"{key}: {value}")
        end += 1
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


# ---------- 时间 ----------

def parse_dt(value: str) -> datetime | None:
    value = (value or "").strip().strip('"').strip("'")
    if not value:
        return None
    for fmt in (None, "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt) if fmt else datetime.fromisoformat(value)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt
    return None


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ---------- 章节定位 ----------

def find_section(lines: list[str], predicate: Callable[[str], bool], level: int = 2) -> tuple[int, int] | None:
    """返回 (标题行, 结束行开区间)。结束于下一个同级或更高级标题。"""
    prefix = "#" * level + " "
    start = None
    for i, line in enumerate(lines):
        if start is None:
            if line.startswith(prefix) and predicate(line):
                start = i
        else:
            if re.match(r"^#{1,%d} " % level, line):
                return start, i
    return (start, len(lines)) if start is not None else None


def fill_x_bullets(lines: list[str], sec: tuple[int, int], metrics: dict[str, Any], captured_at: str) -> tuple[list[str], int]:
    """在章节内的 X 小节里填充空指标，返回 (新行, 实际填充数)。不覆盖已填值。"""
    start, end = sec
    sub = None
    for i in range(start + 1, end):
        if re.match(r"^### .*(X Article|X|𝕏)", lines[i]):
            sub = find_section(lines[start:end], lambda l, i=i: l == lines[i], level=3)
            if sub:
                sub = (sub[0] + start, sub[1] + start)
            break
    if sub is None:
        sub = (start, end)  # 没有平台小节时，直接在章节内填
    filled = 0
    values = dict(metrics)
    values["_retweet_total"] = int(metrics.get("retweet_count", 0)) + int(metrics.get("quote_count", 0))
    out = list(lines)
    has_capture_line = False
    recognized = 0
    for i in range(sub[0], sub[1]):
        m = re.match(r"^(\s*)- (\S+?)：(.*)$", out[i])
        if not m:
            continue
        indent, label, current = m.group(1), m.group(2), m.group(3).strip()
        if label == "抓取时间":
            has_capture_line = True
            if not current:
                out[i] = f"{indent}- 抓取时间：{captured_at}"
            continue
        for want, key in METRIC_BULLETS:
            if label == want:
                recognized += 1
                if not current and key in values:
                    out[i] = f"{indent}- {label}：{values[key]}"
                    filled += 1
                break
    # 早期模板的 7 天章节只有「持续流量 / 转化 / 复用」等判断项，
    # 没有公开指标槽位。此时把数值快照补进章节，避免只写 captured 状态却丢掉数据。
    if recognized == 0:
        insert_at = sub[0] + 1
        while insert_at < len(out) and not out[insert_at].strip():
            insert_at += 1
        snapshot = [
            f"- 曝光：{values.get('impression_count', '')}",
            f"- 收藏：{values.get('bookmark_count', '')}",
            f"- 转发：{values.get('_retweet_total', '')}",
            f"- 评论与私信：{values.get('reply_count', '')}",
            f"- 抓取时间：{captured_at}",
            "",
        ]
        out[insert_at:insert_at] = snapshot
        filled += sum(1 for line in snapshot[:4] if line.split("：", 1)[1])
        has_capture_line = True
    if filled and not has_capture_line:
        insert_at = sub[1]
        while insert_at > sub[0] and not out[insert_at - 1].strip():
            insert_at -= 1
        out.insert(insert_at, f"- 抓取时间：{captured_at}")
    return out, filled


# ---------- X 抓取 ----------

def load_ingest_config(vault: Path) -> dict[str, Any]:
    path = vault / INGEST_CONFIG_REL
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def make_xurl_fetcher(config: dict[str, Any]) -> Callable[[str], dict[str, Any]]:
    def fetch(tweet_id: str) -> dict[str, Any]:
        cmd = [config.get("xurl_path", "xurl")]
        app = config.get("x_app", "")
        if app:
            cmd += ["--app", app]
        cmd.append(f"/2/tweets/{tweet_id}?tweet.fields=public_metrics")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "xurl failed")
        payload = json.loads(proc.stdout)
        data = payload.get("data") or {}
        metrics = data.get("public_metrics")
        if not isinstance(metrics, dict):
            raise RuntimeError(f"no public_metrics in response for {tweet_id}")
        return metrics

    return fetch


# ---------- 单条反馈处理 ----------

def feedback_files(vault: Path) -> list[Path]:
    root = vault / FEEDBACK_DIR_REL
    if not root.exists():
        return []
    return sorted(
        p for p in root.glob("*.md")
        if p.name.lower() != "readme.md"
    )


def is_done(meta: dict[str, str]) -> bool:
    status = meta.get("status", "").strip().lower()
    if status in DONE_STATUSES:
        return True
    return any(meta.get(k, "").strip() for k in ("reviewed_at", "completed_at", "feedback_completed_at"))


def due_of(meta: dict[str, str], now: datetime) -> datetime | None:
    for key in ("due_at", "due_date", "feedback_due_at"):
        dt = parse_dt(meta.get(key, ""))
        if dt:
            return dt
    return None


def publication_anchor(meta: dict[str, str]) -> datetime | None:
    """平台时间优先；缺失时使用本地确认发布的时间安排复盘。"""
    return parse_dt(meta.get("published_at", "")) or parse_dt(
        meta.get("publication_confirmed_at", "")
    )


def linked_article(vault: Path, meta: dict[str, str]) -> Path | None:
    match = re.match(r"\s*\[\[([^\]|#]+)", meta.get("article", ""))
    if not match:
        return None
    raw = match.group(1).strip()
    path = vault / (raw if raw.endswith(".md") else raw + ".md")
    try:
        path.resolve().relative_to(vault.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def sync_article_feedback(vault: Path, meta: dict[str, str]) -> bool:
    path = linked_article(vault, meta)
    if path is None:
        return False
    text = path.read_text(encoding="utf-8")
    updates = {
        key: meta.get(key, "")
        for key in (
            "feedback_24h_status", "feedback_7d_status", *UNFETCHABLE_STATUS_KEYS,
        )
        if meta.get(key, "")
    }
    if not updates:
        return False
    updated = set_frontmatter_keys(text, updates)
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def normalize_card(path: Path, vault: Path, now: datetime) -> bool:
    """把历史叫法收敛到 pending / complete / not-needed，并补齐反馈日程。"""
    text = path.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)
    status = meta.get("status", "").strip().lower()
    updates: dict[str, str] = {}
    if status in LEGACY_NOT_NEEDED_STATUSES:
        updates["status"] = "not-needed"
        updates["feedback_stage"] = "closed"
        for key in ("feedback_24h_status", "feedback_7d_status"):
            if meta.get(key, "").strip() in {"", "pending"}:
                updates[key] = "not-needed"
    elif status in LEGACY_COMPLETE_STATUSES:
        updates["status"] = "complete"
        updates["feedback_stage"] = "closed"
        for key in ("feedback_24h_status", "feedback_7d_status"):
            if meta.get(key, "").strip() in {"", "pending"}:
                updates[key] = "unavailable"

    current_status = updates.get("status", status)
    if current_status in {"complete", "not-needed"}:
        fallback = "unavailable" if current_status == "complete" else "not-needed"
        if meta.get("feedback_stage", "").strip() != "closed":
            updates["feedback_stage"] = "closed"
        for key in UNFETCHABLE_STATUS_KEYS:
            if meta.get(key, "").strip() in {"", "pending"}:
                updates[key] = fallback

    current = {**meta, **updates}
    if current.get("status", "").strip() == "pending":
        anchor = publication_anchor(current)
        if anchor:
            if not current.get("publication_confirmed_at", "").strip() and not current.get("published_at", "").strip():
                updates["publication_confirmed_at"] = iso(anchor)
            if not current.get("feedback_7d_due_at", "").strip():
                updates["feedback_7d_due_at"] = iso(anchor + timedelta(days=7))
            if not current.get("due_at", "").strip():
                stage = current.get("feedback_stage", "24h").strip()
                updates["due_at"] = iso(
                    anchor + (timedelta(days=7) if stage == "7d" else timedelta(hours=24))
                )

    if not updates:
        return False
    updates["updated_at"] = iso(now)
    new_text = set_frontmatter_keys(text, updates)
    path.write_text(new_text, encoding="utf-8")
    sync_article_feedback(vault, {**meta, **updates})
    return True


def video_feedback_paused(vault: Path) -> bool:
    """反馈复盘/config.json 的 video_feedback_paused 为真时，口播视频不再跟进反馈。"""
    try:
        config = json.loads((vault / FEEDBACK_CONFIG_REL).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(config.get("video_feedback_paused"))


def is_video_card(meta: dict[str, str]) -> bool:
    return meta.get("form", "").strip().startswith("口播") or VIDEO_ARTICLE_DIR_REL in meta.get("article", "")


PAUSED_CLOSE_REASON = "口播视频反馈已暂停（视频平台数据拿不到）；保留发布事实，不据此判断内容效果"
PAUSED_CONCLUSION = (
    "- 结论：视频反馈已暂停，不判断选题效果\n"
    "- 理由：视频号、抖音、小红书的数据目前拿不到，反馈卡暂停跟进；保留发布事实\n"
)


def close_pending_without_data(
    path: Path,
    vault: Path,
    now: datetime,
    *,
    reason: str = "本轮未提供可复核的平台数据；保留发布事实，不据此判断内容效果",
    verdict: str = "data-unavailable",
    conclusion: str = (
        "- 结论：本轮数据不可得，不判断选题效果\n"
        "- 理由：没有提供可复核的平台数据；保留发布事实，下一次有数据时再比较\n"
    ),
) -> bool:
    """用户确认不补平台数据时，关闭当前待办，但不伪造效果判断。"""
    text = path.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)
    if meta.get("status", "").strip() != "pending":
        return False
    updates = {
        "status": "complete",
        "feedback_stage": "closed",
        "feedback_completed_at": iso(now),
        "feedback_24h_status": (
            meta.get("feedback_24h_status", "")
            if meta.get("feedback_24h_status", "") not in {"", "pending"}
            else "unavailable"
        ),
        "feedback_7d_status": (
            meta.get("feedback_7d_status", "")
            if meta.get("feedback_7d_status", "") not in {"", "pending"}
            else "unavailable"
        ),
        "close_reason": reason,
        "verdict": verdict,
        "updated_at": iso(now),
    }
    for key in UNFETCHABLE_STATUS_KEYS:
        if meta.get(key, "").strip() in {"", "pending"}:
            updates[key] = "unavailable"
    if "## 复盘结论" not in text:
        text = text.rstrip("\n") + (
            "\n\n## 复盘结论\n\n"
            + conclusion
            + f"- 复盘时间：{iso(now)}\n"
        )
    path.write_text(set_frontmatter_keys(text, updates), encoding="utf-8")
    sync_article_feedback(vault, {**meta, **updates})
    return True


def snapshot_one(path: Path, now: datetime, fetch: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    """机械层：为一条 pending 反馈填 24h / 7d X 快照。返回动作记录。"""
    text = path.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)
    result: dict[str, Any] = {"file": path.name, "actions": [], "error": ""}
    if is_done(meta):
        return result

    updates: dict[str, str] = {}
    # 无 API 通道的平台（微信/视频号/抖音/小红书）口径降级：拿不到的数据不阻塞判断
    downgraded = []
    for key in UNFETCHABLE_STATUS_KEYS:
        if meta.get(key, "").strip() == "pending":
            updates[key] = "unavailable"
            downgraded.append(key.replace("_data_status", ""))
    if downgraded:
        result["actions"].append(f"平台口径标记为不可得：{'、'.join(downgraded)}")

    published = publication_anchor(meta)
    # public_url 可能是微信链接；X 链接也可能在 x_article_url
    tweet_id = ""
    for key in ("public_url", "x_article_url"):
        m = STATUS_ID_RE.search(meta.get(key, ""))
        if m:
            tweet_id = m.group(1)
            break

    lines = text.splitlines()
    if tweet_id and published:
        metrics: dict[str, Any] | None = None
        # 24h 快照
        if meta.get("feedback_24h_status", "").strip() in {"", "pending"} and now >= published + timedelta(hours=24):
            sec = find_section(lines, lambda l: ("24" in l or "首次" in l) and "判断" not in l)
            if sec:
                try:
                    metrics = fetch(tweet_id)
                except Exception as exc:  # noqa: BLE001 - 抓取失败不阻塞其他条目
                    result["error"] = str(exc)
                if metrics is not None:
                    lines, filled = fill_x_bullets(lines, sec, metrics, iso(now))
                    if filled:
                        result["actions"].append(f"24h 快照填充 {filled} 项")
                    updates["feedback_24h_status"] = "complete"
                    updates["feedback_24h_captured_at"] = iso(now)
                    if meta.get("feedback_stage", "").strip() in {"", "24h"}:
                        updates["feedback_stage"] = "7d"
                        updates["due_at"] = iso(published + timedelta(days=7))
        # 7d 快照：到期即抓
        meta_now = {**meta, **updates}
        due = parse_dt(meta_now.get("due_at", "")) or published + timedelta(days=7)
        if (
            meta_now.get("feedback_stage", "").strip() == "7d"
            and meta_now.get("feedback_7d_data_status", "").strip() != "captured"
            and now >= due
        ):
            sec = find_section(lines, lambda l: "7" in l and "24" not in l and "判断" not in l)
            if sec:
                if metrics is None:
                    try:
                        metrics = fetch(tweet_id)
                    except Exception as exc:  # noqa: BLE001
                        result["error"] = str(exc)
                if metrics is not None:
                    lines, filled = fill_x_bullets(lines, sec, metrics, iso(now))
                    if filled:
                        result["actions"].append(f"7d 快照填充 {filled} 项")
                    updates["feedback_7d_data_status"] = "captured"
                    updates["feedback_7d_captured_at"] = iso(now)
                    result["metrics"] = metrics

    if updates:
        new_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        new_text = set_frontmatter_keys(new_text, {**updates, "updated_at": iso(now)})
        path.write_text(new_text, encoding="utf-8")
        sync_article_feedback(path.parents[2], {**meta, **parse_frontmatter(new_text)})
    return result


def close_stale(path: Path, now: datetime) -> bool:
    """止损层：到期超过 STALE_DAYS 仍未复盘的反馈自动关闭。"""
    text = path.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)
    if is_done(meta):
        return False
    due = due_of(meta, now)
    if not due or now - due < timedelta(days=STALE_DAYS):
        return False
    new_text = set_frontmatter_keys(text, {
        "status": "not-needed",
        "feedback_stage": "closed",
        "feedback_24h_status": (
            meta.get("feedback_24h_status", "")
            if meta.get("feedback_24h_status", "") not in {"", "pending"}
            else "not-needed"
        ),
        "feedback_7d_status": (
            meta.get("feedback_7d_status", "")
            if meta.get("feedback_7d_status", "") not in {"", "pending"}
            else "not-needed"
        ),
        "close_reason": f"到期超过 {STALE_DAYS} 天自动关闭（样本不足）",
        "feedback_completed_at": iso(now),
        "updated_at": iso(now),
    })
    path.write_text(new_text, encoding="utf-8")
    sync_article_feedback(path.parents[2], {**meta, **parse_frontmatter(new_text)})
    return True


# ---------- 信号解读（机械启发式，只做建议） ----------

def interpret(metrics: dict[str, Any] | None) -> tuple[str, str]:
    """返回 (建议结论 key, 一句话信号)。样本小时保持保守。"""
    if not metrics:
        return "archive", "暂无平台数据，无法形成数据信号；如有场外反馈可手动补充"
    views = int(metrics.get("impression_count", 0))
    likes = int(metrics.get("like_count", 0))
    saves = int(metrics.get("bookmark_count", 0))
    replies = int(metrics.get("reply_count", 0))
    spread = int(metrics.get("retweet_count", 0)) + int(metrics.get("quote_count", 0))
    if views < 500:
        return "pivot", f"浏览 {views} 未起量，先归因分发（标题/首图/发布时段）而不是内容本身"
    engage = (likes + saves * 2 + replies + spread * 2) / max(views, 1) * 1000
    if saves > replies and saves >= 3:
        return "continue", f"收藏 {saves} > 评论 {replies}，被当作工具内容保存，同一角度可加码"
    if replies > saves and replies >= 3:
        return "continue", f"评论 {replies} > 收藏 {saves}，讨论型选题，可用追问或对立观点续写"
    if engage < 5:
        return "stop", f"浏览 {views} 但互动密度低（约 {engage:.1f}‰），角度或人群可能不对"
    return "continue", f"浏览 {views}、互动密度约 {engage:.1f}‰，信号中性偏正"


# ---------- 工作台 ----------

def knowledge_target(text: str) -> str:
    """从「回写去向」里取第一条长期知识链接。"""
    m = re.search(r"长期知识：\[\[([^\]|]+)", text)
    return m.group(1).strip() if m else ""


def render_board(vault: Path, now: datetime, notes: list[str]) -> str:
    ready: list[str] = []
    waiting: list[str] = []
    not_due: list[str] = []
    for path in feedback_files(vault):
        text = path.read_text(encoding="utf-8")
        meta = parse_frontmatter(text)
        if is_done(meta):
            continue
        rel = f"{FEEDBACK_DIR_REL}/{path.stem}"
        due = due_of(meta, now)
        due_str = due.strftime("%m-%d") if due else "未定"
        stage = meta.get("feedback_stage", "?")
        # 纯视频/微信渠道（无 X 链接）且平台口径都已降级：没有可等的数据，直接进待判断
        has_x = any(STATUS_ID_RE.search(meta.get(k, "")) for k in ("public_url", "x_article_url"))
        platform = meta.get("platform", "")
        no_fetchable_source = (
            not has_x
            and platform
            and "x" not in platform.lower()
            and not any(meta.get(k, "").strip() == "pending" for k in UNFETCHABLE_STATUS_KEYS)
        )
        if not platform:
            waiting.append(f"- [[{rel}|{path.stem}]]：发布平台待确认；公开链接和准确时间可以留空")
        elif not publication_anchor(meta):
            waiting.append(f"- [[{rel}|{path.stem}]]：缺少本地发布确认时间，无法安排复盘")
        elif due and due > now:
            not_due.append(f"- [[{rel}|{path.stem}]]（{stage} 到期 {due_str}）")
        elif meta.get("feedback_7d_data_status", "").strip() == "captured" or no_fetchable_source:
            metrics = last_captured_metrics(text)
            verdict, signal = interpret(metrics)
            mline = (
                f"浏览 {metrics.get('impression_count', '?')} / 赞 {metrics.get('like_count', '?')}"
                f" / 藏 {metrics.get('bookmark_count', '?')} / 评 {metrics.get('reply_count', '?')}"
                if metrics else "无自动数据，可在卡片手动补"
            )
            block = [
                f"<!-- feedback-review:{path.stem} -->",
                f"- [[{rel}|{path.stem}]]（{stage} 到期 {due_str} · {mline}）",
                f"  - 信号：{signal}",
                "  - 结论（勾选一项）：",
            ]
            for key, label in VERDICTS:
                mark = " "
                block.append(f"    - [{mark}] {label}" + ("　←建议" if key == verdict else ""))
            block.append(f"  - 理由：{signal}")
            block.append("")
            ready.append("\n".join(block))
        else:
            waiting.append(f"- [[{rel}|{path.stem}]]（{stage} 到期 {due_str}）：数据待抓取或抓取失败，见运行日志")

    lines = [
        "---",
        "kind: feedback-review-dashboard",
        "status: active",
        f"generated_at: \"{iso(now)}\"",
        "---",
        "",
        "# 反馈复盘",
        "",
        "> 数据由快照脚本自动抓取；你只做判断：每条勾选一个结论，可改「理由」行。",
        "> 下一次脚本运行会把勾选的结论写回反馈卡、追加到对应长期知识，并关闭该条。",
        "> 建议节奏：每周日和知识地图整理放在同一个半小时里批量处理。",
        "",
        f"## 待判断（{len(ready)} 条）",
        "",
    ]
    lines.extend(ready or ["- 无", ""])
    if waiting:
        lines += [f"## 等待条件补齐（{len(waiting)} 条）", ""] + waiting + [""]
    if not_due:
        lines += [f"## 未到期（{len(not_due)} 条）", ""] + not_due + [""]
    if notes:
        lines += ["## 本次运行提醒", ""] + [f"- {n}" for n in notes] + [""]
    return "\n".join(lines) + "\n"


def last_captured_metrics(text: str) -> dict[str, Any] | None:
    """从 7 天快照小节读回已填的指标，供工作台展示。"""
    lines = text.splitlines()
    sec = find_section(lines, lambda l: "7" in l and "24" not in l and "判断" not in l)
    if not sec:
        return None
    metrics: dict[str, Any] = {}
    label_to_key = {
        "浏览": "impression_count", "曝光": "impression_count",
        "点赞": "like_count", "收藏": "bookmark_count",
        "评论": "reply_count", "评论与私信": "reply_count",
    }
    for i in range(sec[0], sec[1]):
        m = re.match(r"^\s*- (\S+?)：(\d+)\s*$", lines[i])
        if m and m.group(1) in label_to_key:
            metrics[label_to_key[m.group(1)]] = int(m.group(2))
    return metrics or None


# ---------- 执行已勾选结论 ----------

def execute_confirmed(vault: Path, now: datetime) -> list[str]:
    board = vault / WORKBENCH_REL
    if not board.exists():
        return []
    notes: list[str] = []
    lines = board.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        m = ITEM_MARK_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        stem = m.group("stem")
        block_end = i + 1
        while block_end < len(lines) and not ITEM_MARK_RE.match(lines[block_end].strip()) and not lines[block_end].startswith("## "):
            block_end += 1
        block = lines[i:block_end]
        checked = [
            label for _, label in VERDICTS
            if any(re.match(rf"^\s*- \[[xX]\] {re.escape(label)}", b) for b in block)
        ]
        reason = ""
        for b in block:
            rm = re.match(r"^\s*- 理由：(.*)$", b)
            if rm:
                reason = rm.group(1).strip()
        if len(checked) == 1:
            ok, msg = apply_verdict(vault, stem, checked[0], reason, now)
            notes.append(msg if ok else f"{stem}：{msg}")
        elif len(checked) > 1:
            notes.append(f"{stem}：勾选了 {len(checked)} 个结论，已跳过，请只保留一个")
        i = block_end
    return notes


def apply_verdict(vault: Path, stem: str, verdict: str, reason: str, now: datetime) -> tuple[bool, str]:
    path = vault / FEEDBACK_DIR_REL / f"{stem}.md"
    if not path.exists():
        return False, "反馈卡不存在"
    text = path.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)
    if is_done(meta):
        return False, "已是完成状态，跳过"
    conclusion = [
        "",
        "## 复盘结论",
        "",
        f"- 结论：{verdict}",
        f"- 理由：{reason or '（工作台未填写理由）'}",
        f"- 复盘时间：{iso(now)}",
        "- 方式：反馈复盘工作台批量确认",
    ]
    if "## 复盘结论" not in text:
        text = text.rstrip("\n") + "\n" + "\n".join(conclusion) + "\n"
    text = set_frontmatter_keys(text, {
        "status": "complete",
        "feedback_stage": "closed",
        "feedback_7d_status": "complete",
        "feedback_completed_at": iso(now),
        "verdict": verdict,
        "updated_at": iso(now),
    })
    path.write_text(text, encoding="utf-8")
    sync_article_feedback(vault, {**meta, **parse_frontmatter(text)})

    # 闭环：结论追加到回写去向的长期知识
    target = knowledge_target(text)
    if target:
        target_path = resolve_note(vault, target)
        if target_path:
            append_feedback_line(
                target_path,
                f"- {now.strftime('%Y-%m-%d')} [[{FEEDBACK_DIR_REL}/{stem}|发布反馈]]：{verdict} — {reason or '见反馈卡'}",
            )
            return True, f"{stem}：已复盘（{verdict}），结论已回写 {target_path.stem}"
        return True, f"{stem}：已复盘（{verdict}），但回写目标 {target} 未找到"
    return True, f"{stem}：已复盘（{verdict}），无回写去向"


def resolve_note(vault: Path, target: str) -> Path | None:
    t = target.strip().strip("/")
    if not t.endswith(".md"):
        t += ".md"
    direct = vault / t
    if direct.exists():
        return direct
    base = Path(t).name
    hits = sorted((vault / "20-知识").rglob(base))
    return hits[0] if hits else None


def append_feedback_line(path: Path, line: str) -> None:
    text = path.read_text(encoding="utf-8")
    if line in text:
        return
    lines = text.splitlines()
    sec = find_section(lines, lambda l: l.strip() == "## 发布反馈")
    if sec:
        insert_at = sec[1]
        while insert_at > sec[0] and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, line)
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["## 发布反馈", "", line]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- 主流程 ----------

def run(
    vault: Path,
    now: datetime | None = None,
    fetch: Callable[[str], dict[str, Any]] | None = None,
    *,
    close_current_without_data: bool = False,
) -> dict[str, Any]:
    now = now or datetime.now().astimezone()
    fetch = fetch or make_xurl_fetcher(load_ingest_config(vault))
    normalized = sum(normalize_card(path, vault, now) for path in feedback_files(vault))
    notes = execute_confirmed(vault, now)
    closed_without_data = 0
    if close_current_without_data:
        closed_without_data = sum(
            close_pending_without_data(path, vault, now) for path in feedback_files(vault)
        )
        if closed_without_data:
            notes.append(f"按用户确认关闭 {closed_without_data} 条无可复核平台数据的反馈")
    if video_feedback_paused(vault):
        paused_closed = 0
        for path in feedback_files(vault):
            meta = parse_frontmatter(path.read_text(encoding="utf-8"))
            if is_video_card(meta) and close_pending_without_data(
                path, vault, now,
                reason=PAUSED_CLOSE_REASON, verdict="paused", conclusion=PAUSED_CONCLUSION,
            ):
                paused_closed += 1
        if paused_closed:
            notes.append(f"口播视频反馈已暂停，关闭 {paused_closed} 条待跟进的视频反馈")
    snapshots = 0
    errors: list[str] = []
    for path in feedback_files(vault):
        result = snapshot_one(path, now, fetch)
        if result["actions"]:
            snapshots += 1
            notes.append(f"{result['file']}：{'；'.join(result['actions'])}")
        if result["error"]:
            errors.append(f"{result['file']}：抓取失败 {result['error']}")
    closed = sum(close_stale(path, now) for path in feedback_files(vault))
    if closed:
        notes.append(f"止损关闭 {closed} 条超期 {STALE_DAYS} 天的反馈")
    board = render_board(vault, now, notes + errors)
    board_path = vault / WORKBENCH_REL
    board_path.parent.mkdir(parents=True, exist_ok=True)
    board_path.write_text(board, encoding="utf-8")
    return {
        "normalized": normalized,
        "closed_without_data": closed_without_data,
        "snapshotted": snapshots,
        "stale_closed": closed,
        "notes": notes,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="反馈复盘：快照 + 工作台 + 止损")
    parser.add_argument("--vault", default=str(DEFAULT_VAULT))
    parser.add_argument(
        "--close-current-without-data",
        action="store_true",
        help="关闭当前所有待反馈项，并明确记录为平台数据不可得；不生成效果判断",
    )
    args = parser.parse_args()
    summary = run(
        Path(args.vault),
        close_current_without_data=args.close_current_without_data,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
