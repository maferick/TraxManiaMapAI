"""Runtime coverage for ``src.cli._print_test_report``.

The end-to-end ``test-in-game`` command is covered implicitly by
the remote-test server + agent tests; here we just pin the
report-formatting helper against a canonical server response so a
future schema change gets caught early.
"""
from __future__ import annotations

import io
import sys

from src.cli.__main__ import _print_test_report


def test_print_report_full_complete(capsys: "pytest.CaptureFixture[str]") -> None:
    job = {
        "id": 42,
        "run_id": "run-xyz",
        "status": "complete",
        "agent_id": "winrig-1",
        "detail": "load=ok spawn=ok finished=False cps=0 cells=0 exit=observer_timeout",
        "report": {
            "load_success": True,
            "load_error": None,
            "spawn_ok": True,
            "finished": False,
            "checkpoint_times_ms": [3200, 6400],
            "exit_reason": "observer_timeout",
            "plugin_version": "plugin-v0.1",
            "driven_cells_count": 3,
            "driven_cells_head": [[0, 9, 0], [1, 9, 0], [2, 9, 0]],
            "validation_status": None,  # v0.1 plugin — no validator
            "author_time_ms": None,
        },
    }
    _print_test_report(job)
    out = capsys.readouterr().out
    assert "job_id         42" in out
    assert "run_id         run-xyz" in out
    assert "status         complete" in out
    assert "load_success      True" in out
    assert "spawn_ok          True" in out
    assert "exit_reason       observer_timeout" in out
    # Checkpoint times listed individually
    assert "[0] 3200 ms" in out
    assert "[1] 6400 ms" in out
    # v0.1 plugin: no validation fields, so the lines are suppressed.
    assert "validation_status" not in out
    assert "author_time_ms" not in out


def test_print_report_v02_plugin_with_validation(
    capsys: "pytest.CaptureFixture[str]",
) -> None:
    # v0.2 plugin: native editor validation ran successfully.
    job = {
        "id": 99, "run_id": "validated", "status": "complete",
        "agent_id": "winrig-1", "detail": "all good",
        "report": {
            "load_success": True, "spawn_ok": True, "finished": True,
            "validation_status": "Validated", "author_time_ms": 18450,
            "exit_reason": "validated",
            "plugin_version": "plugin-v0.2",
        },
    }
    _print_test_report(job)
    out = capsys.readouterr().out
    assert "validation_status Validated" in out
    assert "author_time_ms    18450" in out
    assert "finished          True" in out


def test_print_report_load_error_path(capsys: "pytest.CaptureFixture[str]") -> None:
    job = {
        "id": 7, "run_id": "bad", "status": "complete",
        "agent_id": None, "detail": None,
        "report": {
            "load_success": False,
            "load_error": "titlepack resources missing",
            "spawn_ok": False,
            "finished": False,
            "exit_reason": "load_error",
            "plugin_version": "plugin-v0.1",
        },
    }
    _print_test_report(job)
    out = capsys.readouterr().out
    assert "load_success      False" in out
    assert "load_error        titlepack resources missing" in out
    assert "exit_reason       load_error" in out


def test_print_report_handles_missing_report(
    capsys: "pytest.CaptureFixture[str]",
) -> None:
    job = {
        "id": 1, "run_id": "x", "status": "failed",
        "agent_id": "winrig-1", "detail": "download failed",
        "report": None,
    }
    _print_test_report(job)
    out = capsys.readouterr().out
    assert "(no telemetry report attached)" in out
