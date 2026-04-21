#!/usr/bin/env bash
# Stub. Wired up in PR 1 / PR 3.
# Intent: bring up local services and apply migrations.
set -euo pipefail

echo "[bootstrap_dev] not yet implemented"
echo "Planned steps:"
echo "  1. docker compose -f docker/docker-compose.yml up -d"
echo "  2. wait for mariadb + neo4j to be healthy"
echo "  3. apply migrations from migrations/mariadb/ and migrations/neo4j/"
echo "  4. print connection info"
exit 0
