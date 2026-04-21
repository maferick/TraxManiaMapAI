from __future__ import annotations

from src.evaluation.evaluators.structural import _count_orphans


def test_no_cells_no_orphans() -> None:
    assert _count_orphans(set()) == 0


def test_single_cell_is_orphan() -> None:
    assert _count_orphans({(0, 0, 0)}) == 1


def test_two_axis_neighbors_no_orphans() -> None:
    assert _count_orphans({(0, 0, 0), (1, 0, 0)}) == 0


def test_diagonal_only_still_orphan() -> None:
    # (0,0,0) and (1,1,0) are diagonal, not axis-neighbors.
    assert _count_orphans({(0, 0, 0), (1, 1, 0)}) == 2


def test_mixed_neighbors_and_orphans() -> None:
    cells = {
        (0, 0, 0),        # neighbor: (1,0,0)
        (1, 0, 0),        # neighbor: (0,0,0)
        (10, 10, 10),     # isolated
        (5, 5, 5),        # isolated
    }
    assert _count_orphans(cells) == 2


def test_chain_along_z_no_orphans() -> None:
    cells = {(0, 0, z) for z in range(5)}
    assert _count_orphans(cells) == 0
