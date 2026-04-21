# Developer workflow. Most targets are stubs in PR 1; they get wired up
# across PRs 2-7 as the matching subsystems land.

.PHONY: help dev-up dev-down migrate ingest-sample replay-clean-sample \
        extract-route-sample eval-benchmark-sample constraints-sample \
        test lint format

help:
	@echo "Targets (most are stubs in PR 1):"
	@echo "  dev-up                 - start MariaDB + Neo4j via docker compose"
	@echo "  dev-down               - stop local services"
	@echo "  migrate                - apply MariaDB + Neo4j migrations (PR 3)"
	@echo "  ingest-sample          - run a sample ingestion (PR 3)"
	@echo "  replay-clean-sample    - run replay cleaning on fixtures (PR 4)"
	@echo "  extract-route-sample   - run route inference on fixtures (PR 5)"
	@echo "  eval-benchmark-sample  - run evaluators on benchmark set (PR 7)"
	@echo "  constraints-sample     - build adjacency graph from fixtures (PR 6)"
	@echo "  test                   - run unit + integration tests"
	@echo "  lint                   - ruff + mypy"
	@echo "  format                 - ruff format"

dev-up:
	docker compose -f docker/docker-compose.yml up -d

dev-down:
	docker compose -f docker/docker-compose.yml down

migrate:
	@echo "migrate: stub (PR 3)"

ingest-sample:
	@echo "ingest-sample: stub (PR 3)"

replay-clean-sample:
	@echo "replay-clean-sample: stub (PR 4)"

extract-route-sample:
	@echo "extract-route-sample: stub (PR 5)"

eval-benchmark-sample:
	@echo "eval-benchmark-sample: stub (PR 7)"

constraints-sample:
	@echo "constraints-sample: stub (PR 6)"

test:
	@echo "test: stub — pytest wired up alongside first real code"

lint:
	@echo "lint: stub — ruff + mypy wired up alongside first real code"

format:
	@echo "format: stub — ruff format wired up alongside first real code"
