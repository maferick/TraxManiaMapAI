"""Lightweight registry of evaluator classes.

Evaluators are opt-in registered (no import-time magic on submodules).
Registration validates that the class declares a non-empty ``name`` and
a parseable semver ``version``.
"""
from __future__ import annotations

from .base import Evaluator
from .versioning import EvaluatorVersion

_REGISTRY: dict[str, type[Evaluator]] = {}


def register(evaluator_cls: type[Evaluator]) -> type[Evaluator]:
    name = getattr(evaluator_cls, "name", None)
    version = getattr(evaluator_cls, "version", None)
    if not isinstance(name, str) or not name:
        raise ValueError(
            f"{evaluator_cls.__name__} must set a non-empty class attribute 'name'"
        )
    if not isinstance(version, str):
        raise ValueError(
            f"{evaluator_cls.__name__} must set a string class attribute 'version'"
        )
    EvaluatorVersion.parse(version)
    if name in _REGISTRY and _REGISTRY[name] is not evaluator_cls:
        raise ValueError(
            f"evaluator name {name!r} is already registered by "
            f"{_REGISTRY[name].__name__}"
        )
    _REGISTRY[name] = evaluator_cls
    return evaluator_cls


def get(name: str) -> type[Evaluator]:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"no evaluator registered under name {name!r}") from None


def all_registered() -> dict[str, type[Evaluator]]:
    return dict(_REGISTRY)


def _reset_for_tests() -> None:
    _REGISTRY.clear()
