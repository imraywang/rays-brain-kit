#!/usr/bin/env python3
"""Create a verified, versioned Rays Brain recovery snapshot on another disk."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_VAULT = Path(
    os.environ.get(
        "RAYS_BRAIN",
        str(Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents/rays-brain"),
    )
).resolve()
DEFAULT_DESTINATION = Path("/Volumes/协作/RaysBrain-Recovery")
STATE_HOME = Path(
    os.environ.get("RAYS_BRAIN_STATE", str(Path.home() / ".local/state/rays-brain"))
)
SNAPSHOT_NAME = re.compile(r"^snapshot-\d{8}-\d{6}$")
EXCLUDES = (
    ".git/",
    ".obsidian/",
    "/.claude/worktrees/",
    ".DS_Store",
    "**/__pycache__/",
    "50-系统/40-自动化/对标拆解/media/",
)
RUNTIME_FILES = (
    "state.json",
    "map-digest.json",
    "topic-map-candidates.json",
    "content-snapshot-success.json",
    "weread_state.json",
)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def ignored(relative: Path) -> bool:
    parts = relative.parts
    if not parts:
        return False
    # 与 rsync 的排除规则保持一致：".git/"、".obsidian/" 在任意层级都排除；临时工作树整体不备份
    if ".git" in parts[:-1] or ".obsidian" in parts[:-1] or "__pycache__" in parts:
        return True
    if relative.as_posix().startswith(".claude/worktrees/"):
        return True
    if relative.name == ".DS_Store":
        return True
    return relative.as_posix().startswith("50-系统/40-自动化/对标拆解/media/")


def manifest(root: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ignored(relative) or not path.is_file():
            continue
        result[relative.as_posix()] = {
            "size": path.stat().st_size,
            "sha256": digest(path),
        }
    return result


def validate_destination(source: Path, destination: Path, require_separate_device: bool) -> None:
    destination = destination.resolve()
    if destination == Path("/") or destination == Path.home() or len(destination.parts) < 4:
        raise RuntimeError(f"备份目录范围过大：{destination}")
    parent = destination
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if require_separate_device:
        if not str(destination).startswith("/Volumes/"):
            raise RuntimeError("独立备份必须写到 /Volumes 下的另一块磁盘")
        if source.stat().st_dev == parent.stat().st_dev:
            raise RuntimeError("备份目录与知识库位于同一块磁盘")


def rsync(source: Path, target: Path, previous: Path | None = None) -> None:
    command = ["rsync", "-a", "--delete"]
    for item in EXCLUDES:
        command += ["--exclude", item]
    if previous and previous.is_dir():
        command.append(f"--link-dest={previous}")
    command += [str(source) + "/", str(target) + "/"]
    subprocess.run(command, check=True)


def latest_snapshot(destination: Path) -> Path | None:
    latest = destination / "latest"
    if latest.is_symlink():
        target = (destination / os.readlink(latest)).resolve()
        if target.parent == destination.resolve() and SNAPSHOT_NAME.fullmatch(target.name):
            return target
    snapshots = sorted(
        path for path in destination.glob("snapshot-*")
        if path.is_dir() and SNAPSHOT_NAME.fullmatch(path.name)
    )
    return snapshots[-1] if snapshots else None


def run_backup(
    source: Path,
    destination: Path,
    *,
    keep: int = 8,
    require_separate_device: bool = True,
) -> dict[str, object]:
    source = source.resolve()
    destination = destination.resolve()
    if not (source / ".git").is_dir():
        raise RuntimeError(f"知识库缺少本地历史记录：{source}")
    validate_destination(source, destination, require_separate_device)
    destination.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final = destination / f"snapshot-{timestamp}"
    if final.exists():
        raise RuntimeError(f"备份目标已存在：{final}")
    staging = destination / f".staging-{timestamp}-{os.getpid()}"
    staging.mkdir()
    try:
        previous = latest_snapshot(destination)
        vault_copy = staging / "vault"
        vault_copy.mkdir()
        rsync(source, vault_copy, previous / "vault" if previous else None)

        runtime_copy = staging / "runtime-state"
        runtime_copy.mkdir()
        for name in RUNTIME_FILES:
            path = STATE_HOME / name
            if path.is_file():
                shutil.copy2(path, runtime_copy / name)

        subprocess.run(
            ["git", "-C", str(source), "bundle", "create", str(staging / "history.bundle"), "--all"],
            check=True,
            capture_output=True,
            text=True,
        )
        status = subprocess.run(
            ["git", "-C", str(source), "status", "--short", "--branch"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        (staging / "git-status.txt").write_text(status, encoding="utf-8")

        source_manifest = manifest(source)
        copied_manifest = manifest(vault_copy)
        if source_manifest != copied_manifest:
            missing = sorted(set(source_manifest) - set(copied_manifest))[:10]
            changed = sorted(
                key for key in set(source_manifest) & set(copied_manifest)
                if source_manifest[key] != copied_manifest[key]
            )[:10]
            raise RuntimeError(f"备份校验失败；缺失 {missing}；不一致 {changed}")
        total_bytes = sum(int(item["size"]) for item in copied_manifest.values())
        (staging / "manifest.json").write_text(
            json.dumps(
                {
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "source": str(source),
                    "file_count": len(copied_manifest),
                    "total_bytes": total_bytes,
                    "files": copied_manifest,
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        staging.rename(final)
        latest_tmp = destination / f".latest-{os.getpid()}"
        latest_tmp.symlink_to(final.name)
        latest_tmp.replace(destination / "latest")

        snapshots = sorted(
            path for path in destination.glob("snapshot-*")
            if path.is_dir() and SNAPSHOT_NAME.fullmatch(path.name)
        )
        for old in snapshots[:-max(1, keep)]:
            shutil.rmtree(old)
        return {
            "snapshot": str(final),
            "file_count": len(copied_manifest),
            "total_bytes": total_bytes,
            "verified": True,
            "retained": min(len(snapshots), max(1, keep)),
        }
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="把 Rays Brain 备份到另一块磁盘并逐文件校验")
    parser.add_argument("--vault", default=str(DEFAULT_VAULT))
    parser.add_argument("--destination", default=str(DEFAULT_DESTINATION))
    parser.add_argument("--keep", type=int, default=8, help="保留最近几个可恢复版本，默认 8")
    args = parser.parse_args()
    try:
        result = run_backup(Path(args.vault), Path(args.destination), keep=args.keep)
    except Exception as exc:  # noqa: BLE001 - 命令行只输出清楚的失败原因
        print(f"备份失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
