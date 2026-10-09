"""在工作台里还原 Obsidian 的嵌入内容：.base 视图、query 搜索块和按标题截取的笔记片段。

只实现本 vault 实际用到的子集，零依赖：
- .base：YAML 子集；filters 支持 and / or / not 嵌套、==、!=、>、>=、<、<=、
  file.inFolder()、file.ext、file.name、today()、null。
- query：path:"…"、file:"…"、/正则/、"短语"、普通词，隐式 AND、OR、-取反、括号。
无法识别的条件不猜，视为不满足并在结果里标出 unsupported，页面会提示去 Obsidian 查看。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Callable, Iterable

# ---------- YAML 子集 ----------


def _strip_comment(line: str) -> str:
    out, quote = [], ""
    for ch in line:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (not out or out[-1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scalar(raw: str) -> object:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if raw in {"", "~", "null"}:
        return None
    if raw in {"true", "false"}:
        return raw == "true"
    if re.fullmatch(r"-?\d+(\.\d+)?", raw):
        return float(raw) if "." in raw else int(raw)
    return raw


def parse_yaml(text: str) -> object:
    lines = [
        (len(line) - len(line.lstrip(" ")), line.strip())
        for line in (_strip_comment(raw) for raw in text.splitlines())
        if line.strip()
    ]
    value, _ = _parse_block(lines, 0, lines[0][0] if lines else 0)
    return value


def _split_key(content: str) -> tuple[str, str] | None:
    match = re.match(r"^((?:\"[^\"]*\"|'[^']*'|[^:\"'])+?):(?:\s+(.*))?$", content)
    if not match:
        return None
    key = match.group(1).strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        key = key[1:-1]
    return key, (match.group(2) or "")


def _parse_block(lines: list[tuple[int, str]], i: int, indent: int) -> tuple[object, int]:
    if i >= len(lines):
        return None, i
    if lines[i][1].startswith("- ") or lines[i][1] == "-":
        items: list[object] = []
        while i < len(lines) and lines[i][0] == indent and (lines[i][1].startswith("- ") or lines[i][1] == "-"):
            rest = lines[i][1][1:].strip()
            if not rest:
                value, i = _parse_block(lines, i + 1, lines[i + 1][0] if i + 1 < len(lines) else indent + 2)
                items.append(value)
                continue
            pair = _split_key(rest)
            if pair and not rest.startswith(("\"", "'")):
                # 「- key: value」开启一个映射，后续同层键缩进为 indent + 2
                inner = [(indent + 2, rest)]
                j = i + 1
                while j < len(lines) and lines[j][0] > indent:
                    inner.append(lines[j])
                    j += 1
                value, _ = _parse_block(inner, 0, indent + 2)
                items.append(value)
                i = j
            else:
                items.append(_scalar(rest))
                i += 1
        return items, i
    if _split_key(lines[i][1]) is None:
        # 写在下一行的纯量，如「filters:\n  status == "pending"」
        return _scalar(lines[i][1]), i + 1
    mapping: dict[str, object] = {}
    while i < len(lines) and lines[i][0] == indent:
        pair = _split_key(lines[i][1])
        if pair is None:
            i += 1
            continue
        key, rest = pair
        if rest.strip():
            mapping[key] = _scalar(rest)
            i += 1
        elif i + 1 < len(lines) and lines[i + 1][0] > indent:
            mapping[key], i = _parse_block(lines, i + 1, lines[i + 1][0])
        elif i + 1 < len(lines) and lines[i + 1][0] == indent and lines[i + 1][1].startswith("- "):
            mapping[key], i = _parse_block(lines, i + 1, indent)
        else:
            mapping[key] = None
            i += 1
    return mapping, i


# ---------- Bases 过滤 ----------


class Unsupported(Exception):
    pass


NoteRow = tuple[str, str, dict]  # (vault 相对路径, 文件名, frontmatter)

_COMPARE = re.compile(r"^(?P<left>[\w.]+)\s*(?P<op>==|!=|>=|<=|>|<)\s*(?P<right>.+)$")
_FOLDER = re.compile(r"""^file\.inFolder\(\s*["'](?P<folder>[^"']+)["']\s*\)$""")


def _literal(raw: str, today: str) -> object:
    raw = raw.strip()
    if raw == "today()":
        return today
    if raw == "null":
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if re.fullmatch(r"-?\d+(\.\d+)?", raw):
        return float(raw)
    raise Unsupported(raw)


def _property(note: NoteRow, name: str) -> object:
    rel, stem, meta = note
    if name == "file.name":
        return stem
    if name == "file.ext":
        return rel.rsplit(".", 1)[-1] if "." in rel else ""
    if name == "file.path":
        return rel
    if name.startswith("file."):
        raise Unsupported(name)
    value = meta.get(name.removeprefix("note."), "")
    return value if value not in ("", None) else None


def _compare(left: object, op: str, right: object) -> bool:
    if right is None:
        if op == "==":
            return left is None
        if op == "!=":
            return left is not None
        raise Unsupported(op)
    if left is None:
        return op == "!="
    try:
        lnum, rnum = float(str(left)), float(str(right))
        pair: tuple[object, object] = (lnum, rnum)
    except ValueError:
        pair = (str(left), str(right))
    a, b = pair
    return {
        "==": a == b, "!=": a != b, ">": a > b,  # type: ignore[operator]
        ">=": a >= b, "<": a < b, "<=": a <= b,  # type: ignore[operator]
    }[op]


def eval_filter(node: object, note: NoteRow, today: str) -> bool:
    if node is None:
        return True
    if isinstance(node, list):
        return all(eval_filter(item, note, today) for item in node)
    if isinstance(node, dict):
        if "and" in node:
            return all(eval_filter(item, note, today) for item in node["and"] or [])
        if "or" in node:
            return any(eval_filter(item, note, today) for item in node["or"] or [])
        if "not" in node:
            return not all(eval_filter(item, note, today) for item in node["not"] or [])
        raise Unsupported(str(node))
    expr = str(node).strip()
    folder = _FOLDER.match(expr)
    if folder:
        prefix = folder.group("folder").strip("/") + "/"
        return note[0].startswith(prefix)
    match = _COMPARE.match(expr)
    if not match:
        raise Unsupported(expr)
    left = _property(note, match.group("left"))
    right = _literal(match.group("right"), today)
    return _compare(left, match.group("op"), right)


def _sort_key(value: object) -> tuple[int, float, str]:
    if value is None:
        return (1, 0.0, "")
    try:
        return (0, float(str(value)), "")
    except ValueError:
        return (0, 0.0, str(value))


def run_base(
    base_text: str,
    view_name: str,
    notes: Iterable[NoteRow],
    today: str | None = None,
    limit: int = 50,
) -> dict[str, object]:
    today = today or date.today().isoformat()
    spec = parse_yaml(base_text)
    if not isinstance(spec, dict):
        raise ValueError("视图文件格式无法识别")
    views = [view for view in spec.get("views") or [] if isinstance(view, dict)]
    view = next((v for v in views if v.get("name") == view_name), views[0] if views else {})
    properties = spec.get("properties") or {}
    order = [str(item) for item in view.get("order") or ["file.name"]]
    unsupported = ""
    rows: list[tuple[NoteRow, dict[str, object]]] = []
    for note in notes:
        try:
            keep = eval_filter(spec.get("filters"), note, today) and eval_filter(view.get("filters"), note, today)
        except Unsupported as error:
            unsupported = str(error)
            keep = False
        if keep:
            rows.append((note, {key: _property(note, key) for key in order}))
    for rule in reversed([r for r in view.get("sort") or [] if isinstance(r, dict)]):
        prop = str(rule.get("property", ""))
        descending = str(rule.get("direction", "ASC")).upper() == "DESC"
        present = [row for row in rows if _safe_prop(row[0], prop) is not None]
        missing = [row for row in rows if _safe_prop(row[0], prop) is None]
        present.sort(key=lambda row: _sort_key(_safe_prop(row[0], prop)), reverse=descending)
        rows = present + missing
    columns = [
        {
            "key": key,
            "label": str((properties.get(key) or {}).get("displayName", key))
            if isinstance(properties.get(key), dict) else key,
        }
        for key in order
    ]
    return {
        "type": "base",
        "view": str(view.get("name", "")),
        "views": [str(v.get("name", "")) for v in views],
        "columns": columns,
        "total": len(rows),
        "rows": [
            {
                "path": note[0],
                "title": note[1],
                "cells": {key: ("" if value is None else str(value)) for key, value in cells.items()},
            }
            for note, cells in rows[:limit]
        ],
        "unsupported": unsupported,
    }


def _safe_prop(note: NoteRow, name: str) -> object:
    try:
        return _property(note, name)
    except Unsupported:
        return None


# ---------- query 搜索块 ----------

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<lparen>\()|(?P<rparen>\))|
        (?P<neg>-)(?=\S)|
        (?P<field>path|file|tag|content):(?P<fvalue>"[^"]*"|\S+?)(?=[\s()]|$)|
        (?P<regex>/(?:\\/|[^/])+/)|
        (?P<phrase>"[^"]*")|
        (?P<word>[^\s()]+)
    )""",
    re.VERBOSE,
)


def _tokens(query: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    query = query.strip()
    while pos < len(query):
        match = _TOKEN.match(query, pos)
        if not match or match.end() == pos:
            raise Unsupported(query[pos:])
        pos = match.end()
        kind = match.lastgroup
        if kind == "fvalue":
            out.append((match.group("field"), match.group("fvalue").strip('"')))
        elif kind == "word" and match.group("word") == "OR":
            out.append(("or", ""))
        elif kind == "phrase":
            out.append(("word", match.group("phrase")[1:-1]))
        elif kind == "regex":
            out.append(("regex", match.group("regex")[1:-1].replace("\\/", "/")))
        else:
            out.append((kind or "", match.group(kind) if kind else ""))
    return out


def parse_query(query: str) -> object:
    tokens = _tokens(query)
    pos = 0

    def parse_or() -> object:
        nonlocal pos
        terms = [parse_and()]
        while pos < len(tokens) and tokens[pos][0] == "or":
            pos += 1
            terms.append(parse_and())
        return ("or", terms) if len(terms) > 1 else terms[0]

    def parse_and() -> object:
        nonlocal pos
        terms = []
        while pos < len(tokens) and tokens[pos][0] not in {"or", "rparen"}:
            terms.append(parse_unary())
        if not terms:
            raise Unsupported("空条件")
        return ("and", terms) if len(terms) > 1 else terms[0]

    def parse_unary() -> object:
        nonlocal pos
        kind, value = tokens[pos]
        if kind == "neg":
            pos += 1
            return ("not", parse_unary())
        if kind == "lparen":
            pos += 1
            node = parse_or()
            if pos >= len(tokens) or tokens[pos][0] != "rparen":
                raise Unsupported("括号未闭合")
            pos += 1
            return node
        pos += 1
        if kind == "regex":
            try:
                return ("regex", re.compile(value, re.MULTILINE))
            except re.error as error:
                raise Unsupported(value) from error
        if kind in {"path", "file", "word", "content"}:
            return (kind, value.casefold())
        raise Unsupported(value)

    node = parse_or()
    if pos != len(tokens):
        raise Unsupported("多余的右括号")
    return node


def match_query(node: object, rel: str, text_of: Callable[[], str]) -> bool:
    kind = node[0]  # type: ignore[index]
    if kind == "and":
        return all(match_query(item, rel, text_of) for item in node[1])  # type: ignore[index]
    if kind == "or":
        return any(match_query(item, rel, text_of) for item in node[1])  # type: ignore[index]
    if kind == "not":
        return not match_query(node[1], rel, text_of)  # type: ignore[index]
    value = node[1]  # type: ignore[index]
    if kind == "path":
        return value in rel.casefold()
    if kind == "file":
        return value in rel.rsplit("/", 1)[-1].casefold()
    if kind == "regex":
        return bool(value.search(text_of()))
    return value in text_of().casefold() or value in rel.casefold()


def query_prefilter(node: object) -> list[str]:
    """顶层 AND 里的 path: 条件，用来先按路径缩小扫描范围。"""
    if node[0] == "path":  # type: ignore[index]
        return [node[1]]  # type: ignore[index]
    if node[0] == "and":  # type: ignore[index]
        return [item[1] for item in node[1] if item[0] == "path"]  # type: ignore[index]
    return []


# ---------- 笔记片段 ----------


def heading_section(body: str, heading: str) -> str:
    """取「# 标题」下的内容，直到同级或更高级标题；找不到返回空串。"""
    wanted = heading.strip().casefold()
    lines = body.splitlines()
    start = level = None
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if not match:
            continue
        if start is None and match.group(2).strip().casefold() == wanted:
            start, level = index + 1, len(match.group(1))
        elif start is not None and len(match.group(1)) <= (level or 0):
            return "\n".join(lines[start:index]).strip()
    return "\n".join(lines[start:]).strip() if start is not None else ""
