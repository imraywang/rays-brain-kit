#!/bin/zsh
# 手动入口；定时任务直接调用同目录的 Python 实现。
set -euo pipefail

HERE="${0:A:h}"
exec python3 "$HERE/content_snapshot.py" "$@"
