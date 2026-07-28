#!/usr/bin/env python3
"""微信读书先导预览：不买书先看骨架——简介 + 目录 + 全书热门划线 Top 20。

用法：
  python3 weread_preview.py 3300081407      # bookId
  python3 weread_preview.py "系统之美"       # 书名（自动搜书取第一个结果）

输出 markdown 到 stdout，供配书流程（选书清单先导预览）人工/Agent 摘要使用；
热门划线是版权内容片段，仅作个人选书判断，不要整段搬进公开发布物。
需要环境变量 WEREAD_API_KEY。
"""

import sys

from weread_sync import gateway


def resolve_book_id(arg: str) -> str:
    if arg.isdigit() or arg.startswith("CB_") or arg.startswith("cb_"):
        return arg
    data = gateway("/store/search", keyword=arg, scope=10)
    for grp in data.get("results", []):
        for b in grp.get("books", []):
            bi = b.get("bookInfo", b)
            if bi.get("bookId"):
                print(f"> 按书名搜索命中：《{bi.get('title')}》（{bi.get('author')}），bookId={bi['bookId']}\n")
                return str(bi["bookId"])
    sys.exit(f"搜索「{arg}」没有找到电子书结果")


def fmt_rating(info: dict) -> str:
    r = info.get("newRating")
    label = (info.get("newRatingDetail") or {}).get("title") or ""
    if r is None:
        return "无评分"
    return f"{r / 10:.1f}%{f'（{label}）' if label else ''}"


def main():
    if len(sys.argv) < 2:
        sys.exit("用法：weread_preview.py <bookId|书名>")
    book_id = resolve_book_id(sys.argv[1])

    info = gateway("/book/info", bookId=book_id)
    toc = gateway("/book/chapterinfo", bookId=book_id)
    marks = gateway("/book/bestbookmarks", bookId=book_id)

    title = info.get("title", book_id)
    print(f"# 《{title}》先导预览\n")
    author = info.get("author", "")
    translator = info.get("translator")
    print(f"- 作者：{author}" + (f"（译：{translator}）" if translator else ""))
    print(f"- 推荐值：{fmt_rating(info)}（{info.get('newRatingCount', '?')} 人评分）")
    pub = " ".join(str(x) for x in (info.get("publisher"), (info.get("publishTime") or "")[:10]) if x)
    if pub.strip():
        print(f"- 出版：{pub}")
    chapters = toc.get("chapters", [])
    total_words = sum(c.get("wordCount") or 0 for c in chapters)
    print(f"- 章节：{len(chapters)} 章，约 {total_words / 10000:.1f} 万字")
    if info.get("deepLink"):
        print(f"- [打开阅读]({info['deepLink']})")
    intro = (info.get("intro") or "").strip().replace("\n", " ")
    if intro:
        print(f"\n**简介**：{intro[:300]}\n")

    print("## 目录\n")
    for c in chapters:
        lvl = c.get("level") or 1
        wc = c.get("wordCount") or 0
        wc_s = f"（{wc / 10000:.1f}万字）" if wc >= 5000 else ""
        print("  " * (lvl - 1) + f"- {c.get('title')}{wc_s}")

    items = marks.get("items", [])
    ch_map = {c.get("chapterUid"): c.get("title", "") for c in marks.get("chapters", [])}
    print(f"\n## 热门划线 Top {len(items)}（全书共 {marks.get('totalCount', '?')} 条）\n")

    # 各章热门划线密度：大众公认的精华章节
    density: dict = {}
    for it in items:
        ch = ch_map.get(it.get("chapterUid"), "?")
        density[ch] = density.get(ch, 0) + 1
    hot = sorted(density.items(), key=lambda kv: -kv[1])
    print("划线密度（精读区参考）：" + "；".join(f"{ch}×{n}" for ch, n in hot) + "\n")

    for i, it in enumerate(items, 1):
        ch = ch_map.get(it.get("chapterUid"), "?")
        text = (it.get("markText") or "").replace("\n", " ")
        print(f"{i}. [{ch}] {it.get('totalCount')}人：{text}")


if __name__ == "__main__":
    main()
