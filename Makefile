# 统一检查入口（AE-01）。底层命令仍可单独执行，CI 直接复用这些 target。
PYTHON ?= python3
DEMO_OUT ?= out/demo-check

.PHONY: help check lint arch-lint typecheck validate demo test verify verify-real clean-env-check clean

help:
	@echo "make check       静态检查 + 类型检查 + CLI validate（无需 NPU/真实数据）"
	@echo "make verify      普通测试 + 回归 + synthetic demo"
	@echo "make verify-real 真实数据回归（缺数据明确 BLOCKED 且非零退出）"
	@echo "make clean-env-check 干净 venv 从零安装并跑全部检查（PYTHON=3.10 可指定版本）"
	@echo "make lint / typecheck / validate / demo / test  单独执行"

# 架构/领域检查（AE-05）尚未实现；实现后在此接入，而不是假装已存在。
check: lint arch-lint typecheck validate

lint:
	$(PYTHON) -m ruff check .

arch-lint:
	$(PYTHON) -m tools.arch_lint --config configs/quality/arch_lint.yaml

typecheck:
	$(PYTHON) -m mypy

validate:
	$(PYTHON) -m opt_discovery.cli validate

demo:
	$(PYTHON) -m opt_discovery.cli demo --out $(DEMO_OUT)

test:
	# 不要再传 -q：pyproject 的 addopts 已含 -q，双重 -q 会吞掉 "N passed / N skipped" 摘要
	$(PYTHON) -m pytest

verify: test demo

verify-real:
	$(PYTHON) scripts/verify_real.py

# 干净 venv + 从零 pip 安装 + 全部检查（可用 PYTHON=python3.10 指定版本）
clean-env-check:
	PYTHON=$(PYTHON) bash scripts/check_clean_env.sh

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache $(DEMO_OUT)
