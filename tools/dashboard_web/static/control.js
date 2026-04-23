// Phase-2 control layer — wires action buttons to POST /api/actions/<name>
// and streams the running subprocess's stdout via SSE.
// Keeps the dashboard usable without a framework; ~150 lines of vanilla JS.

(function () {
  'use strict';

  const logEl = document.getElementById('action-log');
  const statusEl = document.getElementById('action-status');

  if (!logEl || !statusEl) {
    // Page rendered with error state; nothing to wire.
    return;
  }

  function appendLogLine(text) {
    const div = document.createElement('div');
    div.className = 'action-log-line';
    div.textContent = text;
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function setStatus(html) {
    statusEl.innerHTML = html;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {
        '&': '&amp;', '<': '&lt;', '>': '&gt;',
        '"': '&quot;', "'": '&#39;',
      }[c];
    });
  }

  // Existing SSE connection, if any. Cleared on new runs / disconnect.
  let currentSource = null;

  // Re-render the "latest generated map" panel from fresh server
  // data. Called after a generate-map run completes so the operator
  // sees the result without a full page reload.
  async function refreshGeneratedPanel() {
    const panel = document.getElementById('generated-latest');
    if (!panel) return;
    let payload;
    try {
      const resp = await fetch('/api/generated-maps');
      if (!resp.ok) return;
      payload = await resp.json();
    } catch (err) {
      return;
    }
    const g = payload.latest;
    if (!g) {
      panel.innerHTML =
        '<p class="generated-empty">No generated-map artifacts yet. ' +
        'Click <strong>Generate map</strong> above.</p>';
      return;
    }
    const verified = g.route_verified === true;
    const badge = verified ? 'verified' : 'rejected';
    const fmt = function (v, d) {
      if (v === null || v === undefined) return '—';
      return typeof v === 'number' ? v.toFixed(d || 3) : String(v);
    };
    const styleBit = g.style_tag_filter
      ? ', style=' + escapeHtml(g.style_tag_filter) : '';
    const rejectBit = g.reject_reason
      ? '<dt>reject_reason</dt><dd>' + escapeHtml(g.reject_reason) + '</dd>'
      : '';
    panel.innerHTML =
      '<div class="generated-headline">' +
        '<span class="generated-badge badge-' + badge + '">' + badge + '</span>' +
        '<span class="generated-base">base #' + escapeHtml(g.base_map_id) + '</span>' +
        '<span class="generated-run-id">run ' + escapeHtml(g.run_id) + '</span>' +
        '<span class="generated-at">' + escapeHtml(g.generated_at || '') + '</span>' +
      '</div>' +
      '<dl class="generated-fields">' +
        '<dt>route_verified</dt><dd>' + (verified ? 'true' : 'false') + '</dd>' +
        '<dt>ai_confidence</dt><dd>' + escapeHtml(fmt(g.ai_confidence, 3)) + '</dd>' +
        '<dt>estimated_time_ms</dt><dd>' + escapeHtml(fmt(g.estimated_time_ms)) + '</dd>' +
        '<dt>intervals</dt><dd>' + escapeHtml(g.interval_count) + '</dd>' +
        rejectBit +
        '<dt>inputs</dt><dd>' +
          'base_map_id=' + escapeHtml(g.base_map_id) +
          ', difficulty=' + escapeHtml(g.difficulty) +
          ', seed=' + escapeHtml(g.random_seed) + styleBit +
        '</dd>' +
        '<dt>artifact</dt><dd>' +
          '<a href="/api/generated-maps/' + encodeURIComponent(g.filename) +
          '" download>' + escapeHtml(g.filename) + '</a>' +
        '</dd>' +
      '</dl>';
  }

  function streamRun(runId, title) {
    if (currentSource) {
      currentSource.close();
    }
    setStatus(
      '<span class="running-label">running:</span> ' +
      '<span class="running-title">' + escapeHtml(title) + '</span> ' +
      '<span class="running-id">#' + escapeHtml(runId) + '</span>'
    );
    // Start offset = current number of log lines. Server replays all
    // buffered lines from offset → we don't want to double-render the
    // lines the template already baked in.
    const baked = logEl.querySelectorAll('.action-log-line').length;
    const url = '/api/actions/' + encodeURIComponent(runId) +
                '/log?offset=' + baked;
    const src = new EventSource(url);
    currentSource = src;
    src.onmessage = function (ev) {
      appendLogLine(ev.data);
    };
    src.addEventListener('done', function (ev) {
      const parts = String(ev.data).split('|');
      const status = parts[0] || 'unknown';
      const exit = parts[1] || '?';
      setStatus(
        '<span class="last-label">last:</span> ' +
        '<span class="last-title">' + escapeHtml(title) + '</span> ' +
        '<span class="last-status status-' + escapeHtml(status) + '">' +
        escapeHtml(status) + '</span> ' +
        '<span class="last-exit">exit ' + escapeHtml(exit) + '</span>'
      );
      src.close();
      currentSource = null;
      // The run that just finished may have been generate-map; refresh
      // the result panel unconditionally since it's a single cheap
      // fetch and we don't want to depend on parsing title strings.
      refreshGeneratedPanel();
    });
    src.onerror = function () {
      appendLogLine('[stream disconnected]');
      src.close();
      currentSource = null;
    };
  }

  async function startAction(name, title) {
    // Clear the log for the new run. Keep the status line while the
    // request is in flight so the UI doesn't look frozen.
    logEl.innerHTML = '';
    setStatus(
      '<span class="running-label">starting:</span> ' +
      '<span class="running-title">' + escapeHtml(title) + '</span>'
    );
    let body = {};
    // Dead-simple parameter prompts for the actions that need input.
    // Real params dialog = future work; this keeps v0 viable.
    if (name === 'ingest-maps-random') {
      const count = window.prompt(
        'How many random maps to ingest? (1–5000)', '200');
      if (count === null) {
        setStatus('<span class="idle-label">cancelled</span>');
        return;
      }
      body.count = parseInt(count, 10);
    } else if (name === 'generate-map') {
      const baseMapId = window.prompt(
        'Base map_id to generate from?\n' +
        '(v0 only succeeds on Linked-CP maps — see scope-v0)',
        '1212');
      if (baseMapId === null) {
        setStatus('<span class="idle-label">cancelled</span>');
        return;
      }
      body.base_map_id = parseInt(baseMapId, 10);
    }
    let resp;
    try {
      resp = await fetch('/api/actions/' + encodeURIComponent(name), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch (err) {
      appendLogLine('[network error] ' + err);
      setStatus('<span class="idle-label">request failed</span>');
      return;
    }
    const payload = await resp.json();
    if (resp.status === 409) {
      appendLogLine('[busy] ' + (payload.detail || 'another action is running'));
      setStatus('<span class="idle-label">busy</span>');
      return;
    }
    if (resp.status === 400) {
      appendLogLine('[invalid input] ' + (payload.detail || 'rejected'));
      setStatus('<span class="idle-label">rejected</span>');
      return;
    }
    if (resp.status !== 202) {
      appendLogLine('[error ' + resp.status + '] ' + JSON.stringify(payload));
      setStatus('<span class="idle-label">error</span>');
      return;
    }
    const run = payload.started;
    streamRun(run.id, run.title);
  }

  document.querySelectorAll('.action-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      startAction(btn.dataset.action, btn.dataset.title);
    });
  });

  // If the page rendered while a run is already in flight, auto-attach
  // to its stream so the operator sees live output without interacting.
  const preloadedRunId = logEl.dataset.runId;
  const runningTitle = statusEl.querySelector('.running-title');
  if (preloadedRunId && runningTitle) {
    streamRun(preloadedRunId, runningTitle.textContent || 'action');
  }
})();
