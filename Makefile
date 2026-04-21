# Developer workflow. Most targets are stubs in PR 1; they get wired up
# across PRs 2-7 as the matching subsystems land.

.PHONY: help dev-up dev-down migrate ingest-sample replay-clean-sample \
        extract-route-sample eval-benchmark-sample constraints-sample \
        test lint format

help:
	@echo "Targets:"
	@echo "  dev-up                 - start MariaDB + Neo4j via docker compose"
	@echo "  dev-down               - stop local services"
	@echo "  migrate                - apply MariaDB migrations"
	@echo "  ingest-sample          - run a sample TMX ingestion (limit 50)"
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
	python -m src.cli migrate

ingest-sample:
	python -m src.cli ingest-maps --limit 50

replay-clean-sample:
	python -m src.cli replay-clean --limit 100 && python -m src.cli assign-cohorts

extract-route-sample:
	python -m src.cli extract-route

eval-benchmark-sample:
	@echo "eval-benchmark-sample: stub (PR 7)"

constraints-sample:
	python -m src.cli neo4j-migrate && python -m src.cli build-graph

test:
	pytest

lint:
	@echo "lint: stub — ruff + mypy wired up alongside first real code"

format:
	@echo "format: stub — ruff format wired up alongside first real code"
