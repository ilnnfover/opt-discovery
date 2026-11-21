#!/usr/bin/env bash
# 对 torch_npu.profiler 落下的 raw 数据做离线导出。
#
# 为什么需要：在线 `llm.stop_profile()` 会顺带触发 msprof 解析导出，导出耗时
# 超过 RPC 超时会报 `cancelled` 并让 EngineCore 退出，db 往往来不及生成；但
# raw 目录（PROF_*/device_N、host）是完整的，离线再解析即可。
#
# 用法：
#   ./scripts/mstx/export_offline.sh <PROF_* 目录 | 含 PROF_* 的 rank 目录> [--with-text]
#
# 产出：<PROF 目录>/msprof_*.db（含 TASK / CANN_API / MSTX_EVENTS）
#       加 --with-text 时另出 mindstudio_profiler_output/*.csv
set -euo pipefail

MSPROF="${MSPROF:-/usr/local/Ascend/cann-9.1.0/bin/msprof}"
TARGET="${1:?用法: $0 <PROF_* 目录 | rank 目录> [--with-text]}"
WITH_TEXT="${2:-}"

if [[ ! -d "$TARGET" ]]; then
  echo "[FAIL] $TARGET 不是目录" >&2
  exit 2
fi

mapfile -t DIRS < <(find "$TARGET" -maxdepth 1 -name 'PROF_*' -type d | sort)
if [[ ${#DIRS[@]} -eq 0 ]]; then
  if [[ "$(basename "$TARGET")" == PROF_* ]]; then
    DIRS=("$TARGET")
  else
    echo "[FAIL] $TARGET 下没有 PROF_* 目录" >&2
    exit 2
  fi
fi

for dir in "${DIRS[@]}"; do
  echo "== 解析 $dir"
  "$MSPROF" --parse=on --output="$dir" >/dev/null
  echo "== 导出 db"
  "$MSPROF" --export=on --type=db --output="$dir" >/dev/null
  if [[ "$WITH_TEXT" == "--with-text" ]]; then
    echo "== 导出 text/csv"
    "$MSPROF" --export=on --type=text --summary-format=csv --output="$dir" >/dev/null
  fi
done

echo "== 自检 MSTX_EVENTS"
python3 - "${DIRS[@]}" <<'PY'
import glob
import sqlite3
import sys

missing = 0
for directory in sys.argv[1:]:
    dbs = sorted(glob.glob(f"{directory}/msprof_*.db"))
    if not dbs:
        print(f"  [FAIL] {directory}: 没有生成 msprof_*.db")
        missing += 1
        continue
    db = dbs[-1]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
    n = con.execute("select count(*) from MSTX_EVENTS").fetchone()[0] if "MSTX_EVENTS" in tables else 0
    print(f"  {db.split('/')[-2]}: MSTX_EVENTS={n} TASK={'TASK' in tables} CANN_API={'CANN_API' in tables}")
    if "MSTX_EVENTS" not in tables or n == 0:
        missing += 1
raise SystemExit(1 if missing else 0)
PY
