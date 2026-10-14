# LiteLLM Makefile
# Simple Makefile for running tests and basic development tasks

.PHONY: help test test-unit test-unit-llms test-unit-proxy-guardrails test-unit-proxy-core test-unit-proxy-misc \
	test-unit-integrations test-unit-core-utils test-unit-other test-unit-root \
	test-proxy-unit-a test-proxy-unit-b test-integration \
	info lint lint-dev format \
	install-dev install-proxy-dev install-test-deps \
	check-circular-imports check-import-safety check \
	lint-install lint-fetch-base bootstrap

# Default target
help:
	@echo "Available commands:"
	@echo "  make bootstrap          - Provision a fresh clone/worktree"
	@echo "  make install-dev        - Install development dependencies"
	@echo "  make install-proxy-dev  - Install proxy development dependencies"
	@echo "  make install-dev-ci     - Install dev dependencies (CI-compatible, pins OpenAI)"
	@echo "  make install-proxy-dev-ci - Install proxy dev dependencies (CI-compatible)"
	@echo "  make install-test-deps  - Install the full local test environment"
	@echo "  make check              - Alias for make lint"
	@echo "  make format             - Apply ruff format code formatting"
	@echo "  make format-check       - Check ruff format code formatting (matches CI)"
	@echo "  make lint               - Run all linting (Ruff, format check, circular imports, import safety)"
	@echo "  make lint-ruff          - Run Ruff linting only"
	@echo "  make lint-format        - Check ruff format formatting (matches CI)"
	@echo "  make check-circular-imports - Check for circular imports"
	@echo "  make check-import-safety - Check import safety"
	@echo "  make test               - Run all tests"
	@echo "  make test-unit          - Run unit tests (tests/test_litellm)"
	@echo "  make test-unit-llms     - Run LLM provider tests (~225 files)"
	@echo "  make test-unit-proxy-guardrails - Run proxy guardrails+mgmt tests (~51 files)"
	@echo "  make test-unit-proxy-core - Run proxy auth+client+db+hooks tests (~52 files)"
	@echo "  make test-unit-proxy-misc - Run proxy misc tests (~77 files)"
	@echo "  make test-unit-integrations - Run integration tests (~60 files)"
	@echo "  make test-unit-core-utils - Run core utils tests (~32 files)"
	@echo "  make test-unit-other    - Run other tests (caching, responses, etc., ~69 files)"
	@echo "  make test-unit-root     - Run root-level tests (~34 files)"
	@echo "  make test-proxy-unit-a  - Run proxy_unit_tests (a-o, ~20 files)"
	@echo "  make test-proxy-unit-b  - Run proxy_unit_tests (p-z, ~28 files)"
	@echo "  make test-integration   - Run integration tests"

UV := uv
UV_RUN := $(UV) run --no-sync
BASE_REF ?=
export BASE_REF
RESOLVE_BASE = python3 scripts/default_branch.py --base "$(BASE_REF)"

LINT_DEP_INSTALL ?= install-dev
LINT_DEP_BASE ?=
LINT_JOBS := $(shell sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)
LINT_OUTPUT_SYNC := $(if $(filter output-sync,$(.FEATURES)),--output-sync=target,)

# Show info
info:
	@echo "UV: $(UV)"

# Installation targets
# --inexact: sync the locked deps without pruning anything already installed, so running
# a lint/format target doesn't tear the proxy extras (prisma, websockets, ...) out from
# under a dev's venv (CI installs its own env per job, so it is unaffected by this).
install-dev:
	$(UV) sync --inexact --frozen

# Deliberately unqueued: provisioning is I/O bound, so it doesn't need one of the
# machine-wide slots the CPU-bound gates below share.
bootstrap:
	$(UV) sync --inexact --frozen --extra proxy --group proxy-dev --group e2e-dev
	$(UV_RUN) python scripts/prisma_generate_if_needed.py
	cd ui/litellm-dashboard && ../../scripts/with_dashboard_node.sh npm install --no-audit --no-fund
	@main_root=$$(git worktree list --porcelain | head -1 | sed 's/^worktree //'); \
	if [ "$$main_root" != "$$(git rev-parse --show-toplevel)" ] && [ -f "$$main_root/.env" ] && [ ! -f .env ]; then \
		cp "$$main_root/.env" .env && echo "bootstrap: copied .env from $$main_root"; \
	else \
		echo "bootstrap: .env left untouched"; \
	fi
	@echo "bootstrap: done"

install-proxy-dev:
	$(UV) sync --frozen --group proxy-dev --extra proxy

# CI-compatible installations (matches GitHub workflows exactly)
install-dev-ci:
	$(UV) sync --frozen

install-proxy-dev-ci:
	$(UV) sync --frozen --group proxy-dev --extra proxy

install-test-deps: install-proxy-dev
	$(UV) sync --frozen --all-groups --all-extras
	$(UV_RUN) prisma generate --schema litellm/proxy/schema.prisma

# Formatting
# Wrap width is ruff.toml's single source of truth (line-length = 120), shared by the
# formatter and the import sorter so there's no 88-vs-120 split to reconcile.
format: install-dev
	cd litellm && $(UV_RUN) ruff format . && cd ..

format-check: install-dev
	cd litellm && $(UV_RUN) ruff format --check . && cd ..

lint-fetch-base:
	@$(RESOLVE_BASE)

# Mirror test-linting.yml's lint job environment: the proxy-dev group plus a generated
# Prisma client, so lint resolves the same modules CI does. --inexact tops up the venv
# instead of pruning the proxy extras gen:api and the running proxy need.
lint-install:
	$(UV) sync --inexact --frozen --group proxy-dev --group e2e-dev
	$(UV_RUN) python scripts/prisma_generate_if_needed.py

# Diff-scoped format check, mirroring test-linting.yml's "Check ruff format" step:
# only the litellm Python files changed vs the base are checked, so a pre-existing
# format issue elsewhere doesn't block an unrelated commit. Git pathspecs match
# recursively, so 'litellm/*.py' covers top-level files and nested modules alike,
# the same set CI's ':(glob)litellm/**/*.py' selects.
lint-format-check-changed: $(LINT_DEP_INSTALL) $(LINT_DEP_BASE)
	@base_ref=$$($(RESOLVE_BASE)) && \
	files=$$(git diff --name-only --diff-filter=ACMR "$$base_ref...HEAD" -- 'litellm/*.py') || exit $$?; \
	if [ -z "$$files" ]; then \
		echo "No changed litellm Python files to format-check."; \
	else \
		echo "$$files" | xargs $(UV_RUN) ruff format --check; \
	fi

# Linting targets
lint-ruff: $(LINT_DEP_INSTALL)
	cd litellm && $(UV_RUN) ruff check . && cd ..
	$(UV_RUN) ruff check --config ruff-tests.toml tests

# faster linter for developing ...
# inspiration from:
# https://github.com/astral-sh/ruff/discussions/10977
# https://github.com/astral-sh/ruff/discussions/4049
lint-format-changed: install-dev
	@base_ref=$$($(RESOLVE_BASE)) && \
	diff=$$(git diff "$$base_ref" --unified=0 --no-color -- '*.py') && \
	printf '%s\n' "$$diff" | \
	perl -ne '\
		if (/^diff --git a\/(.*) b\//) { $$file = $$1; } \
		if (/^@@ .* \+(\d+)(?:,(\d+))? @@/) { \
			$$start = $$1; $$count = $$2 || 1; $$end = $$start + $$count - 1; \
			print "$$file:$$start:1-$$end:999\n"; \
		}' | \
		while read range; do \
			file="$${range%%:*}"; \
			lines="$${range#*:}"; \
			echo "Formatting $$file (lines $$lines)"; \
			$(UV_RUN) ruff format --range "$$lines" "$$file"; \
		done

lint-ruff-dev: install-dev
	@base_ref=$$($(RESOLVE_BASE)) || exit $$?; \
	tmpfile=$$(mktemp /tmp/ruff-dev.XXXXXX) && \
	cd litellm && \
	($(UV_RUN) ruff check . --output-format=pylint || true) > "$$tmpfile" && \
	$(UV_RUN) diff-quality --violations=pylint "$$tmpfile" --compare-branch="$$base_ref" && \
	cd .. ; \
	rm -f "$$tmpfile"

lint-ruff-FULL-dev: install-dev
	@base_ref=$$($(RESOLVE_BASE)) && \
	files=$$(git diff --name-only "$$base_ref" -- '*.py') || exit $$?; \
	if [ -n "$$files" ]; then echo "$$files" | xargs $(UV_RUN) ruff check; \
	else echo "No changed .py files to check."; fi

lint-format: format-check

check-circular-imports: $(LINT_DEP_INSTALL)
	cd litellm && $(UV_RUN) python ../tests/documentation_tests/test_circular_imports.py && cd ..

check-import-safety: $(LINT_DEP_INSTALL)
	@$(UV_RUN) python -c "from litellm import *; print('[from litellm import *] OK! no issues!');" || (echo '🚨 import failed, this means you introduced unprotected imports! 🚨'; exit 1)

lint: lint-install
	@base_ref=$$($(RESOLVE_BASE)) && \
	$(MAKE) BASE_REF="$$base_ref" -j $(LINT_JOBS) $(LINT_OUTPUT_SYNC) LINT_DEP_INSTALL= LINT_DEP_BASE= \
		lint-format-check-changed lint-ruff check-circular-imports check-import-safety

# Faster linting for local development (only checks changed code)
lint-dev: lint-format-changed check-circular-imports check-import-safety

check: lint

# Testing targets
test: install-test-deps
	$(UV_RUN) pytest tests/

test-unit: install-test-deps
	$(UV_RUN) pytest tests/test_litellm -x -vv -n 4

# Matrix test targets (matching CI workflow groups)
test-unit-llms: install-test-deps
	$(UV_RUN) pytest tests/test_litellm/llms --tb=short -vv -n 4 --durations=20

test-unit-proxy-guardrails: install-test-deps
	$(UV_RUN) pytest tests/test_litellm/proxy/guardrails tests/test_litellm/proxy/management_endpoints tests/test_litellm/proxy/management_helpers --tb=short -vv -n 4 --durations=20

test-unit-proxy-core: install-test-deps
	$(UV_RUN) pytest tests/test_litellm/proxy/auth tests/test_litellm/proxy/client tests/test_litellm/proxy/db tests/test_litellm/proxy/hooks tests/test_litellm/proxy/policy_engine --tb=short -vv -n 4 --durations=20

test-unit-proxy-misc: install-test-deps
	$(UV_RUN) pytest tests/test_litellm/proxy/_experimental tests/test_litellm/proxy/agent_endpoints tests/test_litellm/proxy/anthropic_endpoints tests/test_litellm/proxy/common_utils tests/test_litellm/proxy/discovery_endpoints tests/test_litellm/proxy/experimental tests/test_litellm/proxy/google_endpoints tests/test_litellm/proxy/health_endpoints tests/test_litellm/proxy/image_endpoints tests/test_litellm/proxy/middleware tests/test_litellm/proxy/openai_files_endpoint tests/test_litellm/proxy/pass_through_endpoints tests/test_litellm/proxy/prompts tests/test_litellm/proxy/public_endpoints tests/test_litellm/proxy/response_api_endpoints tests/test_litellm/proxy/shutdown tests/test_litellm/proxy/spend_tracking tests/test_litellm/proxy/ui_crud_endpoints tests/test_litellm/proxy/vector_store_endpoints tests/test_litellm/proxy/test_*.py --tb=short -vv -n 4 --durations=20

test-unit-integrations: install-test-deps
	$(UV_RUN) pytest tests/test_litellm/integrations --tb=short -vv -n 4 --durations=20

test-unit-core-utils: install-test-deps
	$(UV_RUN) pytest tests/test_litellm/litellm_core_utils --tb=short -vv -n 2 --durations=20

test-unit-other: install-test-deps
	$(UV_RUN) pytest tests/test_litellm/caching tests/test_litellm/responses tests/test_litellm/secret_managers tests/test_litellm/vector_stores tests/test_litellm/a2a_protocol tests/test_litellm/anthropic_interface tests/test_litellm/completion_extras tests/test_litellm/containers tests/test_litellm/experimental_mcp_client tests/test_litellm/google_genai tests/test_litellm/images tests/test_litellm/interactions tests/test_litellm/passthrough tests/test_litellm/router_strategy tests/test_litellm/router_utils tests/test_litellm/types --tb=short -vv -n 4 --durations=20

test-unit-root: install-test-deps
	$(UV_RUN) pytest tests/test_litellm/test_*.py --tb=short -vv -n 4 --durations=20

# Proxy unit tests (tests/proxy_unit_tests split alphabetically)
test-proxy-unit-a: install-test-deps
	$(UV_RUN) pytest tests/proxy_unit_tests/test_[a-o]*.py --tb=short -vv -n 2 --durations=20

test-proxy-unit-b: install-test-deps
	$(UV_RUN) pytest tests/proxy_unit_tests/test_[p-z]*.py --tb=short -vv -n 2 --durations=20

test-integration: install-test-deps
	$(UV_RUN) pytest tests/ -k "not test_litellm"

# LLM Translation testing targets
test-llm-translation: install-test-deps
	@echo "Running LLM translation tests..."
	@python .github/scripts/run_llm_translation_tests.py

test-llm-translation-single: install-test-deps
	@echo "Running single LLM translation test file..."
	@if [ -z "$(FILE)" ]; then echo "Usage: make test-llm-translation-single FILE=test_filename.py"; exit 1; fi
	@mkdir -p test-results
	$(UV_RUN) pytest tests/llm_translation/$(FILE) \
		--junitxml=test-results/junit.xml \
		-v --tb=short --maxfail=100 --timeout=300

test-llm-translation-flush-vcr-cache:
	$(UV_RUN) python tests/_flush_vcr_cache.py
