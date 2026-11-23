SHELL := /bin/bash
COMPOSE ?= docker compose

.PHONY: help dev up down clean logs ps migrate smoke test lint fmt contracts images e2e staging-up alt-dev alt-smoke alt-down

ALT_ENV ?= deploy/compose/alt-ports.env

help:
	@echo "dev        build and start the core stack"
	@echo "up         start the stack without rebuilding"
	@echo "down       stop containers"
	@echo "clean      stop and delete volumes (destructive)"
	@echo "logs       follow container logs"
	@echo "ps         show container status"
	@echo "migrate    apply pending SQL migrations"
	@echo "smoke      run health, migration, trace and log correlation checks"
	@echo "test       run Go and Python tests"
	@echo "lint       run Go and Python linters"
	@echo "fmt        format Go and Python code"
	@echo "contracts  run event schema contract tests in both languages"
	@echo "images     build all service images"
	@echo "e2e        start core + tools and run the smoke check"
	@echo "alt-dev    start on the alternate host ports from deploy/compose/alt-ports.env"
	@echo "alt-smoke  run smoke against the alternate host ports"
	@echo "alt-down   stop the alternate-port stack"
	@echo "staging-up start the stack with APP_ENV=staging (migrations run automatically)"

dev:
	$(COMPOSE) up -d --build --wait
	@echo "api       http://127.0.0.1:8080/healthz"
	@echo "grafana   http://127.0.0.1:3000 (admin/admin)"
	@echo "prometheus http://127.0.0.1:9090"
	@echo "silo      http://127.0.0.1:9001"

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down --remove-orphans

clean:
	$(COMPOSE) down --remove-orphans -v

logs:
	$(COMPOSE) logs -f --tail=100

ps:
	$(COMPOSE) ps

migrate:
	$(COMPOSE) run --rm migrate

smoke:
	./scripts/smoke.sh

test:
	go test ./...
	cd python && uv run pytest

lint:
	@test -z "$$(gofmt -l $$(find . -name '*.go' -not -path './python/*'))" || (echo "gofmt needed"; exit 1)
	go vet ./...
	@if command -v golangci-lint >/dev/null 2>&1; then golangci-lint run --timeout=5m; else echo "golangci-lint not installed; CI enforces it"; fi
	cd python && uv run ruff format --check . && uv run ruff check . && uv run mypy packages/wre_pipeline/src services/*/src

fmt:
	gofmt -w $$(find . -name '*.go' -not -path './python/*')
	cd python && uv run ruff format . && uv run ruff check --fix .

contracts:
	go test ./internal/contracts
	cd python && uv run pytest tests/test_contracts.py

images:
	$(COMPOSE) build

e2e:
	$(COMPOSE) --profile tools up -d --build --wait
	./scripts/smoke.sh

staging-up:
	APP_ENV=staging $(COMPOSE) up -d --build --wait

alt-dev:
	$(COMPOSE) --env-file $(ALT_ENV) up -d --build --wait
	@echo "api        http://127.0.0.1:28080/healthz"
	@echo "scheduler  http://127.0.0.1:28081/internal/v1/sources"
	@echo "grafana    http://127.0.0.1:23000 (admin/admin)"
	@echo "prometheus http://127.0.0.1:29090"

alt-smoke:
	set -a; . $(ALT_ENV); set +a; ./scripts/smoke.sh

alt-down:
	$(COMPOSE) --env-file $(ALT_ENV) down --remove-orphans
