#!/usr/bin/env bash
# 复采一个小窗口，用于验证跨执行模式的差异（decode Eager vs Graph 的 15x、通信记录数差异等）。
#
# 事实（已核对 $NPU_STACK）：
#   * harness 是 $NPU_STACK/qwen35-*.py，纯 vLLM 进程内 API，参数硬编码，无 CLI 参数；
#   * 脚本自己打印 PROFILE_START_ABS / ACTIVE_ROUND / PROFILE_STOP_ABS，
#     这些 stdout 就是本工具切窗用的 workload.log；
#   * **脚本里没有调用 msprof**，profiling 是由外部命令启动的，而那条命令没有被记录
#     （没有 run.sh、没有 shell history）。这是需要补的缺口。
#
# 用法：
#   ./scripts/recollect_window.sh prefill eager  $PROF_ROOT/recapture
#   ./scripts/recollect_window.sh prefill graph  $PROF_ROOT/recapture
#   ./scripts/recollect_window.sh decode  eager  $PROF_ROOT/recapture
#   ./scripts/recollect_window.sh decode  graph  $PROF_ROOT/recapture

set -euo pipefail

PHASE="${1:-}"
MODE="${2:-}"
OUT_ROOT="${3:-${PROF_ROOT:-./exports}/recapture}"
STACK_DIR="${STACK_DIR:-${NPU_STACK:-./npu-stack}}"

case "$PHASE/$MODE" in
  prefill/eager) SCRIPT="qwen35-prefill-only.py" ;;
  prefill/graph) SCRIPT="qwen35-prefill-graph-noex.py" ;;
  decode/eager)  SCRIPT="qwen35-decode-only.py" ;;
  decode/graph)  SCRIPT="qwen35-decode-graph-noex.py" ;;
  *) echo "用法: $0 {prefill|decode} {eager|graph} [输出根目录]" >&2; exit 2 ;;
esac

echo "== 1. 环境检查"
command -v msprof >/dev/null 2>&1 || {
  echo "  [FAIL] 找不到 msprof（先 source /usr/local/Ascend/*/bin/setenv.bash）" >&2; exit 3; }
if ! msprof --help >/dev/null 2>&1; then
  echo "  [FAIL] msprof 无法初始化平台（缺少 NPU 驱动）：" >&2
  msprof --help 2>&1 | head -3 >&2
  exit 4
fi
echo "  [OK] msprof 可用"
[[ -f "$STACK_DIR/$SCRIPT" ]] || { echo "  [FAIL] 找不到 $STACK_DIR/$SCRIPT" >&2; exit 5; }
echo "  [OK] harness 脚本: $STACK_DIR/$SCRIPT"

DEST="$OUT_ROOT/$(date +%Y%m%d-%H%M%S)-${PHASE}-${MODE}"
mkdir -p "$DEST"

cat <<EOF

== 2. 采集参数（来自 $SCRIPT，硬编码）
EOF
grep -n "LLM(\|SamplingParams(\|range(" "$STACK_DIR/$SCRIPT" | sed 's/^/  /'

cat <<EOF

== 3. 执行（stdout 必须落成 workload.log，否则无法切窗）

  # 3.1 采集：msprof 的具体参数未记录在仓库里，请按本机 msprof --help 补齐
  msprof --output=$DEST/PROF \\
         --application="python3 $STACK_DIR/$SCRIPT" \\
         > $DEST/workload.log 2>&1

  # 3.2 如果只拿到 PROF_* 而没有 CSV，再单独导出一次
  msprof --export=on --dir=$DEST/PROF/PROF_xxx \\
         --output=$DEST/PROF/PROF_xxx/mindstudio_profiler_output

== 4. 采集完成后逐步验证

  export NEW=$DEST/workload.log
  export WL=configs/workloads/qwen35-35b-a3b-910b/${PHASE}_${MODE}.yaml

  # 4.1 轮次是否与历史一致（应仍是 0..4，共 5 轮）
  python -m opt_discovery.cli rounds --export $DEST/PROF/..

  # 4.2 通信字节量（跨模式唯一可比的量）
  python -m opt_discovery.cli comm --export $DEST/PROF/.. --workload \$WL

  # 4.3 解析 + 时间线
  python -m opt_discovery.cli parse-csv --mapping configs/parsers/ascend_op_summary.yaml \\
      --workload \$WL --log $DEST/workload.log --rounds 1-4 \\
      --csv '$DEST/PROF/PROF_*/mindstudio_profiler_output/op_summary*.csv' \\
      --out $DEST/table.json
  python -m opt_discovery.cli timeline --export $DEST/PROF/.. --workload \$WL --rounds 1-4

  # 4.4 与历史基线对比
  python -m opt_discovery.cli compare \\
      --a out/steady/qwen35-35b-a3b-${PHASE}-${MODE}/operator_table.json \\
      --b $DEST/table.json

== 5. 判定标准（写死，避免事后解释）

  并发/形状：同一 harness 下应保持 1 个 prompt；graph 的 M 会因
             cudagraph_capture_sizes 最小桶（16）被 pad，属预期，不是并发变化。
  通信：     字节量应与历史同级（±25%）；记录数在 graph 下少 5~7 倍属正常（记录被合并）。
  15x 差异： 若新采集的 MatMulV2 单次耗时仍 ~140us（eager）/~9us（graph）→ 差异可复现；
             若 eager 侧回到 ~10us 量级 → 历史 eager 数据受采集状态影响，旧结论作废。
EOF
