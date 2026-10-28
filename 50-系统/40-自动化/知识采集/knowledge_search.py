#!/usr/bin/env python3
"""按正文检索 20-知识：写稿调研之前先看长期知识里已经有什么。

长期知识的标题是一整句论点（「技术扩散的中介窗口」），选题用的是另一套词（「企业上 AI」
「前半年看不到效果」），只按文件名找几乎找不到。这里把标题、别名和正文一起做 BM25：
中文按相邻两字切分，英文和数字按词切分，标题和别名加权。

用法：
  python3 knowledge_search.py 企业上AI 前半年看不到效果 电气化 生产率
  python3 knowledge_search.py --limit 5 --json ESP32 嵌入式
  python3 knowledge_search.py "Lex Fridman" 访谈        # 人名、产品名加引号，当成一个词

检索词给三类：选题里的对象（人名、产品、事件）、核心判断里的词、可能用得上的机制或类比
（「电气化」「生产率」）。第三类最容易命中长期知识。

默认输出一段 Markdown，可以直接贴进母稿「素材与来源」的「知识库预检」小节；
没有相关结果时也照样输出，写明「无相关」，这样看得出查过。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
VAULT = Path(os.environ.get("RAYS_BRAIN", str(HERE.parents[2]))).resolve()
KNOWLEDGE_REL = "20-知识"
SKIP_PARTS = {"70-知识地图"}  # 地图是索引，不是可以直接调用的知识
TITLE_WEIGHT = 3
ALIAS_WEIGHT = 2
K1, B = 1.2, 0.75
MIN_COVERAGE = 0.4  # 命中的检索词稀有度之和至少占全部检索词的四成
TITLE_BONUS = 2.0
EXACT_TITLE_BONUS = 30.0
CJK = re.compile(r"[一-鿿]+")
WORD = re.compile(r"[A-Za-z][A-Za-z0-9.+#-]*|\d+(?:\.\d+)?")
WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def tokens(text: str) -> list[str]:
    result: list[str] = []
    for run in CJK.findall(text):
        if len(run) == 1:
            result.append(run)
        result.extend(run[index:index + 2] for index in range(len(run) - 1))
    result.extend(word.lower() for word in WORD.findall(text))
    return result


def split_note(text: str) -> tuple[dict[str, str], str]:
    meta: dict[str, str] = {}
    if not text.startswith("---\n"):
        return meta, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return meta, text
    current = ""
    for line in text[4:end].splitlines():
        if line.startswith(("  - ", "- ")) and current:
            meta[current] = (meta.get(current, "") + " " + line.split("-", 1)[1].strip()).strip()
            continue
        if ":" not in line or line.startswith((" ", "\t")):
            continue
        key, value = line.split(":", 1)
        current = key.strip()
        meta[current] = value.strip().strip('"')
    return meta, text[end + 5:]


def readable(body: str) -> str:
    """链接只留显示文字，去掉代码块。"""
    body = re.sub(r"```.*?```", " ", body, flags=re.S)
    return WIKILINK.sub(lambda m: m.group(2) or m.group(1).rsplit("/", 1)[-1], body)


@dataclass
class Note:
    rel: str
    title: str
    kind: str
    aliases: str
    body: str
    has_primary_source: bool
    counts: Counter = field(default_factory=Counter)
    length: int = 0


def load_notes(vault: Path) -> list[Note]:
    notes: list[Note] = []
    root = vault / KNOWLEDGE_REL
    for path in sorted(root.rglob("*.md")):
        if path.name.lower() == "readme.md" or SKIP_PARTS & set(path.relative_to(root).parts):
            continue
        meta, body = split_note(path.read_text(encoding="utf-8", errors="replace"))
        body = readable(body)
        heading = re.search(r"^#\s+(.+)$", body, flags=re.M)
        title = meta.get("title") or (heading.group(1).strip() if heading else path.stem)
        sources = " ".join(meta.get(key, "") for key in ("source_url", "sources"))
        note = Note(
            rel=path.relative_to(vault).as_posix()[:-3],
            title=title,
            kind=meta.get("kind", ""),
            aliases=meta.get("aliases", "").strip("[]"),
            body=body,
            has_primary_source="http" in sources,
        )
        for token in tokens(note.title):
            note.counts[token] += TITLE_WEIGHT
        for token in tokens(note.aliases):
            note.counts[token] += ALIAS_WEIGHT
        body_tokens = tokens(body)
        note.counts.update(body_tokens)
        note.length = len(body_tokens) + 1
        notes.append(note)
    return notes


def best_line(note: Note, query_tokens: set[str], terms: list[str]) -> str:
    best, best_score = "", 0.0
    for raw in note.body.splitlines():
        line = raw.strip().lstrip("-*>#| ").strip()
        if len(line) < 6:
            continue
        line_tokens = set(tokens(line))
        score = len(line_tokens & query_tokens) + 3 * sum(term in line for term in terms)
        if score > best_score:
            best, best_score = line, score
    return best[:120] + ("…" if len(best) > 120 else "")


def search(vault: Path, terms: list[str], limit: int = 8) -> tuple[list[dict], int]:
    notes = load_notes(vault)
    if not notes:
        return [], 0
    query_tokens = Counter(token for term in terms for token in tokens(term))
    doc_freq = Counter(token for note in notes for token in set(note.counts) & set(query_tokens))
    average = sum(note.length for note in notes) / len(notes)
    lowered = [(note.title + "\n" + note.aliases + "\n" + note.body).lower() for note in notes]
    term_tokens = {term: set(tokens(term)) for term in terms}

    def term_hit(term: str, note: Note, text: str) -> bool:
        """整词出现，或者长的中文短语有六成以上的字对出现。"""
        if term.lower() in text:
            return True
        wanted = term_tokens[term]
        return len(wanted) >= 3 and len(wanted & set(note.counts)) / len(wanted) >= 0.6

    hit_table = [[term for term in terms if term_hit(term, note, text)]
                 for note, text in zip(notes, lowered)]
    # 每个检索词按命中篇数算稀有度。只撞上常见词（代码、芯片）的笔记达不到覆盖门槛，不算相关。
    # 哪篇都没有的词按一半权重留在分母里：选题最关键的词库里没有时，宁可报「无相关」。
    max_idf = math.log(1 + (len(notes) + 0.5) / 0.5)
    term_idf = {}
    total_idf = 0.0
    for term in terms:
        hits = sum(term in row for row in hit_table)
        if hits:
            term_idf[term] = math.log(1 + (len(notes) - hits + 0.5) / (hits + 0.5))
            total_idf += term_idf[term]
        else:
            total_idf += max_idf / 2
    results = []
    for note, text, exact in zip(notes, lowered, hit_table):
        coverage = sum(term_idf[term] for term in exact)
        # 检索词正好是这篇的标题或别名（人物、产品档案），不管覆盖率都留下并排到最前
        names = {note.title.lower(), *(alias.strip().strip('"').lower() for alias in note.aliases.split(","))}
        named = [term for term in terms if term.lower() in names]
        if not named and (not total_idf or coverage / total_idf < MIN_COVERAGE):
            continue
        score = 0.0
        for token in query_tokens:
            tf = note.counts.get(token, 0)
            if not tf:
                continue
            idf = math.log(1 + (len(notes) - doc_freq[token] + 0.5) / (doc_freq[token] + 0.5))
            score += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * note.length / average))
        title_text = (note.title + " " + note.aliases).lower()
        score += sum(TITLE_BONUS * term_idf[term] for term in exact if term.lower() in title_text)
        score += EXACT_TITLE_BONUS * len(named)
        score += 2.0 * coverage
        results.append({
            "file": note.rel,
            "title": note.title,
            "kind": note.kind,
            "score": round(score, 2),
            "matched_terms": exact,
            "coverage": round(coverage / total_idf, 2),
            "has_primary_source": note.has_primary_source,
            "line": best_line(note, set(query_tokens), terms),
        })
    results.sort(key=lambda item: -item["score"])
    if results:
        floor = results[0]["score"] * 0.35
        results = [item for item in results if item["score"] >= floor]
    return results[:limit], len(notes)


def markdown(terms: list[str], results: list[dict], total: int, today: str) -> str:
    lines = [f"### 知识库预检（{today}）", "", f"检索词：{'、'.join(terms)}（20-知识 共 {total} 篇）", ""]
    if not results:
        lines.append("- 无相关")
    for item in results:
        source = "有一手出处" if item["has_primary_source"] else "没有外部出处，引用前回原始资料核对"
        line = f"：「{item['line']}」" if item["line"] else ""
        matched = "、".join(item["matched_terms"])
        weak = "弱相关 · " if item["coverage"] < 0.6 else ""
        lines.append(
            f"- [[{item['file']}|{item['title']}]]（{weak}{item['kind'] or '未标类型'} · 命中 {matched} · {source}）{line}"
        )
    lines += ["", "用上的在正文素材里注明；看过但不用的保留在这里，不用删。"]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="按正文检索 20-知识")
    parser.add_argument("terms", nargs="+", help="检索词：选题、核心判断里的词、可能的类比或机制")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--vault", default=str(VAULT), help="知识库根目录")
    args = parser.parse_args()
    vault = Path(args.vault).expanduser().resolve()
    if not (vault / KNOWLEDGE_REL).is_dir():
        print(f"找不到 {vault / KNOWLEDGE_REL}", file=sys.stderr)
        return 1
    results, total = search(vault, args.terms, args.limit)
    if args.json:
        print(json.dumps({"terms": args.terms, "total": total, "results": results},
                         ensure_ascii=False, indent=2))
    else:
        print(markdown(args.terms, results, total, date.today().isoformat()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
