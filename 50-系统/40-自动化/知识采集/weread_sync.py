#!/usr/bin/env python3
"""微信读书划线同步：把有笔记的书导出为 30-资料/50-读书划线/ 下的书籍卡。

- 每本书一个文件，文件名即书名，供知识笔记以 [[《书名》]] 双链引用。
- 划线与想法写在 weread:begin/end 标记之间，由本脚本维护；
  标记之外的人工区（我的判断、可提炼的长期知识）永不触碰。
- 增量：对比 state 文件里每本书的 sort/noteCount/reviewCount，变化才重拉。

用法：
  python3 weread_sync.py            # 增量同步
  python3 weread_sync.py --full     # 全量重拉
需要环境变量 WEREAD_API_KEY（wrk- 开头）。
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

SKILL_VERSION = "1.0.4"
GATEWAY = "https://i.weread.qq.com/api/agent/gateway"

SCRIPT_DIR = Path(__file__).resolve().parent
VAULT = SCRIPT_DIR.parent.parent.parent
OUT_DIR = VAULT / "30-资料" / "50-读书划线"
STATE_DIR = Path(os.environ.get("RAYS_BRAIN_STATE", str(Path.home() / ".local/state/rays-brain")))
STATE_FILE = STATE_DIR / "weread_state.json"

BEGIN_MARK = "<!-- weread:begin 本区块由 weread_sync.py 自动维护，手动修改会被覆盖 -->"
END_MARK = "<!-- weread:end -->"

# 机器维护的 frontmatter 键；其余键（人工添加）在更新时原样保留
MACHINE_KEYS = [
    "kind", "title", "author", "capture_source", "capture_id", "book_id",
    "category", "marked_status", "reading_progress", "highlight_count",
    "thought_count", "last_note_at", "synced_at", "source_url",
]


def gateway(api_name: str, **params):
    body = {"api_name": api_name, "skill_version": SKILL_VERSION, **params}
    key = os.environ.get("WEREAD_API_KEY", "")
    if not key:
        sys.exit("缺少环境变量 WEREAD_API_KEY，请先 export WEREAD_API_KEY=<你的apikey>")
    last_err = None
    for delay in (0, 5, 20):
        if delay:
            time.sleep(delay)
        resp = requests.post(
            GATEWAY,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        if resp.status_code == 200:
            break
        last_err = f"HTTP {resp.status_code}"
    else:
        raise RuntimeError(f"接口 {api_name} 请求失败（{last_err}）")
    data = resp.json()
    if data.get("upgrade_info"):
        sys.exit(f"weread skill 需要升级，请先处理：{data['upgrade_info'].get('message')}")
    if data.get("errcode"):
        sys.exit(f"接口 {api_name} 返回错误 errcode={data['errcode']}: {data.get('errmsg', '')}")
    return data


def fetch_notebooks():
    """遍历 /user/notebooks 直到 hasMore=0，返回全部书目。"""
    books, last_sort = [], None
    while True:
        params = {"count": 50}
        if last_sort is not None:
            params["lastSort"] = last_sort
        data = gateway("/user/notebooks", **params)
        page = data.get("books", [])
        books.extend(page)
        if not data.get("hasMore") or not page:
            return books
        last_sort = page[-1].get("sort")


def fetch_reviews(book_id: str):
    """遍历 /review/list/mine，返回该书全部个人想法/点评。"""
    reviews, synckey = [], 0
    while True:
        data = gateway("/review/list/mine", bookid=book_id, synckey=synckey, count=50)
        page = data.get("reviews", [])
        reviews.extend(r.get("review", {}) for r in page)
        if not data.get("hasMore") or not page:
            return reviews
        synckey = data.get("synckey", 0)


def fmt_date(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def range_start(r) -> int:
    m = re.match(r"(\d+)", str(r or ""))
    return int(m.group(1)) if m else 1 << 60


def sanitize_filename(title: str) -> str:
    return re.sub(r'[/\\:*?"<>|\n]', "·", title).strip() or "未命名"


def render_notes_block(bookmarks: dict, reviews: list) -> str:
    """按章节分组渲染划线与想法；想法能对上划线 range 的挂在划线下方。"""
    chapters = {c["chapterUid"]: c for c in bookmarks.get("chapters", [])}
    marks = bookmarks.get("updated", [])

    by_range = {}
    chapter_reviews, book_reviews = [], []
    for rv in reviews:
        rng, uid = rv.get("range"), rv.get("chapterUid")
        if rng:
            by_range.setdefault((uid, rng), []).append(rv)
        elif uid is not None:
            chapter_reviews.append(rv)
        else:
            book_reviews.append(rv)

    grouped = {}
    for mk in marks:
        grouped.setdefault(mk.get("chapterUid"), []).append(mk)

    def chapter_key(uid):
        return chapters.get(uid, {}).get("chapterIdx", 1 << 30)

    lines = ["## 划线与想法", ""]
    used = set()
    for uid in sorted(grouped, key=chapter_key):
        ch = chapters.get(uid, {})
        title = ch.get("title") or f"章节 {uid}"
        idx = ch.get("chapterIdx")
        lines.append(f"### {f'{idx:02d} ' if isinstance(idx, int) else ''}{title}")
        lines.append("")
        for mk in sorted(grouped[uid], key=lambda m: range_start(m.get("range"))):
            text = (mk.get("markText") or "").strip()
            if not text:
                continue
            for ln in text.splitlines():
                lines.append(f"> {ln.strip()}")
            d = fmt_date(mk.get("createTime"))
            if d:
                lines.append(f">\n> — 划线于 {d}")
            key = (uid, mk.get("range"))
            for rv in by_range.get(key, []):
                used.add(id(rv))
                lines.append("")
                lines.append(f"💭 **我的想法**：{(rv.get('content') or '').strip()}")
            lines.append("")

    leftovers = [rv for rv in list(by_range.values()) for rv in rv if id(rv) not in used]
    ch_extra = chapter_reviews + leftovers
    if ch_extra:
        lines.append("### 其他章节想法")
        lines.append("")
        for rv in sorted(ch_extra, key=lambda r: (r.get("chapterIdx") or 0, range_start(r.get("range")))):
            ch = chapters.get(rv.get("chapterUid"), {})
            where = rv.get("chapterName") or ch.get("title") or ""
            abstract = (rv.get("abstract") or "").strip()
            if abstract:
                lines.append(f"> {abstract}")
            prefix = f"（{where}）" if where else ""
            lines.append(f"💭 {prefix}{(rv.get('content') or '').strip()}")
            lines.append("")

    if book_reviews:
        lines.append("### 我的书评")
        lines.append("")
        for rv in book_reviews:
            star = rv.get("star")
            star_s = f"（评分 {star}/5）" if isinstance(star, int) and star >= 0 else ""
            d = fmt_date(rv.get("createTime"))
            lines.append(f"{(rv.get('content') or '').strip()} {star_s}{f' — {d}' if d else ''}".rstrip())
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def yaml_quote(v) -> str:
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace('"', '\\"')
    return f'"{s}"'


def build_frontmatter(machine: dict, extra_lines: list) -> str:
    lines = ["---"]
    for k in MACHINE_KEYS:
        if machine.get(k) not in (None, ""):
            lines.append(f"{k}: {yaml_quote(machine[k])}")
    lines.append(f"processing_status: {machine['processing_status']}")
    lines.append("tags:")
    lines.append("  - 读书划线")
    lines.extend(extra_lines)
    lines.append("---")
    return "\n".join(lines)


def parse_existing(path: Path):
    """返回 (旧 processing_status, 人工 frontmatter 行, 标记区之外的人工正文)。"""
    text = path.read_text(encoding="utf-8")
    status, extra_lines = "unprocessed", []
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[4:end]
            body = text[end + 4:]
            in_tags = False
            for ln in fm.splitlines():
                key = ln.split(":", 1)[0].strip() if ":" in ln else None
                if ln.startswith("  - ") or ln.startswith("    "):
                    if not in_tags:
                        extra_lines.append(ln)
                    continue
                in_tags = key == "tags"
                if key == "processing_status":
                    status = ln.split(":", 1)[1].strip()
                elif key and key not in MACHINE_KEYS and key != "tags":
                    extra_lines.append(ln)
    if BEGIN_MARK in body and END_MARK in body:
        head, rest = body.split(BEGIN_MARK, 1)
        _, tail = rest.split(END_MARK, 1)
    else:
        head, tail = body, None
    return status, extra_lines, (head, tail)


HUMAN_TAIL_DEFAULT = "\n\n## 我的判断\n\n\n## 可提炼的长期知识\n\n- [[]]\n"


def write_book_file(path: Path, machine: dict, info_block: str, notes_block: str):
    if path.exists():
        status, extra_lines, (head, tail) = parse_existing(path)
        # 重写只发生在有新笔记时，已复核过的卡要重新进复核队列
        machine["processing_status"] = "unprocessed" if status == "processed" else status
        if tail is None:
            tail = HUMAN_TAIL_DEFAULT
    else:
        machine["processing_status"] = "unprocessed"
        extra_lines = []
        head = f"\n\n# 《{machine['title']}》\n\n{info_block}\n"
        tail = HUMAN_TAIL_DEFAULT
    fm = build_frontmatter(machine, extra_lines)
    content = f"{fm}{head}{BEGIN_MARK}\n\n{notes_block}\n{END_MARK}{tail}"
    path.write_text(content, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="忽略 state，全量重拉")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    state = {}
    if STATE_FILE.exists() and not args.full:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))

    notebooks = fetch_notebooks()
    print(f"共 {len(notebooks)} 本有笔记的书")

    created = updated = skipped = 0
    total_marks = total_thoughts = 0
    new_state = {}
    for nb in notebooks:
        book_id = str(nb.get("bookId"))
        book = nb.get("book", {})
        title = book.get("title", book_id)
        sig = {
            "sort": nb.get("sort"),
            "noteCount": nb.get("noteCount"),
            "reviewCount": nb.get("reviewCount"),
        }
        new_state[book_id] = sig

        fname = sanitize_filename(f"《{title}》") + ".md"
        path = OUT_DIR / fname
        if state.get(book_id) == sig and path.exists():
            skipped += 1
            continue

        try:
            bookmarks = gateway("/book/bookmarklist", bookId=book_id)
            reviews = fetch_reviews(book_id)
        except RuntimeError as e:
            print(f"  跳过《{title}》：{e}（下次同步会重试）")
            new_state.pop(book_id, None)
            continue
        n_marks = len(bookmarks.get("updated", []))
        total_marks += n_marks
        total_thoughts += len(reviews)

        cats = book.get("categories") or []
        machine = {
            "kind": "source",
            "title": title,
            "author": book.get("author", ""),
            "capture_source": "weread",
            "capture_id": f"weread:{book_id}",
            "book_id": book_id,
            "category": cats[0]["title"] if cats else "",
            "marked_status": "读完" if nb.get("markedStatus") == 1 else "在读",
            "reading_progress": nb.get("readingProgress", 0),
            "highlight_count": nb.get("noteCount", 0),
            "thought_count": nb.get("reviewCount", 0),
            "last_note_at": fmt_date(nb.get("sort")),
            "synced_at": datetime.now().strftime("%Y-%m-%d"),
            "source_url": book.get("deepLink", ""),
        }
        translator = book.get("translator")
        info_lines = [
            "## 书籍信息",
            "",
            f"- 作者：{machine['author']}" + (f"（译：{translator}）" if translator else ""),
            f"- 分类：{machine['category']}" if machine["category"] else None,
            f"- 阅读进度：{machine['reading_progress']}%（{machine['marked_status']}）",
            f"- 划线 {machine['highlight_count']} 条 / 想法 {machine['thought_count']} 条",
            f"- [打开阅读]({machine['source_url']})" if machine["source_url"] else None,
        ]
        info_block = "\n".join(l for l in info_lines if l is not None)

        existed = path.exists()
        write_book_file(path, machine, info_block, render_notes_block(bookmarks, reviews))
        created += 0 if existed else 1
        updated += 1 if existed else 0
        print(f"  {'更新' if existed else '新建'}：{fname}（划线 {n_marks}，想法/点评 {len(reviews)}）")
        time.sleep(1)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(new_state, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"完成：新建 {created}，更新 {updated}，未变化跳过 {skipped}；本次拉取划线 {total_marks} 条、想法/点评 {total_thoughts} 条")


if __name__ == "__main__":
    main()
