"""Top-level CLI. Entrypoint: ``python -m src.cli <command>``.

Commands wired in PR 3:
  - ``migrate``          — apply pending MariaDB migrations
  - ``ingest-maps``      — run a TMX map-ingestion pass under a snapshot
  - ``ingest-replays``   — stub; full impl in a later PR

PRs 4–7 add ``replay-clean``, ``extract-route``, ``build-graph``,
``eval-benchmark``.
"""
