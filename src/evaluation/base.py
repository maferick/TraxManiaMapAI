"""Abstract evaluator + uniform result shape.

No concrete evaluator logic lands in PR 2. This file defines the shape
that later PRs fill in. The ``EvaluationResult`` fields mirror the
``EvaluationArtifact`` data contract in ``docs/data-contracts.md`` and
carry the provenance envelope required by ``CLAUDE.md``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import ClassVar

from .versioning import EvaluatorVersion


@dataclass(frozen=True)
class EvaluationResult:
    map_id: int
    evaluator_name: str
    evaluator_version: str
    benchmark_set_version: str | None
    created_at: datetime
    code_version: str | None
    source_artifact_ids: dict[str, str]
    structural_score: float | None = None
    drivability_score: float | None = None
    flow_score: float | None = None
    style_score: float | None = None
    novelty_score: float | None = None
    diversity_metadata: dict[str, object] | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)
    notes: str | None = None

    def __post_init__(self) -> None:
        EvaluatorVersion.parse(self.evaluator_version)
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class Evaluator(ABC):
    """Base class for all evaluators.

    Subclasses must set ``name`` and ``version`` as class attributes.
    ``version`` is a semver string; the registry refuses to register a
    class whose version does not parse.
    """

    name: ClassVar[str]
    version: ClassVar[str]

    @abstractmethod
    def evaluate(
        self,
        map_id: int,
        *,
        benchmark_set_version: str | None = None,
    ) -> EvaluationResult:
        """Score a single map.

        Concrete subclasses decide how map data is fetched (via
        dependencies injected at construction time — PR 7 concrete
        evaluators all require a DB connection, and some require a
        Neo4j driver).
        """
