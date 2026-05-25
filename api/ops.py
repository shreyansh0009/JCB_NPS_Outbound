"""
Sprint 6: Ops Dashboard — non-technical operations team interface.

Routes:
  GET /ops          HTML dashboard (Bootstrap 5 + Chart.js)
  GET /ops/data     JSON payload consumed by the dashboard
  GET /ops/calls    Paginated recent call log (JSON)
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ops", tags=["ops"])

# ── HTML dashboard ────────────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Voice Agent — Ops Dashboard</title>
<link rel="stylesheet"
  href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  body { background:#f8f9fa; font-family:'Segoe UI',system-ui,sans-serif; }
  .kpi-card { border-radius:12px; border:none; box-shadow:0 1px 4px rgba(0,0,0,.08); }
  .kpi-value { font-size:2.2rem; font-weight:700; }
  .kpi-label { font-size:.85rem; color:#6c757d; text-transform:uppercase; letter-spacing:.05em; }
  .section-title { font-size:1rem; font-weight:600; color:#495057; margin-bottom:.75rem; }
  canvas { max-height:260px; }
  .badge-outcome-resolved { background:#198754; }
  .badge-outcome-abandoned { background:#dc3545; }
  .badge-outcome-max_duration { background:#fd7e14; }
  .badge-outcome-error { background:#6f42c1; }
  .badge-outcome-unknown { background:#6c757d; }
  #loading { position:fixed; inset:0; background:rgba(255,255,255,.7);
             display:flex; align-items:center; justify-content:center; z-index:9999; }
  .spinner-border { width:3rem; height:3rem; }
</style>
</head>
<body>

<div id="loading">
  <div class="spinner-border text-primary" role="status">
    <span class="visually-hidden">Loading…</span>
  </div>
</div>

<nav class="navbar navbar-dark bg-dark px-4 py-2 mb-4">
  <span class="navbar-brand fw-bold fs-5">🤖 Godrej AI Voice Agent — Ops Dashboard</span>
  <div class="d-flex align-items-center gap-3">
    <select id="hoursSelect" class="form-select form-select-sm" style="width:auto">
      <option value="1">Last 1 hour</option>
      <option value="6">Last 6 hours</option>
      <option value="24" selected>Last 24 hours</option>
      <option value="72">Last 3 days</option>
    </select>
    <span class="text-white-50 small" id="refreshTs"></span>
  </div>
</nav>

<div class="container-fluid px-4">

  <!-- KPI row -->
  <div class="row g-3 mb-4" id="kpiRow">
    <div class="col-6 col-md-3 col-xl-2">
      <div class="card kpi-card p-3 text-center">
        <div class="kpi-value text-primary" id="kpiTotal">—</div>
        <div class="kpi-label">Total Calls</div>
      </div>
    </div>
    <div class="col-6 col-md-3 col-xl-2">
      <div class="card kpi-card p-3 text-center">
        <div class="kpi-value text-success" id="kpiResolutionRate">—</div>
        <div class="kpi-label">Resolution Rate</div>
      </div>
    </div>
    <div class="col-6 col-md-3 col-xl-2">
      <div class="card kpi-card p-3 text-center">
        <div class="kpi-value text-info" id="kpiAvgDuration">—</div>
        <div class="kpi-label">Avg Duration</div>
      </div>
    </div>
    <div class="col-6 col-md-3 col-xl-2">
      <div class="card kpi-card p-3 text-center">
        <div class="kpi-value" id="kpiAvgTurns">—</div>
        <div class="kpi-label">Avg Turns</div>
      </div>
    </div>
    <div class="col-6 col-md-3 col-xl-2">
      <div class="card kpi-card p-3 text-center">
        <div class="kpi-value text-warning" id="kpiHandoffRate">—</div>
        <div class="kpi-label">Handoff Rate</div>
      </div>
    </div>
    <div class="col-6 col-md-3 col-xl-2">
      <div class="card kpi-card p-3 text-center">
        <div class="kpi-value text-danger" id="kpiAbandoned">—</div>
        <div class="kpi-label">Abandoned</div>
      </div>
    </div>
  </div>

  <!-- Charts row -->
  <div class="row g-3 mb-4">
    <div class="col-md-8">
      <div class="card kpi-card p-3">
        <div class="section-title">Call Volume (Hourly)</div>
        <canvas id="volumeChart"></canvas>
      </div>
    </div>
    <div class="col-md-4">
      <div class="card kpi-card p-3">
        <div class="section-title">Language Distribution</div>
        <canvas id="langChart"></canvas>
      </div>
    </div>
  </div>

  <!-- Agent table + Outcome breakdown -->
  <div class="row g-3 mb-4">
    <div class="col-md-8">
      <div class="card kpi-card p-3">
        <div class="section-title">Agent Performance</div>
        <div class="table-responsive">
          <table class="table table-sm table-hover mb-0">
            <thead class="table-light">
              <tr>
                <th>Agent</th>
                <th class="text-end">Calls Handled</th>
                <th class="text-end">Resolution Rate</th>
                <th class="text-end">Avg Turns</th>
                <th class="text-end">Avg Duration</th>
              </tr>
            </thead>
            <tbody id="agentTableBody"></tbody>
          </table>
        </div>
      </div>
    </div>
    <div class="col-md-4">
      <div class="card kpi-card p-3">
        <div class="section-title">Outcome Breakdown</div>
        <canvas id="outcomeChart"></canvas>
      </div>
    </div>
  </div>

  <!-- Recent call log -->
  <div class="card kpi-card p-3 mb-4">
    <div class="d-flex justify-content-between align-items-center mb-2">
      <div class="section-title mb-0">Recent Calls</div>
      <span class="text-muted small">Last 50 calls</span>
    </div>
    <div class="table-responsive">
      <table class="table table-sm table-hover mb-0" style="font-size:.85rem">
        <thead class="table-light">
          <tr>
            <th>Call SID</th>
            <th>Started</th>
            <th>Duration</th>
            <th>Language</th>
            <th>Turns</th>
            <th>Agents</th>
            <th>Outcome</th>
          </tr>
        </thead>
        <tbody id="callLogBody"></tbody>
      </table>
    </div>
  </div>

</div><!-- /container -->

<script>
const LANG_LABELS = {
  en:'English', hi:'Hindi', bn:'Bengali', ta:'Tamil', te:'Telugu',
  mr:'Marathi', gu:'Gujarati', kn:'Kannada', pa:'Punjabi', ml:'Malayalam',
  or:'Odia', unknown:'Unknown'
};
const OUTCOME_COLORS = {
  resolved:'#198754', abandoned:'#dc3545',
  max_duration:'#fd7e14', error:'#6f42c1', unknown:'#adb5bd'
};

let volumeChart, langChart, outcomeChart;

function fmtDuration(s) {
  if (!s || s < 1) return '0s';
  const m = Math.floor(s / 60), sec = Math.round(s % 60);
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
}

function outcomeClass(o) {
  const map = {resolved:'success', abandoned:'danger',
               max_duration:'warning', error:'secondary', unknown:'dark'};
  return 'badge bg-' + (map[o] || 'dark');
}

async function loadData() {
  const hours = document.getElementById('hoursSelect').value;
  const [dataResp, callsResp] = await Promise.all([
    fetch(`/ops/data?hours=${hours}`),
    fetch(`/ops/calls?limit=50`)
  ]);
  const data = await dataResp.json();
  const calls = await callsResp.json();

  // KPIs
  const s = data.summary;
  document.getElementById('kpiTotal').textContent = s.total_calls.toLocaleString();
  document.getElementById('kpiResolutionRate').textContent = s.resolution_rate + '%';
  document.getElementById('kpiAvgDuration').textContent = fmtDuration(s.avg_duration_s);
  document.getElementById('kpiAvgTurns').textContent = s.avg_turns;
  document.getElementById('kpiHandoffRate').textContent = s.handoff_rate + '%';
  document.getElementById('kpiAbandoned').textContent = s.abandoned.toLocaleString();

  // Volume chart
  const vol = data.hourly_volume;
  const volLabels = vol.map(v => v.hour.slice(11,16)); // HH:MM
  const volTotal  = vol.map(v => v.total);
  const volResolved = vol.map(v => v.resolved);
  if (volumeChart) volumeChart.destroy();
  volumeChart = new Chart(document.getElementById('volumeChart'), {
    type: 'bar',
    data: {
      labels: volLabels,
      datasets: [
        { label:'Total', data: volTotal, backgroundColor:'#0d6efd55', borderColor:'#0d6efd', borderWidth:1 },
        { label:'Resolved', data: volResolved, backgroundColor:'#19875488', borderColor:'#198754', borderWidth:1 },
      ]
    },
    options: { responsive:true, maintainAspectRatio:true, plugins:{ legend:{ position:'top' } },
               scales:{ y:{ beginAtZero:true, ticks:{ stepSize:1 } } } }
  });

  // Language chart
  const lang = data.language_distribution;
  if (langChart) langChart.destroy();
  langChart = new Chart(document.getElementById('langChart'), {
    type: 'doughnut',
    data: {
      labels: lang.map(l => LANG_LABELS[l.language] || l.language),
      datasets: [{ data: lang.map(l => l.count),
        backgroundColor: ['#0d6efd','#198754','#ffc107','#dc3545','#0dcaf0',
                          '#fd7e14','#6f42c1','#20c997','#d63384','#adb5bd'] }]
    },
    options: { responsive:true, plugins:{ legend:{ position:'bottom', labels:{ font:{ size:11 } } } } }
  });

  // Outcome chart
  const oc = data.summary;
  const ocLabels = ['Resolved','Abandoned','Max Duration','Error'];
  const ocData   = [oc.resolved, oc.abandoned, oc.max_duration, oc.error];
  const ocColors = ['#198754','#dc3545','#fd7e14','#6f42c1'];
  if (outcomeChart) outcomeChart.destroy();
  outcomeChart = new Chart(document.getElementById('outcomeChart'), {
    type: 'doughnut',
    data: { labels: ocLabels, datasets: [{ data: ocData, backgroundColor: ocColors }] },
    options: { responsive:true, plugins:{ legend:{ position:'bottom', labels:{ font:{ size:11 } } } } }
  });

  // Agent table
  const tbody = document.getElementById('agentTableBody');
  tbody.innerHTML = '';
  (data.agent_performance || []).forEach(a => {
    tbody.innerHTML += `<tr>
      <td><span class="fw-semibold">${a.agent}</span></td>
      <td class="text-end">${a.calls_handled}</td>
      <td class="text-end">
        <span class="text-${a.resolution_rate >= 80 ? 'success' : a.resolution_rate >= 60 ? 'warning' : 'danger'}">
          ${a.resolution_rate}%
        </span>
      </td>
      <td class="text-end">${a.avg_turns}</td>
      <td class="text-end">${fmtDuration(a.avg_duration_s)}</td>
    </tr>`;
  });
  if (!data.agent_performance || !data.agent_performance.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">No data yet</td></tr>';
  }

  // Recent calls log
  const logBody = document.getElementById('callLogBody');
  logBody.innerHTML = '';
  (calls || []).forEach(c => {
    const agents = (c.agent_path || []).join(' → ') || '—';
    logBody.innerHTML += `<tr>
      <td class="font-monospace text-muted" style="font-size:.8rem">${c.call_sid.slice(0,12)}…</td>
      <td>${fmtTime(c.started_at_iso)}</td>
      <td>${fmtDuration(c.duration_s)}</td>
      <td>${LANG_LABELS[c.language] || c.language}</td>
      <td class="text-center">${c.total_turns}</td>
      <td>${agents}</td>
      <td><span class="${outcomeClass(c.outcome)}">${c.outcome}</span></td>
    </tr>`;
  });
  if (!calls || !calls.length) {
    logBody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">No calls yet</td></tr>';
  }

  document.getElementById('refreshTs').textContent =
    'Updated: ' + new Date().toLocaleTimeString();
  document.getElementById('loading').style.display = 'none';
}

document.getElementById('hoursSelect').addEventListener('change', loadData);

// Auto-refresh every 60s
loadData();
setInterval(loadData, 60000);
</script>
</body>
</html>
"""


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def ops_dashboard():
    """Non-technical ops team HTML dashboard."""
    return HTMLResponse(content=_DASHBOARD_HTML)


@router.get("/data")
async def ops_data(
    request: Request,
    hours: int = Query(default=24, ge=1, le=168),
):
    """
    JSON payload for the ops dashboard.
    Returns summary KPIs, hourly volume, language distribution, and agent performance.
    """
    analytics = _get_analytics(request)
    if analytics is None:
        return JSONResponse({"error": "Analytics not initialised"}, status_code=503)

    summary, hourly, lang_dist, agent_perf = await _gather_all(analytics, hours)
    return {
        "hours": hours,
        "summary": summary,
        "hourly_volume": hourly,
        "language_distribution": lang_dist,
        "agent_performance": agent_perf,
    }


@router.get("/calls")
async def ops_calls(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
):
    """Paginated recent call log for the ops dashboard table."""
    analytics = _get_analytics(request)
    if analytics is None:
        return JSONResponse({"error": "Analytics not initialised"}, status_code=503)
    return await analytics.get_recent_calls(limit=limit)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_analytics(request: Request):
    """Pull the Analytics instance from app.state (set by main.py at startup)."""
    return getattr(request.app.state, "analytics", None)


async def _gather_all(analytics, hours: int):
    import asyncio
    return await asyncio.gather(
        analytics.get_summary(hours),
        analytics.get_hourly_volume(hours),
        analytics.get_language_distribution(hours),
        analytics.get_agent_performance(hours),
    )
