#!/usr/bin/env bash
# Run a harness that starts torch_npu.profiler in the processes emitting MSTX.
# The harness should write its database exports under MSTX_TRACE_DIR.
set -euo pipefail

if [[ $# -lt 3 || "$2" != "--" ]]; then
    echo "用法: $0 <新输出目录> -- <已启用 torch_npu.profiler 的命令> [参数...]" >&2
    exit 2
fi

DEST="$1"
shift 2
SITE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/mstx" && pwd)"
mkdir -p -- "$(dirname -- "$DEST")"
mkdir -- "$DEST"
DEST="$(cd -- "$DEST" && pwd)"

PYTHONPATH="$SITE${PYTHONPATH:+:$PYTHONPATH}" VLLM_MSTX_PATCH=1 MSTX_TRACE_DIR="$DEST" \
    "$@" > "$DEST/workload.log" 2>&1 || {
        code=$?
        echo "采集失败（退出 $code），见 $DEST/workload.log" >&2
        exit "$code"
    }

"${PYTHON:-python3}" - "$DEST" <<'PY'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

root = Path(sys.argv[1])
found = False
for db in sorted(root.rglob("*.db")):
    with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='MSTX_EVENTS'"
        ).fetchone()
        if table and connection.execute("SELECT 1 FROM MSTX_EVENTS LIMIT 1").fetchone():
            print(db)
            found = True
if not found:
    print(f"{root}: 未找到带 MSTX_EVENTS 的数据库，请检查进程内 profiler 配置", file=sys.stderr)
    sys.exit(1)
PY
