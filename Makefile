# drive_housekeeper — common workflows.
# Run `make` or `make help` to list targets. Override variables like:
#   make quickstart SOURCE=/path/to/drive
#   make install PY=python3.11

PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
WORKSPACE ?= workspace
SOURCE ?=

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Variables: SOURCE=<drive path>  WORKSPACE=$(WORKSPACE)  PY=$(PY)"

$(BIN)/python: ## (internal) create the virtualenv
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip

.PHONY: install
install: $(BIN)/python rust ## Install the tool with all optional features
	$(BIN)/pip install -e '.[analysis,dashboard]'

.PHONY: rust
rust: ## Build the optional Rust hashing accelerator into $(VENV)/bin (skipped if cargo is missing)
	@if command -v cargo >/dev/null 2>&1; then \
		cd rust && cargo build --release && \
		install -m 755 target/release/housekeeper-core ../$(BIN)/housekeeper-core && \
		echo "housekeeper-core installed to $(BIN)/housekeeper-core"; \
	else \
		echo "cargo not found — skipping Rust accelerator; falling back to the pure-Python backend" >&2; \
	fi

.PHONY: install-dev
install-dev: $(BIN)/python ## Install everything plus the test/lint toolchain
	$(BIN)/pip install -e '.[analysis,dashboard,dev]'

.PHONY: quickstart
quickstart: ## One command: scan+analyse+classify+report a drive (read-only). Needs SOURCE=<path>
	@test -n "$(SOURCE)" || { echo "error: set SOURCE=<drive path>, e.g. make quickstart SOURCE=/mnt/drive" >&2; exit 2; }
	@test -x "$(BIN)/housekeeper" || { echo "error: entrypoint missing at $(BIN)/housekeeper — run 'make install' first" >&2; exit 2; }
	$(BIN)/housekeeper --workspace $(WORKSPACE) quickstart "$(SOURCE)"

.PHONY: dashboard
dashboard: ## Launch the local, loopback-only review dashboard
	$(BIN)/housekeeper --workspace $(WORKSPACE) dashboard

.PHONY: test
test: ## Run the test suite
	$(BIN)/python -m pytest -q

.PHONY: lint
lint: ## Lint with ruff
	$(BIN)/python -m ruff check src tests

.PHONY: typecheck
typecheck: ## Type-check with mypy
	$(BIN)/python -m mypy src

.PHONY: check
check: lint typecheck test ## Run lint + typecheck + tests (the full gate)

.PHONY: benchmark
benchmark: ## Compare a fresh benchmark run against the committed baseline
	$(BIN)/housekeeper --workspace $(WORKSPACE) benchmark compare

.PHONY: benchmark-baseline
benchmark-baseline: ## Record/refresh the committed benchmark baseline
	$(BIN)/housekeeper --workspace $(WORKSPACE) benchmark baseline

.PHONY: clean
clean: ## Remove caches and build artifacts (never touches your workspace data)
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

.PHONY: distclean
distclean: clean ## Also remove the virtualenv
	rm -rf $(VENV)
