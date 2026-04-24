# Windows Agent (remote-test rig PR2)

Pulls jobs from the Linux queue, deploys `.Map.Gbx` artifacts into
TM2020's `Maps/AI-inbox`, signals the OpenPlanet telemetry plugin
via a file-drop protocol, and ships results back. **No inbound
ports on Windows.**

## Quick start

On the Windows box:

```powershell
# 1. Clone the repo
git clone https://github.com/maferick/TraxManiaMapAI.git
cd TraxManiaMapAI

# 2. Install deps (Python 3.10+)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt   # or: pip install -e .

# 3. Copy + edit the example config
Copy-Item src\remote_test_agent\agent.example.yaml agent.yaml
notepad agent.yaml

# 4. Set the bearer token (matches the Linux server's REMOTE_TEST_TOKEN)
$env:REMOTE_TEST_TOKEN = "your-shared-token"

# 5. Run
python -m src.cli remote-test-agent --config agent.yaml
```

The agent runs forever until Ctrl+C. Output logs every heartbeat,
every claimed job, and every telemetry report.

## Config schema

See `agent.example.yaml`. Key fields:

| path | purpose |
| --- | --- |
| `server.url` | Linux queue server base URL (e.g. `http://192.168.1.42:8787`) |
| `server.token` | Bearer token. Env `REMOTE_TEST_TOKEN` wins if set. |
| `agent.id` | Unique name for this rig — shows in `remote-test-status` output |
| `paths.tm_maps_root` | Usually `%USERPROFILE%\Documents\Trackmania2020\Maps` |
| `paths.ai_inbox_subdir` | Created under `tm_maps_root` if missing (default `AI-inbox`) |
| `paths.plugin_rig_dir` | Usually `%USERPROFILE%\OpenplanetNext\PluginStorage\ai_rig` |

## Protocol with the OpenPlanet plugin

The agent and plugin exchange JSON files under `paths.plugin_rig_dir`:

```
<job_id>.in.json    ← agent writes (plugin reads + loads the map)
<job_id>.out.json   ← plugin writes (agent reads telemetry, POSTs upstream, deletes)
```

See `src/remote_test_agent/plugin_io.py` for the exact shape. The
plugin (PR3, separate repo) MUST emit `protocol: "ai_rig_v1"` and
a matching `job_id` in its `.out.json` or the agent rejects the
file as malformed.

## Lifecycle

```
idle → poll /jobs/next
  │
  ├── 204 No Content       → sleep queue_interval_s → back to idle
  │
  └── 200 claimed-job
        ├── download /jobs/{id}/artifact → verify sha256
        ├── stage into AI-inbox/<run_id>.Map.Gbx
        ├── drop <job_id>.in.json
        ├── POST status=running
        ├── wait for <job_id>.out.json (up to timeout + slack)
        ├── on success:  POST status=complete + telemetry
        ├── on timeout:  POST status=timed_out
        └── on error:    POST status=failed
```

Heartbeats fire on a `heartbeat_interval_s` timer inside the main
loop — no separate thread.

## Robustness

- SIGINT / SIGTERM finish the current job cleanly before exiting.
- One failing job never stops the loop; errors log + move on.
- Artifact SHA-256 is verified on every download so a corrupt LAN
  transfer fails fast instead of confusing TM2020.
- `plugin_wait_max_extra_s` caps how long we wait beyond the job's
  own `timeout_seconds` — prevents a crashed TM from hanging the
  rig.

## Not in PR2

- Launching TM2020 automatically (follow-up; for now, TM must be
  running with OpenPlanet + the plugin active before jobs arrive).
- Running as a Windows service / scheduled task — use `nssm` or a
  PowerShell scheduled-task wrapper if you want it headless.
