"""WarderBox Web Dashboard — real-time monitoring with zero dependencies.

Serves a single-page dashboard that shows:
- Live action stats (total, blocked, errors, cost)
- Recent action log
- Budget status & rate limits
- FinOps: cost breakdown by tool, routing stats, anomaly alerts

Uses Python's built-in http.server. No external packages needed.

Usage:
    from warderbox.dashboard import start_dashboard

    shield = WarderBox(budget=10.0, db_path="audit.db")
    # ... register tools, do work ...

    start_dashboard(shield, port=8080)
    # Open http://localhost:8080
"""

from __future__ import annotations

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from warderbox.shield import WarderBox
    from warderbox.routing import ModelRouter
    from warderbox.anomaly import AnomalyDetector

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WarderBox Dashboard</title>
<style>
:root {
  --bg: #0f172a; --surface: #1e293b; --border: #334155;
  --text: #f1f5f9; --muted: #94a3b8; --accent: #3b82f6;
  --green: #22c55e; --red: #ef4444; --yellow: #eab308; --orange: #f97316;
  --purple: #a855f7; --cyan: #06b6d4;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: var(--bg); color: var(--text); min-height: 100vh; }
.header { background: var(--surface); border-bottom: 1px solid var(--border);
  padding: 16px 24px; display: flex; align-items: center; gap: 12px; }
.header h1 { font-size: 20px; font-weight: 600; }
.header .badge { background: var(--accent); color: white; padding: 2px 8px;
  border-radius: 12px; font-size: 12px; font-weight: 500; }
.auto-refresh { font-size: 12px; color: var(--muted); }

/* Tabs */
.tabs { display: flex; gap: 0; background: var(--surface); border-bottom: 1px solid var(--border);
  padding: 0 24px; }
.tab { padding: 12px 20px; font-size: 14px; font-weight: 500; color: var(--muted);
  cursor: pointer; border-bottom: 2px solid transparent; transition: all 0.2s; }
.tab:hover { color: var(--text); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab-content { display: none; }
.tab-content.active { display: block; }

.container { max-width: 1200px; margin: 0 auto; padding: 24px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  gap: 16px; margin-bottom: 24px; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
.card { background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; padding: 20px; }
.card h3 { font-size: 13px; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.5px; margin-bottom: 8px; }
.card .value { font-size: 32px; font-weight: 700; }
.card .sub { font-size: 13px; color: var(--muted); margin-top: 4px; }
.green { color: var(--green); }
.red { color: var(--red); }
.yellow { color: var(--yellow); }
.orange { color: var(--orange); }
.purple { color: var(--purple); }
.cyan { color: var(--cyan); }

.table-card { background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; overflow: hidden; margin-bottom: 24px; }
.table-card h2 { padding: 16px 20px; font-size: 16px; border-bottom: 1px solid var(--border); }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; padding: 10px 20px; font-size: 12px; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid var(--border); }
td { padding: 10px 20px; font-size: 14px; border-bottom: 1px solid var(--border); }
tr:last-child td { border-bottom: none; }
.status { padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 500; }
.status-ok { background: #166534; color: #86efac; }
.status-blocked { background: #991b1b; color: #fca5a5; }
.status-error { background: #92400e; color: #fde68a; }
.budget-bar { height: 8px; background: var(--border); border-radius: 4px;
  margin-top: 12px; overflow: hidden; }
.budget-fill { height: 100%; border-radius: 4px; transition: width 0.5s; }

/* Cost bar chart */
.cost-bar-row { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.cost-bar-label { font-size: 13px; min-width: 120px; color: var(--text); }
.cost-bar-track { flex: 1; height: 24px; background: var(--border); border-radius: 4px; overflow: hidden; }
.cost-bar-fill { height: 100%; border-radius: 4px; transition: width 0.5s;
  display: flex; align-items: center; padding-left: 8px; font-size: 11px; font-weight: 600; }
.cost-bar-value { font-size: 13px; min-width: 70px; text-align: right; color: var(--muted); }

/* Alert card */
.alert-item { padding: 12px 16px; border-left: 3px solid var(--red); background: rgba(239,68,68,0.08);
  border-radius: 0 8px 8px 0; margin-bottom: 8px; }
.alert-item.high { border-left-color: var(--orange); background: rgba(249,115,22,0.08); }
.alert-item.critical { border-left-color: var(--red); background: rgba(239,68,68,0.08); }
.alert-type { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;
  color: var(--orange); margin-bottom: 4px; }
.alert-item.critical .alert-type { color: var(--red); }
.alert-msg { font-size: 13px; color: var(--text); }
.alert-time { font-size: 11px; color: var(--muted); margin-top: 4px; }

/* Savings highlight */
.savings-card { background: linear-gradient(135deg, #065f46, #064e3b); border: 1px solid #10b981; }
.savings-card .value { color: var(--green); }

/* Routing model chip */
.model-chip { display: inline-block; padding: 2px 10px; border-radius: 12px;
  font-size: 12px; font-weight: 600; }
.model-chip-0 { background: #166534; color: #86efac; }
.model-chip-1 { background: #1e3a5f; color: #93c5fd; }
.model-chip-2 { background: #581c87; color: #d8b4fe; }

/* Confidence bar */
.conf-bar { height: 8px; background: var(--border); border-radius: 4px; overflow: hidden; margin-top: 4px; }
.conf-fill { height: 100%; border-radius: 4px; transition: width 0.5s; }
.conf-label { font-size: 11px; font-weight: 600; margin-top: 2px; }
.estimation-card { background: linear-gradient(135deg, #1e1b4b, #312e81); border: 1px solid #6366f1; }
.estimation-card .value { color: #a5b4fc; }

.empty-state { text-align: center; padding: 40px 20px; color: var(--muted); font-size: 14px; }

@media (max-width: 768px) {
  .grid-2 { grid-template-columns: 1fr; }
  .tabs { overflow-x: auto; }
}
</style>
</head>
<body>
<div class="header">
  <h1>WarderBox</h1>
  <span class="badge">LIVE</span>
  <span class="auto-refresh">Auto-refresh: 3s</span>
</div>
<div class="tabs">
  <div class="tab active" onclick="switchTab('overview',this)">Overview</div>
  <div class="tab" onclick="switchTab('finops',this)">FinOps</div>
  <div class="tab" onclick="switchTab('estimation',this)">Estimation</div>
  <div class="tab" onclick="switchTab('routing',this)">Routing</div>
  <div class="tab" onclick="switchTab('alerts',this)">Alerts</div>
</div>

<!-- Overview Tab -->
<div class="tab-content active" id="tab-overview">
<div class="container">
  <div class="grid" id="stats"></div>
  <div class="grid">
    <div class="card" id="budget-card"></div>
    <div class="card" id="rate-card"></div>
  </div>
  <div class="table-card">
    <h2>Recent Actions</h2>
    <table><thead><tr>
      <th>Tool</th><th>Risk</th><th>Status</th><th>Cost</th><th>Time</th>
    </tr></thead><tbody id="log-body"></tbody></table>
  </div>
</div>
</div>

<!-- FinOps Tab -->
<div class="tab-content" id="tab-finops">
<div class="container">
  <div class="grid" id="finops-stats"></div>
  <div class="grid-2">
    <div class="card">
      <h3>Cost by Tool</h3>
      <div id="cost-by-tool"></div>
    </div>
    <div class="card">
      <h3>Cost Trend</h3>
      <div id="cost-trend"></div>
    </div>
  </div>
  <div class="table-card">
    <h2>Top Spending Tools</h2>
    <table><thead><tr>
      <th>Tool</th><th>Total Cost</th><th>Actions</th><th>Avg Cost</th><th>Share</th>
    </tr></thead><tbody id="tool-cost-body"></tbody></table>
  </div>
</div>
</div>

<!-- Estimation Tab -->
<div class="tab-content" id="tab-estimation">
<div class="container">
  <div class="grid" id="est-stats"></div>
  <div class="grid-2">
    <div class="card">
      <h3>Confidence by Tool</h3>
      <div id="conf-by-tool"></div>
    </div>
    <div class="card">
      <h3>Self-Improving Accuracy</h3>
      <div id="conf-progress"></div>
    </div>
  </div>
  <div class="table-card">
    <h2>Per-Tool Estimation Stats</h2>
    <table><thead><tr>
      <th>Tool</th><th>Calls</th><th>Confidence</th><th>Avg Cost</th><th>Avg Duration</th><th>Failure Rate</th>
    </tr></thead><tbody id="est-tool-body"></tbody></table>
  </div>
</div>
</div>

<!-- Routing Tab -->
<div class="tab-content" id="tab-routing">
<div class="container">
  <div class="grid" id="routing-stats"></div>
  <div class="grid-2">
    <div class="card">
      <h3>Requests by Model</h3>
      <div id="model-dist"></div>
    </div>
    <div class="card">
      <h3>Requests by Complexity</h3>
      <div id="complexity-dist"></div>
    </div>
  </div>
  <div class="table-card">
    <h2>Recent Routing Decisions</h2>
    <table><thead><tr>
      <th>Model</th><th>Complexity</th><th>Score</th><th>Cost/1k</th><th>Reason</th>
    </tr></thead><tbody id="routing-body"></tbody></table>
  </div>
</div>
</div>

<!-- Alerts Tab -->
<div class="tab-content" id="tab-alerts">
<div class="container">
  <div class="grid" id="alert-stats"></div>
  <div class="card">
    <h3>Recent Anomaly Alerts</h3>
    <div id="alert-list"></div>
  </div>
</div>
</div>

<script>
function switchTab(name, el) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
}

function barColor(i) {
  const colors = ['var(--accent)', 'var(--green)', 'var(--purple)', 'var(--cyan)', 'var(--orange)', 'var(--yellow)'];
  return colors[i % colors.length];
}

function modelChip(model, idx) {
  return `<span class="model-chip model-chip-${Math.min(idx, 2)}">${model}</span>`;
}

async function refresh() {
  // Overview
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    document.getElementById('stats').innerHTML = `
      <div class="card"><h3>Total Actions</h3>
        <div class="value">${d.total_actions}</div></div>
      <div class="card"><h3>Successful</h3>
        <div class="value green">${d.successful}</div></div>
      <div class="card"><h3>Blocked</h3>
        <div class="value red">${d.blocked}</div></div>
      <div class="card"><h3>Errors</h3>
        <div class="value orange">${d.errors}</div></div>`;
    const b = d.budget;
    const pct = b.budget ? Math.min(100, (b.spent / b.budget) * 100) : 0;
    const bcolor = pct > 80 ? 'var(--red)' : pct > 50 ? 'var(--yellow)' : 'var(--green)';
    document.getElementById('budget-card').innerHTML = `
      <h3>Budget</h3>
      <div class="value">$${b.spent.toFixed(2)}</div>
      <div class="sub">${b.budget ? 'of $' + b.budget.toFixed(2) + ' (' + pct.toFixed(0) + '%)' : 'No limit set'}</div>
      ${b.budget ? '<div class="budget-bar"><div class="budget-fill" style="width:'+pct+'%;background:'+bcolor+'"></div></div>' : ''}`;
    const rl = d.rate_limits;
    document.getElementById('rate-card').innerHTML = `
      <h3>Rate Limits</h3>
      <div class="value">${rl.global_last_minute}</div>
      <div class="sub">${rl.global_last_minute} calls/min | ${rl.global_last_hour} calls/hr</div>`;
  } catch(e) {}

  // Action log
  try {
    const r = await fetch('/api/history');
    const rows = await r.json();
    document.getElementById('log-body').innerHTML = rows.slice(-50).reverse().map(h => {
      const status = h.blocked ? 'blocked' : (h.success ? 'ok' : 'error');
      const label = h.blocked ? 'BLOCKED' : (h.success ? 'OK' : 'ERROR');
      const t = new Date(h.timestamp * 1000).toLocaleTimeString();
      return `<tr><td>${h.tool}</td><td>${h.risk.toUpperCase()}</td>
        <td><span class="status status-${status}">${label}</span></td>
        <td>$${h.cost.toFixed(4)}</td><td>${t}</td></tr>`;
    }).join('');
  } catch(e) {}

  // FinOps
  try {
    const r = await fetch('/api/finops');
    const d = await r.json();
    const f = d.forecast;
    document.getElementById('finops-stats').innerHTML = `
      <div class="card"><h3>Total Spent</h3>
        <div class="value">$${f.total_spent.toFixed(2)}</div>
        <div class="sub">${f.total_actions} actions</div></div>
      <div class="card"><h3>Burn Rate</h3>
        <div class="value yellow">$${f.burn_rate_per_hour.toFixed(2)}/hr</div>
        <div class="sub">$${f.burn_rate_per_minute.toFixed(4)}/min</div></div>
      <div class="card"><h3>Avg Cost/Action</h3>
        <div class="value">$${f.avg_cost_per_action.toFixed(4)}</div></div>
      <div class="card"><h3>Trend</h3>
        <div class="value ${f.trend === 'increasing' ? 'red' : f.trend === 'decreasing' ? 'green' : 'yellow'}">${f.trend}</div>
        <div class="sub">${f.estimated_actions_remaining !== null ? f.estimated_actions_remaining + ' actions remaining' : ''}</div></div>`;

    // Cost by tool bars
    const tools = Object.entries(f.cost_by_tool).sort((a,b) => b[1] - a[1]);
    const maxCost = tools.length ? tools[0][1] : 1;
    document.getElementById('cost-by-tool').innerHTML = tools.length ? tools.map((t, i) => {
      const pct = (t[1] / maxCost * 100).toFixed(0);
      return `<div class="cost-bar-row">
        <span class="cost-bar-label">${t[0]}</span>
        <div class="cost-bar-track"><div class="cost-bar-fill" style="width:${pct}%;background:${barColor(i)}">${pct > 15 ? '$' + t[1].toFixed(4) : ''}</div></div>
        <span class="cost-bar-value">$${t[1].toFixed(4)}</span></div>`;
    }).join('') : '<div class="empty-state">No cost data yet</div>';

    // Cost trend
    document.getElementById('cost-trend').innerHTML = `
      <div style="text-align:center;padding:20px;">
        <div style="font-size:48px;margin-bottom:8px;">${f.trend === 'increasing' ? '📈' : f.trend === 'decreasing' ? '📉' : '📊'}</div>
        <div style="font-size:18px;font-weight:600;" class="${f.trend === 'increasing' ? 'red' : f.trend === 'decreasing' ? 'green' : 'yellow'}">${f.trend.toUpperCase()}</div>
        ${f.estimated_exhaustion ? '<div style="margin-top:12px;font-size:13px;color:var(--muted)">Budget exhaustion: ' + new Date(f.estimated_exhaustion).toLocaleString() + '</div>' : ''}
      </div>`;

    // Tool cost table
    const totalCost = tools.reduce((s, t) => s + t[1], 0);
    document.getElementById('tool-cost-body').innerHTML = tools.map(t => {
      const share = totalCost > 0 ? (t[1] / totalCost * 100).toFixed(1) : '0.0';
      const count = d.tool_actions ? (d.tool_actions[t[0]] || 0) : '-';
      const avg = count > 0 ? (t[1] / count).toFixed(4) : '-';
      return `<tr><td>${t[0]}</td><td>$${t[1].toFixed(4)}</td><td>${count}</td><td>$${avg}</td><td>${share}%</td></tr>`;
    }).join('') || '<tr><td colspan="5" class="empty-state">No data</td></tr>';
  } catch(e) {}

  // Routing
  try {
    const r = await fetch('/api/routing');
    const d = await r.json();
    if (d.enabled) {
      document.getElementById('routing-stats').innerHTML = `
        <div class="card"><h3>Total Routed</h3>
          <div class="value">${d.stats.total_routed}</div></div>
        <div class="savings-card card"><h3>Cost Savings</h3>
          <div class="value">${d.stats.savings_percent.toFixed(1)}%</div>
          <div class="sub">$${d.stats.total_cost_actual.toFixed(4)} vs $${d.stats.total_cost_if_max.toFixed(4)}</div></div>
        <div class="card"><h3>Fallbacks</h3>
          <div class="value orange">${d.stats.fallback_count}</div></div>
        <div class="card"><h3>Models</h3>
          <div class="value purple">${Object.keys(d.stats.by_model).length}</div></div>`;

      // Model distribution
      const models = Object.entries(d.stats.by_model).sort((a,b) => b[1] - a[1]);
      const maxM = models.length ? models[0][1] : 1;
      document.getElementById('model-dist').innerHTML = models.length ? models.map((m, i) => {
        const pct = (m[1] / maxM * 100).toFixed(0);
        return `<div class="cost-bar-row">
          <span class="cost-bar-label">${modelChip(m[0], i)}</span>
          <div class="cost-bar-track"><div class="cost-bar-fill" style="width:${pct}%;background:${barColor(i)}">${pct > 15 ? m[1] : ''}</div></div>
          <span class="cost-bar-value">${m[1]}</span></div>`;
      }).join('') : '<div class="empty-state">No routing data</div>';

      // Complexity distribution
      const comps = Object.entries(d.stats.by_complexity).sort();
      const maxC = comps.length ? Math.max(...comps.map(c => c[1])) : 1;
      const compColors = {'low': 'var(--green)', 'medium': 'var(--yellow)', 'high': 'var(--red)'};
      document.getElementById('complexity-dist').innerHTML = comps.length ? comps.map(c => {
        const pct = (c[1] / maxC * 100).toFixed(0);
        return `<div class="cost-bar-row">
          <span class="cost-bar-label">${c[0].toUpperCase()}</span>
          <div class="cost-bar-track"><div class="cost-bar-fill" style="width:${pct}%;background:${compColors[c[0]] || 'var(--accent)'}">${pct > 15 ? c[1] : ''}</div></div>
          <span class="cost-bar-value">${c[1]}</span></div>`;
      }).join('') : '<div class="empty-state">No data</div>';

      // Recent decisions
      document.getElementById('routing-body').innerHTML = d.recent.map((r, i) => {
        return `<tr><td>${modelChip(r.model, 0)}</td><td>${r.complexity.toUpperCase()}</td>
          <td>${r.complexity_score.toFixed(3)}</td><td>$${r.estimated_cost_per_1k.toFixed(5)}</td>
          <td style="font-size:12px">${r.reason}</td></tr>`;
      }).join('') || '<tr><td colspan="5" class="empty-state">No routing decisions</td></tr>';
    } else {
      document.getElementById('routing-stats').innerHTML = '<div class="card" style="grid-column:1/-1"><div class="empty-state">Model routing not configured. Attach a ModelRouter to enable.</div></div>';
      document.getElementById('model-dist').innerHTML = '';
      document.getElementById('complexity-dist').innerHTML = '';
      document.getElementById('routing-body').innerHTML = '';
    }
  } catch(e) {}

  // Estimation
  try {
    const r = await fetch('/api/estimator');
    const d = await r.json();
    const toolCount = d.tracked_tools;
    const dataPoints = d.total_data_points;
    const tools = Object.entries(d.tools);
    const maxConf = tools.length ? Math.max(...tools.map(t => t[1].confidence)) : 0;
    const avgConf = tools.length ? tools.reduce((s, t) => s + t[1].confidence, 0) / tools.length : 0;
    const confLevel = maxConf >= 0.95 ? 'green' : maxConf >= 0.5 ? 'yellow' : maxConf > 0 ? 'orange' : 'muted';

    document.getElementById('est-stats').innerHTML = `
      <div class="card"><h3>Tracked Tools</h3>
        <div class="value cyan">${toolCount}</div></div>
      <div class="card"><h3>Data Points</h3>
        <div class="value">${dataPoints}</div>
        <div class="sub">Self-improving with every call</div></div>
      <div class="estimation-card card"><h3>Best Confidence</h3>
        <div class="value">${(maxConf * 100).toFixed(0)}%</div>
        <div class="sub">${maxConf >= 0.95 ? 'Maximum accuracy reached' : maxConf >= 0.8 ? 'High accuracy' : maxConf >= 0.5 ? 'Growing — keep running' : maxConf > 0 ? 'Early stage — learning' : 'No data yet'}</div></div>
      <div class="card"><h3>Avg Confidence</h3>
        <div class="value ${confLevel}">${(avgConf * 100).toFixed(0)}%</div></div>`;

    // Confidence bars per tool
    document.getElementById('conf-by-tool').innerHTML = tools.length ? tools
      .sort((a,b) => b[1].confidence - a[1].confidence)
      .map((t, i) => {
        const pct = (t[1].confidence * 100).toFixed(0);
        const clr = t[1].confidence >= 0.95 ? 'var(--green)' : t[1].confidence >= 0.8 ? 'var(--cyan)' : t[1].confidence >= 0.5 ? 'var(--yellow)' : 'var(--orange)';
        return `<div class="cost-bar-row">
          <span class="cost-bar-label">${t[0]}</span>
          <div class="cost-bar-track"><div class="cost-bar-fill" style="width:${pct}%;background:${clr}">${pct > 15 ? pct + '%' : ''}</div></div>
          <span class="cost-bar-value">${pct}%</span></div>`;
      }).join('') : '<div class="empty-state">No tools tracked yet</div>';

    // Self-improving progress visualization
    const thresholds = [{n: 5, c: 20, l: 'Initial'}, {n: 20, c: 50, l: 'Learning'}, {n: 100, c: 80, l: 'Reliable'}, {n: 100, c: 95, l: 'Maximum'}];
    document.getElementById('conf-progress').innerHTML = `
      <div style="padding:12px 0;">
        ${thresholds.map((th, i) => {
          const reached = dataPoints >= th.n && (i < 3 || maxConf >= 0.95);
          return `<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">
            <div style="width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:700;
              background:${reached ? 'var(--green)' : 'var(--border)'};color:${reached ? '#fff' : 'var(--muted)'}">${th.c}%</div>
            <div>
              <div style="font-size:14px;font-weight:600;color:${reached ? 'var(--text)' : 'var(--muted)'}">${th.l}</div>
              <div style="font-size:12px;color:var(--muted)">${i < 3 ? th.n + '+ calls' : '100+ calls per tool'}</div>
            </div>
            ${reached ? '<span style="margin-left:auto;color:var(--green);font-size:12px;font-weight:600">&#10003;</span>' : ''}
          </div>`;
        }).join('')}
        <div style="font-size:12px;color:var(--muted);margin-top:8px;font-style:italic;">Gets smarter the more your agents run</div>
      </div>`;

    // Per-tool table
    document.getElementById('est-tool-body').innerHTML = tools.length ? tools
      .sort((a,b) => b[1].total_calls - a[1].total_calls)
      .map(t => {
        const s = t[1];
        const confPct = (s.confidence * 100).toFixed(0);
        const confClr = s.confidence >= 0.95 ? 'green' : s.confidence >= 0.8 ? 'cyan' : s.confidence >= 0.5 ? 'yellow' : 'orange';
        return `<tr><td>${t[0]}</td><td>${s.total_calls}</td>
          <td><span class="${confClr}">${confPct}%</span>
            <div class="conf-bar"><div class="conf-fill" style="width:${confPct}%;background:var(--${confClr})"></div></div></td>
          <td>$${s.avg_cost.toFixed(4)}</td><td>${s.avg_duration.toFixed(2)}s</td>
          <td>${(s.failure_rate * 100).toFixed(1)}%</td></tr>`;
      }).join('') : '<tr><td colspan="6" class="empty-state">No estimation data yet. Run some tools to start learning.</td></tr>';
  } catch(e) {}

  // Alerts
  try {
    const r = await fetch('/api/alerts');
    const d = await r.json();
    document.getElementById('alert-stats').innerHTML = `
      <div class="card"><h3>Total Alerts</h3>
        <div class="value ${d.total > 0 ? 'red' : 'green'}">${d.total}</div></div>
      <div class="card"><h3>Cost Spikes</h3>
        <div class="value orange">${d.by_type.cost_spike || 0}</div></div>
      <div class="card"><h3>Loop Detected</h3>
        <div class="value red">${d.by_type.loop_detected || 0}</div></div>
      <div class="card"><h3>Rate Spikes</h3>
        <div class="value yellow">${d.by_type.rate_spike || 0}</div></div>`;

    document.getElementById('alert-list').innerHTML = d.alerts.length ? d.alerts.slice(-30).reverse().map(a => {
      const t = new Date(a.timestamp * 1000).toLocaleString();
      return `<div class="alert-item ${a.severity}">
        <div class="alert-type">${a.type} &middot; ${a.severity}</div>
        <div class="alert-msg">${a.message}</div>
        <div class="alert-time">${t}${a.tool_name ? ' &middot; ' + a.tool_name : ''}</div></div>`;
    }).join('') : '<div class="empty-state">No anomalies detected</div>';
  } catch(e) {}
}
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>"""


class _DashboardHandler(BaseHTTPRequestHandler):
    shield: WarderBox
    router: ModelRouter | None
    anomaly_detector: AnomalyDetector | None

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._respond(200, "text/html", _DASHBOARD_HTML)
        elif self.path == "/api/stats":
            data = self.shield.stats()
            self._respond(200, "application/json", json.dumps(data))
        elif self.path == "/api/history":
            data = self.shield.history()
            self._respond(200, "application/json", json.dumps(data))
        elif self.path == "/api/finops":
            data = self._get_finops()
            self._respond(200, "application/json", json.dumps(data))
        elif self.path == "/api/routing":
            data = self._get_routing()
            self._respond(200, "application/json", json.dumps(data))
        elif self.path == "/api/estimator":
            data = self._get_estimator()
            self._respond(200, "application/json", json.dumps(data))
        elif self.path == "/api/alerts":
            data = self._get_alerts()
            self._respond(200, "application/json", json.dumps(data))
        else:
            self._respond(404, "text/plain", "Not found")

    def _get_finops(self) -> dict:
        from warderbox.forecast import CostForecaster
        forecaster = CostForecaster(self.shield)
        report = forecaster.forecast()

        tool_actions: dict[str, int] = {}
        for h in self.shield.history():
            tool = h["tool"]
            tool_actions[tool] = tool_actions.get(tool, 0) + 1

        return {
            "forecast": report.to_dict(),
            "tool_actions": tool_actions,
        }

    def _get_routing(self) -> dict:
        if not self.router:
            return {"enabled": False}
        stats = self.router.stats()
        return {
            "enabled": True,
            "stats": stats.to_dict(),
            "recent": self.router.recent_decisions(limit=20),
        }

    def _get_estimator(self) -> dict:
        estimator = self.shield.estimator
        basic = estimator.stats()
        detailed: dict[str, dict] = {}
        for name in basic["tools"]:
            detail = estimator.get_tool_stats(name)
            if detail:
                detailed[name] = detail
        return {
            "tracked_tools": basic["tracked_tools"],
            "total_data_points": basic["total_data_points"],
            "tools": detailed,
        }

    def _get_alerts(self) -> dict:
        if not self.anomaly_detector:
            return {"total": 0, "by_type": {}, "alerts": []}
        stats = self.anomaly_detector.stats()
        alerts = [a.to_dict() for a in self.anomaly_detector.alerts[-50:]]
        return {
            "total": stats["total_alerts"],
            "by_type": stats["alerts_by_type"],
            "alerts": alerts,
        }

    def _respond(self, code: int, content_type: str, body: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format, *args):
        pass


def start_dashboard(
    shield: WarderBox,
    port: int = 8080,
    host: str = "127.0.0.1",
    open_browser: bool = True,
    background: bool = True,
    router: ModelRouter | None = None,
    anomaly_detector: AnomalyDetector | None = None,
) -> HTTPServer:
    """Start the WarderBox web dashboard.

    Args:
        shield: The WarderBox instance to monitor.
        port: Port to serve on (default 8080).
        host: Host to bind to (default localhost).
        open_browser: Open the dashboard in the default browser.
        background: Run in a background thread (default True).
        router: Optional ModelRouter for routing stats tab.
        anomaly_detector: Optional AnomalyDetector for alerts tab.

    Returns:
        The HTTPServer instance (call .shutdown() to stop).
    """
    handler = type("Handler", (_DashboardHandler,), {
        "shield": shield,
        "router": router,
        "anomaly_detector": anomaly_detector,
    })
    server = HTTPServer((host, port), handler)

    if background:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    else:
        if open_browser:
            import webbrowser
            webbrowser.open(f"http://{host}:{port}")
        server.serve_forever()
        return server

    if open_browser:
        import webbrowser
        webbrowser.open(f"http://{host}:{port}")

    return server
