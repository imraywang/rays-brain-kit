#!/usr/bin/env python3
"""Create a guarded daily Git snapshot for content files only."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_VAULT = (
    Path.home()
    / "Library/Mobile Documents/iCloud~md~obsidian/Documents/rays-brain"
)
VAULT = Path(os.environ.get("RAYS_BRAIN", str(DEFAULT_VAULT))).resolve()
STATE_DIR = Path(
    os.environ.get(
        "RAYS_BRAIN_STATE",
        str(Path.home() / ".local/state/rays-brain"),
    )
)
LOCK_DIR = STATE_DIR / "content-snapshot.lock"
LOCK_INFO_FILE = LOCK_DIR / "owner.json"
SUCCESS_FILE = STATE_DIR / "content-snapshot-success.json"
LOCK_GRACE_SECONDS = 60
WEREAD_SYNC = VAULT / "50-系统/40-自动化/知识采集/weread_sync.py"
CONTENT_PATHS = (
    "00-入口",
    "10-创作",
    "20-知识",
    "30-资料",
    "40-发布",
    "70-思考",
    "90-归档",
    "HOME.md",
    "README.md",
)
ALLOWED_PATH = re.compile(
    r"^(?:00-入口|10-创作|20-知识|30-资料|40-发布|70-思考|"
    r"90-归档)/|^(?:HOME|README)\.md$"
)


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}")


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=VAULT,
        text=True,
        capture_output=True,
        check=check,
    )


def staged_paths() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z"],
        cwd=VAULT,
        capture_output=True,
        check=True,
    )
    return [
        raw.decode("utf-8", errors="surrogateescape")
        for raw in result.stdout.split(b"\0")
        if raw
    ]


def validate() -> None:
    if git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        raise RuntimeError(f"不是 Git 仓库：{VAULT}")
    if not WEREAD_SYNC.is_file():
        raise RuntimeError(f"找不到微信读书同步脚本：{WEREAD_SYNC}")
    missing = [item for item in CONTENT_PATHS if not (VAULT / item).exists()]
    if missing:
        raise RuntimeError("内容白名单路径不存在：" + "、".join(missing))


def weread_key() -> str:
    if os.environ.get("WEREAD_API_KEY"):
        return str(os.environ["WEREAD_API_KEY"])
    zshrc = Path.home() / ".zshrc"
    if not zshrc.exists():
        return ""
    for line in zshrc.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^export\s+WEREAD_API_KEY=(.+)$", line.strip())
        if match:
            return match.group(1).strip().strip("\"'")
    return ""


def write_success(status: str, commit: str = "") -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"status": status, "completed_at": now_text()}
    if commit:
        data["commit"] = commit
    temporary = SUCCESS_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(SUCCESS_FILE)


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def inspect_lock() -> dict[str, Any]:
    if not LOCK_DIR.exists():
        return {"state": "clear"}
    try:
        age_seconds = max(0.0, datetime.now().timestamp() - LOCK_DIR.stat().st_mtime)
    except OSError:
        age_seconds = 0.0
    info: dict[str, Any] = {}
    try:
        info = json.loads(LOCK_INFO_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    try:
        pid = int(info.get("pid", 0))
    except (TypeError, ValueError):
        pid = 0
    if pid and process_is_running(pid):
        return {
            "state": "active",
            "pid": pid,
            "started_at": str(info.get("started_at", "")),
            "age_seconds": age_seconds,
        }
    if not pid and age_seconds < LOCK_GRACE_SECONDS:
        return {"state": "starting", "age_seconds": age_seconds}
    return {
        "state": "stale",
        "pid": pid,
        "started_at": str(info.get("started_at", "")),
        "age_seconds": age_seconds,
    }


def acquire_lock() -> bool:
    for _ in range(2):
        try:
            LOCK_DIR.mkdir()
        except FileExistsError:
            lock = inspect_lock()
            if lock["state"] != "stale":
                log("已有内容快照在运行，本次跳过")
                return False
            unexpected = [
                path.name
                for path in LOCK_DIR.iterdir()
                if path.name != LOCK_INFO_FILE.name
            ]
            if unexpected:
                raise RuntimeError(
                    "内容快照遗留锁包含未知文件，需人工检查："
                    + "、".join(unexpected)
                )
            LOCK_INFO_FILE.unlink(missing_ok=True)
            LOCK_DIR.rmdir()
            log("已清理失效的内容快照锁")
            continue
        LOCK_INFO_FILE.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "started_at": now_text(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return True
    raise RuntimeError("无法取得内容快照运行锁")


def release_lock() -> None:
    try:
        info = json.loads(LOCK_INFO_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        info = {}
    if info.get("pid") != os.getpid():
        return
    LOCK_INFO_FILE.unlink(missing_ok=True)
    try:
        LOCK_DIR.rmdir()
    except OSError:
        pass


def run_snapshot() -> int:
    validate()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not acquire_lock():
        return 0

    staged_by_this_run = False
    try:
        if git("diff", "--cached", "--quiet", check=False).returncode != 0:
            log("索引非空（疑似进行中的人工提交），本次快照跳过")
            return 0

        key = weread_key()
        if key:
            environment = dict(os.environ)
            environment["WEREAD_API_KEY"] = key
            sync = subprocess.run(
                [sys.executable, str(WEREAD_SYNC)],
                cwd=VAULT,
                env=environment,
                text=True,
            )
            if sync.returncode != 0:
                log("微信读书同步失败，继续快照")
        else:
            log("未找到 WEREAD_API_KEY，跳过微信读书同步")

        staged_by_this_run = True
        try:
            git("add", "-A", "--", *CONTENT_PATHS)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            raise RuntimeError(
                "内容加入快照失败" + (f"：{detail}" if detail else "")
            ) from exc
        staged_files = staged_paths()
        unexpected = [path for path in staged_files if not ALLOWED_PATH.search(path)]
        if unexpected:
            raise RuntimeError(
                "暂存区出现白名单外文件：" + "、".join(unexpected)
            )

        if not staged_files:
            staged_by_this_run = False
            write_success("no-change")
            log("内容无变化，跳过快照")
            return 0

        message = "auto: 内容快照 " + datetime.now().strftime("%Y-%m-%d %H:%M")
        git("commit", "-q", "-m", message)
        staged_by_this_run = False
        commit = git("rev-parse", "HEAD").stdout.strip()
        write_success("committed", commit)
        log("快照完成：" + git("log", "--oneline", "-1").stdout.strip())
        return 0
    finally:
        if staged_by_this_run:
            git("restore", "--staged", "--", *CONTENT_PATHS, check=False)
        release_lock()


def main() -> int:
    parser = argparse.ArgumentParser(description="为内容目录建立每日安全快照")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查仓库、同步脚本和白名单，不同步、暂存或提交",
    )
    args = parser.parse_args()
    if args.check:
        validate()
        lock = inspect_lock()
        if lock["state"] == "stale":
            raise RuntimeError("发现失效的内容快照锁，正式运行时会自动清理")
        log("内容快照检查通过：仓库、同步脚本和内容白名单均可用")
        return 0
    return run_snapshot()


if __name__ == "__main__":
    raise SystemExit(main())
