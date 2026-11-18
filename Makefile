# 统一检查入口（AE-01）。底层命令仍可单独执行，CI 直接复用这些 target。
PYTHON ?= python3
DEMO_OUT ?= out/demo-check

.PHONY: help check lint typecheck validate demo test verify verify-real clean

help:
	@echo "make check       静态检查 + 类型检查 + CLI validate（无需 NPU/真实数据）"
	@echo "make verify      普通测试 + 回归 + synthetic demo"
	@echo "make verify-real 真实数据回归（缺数据明确 BLOCKED 且非零退出）"
	@echo "make lint / typecheck / validate / demo / test  单独执行"

# 架构/领域检查（AE-05）尚未实现；实现后在此接入，而不是假装已存在。
check: lint typecheck validate

lint:
	$(PYTHON) -m ruff check .

typecheck:
	$(PYTHON) -m mypy

validate:
	$(PYTHON) -m opt_discovery.cli validate

demo:
	$(PYTHON) -m opt_discovery.cli demo --out $(DEMO_OUT)

test:
	$(PYTHON) -m pytest -q

verify: test demo

verify-real:
	$(PYTHON) scripts/verify_real.py

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache $(DEMO_OUT)
