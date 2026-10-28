#!/usr/bin/env python3
"""把调研里带日期的事实追加到对象档案的「时间线」。

人物、公司、产品、模型的档案在 20-知识 里本来就有；每做一期内容，就把这期查实的关键事实按
「日期 事实（来源）」记到对应档案下。下次再碰到同一个对象，知识库预检会显示时间线有几条、
最新到哪天，只需要补之后的变化。

  # 单条
  python3 entity_timeline.py --entity 长鑫 --date 2026-07-27 \\
      --fact "科创板上市，发行价 8.66 元，首日收 49 元" \\
      --source "[[30-资料/10-自主调研/2026-10-25-国内AI产业链跟钱走/国内AI产业链跟钱走-选题调研]]"
  # 一期调研的多条，先预览再 --write
  python3 entity_timeline.py --from facts.json --write

facts.json 是列表，每条 {"entity", "date", "fact", "source"}。日期写事实发生的那天，不是记录的那天。
来源写一手链接，或者写这期调研笔记的 [[链接]]（调研笔记里有一手出处）。
默认只预览；同一天同一句事实不会重复记。找不到档案时报出相近的名字；确实是新对象时，
在这条里加 "create": "公司"（人物 / 组织 / 公司 / 产品 / 模型），可选 "aliases"、"description"，
由知识采集的建档逻辑新建档案（同一套 entity_id 和地图归属），命令行用 --create。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from knowledge_search import VAULT, split_note  # noqa: E402

ENTITY_DIRS = ("20-知识/20-人物与组织", "20-知识/25-产品与模型")
HEADING = "## 时间线"
INSERT_BEFORE = ("## 相关对象", "## 来源与边界")
LINE = re.compile(r"^-\s*(\d{4}-\d{2}-\d{2})\s+(.*)$")


def key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().strip('"').casefold().replace(" ", "")


def entity_index(vault: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for directory in ENTITY_DIRS:
        for path in sorted((vault / directory).glob("*.md")):
            meta, body = split_note(path.read_text(encoding="utf-8", errors="replace"))
            if meta.get("kind") != "entity":
                continue
            heading = re.search(r"^#\s+(.+)$", body, flags=re.M)
            names = {path.stem, heading.group(1) if heading else ""}
            names |= {alias for alias in re.findall(r'"([^"]+)"', meta.get("aliases", ""))}
            for name in names:
                if name.strip():
                    index.setdefault(key(name), []).append(path)
    return index


def find_entity(vault: Path, name: str, index: dict[str, list[Path]] | None = None) -> Path:
    index = index if index is not None else entity_index(vault)
    matches = sorted(set(index.get(key(name), [])))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SystemExit(f"「{name}」对上了多个档案：{[p.name for p in matches]}，换一个更准的名字")
    near = sorted({p.stem for k, paths in index.items() if key(name) in k or k in key(name)
                   for p in paths})[:8]
    raise SystemExit(f"找不到「{name}」的对象档案" + (f"，相近的有：{near}" if near else "")
                     + "。先按知识库的对象档案规则建档，再记时间线。")


ENTITY_TYPES = {"人物", "组织", "公司", "产品", "模型"}


def create_entity(vault: Path, item: dict) -> Path:
    """交给 knowledge_ingest 建档，和采集流程用同一套 entity_id、目录和地图归属。"""
    entity_type = item["create"]
    if entity_type not in ENTITY_TYPES:
        raise SystemExit(f"create 只能是 {sorted(ENTITY_TYPES)}：{entity_type}")
    import os
    os.environ["RAYS_BRAIN"] = str(vault)
    import knowledge_ingest
    source = item["source"]
    link = re.match(r"\[\[([^\]|]+)", source)
    return knowledge_ingest.ensure_entity_profile(
        {"name": item["entity"], "entity_type": entity_type,
         "aliases": item.get("aliases", []), "description": item.get("description", "")},
        source_file=(link.group(1) + ".md") if link else "",
        source_url=source if source.startswith("http") else "",
    )


def check_fact(item: dict[str, str]) -> dict[str, str]:
    missing = [field for field in ("entity", "date", "fact", "source") if not str(item.get(field, "")).strip()]
    if missing:
        raise SystemExit(f"缺少 {missing}：{item}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["date"]):
        raise SystemExit(f"日期要写成 YYYY-MM-DD：{item['date']}")
    fact = item["fact"].strip().rstrip("。")
    checked = {"entity": item["entity"].strip(), "date": item["date"], "fact": fact,
               "source": item["source"].strip()}
    for extra in ("create", "aliases", "description"):
        if item.get(extra):
            checked[extra] = item[extra]
    return checked


def add_lines(text: str, new_lines: list[str]) -> tuple[str, list[str]]:
    """把新事实并进「时间线」小节，按日期从新到旧排；同一句已存在的跳过。"""
    match = re.search(rf"^{re.escape(HEADING)}\s*$\n?(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
    if match:
        existing = [line for line in match.group(1).splitlines() if line.strip()]
    else:
        existing = []
    seen = {unicodedata.normalize("NFC", line.strip()) for line in existing}
    added = [line for line in new_lines if unicodedata.normalize("NFC", line) not in seen]
    if not added:
        return text, []
    dated = [line for line in existing + added if LINE.match(line)]
    notes = [line for line in existing if not LINE.match(line)]
    dated.sort(key=lambda line: LINE.match(line).group(1), reverse=True)
    section = HEADING + "\n\n" + "\n".join(notes + dated) + "\n\n"
    if match:
        return text[:match.start()] + section + text[match.end():], added
    for anchor in INSERT_BEFORE:
        position = text.find("\n" + anchor + "\n")
        if position >= 0:
            return text[:position + 1] + section + text[position + 1:], added
    return text.rstrip("\n") + "\n\n" + section, added


def touch_updated_at(text: str) -> str:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    return re.sub(r'^updated_at: .*$', f'updated_at: "{now}"', text, count=1, flags=re.M)


def main() -> int:
    parser = argparse.ArgumentParser(description="对象档案时间线")
    parser.add_argument("--entity")
    parser.add_argument("--date")
    parser.add_argument("--fact")
    parser.add_argument("--source")
    parser.add_argument("--from", dest="source_file", help="facts.json：[{entity, date, fact, source}]")
    parser.add_argument("--create", help="档案不存在时新建，写类型：人物 / 组织 / 公司 / 产品 / 模型")
    parser.add_argument("--write", action="store_true", help="写入；默认只预览")
    parser.add_argument("--vault", default=str(VAULT))
    args = parser.parse_args()
    vault = Path(args.vault).expanduser().resolve()
    if args.source_file:
        items = json.loads(Path(args.source_file).read_text(encoding="utf-8"))
    else:
        items = [{"entity": args.entity or "", "date": args.date or "",
                  "fact": args.fact or "", "source": args.source or "", "create": args.create or ""}]
    facts = [check_fact(item) for item in items]
    index = entity_index(vault)
    by_file: dict[Path, list[str]] = {}
    created: list[str] = []
    to_create: set[str] = set()
    for fact in facts:
        if key(fact["entity"]) in to_create:
            print(f"  （建档后记入）- {fact['date']} {fact['fact']}")
            continue
        if fact.get("create") and not index.get(key(fact["entity"])):
            if not args.write:
                to_create.add(key(fact["entity"]))
                print(f"将新建对象档案：{fact['entity']}（{fact['create']}）")
                print(f"  （建档后记入）- {fact['date']} {fact['fact']}")
                continue
            path = create_entity(vault, fact)
            created.append(path.relative_to(vault).as_posix())
            index = entity_index(vault)
        path = find_entity(vault, fact["entity"], index)
        by_file.setdefault(path, []).append(f"- {fact['date']} {fact['fact']}（来源：{fact['source']}）")
    for item in created:
        print(f"新建对象档案 {item}")
    total = 0
    for path, lines in by_file.items():
        text = path.read_text(encoding="utf-8")
        updated, added = add_lines(text, lines)
        total += len(added)
        print(f"{path.relative_to(vault).as_posix()}：新增 {len(added)} 条"
              + (f"，{len(lines) - len(added)} 条已记过" if len(lines) > len(added) else ""))
        for line in added:
            print(f"  {line}")
        if args.write and added:
            path.write_text(touch_updated_at(updated), encoding="utf-8")
    print(f"共新增 {total} 条" + ("" if args.write else "（预览，加 --write 写入）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
