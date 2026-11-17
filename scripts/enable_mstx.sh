#!/usr/bin/env bash
# 一次"带仪表"的复采：同时补齐三类一直缺的证据。
#
#   1) MSTX 打点（--msproftx on → MsprofTx 表）  → host_silent / sync_wait
#   2) AIC 指标（--aic-metrics）                → aicore_utilization / arithmetic_intensity
#   3) HBM 采集（--sys-hardware-mem on）        → 带宽利用率
#
# 参数已按本机 `msprof --help`（CANN 9.1.0 / driver 26.0.rc1）核对：
#   --msproftx on | off            默认 off
#   --aic-metrics <groups>         ArithmeticUtilization,PipeUtilization,Memory,MemoryL0,
#                                  ResourceConflictRatio,MemoryUB,L2Cache,MemoryAccess（默认 PipeUtilization）
#   --sys-hardware-mem on|off      LLC/DDR/HBM 采集，默认 off
#
# 用法：
#   ./scripts/enable_mstx.sh decode $PROF_ROOT/recapture          # 默认带全部仪表
#   METRICS=off ./scripts/enable_mstx.sh decode $PROF_ROOT/recapture   # 只采基线
set -euo pipefail

PHASE="${1:-decode}"
OUT_ROOT="${2:-${PROF_ROOT:-./exports}/recapture}"
STACK="${NPU_STACK:-./npu-stack}"
SITE=<repo>/scripts/mstx
METRICS_MODE="${METRICS:-full}"      # full | off

case "$PHASE" in
  prefill) SCRIPT="$STACK/qwen35-prefill-only.py" ;;
  decode)  SCRIPT="$STACK/qwen35-decode-only.py" ;;
  graph)   SCRIPT="$STACK/qwen35-decode-graph-noex.py" ;;
  *) echo "用法: $0 {prefill|decode|graph} [输出根目录]" >&2; exit 2 ;;
esac

command -v msprof >/dev/null 2>&1 || { echo "[FAIL] 找不到 msprof（先 source setenv.bash）" >&2; exit 3; }
msprof --help >/dev/null 2>&1 || { echo "[FAIL] msprof 无法初始化（驱动/权限）" >&2; exit 4; }

# 注意：本机 msprof 只接受 --key=value，用空格分隔会报
# "Argument --msproftx: expected one argument"
INSTRUMENT=(--msproftx=on)
if [[ "$METRICS_MODE" == "full" ]]; then
  # 本机只接受**单个**指标组（传逗号列表会报 invalid value），一次采集只能选一组：
  #   ArithmeticUtilization → aicore_utilization / arithmetic_intensity
  #   Memory / MemoryUB     → 带宽类
  #   PipeUtilization       → 流水占比（op_summary 默认已有）
  INSTRUMENT+=(--aic-metrics="${METRIC_GROUP:-ArithmeticUtilization}")
  INSTRUMENT+=(--sys-hardware-mem=on)
fi

DEST="$OUT_ROOT/$(date +%Y%m%d-%H%M%S)-${PHASE}-instr"
mkdir -p "$DEST"

echo "== 采集（仪表: ${INSTRUMENT[*]}）"
echo "   PYTHONPATH=$SITE VLLM_MSTX_PATCH=1 \\"
echo "   msprof ${INSTRUMENT[*]} --output=$DEST/PROF \\"
echo "          --application=\"python3 $SCRIPT\" > $DEST/workload.log 2>&1"

PYTHONPATH="$SITE:${PYTHONPATH:-}" VLLM_MSTX_PATCH=1 \
  msprof "${INSTRUMENT[@]}" --output="$DEST/PROF" \
         --application="python3 $SCRIPT" > "$DEST/workload.log" 2>&1 || {
  echo "[WARN] 采集退出码非 0，查看 $DEST/workload.log" >&2; }

echo
echo "== 采集后自检"
DB=$(ls -d "$DEST"/PROF*/msprof_*.db 2>/dev/null | head -1 || true)
if [[ -z "$DB" ]]; then
  echo "[FAIL] 没有生成合并 db，见 $DEST/workload.log" >&2
  exit 5
fi
python3 - "$DB" <<'PY'
import sqlite3, sys
db = sys.argv[1]
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
tabs = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
print("  MsprofTx:", "有" if "MsprofTx" in tabs else "无",
      con.execute("select count(*) from MsprofTx").fetchone()[0] if "MsprofTx" in tabs else "")
for t in ("TASK", "CANN_API"):
    print(f"  {t}:", con.execute(f"select count(*) from {t}").fetchone()[0] if t in tabs else "无")
PY

echo
echo "== 再跑分析（打点存在时 host_silent 会被归到具体 Python 阶段）"
echo "  python -m opt_discovery.cli timeline --export $DEST/PROF/.. \\"
echo "      --workload configs/workloads/qwen35-35b-a3b-910b/${PHASE}_eager.yaml --rounds 1-4"
echo "  python -m opt_discovery.cli analyze --out out/steady-instr --source-map"
