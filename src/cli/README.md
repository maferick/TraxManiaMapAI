# src/cli

CLI entry points. Not implemented yet.

Planned commands (to appear across PR 3 – PR 7):

```
python -m src.cli ingest-maps     --source tmx --limit 1000
python -m src.cli ingest-replays  --map-batch 2026q1-tech
python -m src.cli normalize-maps  --batch raw_import_001
python -m src.cli build-benchmarks --config data/benchmarks/seed.yaml
python -m src.cli replay-clean    --batch raw_import_001
python -m src.cli extract-route   --map-id <id>
python -m src.cli build-graph     --snapshot 2026-04-tmx
python -m src.cli eval-benchmark  --set tech-strong-v1
```
