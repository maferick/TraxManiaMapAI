from __future__ import annotations

import json

import pytest

from src.route.artifact import (
    ROUTE_ARTIFACT_SCHEMA_VERSION,
    BranchCandidate,
    Centerline,
    CenterlinePoint,
    RouteExtractionResult,
    SegmentBoundary,
    content_hash,
    from_json,
    to_canonical_bytes,
    to_json,
)


def _result() -> RouteExtractionResult:
    return RouteExtractionResult(
        centerline=Centerline(
            (
                CenterlinePoint(s=0.0, x=0.0, y=0.0, z=0.0),
                CenterlinePoint(s=10.0, x=10.0, y=0.0, z=0.0),
                CenterlinePoint(s=20.0, x=20.0, y=0.0, z=0.0),
            )
        ),
        branches=(
            BranchCandidate(
                s=15.0,
                cluster_count=2,
                replays_in_primary=10,
                replays_in_alternates=3,
                evidence={"k": 1},
            ),
        ),
        segments=(SegmentBoundary(s=10.0, reason="checkpoint", evidence={}),),
        diagnostics={"n_replays": 8},
    )


def test_centerline_requires_two_points() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        Centerline((CenterlinePoint(0.0, 0.0, 0.0, 0.0),))


def test_centerline_requires_non_decreasing_s() -> None:
    with pytest.raises(ValueError, match="non-decreasing"):
        Centerline(
            (
                CenterlinePoint(s=0.0, x=0, y=0, z=0),
                CenterlinePoint(s=-1.0, x=0, y=0, z=0),
            )
        )


def test_centerline_length() -> None:
    cl = Centerline(
        (
            CenterlinePoint(s=0.0, x=0, y=0, z=0),
            CenterlinePoint(s=42.0, x=0, y=0, z=0),
        )
    )
    assert cl.length_m == pytest.approx(42.0)


def test_to_json_and_from_json_round_trip() -> None:
    r = _result()
    j = to_json(r)
    assert j["schema_version"] == ROUTE_ARTIFACT_SCHEMA_VERSION
    r2 = from_json(j)
    assert len(r2.centerline) == len(r.centerline)
    assert r2.branches[0].cluster_count == 2
    assert r2.segments[0].reason == "checkpoint"


def test_canonical_bytes_are_stable_across_dict_order() -> None:
    r = _result()
    b1 = to_canonical_bytes(r)
    b2 = to_canonical_bytes(from_json(json.loads(b1)))
    assert b1 == b2


def test_content_hash_is_sha256_hex() -> None:
    h = content_hash(_result())
    assert len(h) == 64
    int(h, 16)


def test_rejects_wrong_schema_version() -> None:
    r = _result()
    payload = to_json(r)
    payload["schema_version"] = 999
    with pytest.raises(ValueError, match="schema_version"):
        from_json(payload)
