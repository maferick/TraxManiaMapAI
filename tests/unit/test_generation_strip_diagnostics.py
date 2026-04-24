"""Unit tests for src.generation.strip_diagnostics.

Pure-function tests over synthetic parsed-GBX dicts. The wrapper
subprocess + DB lookup are exercised in the live smoke in the PR
body; here we pin the analysis logic.
"""
from __future__ import annotations

import pytest

from src.generation.strip_diagnostics import (
    _chebyshev,
    _is_multicell_candidate,
    _shape_bucket,
    diagnose_strip,
    format_report_markdown,
)


def _block(name: str, x: int, y: int, z: int, *, placement: str = "grid"):
    return {
        "name": name,
        "placement": placement,
        "x": x if placement == "grid" else None,
        "y": y if placement == "grid" else None,
        "z": z if placement == "grid" else None,
        "abs_x": None if placement == "grid" else float(x),
        "abs_y": None if placement == "grid" else float(y),
        "abs_z": None if placement == "grid" else float(z),
    }


def _parsed(blocks, baked_count: int = 0) -> dict:
    return {"blocks": blocks, "baked_block_count": baked_count}


# ---------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------

class TestLowLevelHelpers:
    def test_chebyshev(self) -> None:
        assert _chebyshev((0, 0, 0), (0, 0, 0)) == 0
        assert _chebyshev((0, 0, 0), (3, 1, 2)) == 3
        assert _chebyshev((5, 5, 5), (4, 7, 5)) == 2

    @pytest.mark.parametrize("name,expected", [
        ("RoadTechSlope2Up", True),
        ("PlatformPlasticLoop1", True),
        ("GateExpandableFinish", True),
        ("RoadTechCurve2", True),
        ("PlatformPlasticCheckpoint16m", True),  # size suffix
        ("RoadTechStraight", False),
        ("GateCheckpoint", False),
        ("StructurePillar", False),
    ])
    def test_multicell_candidate_detection(
        self, name: str, expected: bool,
    ) -> None:
        assert _is_multicell_candidate(name) is expected

    @pytest.mark.parametrize("name,expected", [
        ("PlatformPlasticStart", "anchor"),
        ("GateCheckpoint", "anchor"),
        ("RoadTechSlopeDiag1", "ramp"),
        ("PlatformPlasticLoop1", "loop"),
        ("RoadTechCurve2", "curve"),
        ("StructurePillar", "support"),
        ("PlatformPlasticGeneric", "other"),
    ])
    def test_shape_bucket(self, name: str, expected: str) -> None:
        assert _shape_bucket(name) == expected


# ---------------------------------------------------------------------
# diagnose_strip — end to end over synthetic inputs
# ---------------------------------------------------------------------

class TestDiagnoseStrip:
    def test_no_drops_reports_cleanly(self) -> None:
        b = [_block("RoadTechStraight", 0, 0, 0),
             _block("RoadTechCurve1", 1, 0, 0)]
        r = diagnose_strip(
            base_map_id=42,
            base_map=_parsed(b),
            stripped_map=_parsed(b),
        )
        assert r.base_block_count == 2
        assert r.stripped_block_count == 2
        assert r.dropped_by_shape_bucket == {}
        assert "No net drops" in "|".join(r.hypotheses)

    def test_detects_ramp_drop_near_route(self) -> None:
        # Route goes (0,0,0) → (0,0,1). A ramp at (0,0,2) dropped is
        # "near the route" (cheb=1 from (0,0,1)); should be flagged.
        base = [
            _block("RoadTechStraight", 0, 0, 0),
            _block("RoadTechStraight", 0, 0, 1),
            _block("RoadTechSlopeDiag1", 0, 0, 2),  # the ramp
            _block("RoadTechStraight", 9, 9, 9),     # far away
        ]
        stripped = [
            _block("RoadTechStraight", 0, 0, 0),
            _block("RoadTechStraight", 0, 0, 1),
            _block("RoadTechStraight", 9, 9, 9),
        ]
        r = diagnose_strip(
            base_map_id=1,
            base_map=_parsed(base),
            stripped_map=_parsed(stripped),
            chosen_corridor_cells=[(0, 0, 0), (0, 0, 1)],
            anchor_cells=[("Spawn", 0, (0, 0, 0)), ("Goal", 0, (0, 0, 1))],
        )
        assert r.dropped_by_shape_bucket.get("ramp", 0) == 1
        assert any(
            "ramp" in h.lower() for h in r.hypotheses
        ), r.hypotheses
        # The ramp at (0,0,2) was within radius 2 of route cell (0,0,1).
        assert len(r.route_cell_drops) >= 1

    def test_multicell_candidate_bubbles_up(self) -> None:
        # Loop-named block dropped → multicell_candidate_drops non-empty
        # and the hypotheses mention it.
        base = [
            _block("RoadTechStraight", 0, 0, 0),
            _block("PlatformPlasticLoop2", 5, 0, 5),
        ]
        stripped = [_block("RoadTechStraight", 0, 0, 0)]
        r = diagnose_strip(
            base_map_id=2,
            base_map=_parsed(base),
            stripped_map=_parsed(stripped),
            chosen_corridor_cells=[(0, 0, 0)],
        )
        assert len(r.multicell_candidate_drops) == 1
        assert r.multicell_candidate_drops[0].name == "PlatformPlasticLoop2"
        assert any(
            "multi-cell" in h or "multicell" in h.lower()
            for h in r.hypotheses
        )

    def test_spawn_surround_loss_triggers_hypothesis(self) -> None:
        # Spawn at (5,5,5) with 10 surrounding blocks; strip keeps 1.
        base = [_block("PlatformPlasticStart", 5, 5, 5)] + [
            _block(f"PlatformPlasticLoopOutStartCurve{i}", 5 + dx, 5, 5 + dz)
            for i, (dx, dz) in enumerate([
                (1, 0), (-1, 0), (0, 1), (0, -1), (1, 1),
                (-1, -1), (2, 0), (-2, 0), (0, 2), (0, -2),
            ])
        ]
        stripped = [_block("PlatformPlasticStart", 5, 5, 5)]
        r = diagnose_strip(
            base_map_id=3,
            base_map=_parsed(base),
            stripped_map=_parsed(stripped),
            anchor_cells=[("Spawn", 0, (5, 5, 5))],
        )
        spawn_ads = next(
            a for a in r.anchor_surrounds if a.tag.lower() == "spawn"
        )
        assert spawn_ads.kept_blocks == 1
        # Loss > 30% + ≥ 4 absolute drops → hypothesis fires.
        assert any("Spawn surround lost" in h for h in r.hypotheses)

    def test_near_anchor_label_populated(self) -> None:
        base = [
            _block("RoadTechStraight", 10, 0, 10),  # near Spawn
            _block("RoadTechStraight", 50, 0, 50),  # far
        ]
        stripped: list[dict] = []
        r = diagnose_strip(
            base_map_id=4,
            base_map=_parsed(base),
            stripped_map=_parsed(stripped),
            anchor_cells=[("Spawn", 0, (10, 0, 10))],
        )
        spawn_near = [
            db for ads in r.anchor_surrounds for db in ads.dropped_blocks
            if db.near_anchor == "Spawn#0"
        ]
        assert len(spawn_near) == 1
        assert spawn_near[0].cell == (10, 0, 10)


# ---------------------------------------------------------------------
# Markdown formatting smoke — readable, stable, covers every section.
# ---------------------------------------------------------------------

class TestMarkdownFormat:
    def test_headers_present(self) -> None:
        base = [_block("PlatformPlasticStart", 0, 0, 0),
                _block("RoadTechSlopeDiag1", 1, 0, 0)]
        stripped = [_block("PlatformPlasticStart", 0, 0, 0)]
        r = diagnose_strip(
            base_map_id=1212,
            base_map=_parsed(base),
            stripped_map=_parsed(stripped),
            anchor_cells=[("Spawn", 0, (0, 0, 0))],
            chosen_corridor_cells=[(0, 0, 0)],
        )
        md = format_report_markdown(
            r, run_id="abc123def", strip_policy="halo_axis_1_plus_anchor_radius_3_vext_3",
        )
        for required in (
            "# Strip-failure diagnostic — map 1212",
            "## Summary",
            "## Likely reasons",
            "## Dropped blocks — shape bucket breakdown",
            "## Anchor surround preservation",
            "## Multi-cell candidate drops",
            "## What this report can't tell you",
            "run_id",
            "strip_policy",
        ):
            assert required in md, required
