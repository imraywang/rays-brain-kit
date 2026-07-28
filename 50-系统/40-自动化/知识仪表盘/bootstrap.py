#!/usr/bin/env python3
"""初始化一个可直接被知识仪表盘使用的 vault 骨架。

用法：
    python3 bootstrap.py <vault路径>          # 只建目录骨架，缺什么补什么
    python3 bootstrap.py <vault路径> --demo   # 另写入演示数据，便于第一次预览工作台

既有文件一律不动，重复执行安全。目录布局读取与 server.py 相同的配置
（同目录 config.json，或环境变量 RAYS_BRAIN_CONFIG 指定的文件）。
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent

SKELETON_KEYS = (
    "review_dir",
    "topics_dir",
    "topic_reserve_dir",
    "writing_tasks_dir",
    "drafts_dir",
    "knowledge_dir",
    "sources_dir",
    "published_dir",
    "feedback_dir",
    "archive_dir",
)

REVIEW_CARD = """---
kind: capture-review
status: 待审核
recommendation: {recommendation}
relevance_score: {score}
knowledge_value_score: {knowledge_value_score}
writing_value_score: {writing_value_score}
timeliness: {timeliness}
suggested_kind: {suggested_kind}
source_url: "{source_url}"
---

# {title}

## 摘要

{summary}

## 人工审核

{review_choices}
"""


def load_layout() -> dict[str, str]:
    spec = importlib.util.spec_from_file_location("rays_dashboard_server", HERE / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return dict(module.LAYOUT)


def review_choices() -> str:
    spec = importlib.util.spec_from_file_location("rays_dashboard_protocol", HERE / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return "\n".join(f"- [ ] {label}" for label in module.ACTION_LABELS.values())


def write_if_missing(vault: Path, relative: str, content: str, created: list[str]) -> None:
    target = vault / relative
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    created.append(relative)


def build_vault(vault: Path, layout: dict[str, str], demo: bool = False) -> list[str]:
    created: list[str] = []
    for key in SKELETON_KEYS:
        target = vault / layout[key]
        if not target.exists():
            target.mkdir(parents=True)
            created.append(layout[key] + "/")
    write_if_missing(vault, layout["inbox_file"], "# 灵感收件箱\n", created)
    if demo:
        review = layout["review_dir"]
        write_if_missing(vault, f"{review}/演示卡-AI工作流.md", REVIEW_CARD.format(
            title="AI 工作流正在吃掉传统 SaaS 的交互层",
            score=93, recommendation="保留", suggested_kind="viewpoint",
            knowledge_value_score=88, writing_value_score=96, timeliness="高",
            source_url="https://example.com/agent-workflows",
            summary="演示数据：一条高价值观点。选「沉淀为知识」看它流向长期知识库。",
            review_choices=review_choices(),
        ), created)
        write_if_missing(vault, f"{review}/演示卡-营销噪音.md", REVIEW_CARD.format(
            title="某产品发布会的营销通稿",
            score=42, recommendation="清理", suggested_kind="",
            knowledge_value_score=25, writing_value_score=32, timeliness="低",
            source_url="https://example.com/press-release",
            summary="演示数据：低价值内容，适合练习「移入待清理」。",
            review_choices=review_choices(),
        ), created)
        write_if_missing(vault, f"{layout['topics_dir']}/演示候选-模型需要背景.md",
            "---\nkind: topic-candidate\nstatus: candidate\npriority_score: 94\nwriting_value_score: 97\nknowledge_value_score: 84\ntime_sensitivity: 常青\nsource_published_at: 2099-01-01\nfresh_until: 2099-12-31\nfreshness_status: fresh\n---\n\n# 新模型需要的不是命令，而是背景\n\n## 一句话判断\n\n演示数据：一个已经通过审核、可继续立项的候选选题。\n", created)
        write_if_missing(vault, f"{layout['writing_tasks_dir']}/演示任务-知识管道.md",
            "---\nkind: writing-task\nstatus: active\npriority_score: 90\nwriting_value_score: 92\nknowledge_value_score: 80\ntimeliness: 中\n---\n\n# 写作任务：我的知识管道\n\n## 一句话判断\n\n演示数据：一个已经立项、等待推进的写作任务。\n", created)
        write_if_missing(vault, f"{layout['knowledge_dir']}/10-概念/演示-上下文工程.md",
            "---\nkind: concept\nstatus: seed\n---\n\n# 上下文工程\n\n演示数据：一条长期知识。\n", created)
        write_if_missing(vault, f"{layout['knowledge_dir']}/40-观点/演示-工作台是流程的镜子.md",
            "---\nkind: viewpoint\nstatus: seed\n---\n\n# 工作台是流程的镜子\n\n演示数据：另一种知识类型。\n", created)
        write_if_missing(vault, f"{layout['drafts_dir']}/演示草稿-知识管道.md",
            "---\nkind: draft\nstatus: drafting\nknowledge_value_score: 80\nwriting_value_score: 92\ntimeliness: 中\n---\n\n# 我的知识管道是怎么跑起来的\n\n演示数据：一篇写作中的草稿。\n", created)
        write_if_missing(vault, f"{layout['published_dir']}/30-公众号/演示-从收藏到成稿.md",
            "---\nkind: article-published\nstatus: published\nplatform: 公众号\n---\n\n# 从收藏到成稿\n\n演示数据：一篇已发布的成品。\n", created)
        write_if_missing(vault, f"{layout['feedback_dir']}/演示反馈-从收藏到成稿.md",
            "---\nkind: content-feedback\nstatus: pending\ndue_date: 2026-07-30\n---\n\n# 待复盘：从收藏到成稿\n\n演示数据：发布后等待补充数据并复盘。\n", created)
    return created


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化知识仪表盘所需的 vault 骨架")
    parser.add_argument("vault", help="目标 vault 路径（空目录或既有库都可以）")
    parser.add_argument("--demo", action="store_true", help="写入演示数据，便于第一次预览工作台")
    args = parser.parse_args()
    vault = Path(args.vault).expanduser().resolve()
    vault.mkdir(parents=True, exist_ok=True)
    created = build_vault(vault, load_layout(), demo=args.demo)
    if created:
        print("已创建：")
        for item in created:
            print(f"  {item}")
    else:
        print("骨架已完整，没有需要补的内容。")
    print()
    print("启动工作台：")
    print(f'  RAYS_BRAIN="{vault}" python3 "{HERE / "server.py"}"')


if __name__ == "__main__":
    main()
