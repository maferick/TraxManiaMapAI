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
    // Dead-simple parameter prompt for the one action that needs input.
    // Real params dialog = future work; this keeps v0 viable.
    if (name === 'ingest-maps-random') {
      const count = window.prompt(
        'How many random maps to ingest? (1–5000)', '200');
      if (count === null) {
        setStatus('<span class="idle-label">cancelled</span>');
        return;
      }
      body.count = parseInt(count, 10);
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
