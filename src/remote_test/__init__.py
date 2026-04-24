"""Remote in-game test rig — Linux server side.

Option A+ architecture (see the ``remote-test`` subcommand help):

  Linux box  ──(HTTP, bearer auth)──  Windows agent  ──  TM2020 + OpenPlanet

All flow is initiated by the Windows side pulling from the Linux
queue; nothing on the Windows host accepts inbound connections.
The plugin writes telemetry to a file the local agent ships back to
this service via ``POST /jobs/{id}/report``.

Public surface
--------------

- :class:`Job` / :class:`JobStatus` / :class:`AgentHeartbeat` — see
  :mod:`src.remote_test.models`
- :class:`JobStore` — SQLite-backed storage, see
  :mod:`src.remote_test.storage`
- :func:`create_app` — Flask WSGI factory, see
  :mod:`src.remote_test.server`

The module is self-contained: no MariaDB dependency, no Neo4j. The
only hard dep is Flask, which is already a project dep via the
dashboard.
"""
from src.remote_test.models import (
    AgentHeartbeat,
    Job,
    JobStatus,
)
from src.remote_test.server import create_app
from src.remote_test.storage import JobStore

__all__ = [
    "AgentHeartbeat",
    "Job",
    "JobStatus",
    "JobStore",
    "create_app",
]
