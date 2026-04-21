"""Clusterer abstraction.

Route inference MUST NOT hardwire one clustering method (per
``CLAUDE.md``). All concrete clusterers implement the same ABC and
are selectable by name through :func:`create`. The abstraction is
also a seam for per-segment and ensemble strategies.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping

import numpy as np


class ClustererUnavailableError(RuntimeError):
    """The clusterer's backend (e.g. scikit-learn) is not installed."""


@dataclass(frozen=True)
class ClusterResult:
    labels: np.ndarray   # (M,) int. -1 indicates noise (if the backend emits it).

    @property
    def n_clusters(self) -> int:
        unique = set(int(x) for x in self.labels.tolist())
        unique.discard(-1)
        return len(unique)

    @property
    def has_noise(self) -> bool:
        return bool(np.any(self.labels == -1))


class Clusterer(ABC):
    name: ClassVar[str]
    version: ClassVar[str]

    @abstractmethod
    def fit_predict(self, points: np.ndarray) -> ClusterResult:
        """Assign each point to a cluster label. Input shape: (M, D)."""

    @classmethod
    @abstractmethod
    def default_params(cls) -> dict[str, Any]:
        """Default constructor kwargs. Used by the registry factory."""


_REGISTRY: dict[str, type[Clusterer]] = {}


def register(cls: type[Clusterer]) -> type[Clusterer]:
    name = getattr(cls, "name", None)
    if not isinstance(name, str) or not name:
        raise ValueError(f"{cls.__name__} must declare a non-empty class attribute 'name'")
    existing = _REGISTRY.get(name)
    if existing is not None and existing is not cls:
        raise ValueError(
            f"clusterer name {name!r} is already registered by {existing.__name__}"
        )
    _REGISTRY[name] = cls
    return cls


def get(name: str) -> type[Clusterer]:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"no clusterer registered under {name!r}; "
            f"available: {sorted(_REGISTRY)}"
        ) from None


def create(name: str, params: Mapping[str, Any] | None = None) -> Clusterer:
    cls = get(name)
    merged = dict(cls.default_params())
    if params:
        merged.update(params)
    return cls(**merged)


def all_registered() -> dict[str, type[Clusterer]]:
    return dict(_REGISTRY)


def _reset_for_tests() -> None:
    _REGISTRY.clear()
