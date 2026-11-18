#!/usr/bin/env bash
# 全新 CPU 环境检查（AE-01 验收项）。
#
# 做什么：
#   1) 把当前工作树复制到临时目录（不含 .git、out/、缓存、egg-info），模拟「只有源码的新机器」；
#   2) 用指定解释器建一个**干净 venv**（不继承系统 site-packages）；
#   3) `pip install --no-cache-dir -e ".[dev]"` 从零安装运行时+开发依赖；
#   4) 依次跑 make check / make verify / verify-real，并验证 console script。
#
# 用法：
#   bash scripts/check_clean_env.sh                      # 用默认 python3
#   PYTHON=python3.10 bash scripts/check_clean_env.sh    # 指定最低支持版本
#   KEEP=1 PYTHON=python3.10 bash scripts/check_clean_env.sh   # 保留临时目录便于排查
#   OPT_DISCOVERY_DATA_ROOT=<统一补采根> bash scripts/check_clean_env.sh  # 额外跑真实回归
#
# 退出码：任一步失败即非零；缺数据时 verify-real 必须是 BLOCKED(2)（脚本用 --expect-blocked 断言）。
set -euo pipefail

PYTHON_BIN="${PYTHON:-python3}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="${WORKDIR:-$(mktemp -d /tmp/opt-clean-env.XXXXXX)}"
VENV_DIR="$WORKDIR/venv"
REPO_DIR="$WORKDIR/repo"

log() { printf '\n[clean-env] %s\n' "$*"; }

cleanup() {
  local status=$?
  if [ -n "${KEEP:-}" ]; then
    printf '[clean-env] 保留临时目录: %s\n' "$WORKDIR"
  else
    rm -rf "$WORKDIR"
  fi
  exit "$status"
}
trap cleanup EXIT

log "解释器: $("$PYTHON_BIN" -c 'import sys; print(sys.version.split()[0], sys.executable)')"
log "源码:   $SRC_DIR"
log "工作区: $WORKDIR"

# 1) 只复制源码（排除本地产物/缓存/git 元数据）
mkdir -p "$REPO_DIR"
tar -C "$SRC_DIR" \
  --exclude=.git --exclude=out --exclude=export_only_prof_dir \
  --exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache \
  --exclude=.mypy_cache --exclude='*.egg-info' \
  -cf - . | tar -C "$REPO_DIR" -xf -

# 2) 干净 venv
log "创建 venv"
"$PYTHON_BIN" -m venv "$VENV_DIR"
VENV_PY="$VENV_DIR/bin/python"
"$VENV_PY" -m pip install --quiet --upgrade pip

# 3) 从零安装（禁用缓存，确保真的走依赖解析与下载）
log "安装项目与 dev 依赖（pip install --no-cache-dir -e '.[dev]'）"
(
  cd "$REPO_DIR"
  "$VENV_PY" -m pip install --quiet --no-cache-dir -e ".[dev]"
)

log "已安装: $("$VENV_PY" -c 'import sys, yaml, pytest, mypy, ruff; print(sys.version.split()[0], "| PyYAML", yaml.__version__, "| pytest", pytest.__version__)')"

# 4) 检查（必须用 venv 里的解释器，不能回落到系统 python3）
cd "$REPO_DIR"
log "make check"
make PYTHON="$VENV_PY" check

log "make verify（测试 + synthetic demo）"
make PYTHON="$VENV_PY" verify DEMO_OUT="$WORKDIR/demo"

log "console script"
"$VENV_DIR/bin/opt-discovery" --help >/dev/null
echo "[clean-env] opt-discovery --help OK"

log "verify-real"
if [ -n "${OPT_DISCOVERY_DATA_ROOT:-}" ]; then
  # 有真实数据：PASS(0) 或 SKIPPED(3) 都说明入口工作正常；SKIPPED 表示有用例
  # 不可复现，按契约不计为通过（不能在这里把它当成 PASS）。FAILED(1)/BLOCKED(2) 直接失败。
  set +e
  "$VENV_PY" scripts/verify_real.py
  real_rc=$?
  set -e
  case "$real_rc" in
    0) log "真实回归: PASS（全部用例执行通过）" ;;
    3) log "真实回归: SKIPPED（有未执行用例，不计为通过；见上方输出）" ;;
    *) echo "[clean-env] 真实回归失败（退出码 $real_rc）"; exit "$real_rc" ;;
  esac
else
  # 无真实数据：必须是 BLOCKED(2)，不能是 PASS
  "$VENV_PY" scripts/verify_real.py --expect-blocked
fi

log "PASS: 全新 CPU 环境安装与检查全部通过（$("$VENV_PY" -c 'import sys; print(sys.version.split()[0])')）"
