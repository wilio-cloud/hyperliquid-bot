"""Servidor web lleuger per monitoritzar el bot d'arbitratge i scalping en viu."""

import os
import time
from aiohttp import web

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ca">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Arbitratge Delta-Neutral • Hyperliquid DEX</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
        body { background: #0b0f19; color: #f8fafc; padding: 16px; }
        .container { max-width: 1100px; margin: 0 auto; }
        header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; border-bottom: 1px solid #1e293b; padding-bottom: 14px; }
        h1 { font-size: 1.35rem; color: #38bdf8; display: flex; align-items: center; gap: 10px; }
        .badge { background: #10b981; color: #fff; font-size: 0.75rem; padding: 3px 10px; border-radius: 999px; font-weight: bold; letter-spacing: 0.5px; }
        .badge-strategy { background: #6366f1; color: #fff; font-size: 0.75rem; padding: 3px 8px; border-radius: 4px; font-weight: 600; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 24px; }
        .card { background: #131d31; border: 1px solid #1e293b; border-radius: 10px; padding: 14px; }
        .card .label { font-size: 0.75rem; color: #94a3b8; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
        .card .val { font-size: 1.35rem; font-weight: bold; }
        .card .sub { font-size: 0.75rem; color: #64748b; margin-top: 4px; }
        .green { color: #10b981; }
        .red { color: #ef4444; }
        .yellow { color: #facc15; }
        .purple { color: #c084fc; }
        .card-table { background: #131d31; border: 1px solid #1e293b; border-radius: 10px; margin-bottom: 24px; overflow-x: auto; }
        .table-title { padding: 14px 18px; font-weight: bold; font-size: 1rem; border-bottom: 1px solid #1e293b; color: #e2e8f0; display: flex; justify-content: space-between; align-items: center; }
        table { width: 100%; border-collapse: collapse; font-size: 0.85rem; text-align: left; }
        th, td { padding: 10px 14px; border-bottom: 1px solid #1e293b; }
        th { color: #94a3b8; font-weight: 600; font-size: 0.78rem; text-transform: uppercase; }
        tr:last-child td { border-bottom: none; }
        .tag-signal { padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; display: inline-block; }
        .tag-buy { background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid #10b981; }
        .tag-sell { background: rgba(239, 68, 68, 0.15); color: #ef4444; border: 1px solid #ef4444; }
        .tag-neutral { color: #64748b; }
        .footer { text-align: center; font-size: 0.75rem; color: #475569; margin-top: 24px; }

        /* Estils del Panell de Gràfica i Simulador */
        .card-chart { background: #131d31; border: 1px solid #1e293b; border-radius: 10px; padding: 16px; margin-bottom: 24px; }
        .chart-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; padding-bottom: 10px; border-bottom: 1px solid #1e293b; }
        .chart-controls { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
        .btn-group { display: inline-flex; background: #0b0f19; border-radius: 8px; padding: 2px; border: 1px solid #1e293b; }
        .btn-horizon { background: transparent; color: #94a3b8; border: none; padding: 6px 12px; font-size: 0.76rem; font-weight: 600; border-radius: 6px; cursor: pointer; transition: all 0.2s ease; }
        .btn-horizon:hover { color: #f8fafc; }
        .btn-horizon.active { background: #38bdf8; color: #0b0f19; font-weight: bold; }
        .toggles-group { display: flex; flex-wrap: wrap; gap: 6px; }
        .toggle-chip { font-size: 0.72rem; padding: 4px 10px; border-radius: 999px; cursor: pointer; border: 1px solid transparent; user-select: none; transition: all 0.15s; font-weight: 600; }
        .chip-real { background: rgba(192, 132, 252, 0.15); color: #c084fc; border-color: #c084fc; }
        .chip-actual { background: rgba(16, 185, 129, 0.15); color: #10b981; border-color: #10b981; }
        .chip-target { background: rgba(56, 189, 248, 0.15); color: #38bdf8; border-color: #38bdf8; }
        .chip-cons { background: rgba(250, 204, 21, 0.15); color: #facc15; border-color: #facc15; }
        .chip-ny { background: rgba(249, 115, 22, 0.15); color: #fb923c; border-color: #f97316; }
        .chip-custom { background: rgba(255, 255, 255, 0.08); color: #e2e8f0; border-color: #94a3b8; }
        .chip-inactive { opacity: 0.35; border-color: transparent !important; text-decoration: line-through; }
        .slider-container { display: flex; align-items: center; gap: 12px; background: #0b0f19; padding: 8px 14px; border-radius: 8px; border: 1px solid #1e293b; font-size: 0.8rem; color: #cbd5e1; margin-bottom: 14px; }
        .slider-container input[type=range] { flex: 1; accent-color: #38bdf8; cursor: pointer; }
        .kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-top: 14px; padding-top: 12px; border-top: 1px solid #1e293b; }
        .kpi-box { background: #0b0f19; padding: 10px; border-radius: 6px; border: 1px solid #1e293b; }
        .kpi-box .kpi-lbl { font-size: 0.7rem; color: #94a3b8; text-transform: uppercase; margin-bottom: 3px; }
        .kpi-box .kpi-val { font-size: 1.05rem; font-weight: bold; }
        .kpi-box .kpi-sub { font-size: 0.7rem; color: #64748b; margin-top: 2px; }
        .ny-badge { padding: 4px 10px; border-radius: 6px; font-size: 0.75rem; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; }
        .ny-open { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981; }
        .ny-pre { background: rgba(249, 115, 22, 0.2); color: #fb923c; border: 1px solid #f97316; }
        .ny-closed { background: rgba(148, 163, 184, 0.15); color: #94a3b8; border: 1px solid #475569; }
        .btn-pause { background: #ef4444; color: #ffffff; border: none; padding: 6px 14px; font-size: 0.78rem; font-weight: 700; border-radius: 6px; cursor: pointer; transition: all 0.2s ease; display: inline-flex; align-items: center; gap: 6px; }
        .btn-pause:hover { opacity: 0.88; }
        .btn-resume { background: #10b981; color: #0b0f19; border: none; padding: 6px 14px; font-size: 0.78rem; font-weight: 700; border-radius: 6px; cursor: pointer; transition: all 0.2s ease; display: inline-flex; align-items: center; gap: 6px; }
        .btn-resume:hover { opacity: 0.88; }
        .btn-size { background: #1e293b; color: #94a3b8; border: 1px solid #334155; padding: 3px 8px; border-radius: 5px; font-size: 0.72rem; font-weight: 600; cursor: pointer; transition: all 0.15s ease; }
        .btn-size:hover { background: #38bdf8; color: #0b0f19; border-color: #38bdf8; }
        .badge-paused { background: rgba(239, 68, 68, 0.25); color: #f87171; border: 1px solid #ef4444; padding: 4px 10px; border-radius: 6px; font-size: 0.75rem; font-weight: 700; display: none; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
                    <h1 style="margin: 0;">⚡ Arbitratge Delta-Neutral</h1>
                    <span class="badge" id="badge-mode" style="background: #f59e0b; color: #1e293b;">SIMULACIÓ (PAPER)</span>
                    <span class="badge-paused" id="badge-paused">⛔ TRADING PAUSAT</span>
                </div>
                <div style="font-size: 0.8rem; color: #94a3b8; margin-top: 4px;" id="header-sub">Hyperliquid DEX vs OKX Perpetuals • Dades de Mercat L2 en Temps Real (Sense Diners Reals)</div>
            </div>
            <div style="text-align: right; display: flex; flex-direction: column; align-items: flex-end; gap: 6px;">
                <div style="display: flex; align-items: center; gap: 8px;">
                    <button class="btn-pause" id="btn-pause-toggle" onclick="toggleTradingPause()">⏸️ Pausar Trading</button>
                    <span class="badge-strategy" id="mode-tag">DELTA-NEUTRAL ARB (3x)</span>
                </div>
                <div style="display: flex; align-items: center; gap: 5px; margin-top: 2px;">
                    <span style="font-size: 0.72rem; color: #94a3b8;">Mida/Ranura:</span>
                    <button class="btn-size" onclick="setOrderSize(30)">30$</button>
                    <button class="btn-size" onclick="setOrderSize(120)">120$</button>
                    <button class="btn-size" onclick="setOrderSize(150)">150$</button>
                    <button class="btn-size" onclick="promptCustomSize()">✏️</button>
                </div>
                <div style="font-size: 0.8rem; color: #94a3b8;" id="uptime">Carregant...</div>
            </div>
        </header>

        <div class="grid">
            <div class="card">
                <div class="label">Balanç Total</div>
                <div class="val" id="balance">-</div>
                <div class="sub" id="balance-sub">HL: - | dYdX: -</div>
            </div>
            <div class="card">
                <div class="label">PnL Net Total</div>
                <div class="val" id="pnl">-</div>
                <div class="sub" id="pnl-sub">Realitzat: -</div>
            </div>
            <div class="card">
                <div class="label">Funding Cobrat</div>
                <div class="val green" id="funding">-</div>
                <div class="sub">Rendiment Passiu (APR)</div>
            </div>
            <div class="card">
                <div class="label">Operacions</div>
                <div class="val" id="trades">-</div>
                <div class="sub" id="winrate-sub">Winrate: -%</div>
            </div>
            <div class="card">
                <div class="label">APR Net Carry</div>
                <div class="val" id="pace">-</div>
                <div class="sub" id="pace-sub">Objectiu: ≥18% APR net</div>
            </div>
            <div class="card">
                <div class="label">Comissions</div>
                <div class="val purple" id="fees">-</div>
                <div class="sub">Maker & Taker nets</div>
            </div>
        </div>

        <!-- Gràfica de Projecció Composta Funding Carry -->
        <div class="card-chart">
            <div class="chart-header">
                <div>
                    <h2 style="font-size: 1.05rem; color: #38bdf8; display: flex; align-items: center; gap: 8px;">
                        📈 Projecció de Creixement Compost — Funding Carry
                    </h2>
                    <div style="font-size: 0.78rem; color: #94a3b8; margin-top: 3px;">
                        Model compost mensual • Delta-neutral • Reinversió automàtica
                    </div>
                </div>
                <div id="live-apr-badge" style="background: rgba(16,185,129,0.15); color: #34d399; padding: 4px 12px; border-radius: 6px; font-size: 0.82rem; font-weight: 600;">
                    ⚡ APR en viu: ---%
                </div>
            </div>

            <!-- Controls d'Horitzó -->
            <div class="chart-controls">
                <div class="btn-group" id="horizon-selector">
                    <button class="btn-horizon active" onclick="setHorizon(12)">1 Any</button>
                    <button class="btn-horizon" onclick="setHorizon(60)">5 Anys</button>
                    <button class="btn-horizon" onclick="setHorizon(120)">10 Anys</button>
                </div>
                <div class="toggles-group">
                    <span class="toggle-chip" style="border-color: #c084fc; color: #c084fc;" id="chip-real" onclick="toggleCurve('real')">🟣 Equitat Real</span>
                    <span class="toggle-chip" style="border-color: #10b981; color: #10b981;" id="chip-optimistic" onclick="toggleCurve('optimistic')">🟢 Optimista (30%)</span>
                    <span class="toggle-chip" style="border-color: #38bdf8; color: #38bdf8;" id="chip-realistic" onclick="toggleCurve('realistic')">🔵 Realista (20%)</span>
                    <span class="toggle-chip" style="border-color: #facc15; color: #facc15;" id="chip-conservative" onclick="toggleCurve('conservative')">🟡 Conservador (12%)</span>
                    <span class="toggle-chip" style="border-color: #f97316; color: #f97316;" id="chip-realtime" onclick="toggleCurve('realtime')">🟠 Ritme Temps Real</span>
                </div>
            </div>

            <!-- Canvas de Chart.js -->
            <div style="position: relative; height: 340px; width: 100%;">
                <canvas id="growthChart"></canvas>
            </div>

            <!-- KPIs de Projecció -->
            <div class="kpi-row">
                <div class="kpi-box">
                    <div class="kpi-lbl">Capital Actual</div>
                    <div class="kpi-val purple" id="kpi-real-bal">935 $</div>
                    <div class="kpi-sub" id="kpi-real-sub">2 posicions actives</div>
                </div>
                <div class="kpi-box">
                    <div class="kpi-lbl">Projecció 1 Any</div>
                    <div class="kpi-val green" id="kpi-1y-val">~1,122 $</div>
                    <div class="kpi-sub" id="kpi-1y-sub">+20% compost anual</div>
                </div>
                <div class="kpi-box">
                    <div class="kpi-lbl">Projecció 5 Anys</div>
                    <div class="kpi-val green" id="kpi-5y-val">~2,328 $</div>
                    <div class="kpi-sub" id="kpi-5y-sub">+149% acumulat</div>
                </div>
                <div class="kpi-box">
                    <div class="kpi-lbl">Projecció 10 Anys</div>
                    <div class="kpi-val" style="color: #fb923c;" id="kpi-10y-val">~5,789 $</div>
                    <div class="kpi-sub" id="kpi-10y-sub">+519% compost</div>
                </div>
            </div>
        </div>


        <!-- Taula 1: Monitor de Spreads en Viu -->
        <div class="card-table">
            <div class="table-title">
                <span>📊 Monitor de Spreads i Funding en Temps Real</span>
                <span style="font-size: 0.8rem; font-weight: normal; color: #94a3b8;" id="min-spread-label">Llindar mínim: <b style="color: #10b981;">±0.150%</b> (Marge Net Garantit)</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Moneda</th>
                        <th>Preu Hyperliquid</th>
                        <th id="th-venue2-px">Preu dYdX</th>
                        <th id="th-spread-label">Spread % (HL vs dYdX)</th>
                        <th>HL Fund (8h)</th>
                        <th id="th-venue2-fund">dYdX Fund (8h)</th>
                        <th>Dif. Funding (APR)</th>
                        <th>Estat Senyal</th>
                    </tr>
                </thead>
                <tbody id="spreads-body">
                    <tr><td colspan="8" style="text-align: center; color: #64748b;" id="loading-msg">Connectant amb els WebSockets dels exchanges...</td></tr>
                </tbody>
            </table>
        </div>

        <!-- Taula 2: Posicions Delta-Neutral Actives -->
        <div class="card-table">
            <div class="table-title">
                <span>⚖️ Posicions Arbitrades Actives (Delta Neutral = 0)</span>
                <span style="font-size: 0.8rem; font-weight: normal; color: #94a3b8;" id="pos-count">0 posicions</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Moneda</th>
                        <th>Direcció</th>
                        <th>Pota Hyperliquid</th>
                        <th id="th-leg-v2">Pota dYdX v4</th>
                        <th>Delta Net</th>
                        <th>Funding Cobrat</th>
                        <th>PnL No Realitzat</th>
                        <th>Estat / Sortida</th>
                    </tr>
                </thead>
                <tbody id="positions-body">
                    <tr><td colspan="8" style="text-align: center; color: #64748b;">Sense posicions d'arbitratge actives actualment</td></tr>
                </tbody>
            </table>
        </div>

        <!-- Taula 3: Control i Rendiment per Criptomoneda -->
        <div class="card-table">
            <div class="table-title">
                <span>🎯 Control de Freqüència i Rendiment per Criptomoneda</span>
                <span style="font-size: 0.8rem; font-weight: normal; color: #94a3b8;" id="pace-summary">Objectiu global: 8-10 op/h</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Moneda</th>
                        <th>Trades</th>
                        <th>Ritme (op/h)</th>
                        <th>Winrate</th>
                        <th>PnL Net Acumulat</th>
                        <th>PnL Mitjà / Trade</th>
                        <th>Temps Obert Mitjà</th>
                        <th>Diagnòstic / Calibració</th>
                    </tr>
                </thead>
                <tbody id="coin-stats-body">
                    <tr><td colspan="8" style="text-align: center; color: #64748b;">Carregant mètriques per moneda...</td></tr>
                </tbody>
            </table>
        </div>

        <!-- Taula 4: Darreres Operacions Tancades -->
        <div class="card-table">
            <div class="table-title">
                <span>📜 Darreres Operacions d'Arbitratge Tancades</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Moneda</th>
                        <th>Direcció</th>
                        <th>Motiu Sortida</th>
                        <th>Spread Capturat</th>
                        <th>Funding Net</th>
                        <th>Comissions</th>
                        <th>PnL Net ($)</th>
                    </tr>
                </thead>
                <tbody id="closed-body">
                    <tr><td colspan="7" style="text-align: center; color: #64748b;">Esperant convergències...</td></tr>
                </tbody>
            </table>
        </div>

        <div class="footer">Actualització automàtica en viu cada 1.5s • Cross-Exchange Statistical Arbitrage Engine</div>
    </div>

    <script>
        const fmtNum = (n, d = 2) => (typeof n === 'number' && !isNaN(n)) ? n.toFixed(d) : '0.00';
        const fmtPx = (p) => (typeof p === 'number' && !isNaN(p)) ? p.toFixed(p < 1.0 ? 4 : 2) : '-';
        const fmtPct = (p) => (typeof p === 'number' && !isNaN(p)) ? (p >= 0 ? '+' : '') + p.toFixed(3) + '%' : '-';

        let chartInstance = null;
        let currentHorizon = 12; // 12, 60, 120 mesos
        let curveVisibility = {
            real: true,
            optimistic: true,
            realistic: true,
            conservative: true,
            realtime: true
        };
        let cachedStatusData = null;

        function initChart() {
            if (typeof Chart === 'undefined') {
                return;
            }
            const canvas = document.getElementById('growthChart');
            if (!canvas) return;

            const ctx = canvas.getContext('2d');
            chartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: [],
                    datasets: [
                        {
                            label: 'Equitat Real',
                            data: [],
                            borderColor: '#c084fc',
                            backgroundColor: 'rgba(192, 132, 252, 0.1)',
                            fill: true,
                            borderWidth: 3,
                            pointRadius: 2,
                            pointHoverRadius: 5,
                            tension: 0.2,
                            spanGaps: true
                        },
                        {
                            label: 'Optimista (30% anual)',
                            data: [],
                            borderColor: '#10b981',
                            backgroundColor: 'rgba(16, 185, 129, 0.05)',
                            fill: true,
                            borderWidth: 2.5,
                            pointRadius: 0,
                            tension: 0.3,
                            borderDash: []
                        },
                        {
                            label: 'Realista (20% anual)',
                            data: [],
                            borderColor: '#38bdf8',
                            borderWidth: 2.5,
                            pointRadius: 0,
                            tension: 0.3,
                            borderDash: [6, 3]
                        },
                        {
                            label: 'Conservador (12% anual)',
                            data: [],
                            borderColor: '#facc15',
                            borderWidth: 2,
                            pointRadius: 0,
                            tension: 0.3,
                            borderDash: [4, 4]
                        },
                        {
                            label: 'Ritme Temps Real',
                            data: [],
                            borderColor: '#f97316',
                            backgroundColor: 'rgba(249, 115, 22, 0.05)',
                            fill: true,
                            borderWidth: 2,
                            pointRadius: 0,
                            tension: 0.3,
                            borderDash: [2, 2]
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: {
                        mode: 'index',
                        intersect: false
                    },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            backgroundColor: 'rgba(19, 29, 49, 0.95)',
                            titleColor: '#f8fafc',
                            bodyColor: '#e2e8f0',
                            borderColor: '#334155',
                            borderWidth: 1,
                            padding: 10,
                            callbacks: {
                                label: function(context) {
                                    const l = context.dataset.label || '';
                                    const v = context.parsed.y;
                                    if (v !== null && v !== undefined) {
                                        if (v >= 10000) return ` ${l}: ${(v/1000).toFixed(1)}k $`;
                                        return ` ${l}: ${v.toFixed(0)} $`;
                                    }
                                    return null;
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            grid: { color: 'rgba(255, 255, 255, 0.04)' },
                            ticks: { color: '#94a3b8', font: { size: 11 }, maxRotation: 45 }
                        },
                        y: {
                            grid: { color: 'rgba(255, 255, 255, 0.04)' },
                            ticks: {
                                color: '#94a3b8',
                                font: { size: 11 },
                                callback: function(v) {
                                    if (v >= 10000) return (v/1000).toFixed(0) + 'k $';
                                    if (v >= 1000) return (v/1000).toFixed(1) + 'k $';
                                    return v.toFixed(0) + ' $';
                                }
                            }
                        }
                    }
                }
            });
        }

        function updateChartData() {
            if (!chartInstance || !cachedStatusData) return;

            const data = cachedStatusData;
            const m = data.metrics || {};
            const curBal = (typeof m.balance === 'number') ? m.balance : 935.0;
            const initialBal = (typeof m.initial_balance === 'number' && m.initial_balance > 0) ? m.initial_balance : curBal;

            // Calcular APR real des de l'inici
            const uptimeStr = data.uptime || '00h 00m 00s';
            const uptimeParts = uptimeStr.split(/[hms ]+/).filter(x => x);
            const uptimeHours = uptimeParts.length >= 2 ? (parseInt(uptimeParts[0]||0) + parseInt(uptimeParts[1]||0)/60) : 0.01;
            const realGrowthRate = (curBal / initialBal - 1); // Growth since start
            const realAprFromData = uptimeHours > 0.5 ? (realGrowthRate / uptimeHours * 8760 * 100) : 0;

            // Obtenir APR ponderat de les posicions actives
            const netCarryApr = (typeof m.net_carry_apr === 'number') ? m.net_carry_apr : 0;

            // Escenaris APR anual (compost mensualment)
            const scenarios = {
                optimistic:   0.30,  // 30% anual
                realistic:    0.20,  // 20% anual
                conservative: 0.12,  // 12% anual
            };

            // Ritme temps real: extrapolat de l'APR observat dels funding rates actuals
            // Usem el net_carry_apr del backend si disponible, si no l'APR real mesurat
            let realtimeApr = 0;
            if (netCarryApr > 0) {
                realtimeApr = netCarryApr / 100; // ja ve en %
            } else if (uptimeHours > 1) {
                realtimeApr = realGrowthRate / uptimeHours * 8760;
            }

            // Generar labels i dades segons l'horitzó (en mesos)
            const totalMonths = currentHorizon;
            let stepMonths = 1;
            if (totalMonths >= 60) stepMonths = 3;   // cada trimestre
            if (totalMonths >= 120) stepMonths = 6;   // cada semestre

            let labels = ['Ara'];
            let realData = [curBal];

            // Corbes de projecció
            let optData = [curBal];
            let realstData = [curBal];
            let consData = [curBal];
            let rtData = [curBal];

            // Equitat real històrica: mapegem els punts reals als primers mesos
            const hist = data.equity_history || [];

            for (let month = stepMonths; month <= totalMonths; month += stepMonths) {
                // Label
                if (month < 12) {
                    labels.push(month + 'm');
                } else if (month % 12 === 0) {
                    labels.push((month/12) + ' any' + (month > 12 ? 's' : ''));
                } else {
                    labels.push(Math.floor(month/12) + 'a ' + (month%12) + 'm');
                }

                // Creixement compost mensual: capital * (1 + APR/12)^mes
                optData.push(curBal * Math.pow(1 + scenarios.optimistic/12, month));
                realstData.push(curBal * Math.pow(1 + scenarios.realistic/12, month));
                consData.push(curBal * Math.pow(1 + scenarios.conservative/12, month));
                rtData.push(curBal * Math.pow(1 + realtimeApr/12, month));

                // Equitat real: només tenim dades dels primers minuts/hores
                realData.push(null);
            }

            chartInstance.data.labels = labels;
            chartInstance.data.datasets[0].data = realData;
            chartInstance.data.datasets[1].data = optData;
            chartInstance.data.datasets[2].data = realstData;
            chartInstance.data.datasets[3].data = consData;
            chartInstance.data.datasets[4].data = rtData;

            // Visibilitat
            chartInstance.data.datasets[0].hidden = !curveVisibility.real;
            chartInstance.data.datasets[1].hidden = !curveVisibility.optimistic;
            chartInstance.data.datasets[2].hidden = !curveVisibility.realistic;
            chartInstance.data.datasets[3].hidden = !curveVisibility.conservative;
            chartInstance.data.datasets[4].hidden = !curveVisibility.realtime;

            chartInstance.update('none');

            // Actualitzar badge APR en viu
            const aprBadge = document.getElementById('live-apr-badge');
            if (aprBadge) {
                const activePos = (data.positions || []).length;
                if (netCarryApr > 0) {
                    aprBadge.innerHTML = `⚡ APR en viu: <b>${netCarryApr.toFixed(1)}%</b> (${activePos} pos)`;
                    aprBadge.style.color = '#34d399';
                } else if (activePos > 0) {
                    aprBadge.innerHTML = `⏳ Acumulant funding... (${activePos} pos)`;
                    aprBadge.style.color = '#facc15';
                } else {
                    aprBadge.innerHTML = `🔍 Buscant oportunitats...`;
                    aprBadge.style.color = '#94a3b8';
                }
            }

            // Actualitzar KPIs
            const kpiRealBal = document.getElementById('kpi-real-bal');
            if (kpiRealBal) kpiRealBal.innerText = `${curBal.toFixed(0)} $`;
            const kpiRealSub = document.getElementById('kpi-real-sub');
            if (kpiRealSub) {
                const activePos = (data.positions || []).length;
                const pnl = curBal - initialBal;
                kpiRealSub.innerText = `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}$ | ${activePos} posicions`;
            }

            // KPI 1 Any (20% realista)
            const est1y = curBal * Math.pow(1 + scenarios.realistic/12, 12);
            const kpi1y = document.getElementById('kpi-1y-val');
            if (kpi1y) kpi1y.innerText = `~${est1y.toFixed(0)} $`;
            const kpi1ySub = document.getElementById('kpi-1y-sub');
            if (kpi1ySub) kpi1ySub.innerText = `+${((est1y-curBal)/curBal*100).toFixed(0)}% (${((est1y-curBal)/12).toFixed(1)}$/mes)`;

            // KPI 5 Anys
            const est5y = curBal * Math.pow(1 + scenarios.realistic/12, 60);
            const kpi5y = document.getElementById('kpi-5y-val');
            if (kpi5y) kpi5y.innerText = `~${est5y >= 1000 ? (est5y/1000).toFixed(1)+'k' : est5y.toFixed(0)} $`;
            const kpi5ySub = document.getElementById('kpi-5y-sub');
            if (kpi5ySub) kpi5ySub.innerText = `+${((est5y-curBal)/curBal*100).toFixed(0)}% acumulat`;

            // KPI 10 Anys
            const est10y = curBal * Math.pow(1 + scenarios.realistic/12, 120);
            const kpi10y = document.getElementById('kpi-10y-val');
            if (kpi10y) kpi10y.innerText = `~${est10y >= 1000 ? (est10y/1000).toFixed(1)+'k' : est10y.toFixed(0)} $`;
            const kpi10ySub = document.getElementById('kpi-10y-sub');
            if (kpi10ySub) kpi10ySub.innerText = `+${((est10y-curBal)/curBal*100).toFixed(0)}% compost`;
        }

        function setHorizon(h) {
            currentHorizon = h;
            const btns = document.querySelectorAll('.btn-horizon');
            btns.forEach(b => b.classList.remove('active'));
            if (h === 12 && btns[0]) btns[0].classList.add('active');
            else if (h === 60 && btns[1]) btns[1].classList.add('active');
            else if (h === 120 && btns[2]) btns[2].classList.add('active');
            updateChartData();
        }

        function toggleCurve(key) {
            curveVisibility[key] = !curveVisibility[key];
            const chip = document.getElementById('chip-' + key);
            if (chip) {
                if (curveVisibility[key]) {
                    chip.classList.remove('chip-inactive');
                } else {
                    chip.classList.add('chip-inactive');
                }
            }
            if (chartInstance) {
                const datasetMap = { real: 0, actual: 1, target: 2, conservative: 3, ny: 4, custom: 5 };
                const idx = datasetMap[key];
                if (idx !== undefined && chartInstance.data.datasets[idx]) {
                    chartInstance.data.datasets[idx].hidden = !curveVisibility[key];
                    chartInstance.update('none');
                }
            }
        }



        async function setOrderSize(sz) {
            try {
                const r = await fetch(`/api/set_size?size=${sz}&dynamic=false`);
                const d = await r.json();
                if (d.status === 'ok') {
                    fetchStatus();
                }
            } catch (err) {
                console.error('Error setting size:', err);
            }
        }

        function promptCustomSize() {
            const val = prompt("Introdueix la nova mida en USD per ordre (ex: 150):", "150");
            if (val && !isNaN(val)) {
                setOrderSize(parseFloat(val));
            }
        }

        let isTradingPaused = false;
        async function toggleTradingPause() {
            const btn = document.getElementById('btn-pause-toggle');
            if (btn) btn.disabled = true;
            try {
                const ep = isTradingPaused ? '/api/resume' : '/api/pause';
                const r = await fetch(ep);
                const d = await r.json();
                if (d.status === 'ok') {
                    updatePauseUI(d.trading_paused);
                }
            } catch (err) {
                console.error('Error toggling pause:', err);
            } finally {
                if (btn) btn.disabled = false;
            }
        }

        function updatePauseUI(paused) {
            isTradingPaused = !!paused;
            const btn = document.getElementById('btn-pause-toggle');
            const badge = document.getElementById('badge-paused');
            if (btn) {
                if (isTradingPaused) {
                    btn.className = 'btn-resume';
                    btn.innerText = '▶️ Reprendre Trading';
                } else {
                    btn.className = 'btn-pause';
                    btn.innerText = '⏸️ Pausar Trading';
                }
            }
            if (badge) {
                badge.style.display = isTradingPaused ? 'inline-block' : 'none';
            }
        }

        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                if (!res.ok) {
                    document.getElementById('uptime').innerText = 'Servidor reiniciant (' + res.status + ')...';
                    return;
                }
                const data = await res.json();
                const m = data.metrics || {};

                if (m.trading_paused !== undefined) {
                    updatePauseUI(m.trading_paused);
                }
                
                const venueRaw = (m.venue2_name || 'AEVO').toUpperCase();
                let v2Name = 'Aevo DEX';
                let v2Short = 'Aevo';
                let subText = 'Hyperliquid DEX vs Aevo DEX • 100% Descentralitzat (DEX-to-DEX)';

                if (venueRaw === 'DYDX') {
                    v2Name = 'dYdX v4';
                    v2Short = 'dYdX';
                    subText = 'Hyperliquid DEX vs dYdX v4 • 100% Descentralitzat (DEX-to-DEX)';
                } else if (venueRaw === 'OKX') {
                    v2Name = 'OKX Perpetuals';
                    v2Short = 'OKX';
                    subText = 'Hyperliquid DEX vs OKX Perpetuals • Arbitratge Creuat 0% Risc';
                } else if (venueRaw === 'BINANCE') {
                    v2Name = 'Binance';
                    v2Short = 'BN';
                    subText = 'Hyperliquid DEX vs Binance Futures • 0% Risc Direccional';
                }

                document.title = `Arbitratge Delta-Neutral • Hyperliquid vs ${v2Name}`;
                const subEl = document.getElementById('header-sub');
                if (subEl) {
                    subEl.innerText = subText;
                }
                const thPx = document.getElementById('th-venue2-px');
                if (thPx) thPx.innerText = `Preu ${v2Name}`;
                const thSpread = document.getElementById('th-spread-label');
                if (thSpread) thSpread.innerText = `Spread % (HL vs ${v2Short})`;
                const thFund = document.getElementById('th-venue2-fund');
                if (thFund) thFund.innerText = `${v2Short} Fund (8h)`;
                const thLeg = document.getElementById('th-leg-v2');
                if (thLeg) thLeg.innerText = `Pota ${v2Name}`;

                const tagEl = document.getElementById('mode-tag');
                if (tagEl) {
                    const regTxt = m.regime_label ? ` • ${m.regime_label}` : '';
                    tagEl.innerText = `DELTA-NEUTRAL ARB (${m.leverage_str || '2x'})${regTxt}`;
                }

                const badgeMode = document.getElementById('badge-mode');
                if (badgeMode) {
                    if (m.execution_mode === 'live') {
                        badgeMode.innerText = 'DINERS REALS (LIVE)';
                        badgeMode.style.background = '#ef4444';
                        badgeMode.style.color = '#ffffff';
                    } else {
                        badgeMode.innerText = m.maker_first ? 'SIMULACIÓ (PAPER • MAKER-FIRST)' : 'SIMULACIÓ (PAPER TRADING)';
                        badgeMode.style.background = '#f59e0b';
                        badgeMode.style.color = '#1e293b';
                    }
                }

                // Mètriques principals
                const totalBal = (typeof m.balance === 'number') ? m.balance : 1000.0;
                document.getElementById('balance').innerText = totalBal.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' $';
                if (m.hl_balance !== undefined && m.bn_balance !== undefined) {
                    const szTxt = m.current_order_size ? ` • Ordre: ${m.current_order_size.toFixed(0)}$ (${m.size_pct || 30}% compost)` : '';
                    document.getElementById('balance-sub').innerText = `HL: ${m.hl_balance.toFixed(2)}$ | ${v2Short}: ${m.bn_balance.toFixed(2)}$${szTxt}`;
                }

                const pnlEl = document.getElementById('pnl');
                const pnl = (typeof m.net_pnl === 'number') ? m.net_pnl : 0.0;
                pnlEl.innerText = (pnl >= 0 ? '+' : '') + pnl.toFixed(3) + ' $';
                pnlEl.className = 'val ' + (pnl >= 0 ? 'green' : 'red');
                document.getElementById('pnl-sub').innerText = `Realitzat: ${(m.realized_pnl >= 0 ? '+' : '') + fmtNum(m.realized_pnl, 3)}$`;

                const funding = (typeof m.total_funding === 'number') ? m.total_funding : 0.0;
                document.getElementById('funding').innerText = (funding >= 0 ? '+' : '') + funding.toFixed(4) + ' $';

                document.getElementById('trades').innerText = `${m.total_trades || 0} (${m.wins || 0}W / ${m.losses || 0}L)`;
                document.getElementById('winrate-sub').innerText = `Winrate: ${fmtNum(m.winrate_pct, 1)}%`;

                // APR Net Carry (en comptes de Ritme Horari)
                const paceEl = document.getElementById('pace');
                const carryApr = (typeof m.net_carry_apr === 'number') ? m.net_carry_apr : 0.0;
                const paceVal = (typeof m.trades_per_hour === 'number') ? m.trades_per_hour : 0.0;
                if (paceEl) {
                    if (data.positions && data.positions.length > 0) {
                        paceEl.innerText = carryApr.toFixed(1) + '% APR';
                        if (carryApr >= 18.0) {
                            paceEl.className = 'val green';
                        } else if (carryApr >= 8.0) {
                            paceEl.className = 'val yellow';
                        } else {
                            paceEl.className = 'val';
                        }
                    } else {
                        paceEl.innerText = 'Sense posicions';
                        paceEl.className = 'val';
                    }
                }
                const paceSub = document.getElementById('pace-sub');
                if (paceSub) {
                    const dailyEst = (m.balance || 0) * carryApr / 100.0 / 365.0;
                    paceSub.innerText = `~${dailyEst.toFixed(2)}$/dia | ${paceVal.toFixed(1)} op/h`;
                }
                const paceSumm = document.getElementById('pace-summary');
                if (paceSumm) {
                    const aprColor = carryApr >= 18.0 ? '#10b981' : (carryApr >= 8.0 ? '#facc15' : '#94a3b8');
                    paceSumm.innerHTML = `APR Carry: <b style="color: ${aprColor};">${carryApr.toFixed(1)}%</b> (Objectiu: ≥18% APR)`;
                }

                document.getElementById('fees').innerText = fmtNum(m.total_fees, 4) + ' $';
                document.getElementById('uptime').innerText = 'Temps actiu: ' + (data.uptime || '-');

                // Actualització del Simulador i Gràfica Interactiva
                cachedStatusData = data;
                const paceActEl = document.getElementById('lbl-pace-act');
                if (paceActEl) paceActEl.innerText = paceVal > 0 ? paceVal.toFixed(1) : '4.0';
                try {
                    if (!chartInstance && typeof Chart !== 'undefined') {
                        initChart();
                    }
                    if (chartInstance) {
                        updateChartData();
                    }
                } catch (errChart) {
                    console.error("Error renderitzant gràfica:", errChart);
                }

                // Taula Spreads
                const minSpreadVal = (typeof m.min_spread === 'number') ? m.min_spread : 0.120;
                const minSpreadLabel = document.getElementById('min-spread-label');
                if (minSpreadLabel) {
                    const regDesc = m.maker_first ? 'Maker-First Optimitzat' : (m.is_weekend ? 'Mode Cap de Setmana' : 'Mode Setmanal');
                    minSpreadLabel.innerHTML = `Llindar mínim: <b style="color: #10b981;">±${minSpreadVal.toFixed(3)}%</b> (${regDesc})`;
                }

                const spreadsBody = document.getElementById('spreads-body');
                if (data.spreads && data.spreads.length > 0) {
                    spreadsBody.innerHTML = data.spreads.map(s => {
                        const spreadVal = s.spread_pct || 0.0;
                        const spreadColor = Math.abs(spreadVal) >= minSpreadVal ? 'green' : (Math.abs(spreadVal) >= (minSpreadVal * 0.7) ? 'yellow' : 'white');

                        let signalTag = '<span class="tag-neutral">NORMAL</span>';
                        if (s.signal_type === 'SIGNAL') {
                            const isBuy = (s.signal_status || '').includes('BUY HL');
                            signalTag = `<span class="tag-signal ${isBuy ? 'tag-buy' : 'tag-sell'}">${s.signal_status}</span>`;
                        } else if (s.signal_type === 'BLOCKED') {
                            signalTag = `<span class="tag-signal" style="background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid #f87171;">${s.signal_status}</span>`;
                        } else if (s.signal_type === 'APROP') {
                            signalTag = `<span class="tag-signal" style="background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid #38bdf8;">${s.signal_status}</span>`;
                        } else if (s.signal_type === 'HARVEST') {
                            signalTag = '<span class="tag-signal" style="background: rgba(250, 204, 21, 0.15); color: #facc15; border: 1px solid #facc15;">💰 HARVEST APR</span>';
                        } else if (s.signal_status) {
                            signalTag = `<span class="tag-neutral">${s.signal_status}</span>`;
                        }

                        const apr = s.annual_funding_diff_apr || 0;
                        const aprColor = apr > 10.0 ? 'green' : (apr < -10.0 ? 'red' : '');

                        return `<tr>
                            <td style="font-weight: bold; color: #facc15;">${s.coin}</td>
                            <td>${fmtPx(s.hl_price)} $</td>
                            <td>${fmtPx(s.bn_price)} $</td>
                            <td class="${spreadColor}" style="font-weight: bold;">${(spreadVal >= 0 ? '+' : '') + spreadVal.toFixed(4)}%</td>
                            <td>${(s.hl_funding_8h >= 0 ? '+' : '') + fmtNum(s.hl_funding_8h, 4)}%</td>
                            <td>${(s.bn_funding_8h >= 0 ? '+' : '') + fmtNum(s.bn_funding_8h, 4)}%</td>
                            <td class="${aprColor}" style="font-weight: bold;">${(apr >= 0 ? '+' : '') + apr.toFixed(1)}%</td>
                            <td>${signalTag}</td>
                        </tr>`;
                    }).join('');
                }

                // Taula Posicions Actives
                const posBody = document.getElementById('positions-body');
                const posCount = document.getElementById('pos-count');
                const maxPos = (data.metrics && data.metrics.max_positions) ? data.metrics.max_positions : 4;
                if (data.positions && data.positions.length > 0) {
                    posCount.innerText = `${data.positions.length} / ${maxPos} posicions`;
                    posBody.innerHTML = data.positions.map(p => {
                        const pnlVal = p.unrealized_pnl || 0.0;
                        const pnlColor = pnlVal >= 0 ? 'green' : 'red';
                        const hlSide = (p.leg_hl && p.leg_hl.side) ? p.leg_hl.side : '-';
                        const hlPx = (p.leg_hl && p.leg_hl.entry_price) ? p.leg_hl.entry_price.toFixed(2) : '-';
                        const hlSz = (p.leg_hl && p.leg_hl.size_usd) ? p.leg_hl.size_usd.toFixed(0) : '-';
                        const bnSide = (p.leg_bn && p.leg_bn.side) ? p.leg_bn.side : '-';
                        const bnPx = (p.leg_bn && p.leg_bn.entry_price) ? p.leg_bn.entry_price.toFixed(2) : '-';
                        const bnSz = (p.leg_bn && p.leg_bn.size_usd) ? p.leg_bn.size_usd.toFixed(0) : '-';

                        const carryBadge = (p.strategy_type === 'FUNDING_CARRY') ? ' <span style="font-size: 0.65rem; background: #8b5cf6; color: #fff; padding: 2px 5px; border-radius: 4px; vertical-align: middle;">CARRY</span>' : '';
                        const exitDiag = p.exit_diagnostic || 'Monitoritzant';
                        return `<tr>
                            <td style="font-weight: bold; color: #facc15;">${p.coin}${carryBadge}</td>
                            <td style="font-weight: bold; color: #38bdf8;">${p.direction}</td>
                            <td>${hlSide} @ ${hlPx} (${hlSz}$)</td>
                            <td>${bnSide} @ ${bnPx} (${bnSz}$)</td>
                            <td style="color: #10b981; font-weight: bold;">0.00 (Neutral)</td>
                            <td style="color: #c084fc;">${((p.accumulated_funding || 0) >= 0 ? '+' : '') + fmtNum(p.accumulated_funding, 4)}$</td>
                            <td class="${pnlColor}" style="font-weight: bold;">${(pnlVal >= 0 ? '+' : '') + pnlVal.toFixed(3)}$</td>
                            <td style="font-size: 0.8rem; color: #94a3b8;">${exitDiag}</td>
                        </tr>`;
                    }).join('');
                } else {
                    posCount.innerText = `0 / ${maxPos} posicions`;
                    posBody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: #64748b;">Sense posicions actives en curs</td></tr>';
                }

                // Taula Control i Rendiment per Criptomoneda
                const coinStatsBody = document.getElementById('coin-stats-body');
                if (coinStatsBody && data.coin_stats && data.coin_stats.length > 0) {
                    coinStatsBody.innerHTML = data.coin_stats.map(c => {
                        const pnlVal = c.realized_pnl || 0.0;
                        const pnlColor = pnlVal > 0 ? 'green' : (pnlVal < 0 ? 'red' : '');
                        const pnlSign = pnlVal >= 0 ? '+' : '';
                        const avgPnl = c.avg_pnl || 0.0;
                        const avgPnlSign = avgPnl >= 0 ? '+' : '';

                        let tagClass = 'tag-neutral';
                        let tagStyle = '';
                        if (c.diag_type === 'ACTIVE') {
                            tagClass = 'tag-buy';
                        } else if (c.diag_type === 'OPTIMAL') {
                            tagStyle = 'background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981;';
                        } else if (c.diag_type === 'GOOD') {
                            tagStyle = 'background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid #38bdf8;';
                        } else if (c.diag_type === 'PROTECTED') {
                            tagStyle = 'background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid #ef4444;';
                        } else if (c.diag_type === 'NEAR') {
                            tagStyle = 'background: rgba(250, 204, 21, 0.15); color: #facc15; border: 1px solid #eab308;';
                        } else if (c.diag_type === 'TIGHT') {
                            tagStyle = 'background: rgba(99, 102, 241, 0.15); color: #a5b4fc; border: 1px solid #6366f1;';
                        }

                        const durationStr = c.trades_count > 0 ? (c.avg_duration_min > 0 ? `${c.avg_duration_min.toFixed(1)} min` : `${c.avg_duration_sec.toFixed(0)}s`) : '-';
                        const paceColor = c.trades_per_hour >= 2.0 ? '#10b981' : (c.trades_per_hour > 0 ? '#38bdf8' : '#64748b');

                        return `<tr>
                            <td style="font-weight: bold; color: #facc15;">${c.coin}</td>
                            <td>${c.trades_count} (${c.wins}W / ${c.losses}L)</td>
                            <td style="font-weight: 600; color: ${paceColor};">${c.trades_per_hour.toFixed(1)} op/h</td>
                            <td style="color: ${c.winrate_pct >= 80 ? '#10b981' : (c.trades_count === 0 ? '#64748b' : '#facc15')}; font-weight: bold;">${c.trades_count > 0 ? c.winrate_pct.toFixed(0) + '%' : '-'}</td>
                            <td class="${pnlColor}" style="font-weight: bold;">${pnlSign}${fmtNum(pnlVal, 3)} $</td>
                            <td class="${pnlColor}">${c.trades_count > 0 ? avgPnlSign + fmtNum(avgPnl, 3) + ' $' : '-'}</td>
                            <td style="color: #94a3b8;">${durationStr}</td>
                            <td><span class="tag-signal ${tagClass}" style="${tagStyle}">${c.diagnostic}</span></td>
                        </tr>`;
                    }).join('');
                }

                // Taula Tancades
                const closedBody = document.getElementById('closed-body');
                if (data.recent_closed && data.recent_closed.length > 0) {
                    closedBody.innerHTML = data.recent_closed.slice().reverse().map(p => {
                        const pnlVal = p.realized_pnl || 0.0;
                        const pnlColor = pnlVal >= 0 ? 'green' : 'red';
                        const reasonColor = (p.exit_reason === 'CONVERGENCE_TARGET' || p.exit_reason === 'TAKE_PROFIT_TARGET' || p.exit_reason === 'TIME_BREAKEVEN' || p.exit_reason === 'TIME_QUICK_PROFIT') ? 'green' : (p.exit_reason && p.exit_reason.includes('STOP') ? 'red' : 'yellow');
                        return `<tr>
                            <td style="font-weight: bold; color: #facc15;">${p.coin}</td>
                            <td style="color: #38bdf8;">${p.direction}</td>
                            <td class="${reasonColor}" style="font-weight: bold;">${p.exit_reason}</td>
                            <td>${p.entry_spread_pct ? p.entry_spread_pct.toFixed(3) + '%' : '-'}</td>
                            <td style="color: #c084fc;">${((p.accumulated_funding || 0) >= 0 ? '+' : '') + fmtNum(p.accumulated_funding, 4)}$</td>
                            <td>${fmtNum(p.total_fees, 4)}$</td>
                            <td class="${pnlColor}" style="font-weight: bold;">${(pnlVal >= 0 ? '+' : '') + pnlVal.toFixed(3)}$</td>
                        </tr>`;
                    }).join('');
                }
            } catch (e) {
                console.error("Error actualitzant dashboard:", e);
                document.getElementById('uptime').innerText = 'Connexió en curs (reintentant)...';
            }
        }
        setInterval(fetchStatus, 1500);
        fetchStatus();
    </script>
</body>
</html>
"""

class WebDashboardServer:
    def __init__(self, exchange, start_time: float, port: int = 8080, app_ref=None):
        self.exchange = exchange
        self.start_time = start_time
        self.app_ref = app_ref
        self.port = int(os.environ.get("PORT", port))
        self.app = web.Application()
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/api/status", self.handle_status)
        self.app.router.add_get("/api/logs", self.handle_logs)
        self.app.router.add_get("/api/diag", self.handle_diag)
        self.app.router.add_get("/api/close_all", self.handle_close_all)
        self.app.router.add_post("/api/close_all", self.handle_close_all)
        self.app.router.add_get("/api/pause", self.handle_pause)
        self.app.router.add_post("/api/pause", self.handle_pause)
        self.app.router.add_get("/api/resume", self.handle_resume)
        self.app.router.add_get("/api/set_leverage", self.handle_set_leverage)
        self.app.router.add_post("/api/set_leverage", self.handle_set_leverage)
        self.app.router.add_get("/api/set_size", self.handle_set_size)
        self.app.router.add_post("/api/set_size", self.handle_set_size)
        self.app.router.add_get("/api/set_max_positions", self.handle_set_max_positions)
        self.app.router.add_post("/api/set_max_positions", self.handle_set_max_positions)
        self.runner = None

    async def handle_index(self, request):
        return web.Response(text=HTML_TEMPLATE, content_type="text/html")

    async def handle_status(self, request):
        elapsed = int(time.time() - self.start_time)
        mins, secs = divmod(elapsed, 60)
        hours, mins = divmod(mins, 60)
        uptime = f"{hours:02d}h {mins:02d}m {secs:02d}s"

        # Si l'aplicació té motor d'arbitratge
        spreads_data = []
        positions_data = []
        closed_data = []
        coin_stats_data = []
        projections_data = {}
        ny_session_data = {}
        equity_history_data = []

        if hasattr(self.app_ref, "get_dashboard_data"):
            data = self.app_ref.get_dashboard_data()
            spreads_data = data.get("spreads", [])
            positions_data = data.get("positions", [])
            closed_data = data.get("recent_closed", [])
            coin_stats_data = data.get("coin_stats", [])
            projections_data = data.get("projections", {})
            ny_session_data = data.get("ny_session", {})
            equity_history_data = data.get("equity_history", getattr(self.exchange, "equity_history", []))
            metrics = data.get("metrics", self.exchange.metrics)
        else:
            metrics = self.exchange.metrics
            equity_history_data = getattr(self.exchange, "equity_history", [])
            if hasattr(self.exchange, "active_positions"):
                positions_data = [
                    {
                        "coin": p.coin,
                        "direction": p.direction.value,
                        "entry_spread_pct": p.entry_spread_pct,
                        "current_spread_pct": p.current_spread_pct,
                        "unrealized_pnl": p.unrealized_pnl,
                        "accumulated_funding": p.accumulated_funding,
                        "leg_hl": {
                            "side": p.leg_hl.side.value,
                            "entry_price": p.leg_hl.entry_price,
                            "size_usd": p.leg_hl.size_usd,
                        },
                        "leg_bn": {
                            "side": p.leg_bn.side.value,
                            "entry_price": p.leg_bn.entry_price,
                            "size_usd": p.leg_bn.size_usd,
                        },
                        "strategy_type": getattr(p, "strategy_type", "SPREAD_SCALP"),
                        "current_net_apr": getattr(p, "current_net_apr", 0.0),
                        "exit_diagnostic": getattr(p, "exit_diagnostic", "Monitoritzant"),
                    }
                    for p in self.exchange.active_positions.values()
                ]
                closed_data = [
                    {
                        "coin": p.coin,
                        "direction": p.direction.value,
                        "exit_reason": p.exit_reason,
                        "entry_spread_pct": p.entry_spread_pct,
                        "accumulated_funding": p.accumulated_funding,
                        "total_fees": p.total_fees,
                        "realized_pnl": p.realized_pnl,
                        "strategy_type": getattr(p, "strategy_type", "SPREAD_SCALP"),
                    }
                    for p in self.exchange.closed_positions[-35:]
                ]

        # Calcular APR net ponderat de les posicions de carry actives
        net_carry_apr = 0.0
        if positions_data:
            total_notional = 0.0
            weighted_apr = 0.0
            for p in positions_data:
                notional = p.get("leg_hl", {}).get("size_usd", 0.0) + p.get("leg_bn", {}).get("size_usd", 0.0)
                apr = p.get("current_net_apr", 0.0)
                weighted_apr += apr * notional
                total_notional += notional
            if total_notional > 0:
                net_carry_apr = weighted_apr / total_notional
        metrics["net_carry_apr"] = round(net_carry_apr, 2)
        metrics["strategy_mode"] = getattr(self.app_ref, "strategy_mode", "arbitrage") if self.app_ref else "arbitrage"

        return web.json_response({
            "uptime": uptime,
            "metrics": metrics,
            "spreads": spreads_data,
            "positions": positions_data,
            "recent_closed": closed_data,
            "coin_stats": coin_stats_data,
            "projections": projections_data,
            "ny_session": ny_session_data,
            "equity_history": equity_history_data,
        })

    async def handle_logs(self, request):
        log_file = "trading_bot.log"
        lines = []
        n_lines = min(int(request.query.get("n", "200")), 5000)
        query = request.query.get("q", "").lower()
        if os.path.exists(log_file):
            try:
                with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                    all_lines = f.readlines()
                    filtered = [l for l in all_lines if "GET /api/" not in l]
                    if query:
                        filtered = [l for l in filtered if query in l.lower()]
                    lines = (filtered or all_lines)[-n_lines:]
            except Exception as e:
                lines = [f"Error llegint logs: {e}\n"]
        else:
            lines = ["Cap fitxer trading_bot.log trobat encara.\n"]
        return web.Response(text="".join(lines), content_type="text/plain")

    async def handle_diag(self, request):
        diag = {
            "timestamp": time.time(),
            "env_configured": {},
            "hyperliquid": {},
            "dydx": {},
            "live_exchange": {},
            "ready_for_live": False,
            "checklist": [],
        }

        wallet_addr = os.getenv("WALLET_ADDRESS", "").strip()
        hl_key = (os.getenv("HL_AGENT_PRIVATE_KEY") or os.getenv("PRIVATE_KEY") or "").strip()
        dydx_addr = os.getenv("DYDX_ADDRESS", "").strip()
        dydx_mnemonic = (os.getenv("DYDX_MNEMONIC") or "").strip()
        dydx_pk = (os.getenv("DYDX_PRIVATE_KEY") or os.getenv("DYDX_PRIVATE") or "").strip()
        okx_key = (os.getenv("OKX_API_KEY") or "").strip().strip('"').strip("'")
        okx_secret = (os.getenv("OKX_API_SECRET") or "").strip().strip('"').strip("'")
        okx_passphrase = (os.getenv("OKX_PASSPHRASE") or "").strip().strip('"').strip("'")
        okx_is_demo = os.getenv("OKX_IS_DEMO", "false").lower() in ("1", "true", "yes")
        exec_mode = os.getenv("EXECUTION_MODE", "paper").strip().lower()
        venue2 = os.getenv("VENUE2", "okx").strip().lower()

        # Resum de variables d'entorn (emmascarades per seguretat)
        diag["env_configured"] = {
            "execution_mode": exec_mode,
            "venue2": venue2,
            "has_wallet_address": bool(wallet_addr),
            "wallet_address_masked": f"{wallet_addr[:6]}...{wallet_addr[-4:]}" if len(wallet_addr) >= 10 else ("configured" if wallet_addr else "missing"),
            "has_hl_agent_key": bool(hl_key),
            "hl_agent_key_len": len(hl_key) if hl_key else 0,
            "has_okx_key": bool(okx_key),
            "okx_key_masked": f"{okx_key[:4]}...{okx_key[-4:]}" if len(okx_key) >= 8 else ("configured" if okx_key else "missing"),
            "okx_key_len": len(okx_key),
            "has_okx_secret": bool(okx_secret),
            "okx_secret_len": len(okx_secret),
            "has_okx_passphrase": bool(okx_passphrase),
            "okx_passphrase_len": len(okx_passphrase),
            "okx_passphrase_has_whitespace": any(c.isspace() for c in okx_passphrase),
            "okx_passphrase_is_alnum": okx_passphrase.isalnum(),
            "okx_passphrase_chars_info": f"first={okx_passphrase[:2]}, last={okx_passphrase[-2:]}" if len(okx_passphrase) >= 4 else "too_short",
            "okx_is_demo": okx_is_demo,
            "has_dydx_address": bool(dydx_addr),
            "dydx_address_masked": f"{dydx_addr[:8]}...{dydx_addr[-4:]}" if len(dydx_addr) >= 12 else ("configured" if dydx_addr else "missing"),
            "has_dydx_mnemonic": bool(dydx_mnemonic),
            "dydx_mnemonic_word_count": len(dydx_mnemonic.split()) if dydx_mnemonic else 0,
            "has_dydx_private_key": bool(dydx_pk),
            "size": os.getenv("SIZE"),
            "min_size": os.getenv("MIN_SIZE"),
            "max_size": os.getenv("MAX_SIZE"),
            "size_pct": os.getenv("SIZE_PCT"),
            "dynamic_size": os.getenv("DYNAMIC_SIZE"),
            "leverage": os.getenv("LEVERAGE"),
            "max_positions": os.getenv("MAX_POSITIONS"),
        }

        if self.app_ref:
            diag["runtime_app_config"] = {
                "size_usd": self.app_ref.size_usd,
                "dynamic_size": self.app_ref.dynamic_size,
                "size_pct": self.app_ref.size_pct,
                "min_size_usd": self.app_ref.min_size_usd,
                "max_size_usd": self.app_ref.max_size_usd,
                "max_positions": self.app_ref.max_positions,
                "leverage": self.app_ref.leverage,
                "calculated_order_size": self.app_ref.calculate_order_size(),
            }

        # 1. Prova de connexió amb Hyperliquid API
        if wallet_addr:
            try:
                import aiohttp
                timeout = aiohttp.ClientTimeout(total=8.0)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    url = "https://api.hyperliquid.xyz/info"
                    payload = {"type": "clearinghouseState", "user": wallet_addr}
                    spot_payload = {"type": "spotClearinghouseState", "user": wallet_addr}
                    account_val = 0.0
                    withdrawable = 0.0
                    open_pos_count = 0
                    spot_val = 0.0
                    
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            ms = data.get("marginSummary", {})
                            account_val = float(ms.get("accountValue", 0.0))
                            withdrawable = float(data.get("withdrawable", 0.0))
                            positions = data.get("assetPositions", [])
                            open_pos_count = sum(1 for p in positions if float(p.get("position", {}).get("szi", 0.0)) != 0.0)
                            open_pos_details = [
                                {
                                    "coin": p.get("position", {}).get("coin"),
                                    "szi": float(p.get("position", {}).get("szi", 0.0)),
                                    "entryPx": float(p.get("position", {}).get("entryPx", 0.0)),
                                    "unrealizedPnl": float(p.get("position", {}).get("unrealizedPnl", 0.0)),
                                }
                                for p in positions if float(p.get("position", {}).get("szi", 0.0)) != 0.0
                            ]

                    async with session.post(url, json=spot_payload) as resp_spot:
                        if resp_spot.status == 200:
                            data_spot = await resp_spot.json()
                            for b in data_spot.get("balances", []):
                                if b.get("coin") == "USDC":
                                    spot_val = float(b.get("total", 0.0))
                                    break

                    recent_fills = []
                    fills_payload = {"type": "userFills", "user": wallet_addr}
                    try:
                        async with session.post(url, json=fills_payload) as resp_fills:
                            if resp_fills.status == 200:
                                recent_fills = await resp_fills.json()
                    except Exception as fe:
                        logger.debug(f"Error consultant userFills: {fe}")

                    total_hl = account_val + spot_val
                    diag["hyperliquid"] = {
                        "status": "CONNECTED",
                        "account_value_usd": round(total_hl, 2),
                        "perps_margin_usd": round(account_val, 2),
                        "spot_usdc_usd": round(spot_val, 2),
                        "withdrawable_usd": round(withdrawable, 2),
                        "open_positions_count": open_pos_count,
                        "open_positions": open_pos_details,
                        "recent_fills_count": len(recent_fills),
                        "recent_fills": recent_fills[:10] if isinstance(recent_fills, list) else [],
                        "agent_key_configured": bool(hl_key),
                    }
            except Exception as e:
                diag["hyperliquid"] = {"status": "ERROR", "error": str(e)}
        else:
            diag["hyperliquid"] = {"status": "NOT_CONFIGURED", "message": "WALLET_ADDRESS no establerta a les variables d'entorn."}

        # 2. Prova de connexió amb Venue 2 (OKX o dYdX)
        diag["okx"] = {}
        if okx_key and okx_secret and okx_passphrase:
            try:
                from core.okx_live_client import OkxLiveClient
                probe_results = {}
                active_match = None
                matched_positions = []

                # Només eea.okx.com ja que s'ha confirmat com el domini regional d'aquest compte
                domain = "https://eea.okx.com"
                candidates = [
                    ("real_default", False, okx_passphrase),
                    ("demo_default", True, okx_passphrase),
                    ("real_lower", False, okx_passphrase.lower()),
                    ("real_upper", False, okx_passphrase.upper()),
                ]

                for tag, mode_demo, test_pass in candidates:
                    try:
                        client = OkxLiveClient(
                            api_key=okx_key,
                            api_secret=okx_secret,
                            passphrase=test_pass,
                            is_demo=mode_demo,
                            base_url=domain,
                        )
                        raw_res = await client._request("GET", "/api/v5/account/balance")
                        raw_fund = await client._request("GET", "/api/v5/asset/balances")
                        t_code = raw_res.get("code")
                        f_code = raw_fund.get("code")
                        probe_results[tag] = {
                            "trading_code": t_code,
                            "trading_msg": raw_res.get("msg"),
                            "funding_code": f_code,
                            "funding_msg": raw_fund.get("msg"),
                        }
                        if t_code == "0" or f_code == "0":
                            bal_calc = await client.get_account_balance()
                            fund_calc = await client.get_funding_balance()
                            pos_calc = await client.get_positions()
                            cfg_res = await client._request("GET", "/api/v5/account/config")
                            acct_cfg = cfg_res.get("data", [{}])[0] if cfg_res.get("code") == "0" else {}
                            
                            await client.init_contract_specs()
                            probe_tests = {
                                "all_positions_count": len(pos_calc),
                                "positions": pos_calc,
                                "symbol_map": client.symbol_map,
                                "contract_specs_count": len(client.contract_specs),
                            }
                            probe_ord = probe_tests

                            probe_results[tag]["trading_total"] = bal_calc.get("total", 0.0)
                            probe_results[tag]["funding_total"] = fund_calc.get("total_usd", 0.0)
                            probe_results[tag]["trading_currencies"] = bal_calc.get("currencies", {})
                            probe_results[tag]["funding_currencies"] = fund_calc.get("currencies", {})
                            probe_results[tag]["positions"] = pos_calc
                            probe_results[tag]["account_config"] = acct_cfg
                            probe_results[tag]["order_probe"] = probe_ord
                            if not active_match:
                                active_match = {
                                    "domain": domain,
                                    "tag": tag,
                                    "is_demo": mode_demo,
                                    "trading_total": bal_calc.get("total", 0.0),
                                    "funding_total": fund_calc.get("total_usd", 0.0),
                                    "trading_currencies": bal_calc.get("currencies", {}) or fund_calc.get("currencies", {}),
                                    "funding_currencies": fund_calc.get("currencies", {}),
                                    "trading_code": t_code,
                                    "account_config": acct_cfg,
                                    "order_probe": probe_ord,
                                }
                                matched_positions = pos_calc
                                break  # Trobat amb èxit!
                    except Exception as pe:
                        probe_results[tag] = {"error": str(pe)}

                diag["okx"] = {
                    "status": "CONNECTED" if active_match else "ERROR",
                    "api_response_code": active_match.get("trading_code", "50119") if active_match else "50119",
                    "api_response_msg": "OK" if active_match else "No match found across all OKX regional endpoints",
                    "total_equity_usd": round(active_match.get("trading_total", 0.0), 2) if active_match else 0.0,
                    "available_balance_usd": round(active_match.get("trading_total", 0.0), 2) if active_match else 0.0,
                    "funding_total_usd": round(active_match.get("funding_total", 0.0), 2) if active_match else 0.0,
                    "account_level": active_match.get("account_config", {}).get("acctLv", "N/A"),
                    "pos_mode": active_match.get("account_config", {}).get("posMode", "N/A"),
                    "account_config": active_match.get("account_config", {}),
                    "order_probe": active_match.get("order_probe", {}),
                    "trading_currencies": active_match.get("trading_currencies", {}) if active_match else {},
                    "funding_currencies": active_match.get("funding_currencies", {}) if active_match else {},
                    "open_positions_count": len(matched_positions),
                    "open_positions": matched_positions,
                    "is_demo": okx_is_demo,
                    "active_match": active_match,
                    "credentials_valid": bool(active_match),
                    "probe_matrix": probe_results,
                }
            except Exception as oe:
                diag["okx"] = {
                    "status": "ERROR",
                    "error": str(oe),
                    "credentials_valid": False,
                }
        else:
            diag["okx"] = {
                "status": "NOT_CONFIGURED",
                "message": "Falten OKX_API_KEY, OKX_API_SECRET o OKX_PASSPHRASE a les variables d'entorn.",
            }

        # dYdX v4 Indexer API (si dYdX està configurat)
        diag["dydx"] = {}
        if dydx_addr:
            try:
                import aiohttp
                timeout = aiohttp.ClientTimeout(total=8.0)
                headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "Accept": "application/json"}
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    url = f"https://indexer.dydx.trade/v4/addresses/{dydx_addr}/subaccountNumber/0"
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            sub = data.get("subaccount", {})
                            equity = float(sub.get("equity") or 0.0)
                            free_col = float(sub.get("freeCollateral") or 0.0)
                            open_pos = sub.get("openPerpetualPositions", {})
                            diag["dydx"] = {
                                "status": "CONNECTED",
                                "equity_usd": round(equity, 2),
                                "free_collateral_usd": round(free_col, 2),
                                "open_positions_count": len(open_pos),
                                "open_positions": open_pos,
                                "credentials_configured": bool(dydx_mnemonic or dydx_pk),
                            }
                        elif resp.status == 404:
                            diag["dydx"] = {
                                "status": "NOT_INITIALIZED_OR_EMPTY",
                                "message": f"Subcompte 0 no trobat a dYdX Indexer per a {dydx_addr}.",
                            }
                        else:
                            diag["dydx"] = {
                                "status": "ERROR",
                                "http_code": resp.status,
                                "error": await resp.text(),
                            }
            except Exception as e:
                diag["dydx"] = {"status": "ERROR", "error": str(e)}
        else:
            diag["dydx"] = {"status": "NOT_CONFIGURED", "message": "DYDX_ADDRESS no establerta a les variables d'entorn."}

        # 3. Diagnòstic de l'Exchange en Viu (si ja està en mode live)
        if hasattr(self.exchange, "hl_client") and hasattr(self.exchange, "venue2_client"):
            try:
                hl_state = await self.exchange.hl_client.get_account_state()
                v2_pos = await self.exchange.venue2_client.get_positions()
                hl_real_positions = [
                    p.get("position")
                    for p in hl_state.get("assetPositions", [])
                    if float(p.get("position", {}).get("szi", 0.0)) != 0.0
                ] if isinstance(hl_state, dict) else []
                diag["live_exchange"] = {
                    "is_live": True,
                    "active_internal_positions": len(getattr(self.exchange, "active_positions", {})),
                    "hl_real_open_positions": len(hl_real_positions),
                    "hl_open_positions_detail": hl_real_positions,
                    "venue2_real_open_positions": len(v2_pos) if isinstance(v2_pos, list) else 0,
                    "venue2_open_positions_detail": v2_pos if isinstance(v2_pos, list) else [],
                }
            except Exception as le:
                diag["live_exchange"] = {"is_live": True, "error": str(le)}
        else:
            diag["live_exchange"] = {"is_live": False, "note": "El bot està corrent en mode simulació (Paper)."}

        # 4. Checklist de Preparació per a Trading Real
        hl_ok = diag["hyperliquid"].get("status") == "CONNECTED" and bool(hl_key)
        has_hl_funds = diag["hyperliquid"].get("account_value_usd", 0.0) >= 10.0

        if venue2 == "okx":
            okx_connected = diag["okx"].get("status") == "CONNECTED"
            okx_eq = diag["okx"].get("total_equity_usd", 0.0)
            real_audit = diag["okx"].get("real_account_audit", {})
            real_trad = real_audit.get("real_trading_total", 0.0)
            real_fund = real_audit.get("real_funding_total", 0.0)

            if okx_eq >= 10.0:
                fons_text = f"✅ ${okx_eq} (Compte Actiu)"
                has_okx_funds = True
            elif real_trad >= 10.0:
                fons_text = f"✅ ${real_trad} al Compte Real de Trading (Desactiva OKX_IS_DEMO per usar-los)"
                has_okx_funds = True
            elif real_fund >= 10.0:
                fons_text = f"⚠️ ${real_fund} al Compte de Finançament/Funding (Cal moure a Trading dins d'OKX)"
                has_okx_funds = False
            else:
                fons_text = f"⚠️ Menys de 10$ USDT (${okx_eq})"
                has_okx_funds = False

            checklist = [
                f"Hyperliquid API: {'✅ CONNECTAT' if hl_ok else '❌ FALTA O ERROR'}",
                f"Hyperliquid Fons: {'✅ $' + str(diag['hyperliquid'].get('account_value_usd', 0)) if has_hl_funds else '⚠️ Menys de 10$ USDC'}",
                f"OKX Perpetuals API: {'✅ CONNECTAT' if okx_connected else '❌ FALTA O ERROR (Revisa OKX_API_KEY/SECRET/PASSPHRASE)'}",
                f"OKX Credencials: {'✅ VÀLIDES' if okx_connected else '❌ ERROR O NO CONFIGURADES'}",
                f"OKX Fons: {fons_text}",
                f"Mode d'Execució a Railway: {'⚡ LIVE (REAL)' if exec_mode == 'live' else '📄 PAPER (SIMULACIÓ)'}",
            ]
            ready_for_live = hl_ok and okx_connected and has_hl_funds and has_okx_funds
        else:
            dydx_ready = True
            dydx_ready_reason = "OK"
            if hasattr(self.exchange, "venue2_client") and hasattr(self.exchange.venue2_client, "is_ready_to_trade"):
                dydx_ready, dydx_ready_reason = self.exchange.venue2_client.is_ready_to_trade()
            elif dydx_addr and (dydx_mnemonic or dydx_pk):
                try:
                    from core.dydx_live_client import DydxLiveClient
                    temp_dydx = DydxLiveClient(address=dydx_addr, mnemonic=dydx_mnemonic, private_key=dydx_pk)
                    dydx_ready, dydx_ready_reason = temp_dydx.is_ready_to_trade()
                except Exception:
                    pass

            dydx_ok = diag["dydx"].get("status") == "CONNECTED" and bool(dydx_mnemonic or dydx_pk) and dydx_ready
            has_dydx_funds = diag["dydx"].get("free_collateral_usd", 0.0) >= 10.0

            checklist = [
                f"Hyperliquid API: {'✅ CONNECTAT' if hl_ok else '❌ FALTA O ERROR'}",
                f"Hyperliquid Fons: {'✅ $' + str(diag['hyperliquid'].get('account_value_usd', 0)) if has_hl_funds else '⚠️ Menys de 10$ USDC'}",
                f"dYdX v4 API: {'✅ CONNECTAT' if diag['dydx'].get('status') == 'CONNECTED' else '❌ FALTA O ERROR'}",
                f"dYdX v4 Credencials: {'✅ COHERENTS' if dydx_ready else '❌ ERROR CLAU (Cal DYDX_MNEMONIC de 24 paraules)'}",
                f"dYdX Fons: {'✅ $' + str(diag['dydx'].get('free_collateral_usd', 0)) if has_dydx_funds else '⚠️ Menys de 10$ USDC'}",
                f"Mode d'Execució a Railway: {'⚡ LIVE (REAL)' if exec_mode == 'live' else '📄 PAPER (SIMULACIÓ)'}",
            ]
            ready_for_live = hl_ok and dydx_ok and has_hl_funds and has_dydx_funds

        diag["checklist"] = checklist
        diag["ready_for_live"] = ready_for_live

        return web.json_response(diag)

    async def handle_close_all(self, request):
        if hasattr(self.exchange, "close_all_live_positions"):
            res = await self.exchange.close_all_live_positions()
            return web.json_response(res)
        return web.json_response({"status": "err", "error": "close_all_live_positions not available"})

    async def handle_pause(self, request):
        if hasattr(self.exchange, "trading_paused"):
            self.exchange.trading_paused = True
            return web.json_response({"status": "ok", "trading_paused": True})
        return web.json_response({"status": "err", "error": "trading_paused not available"})

    async def handle_resume(self, request):
        if hasattr(self.exchange, "trading_paused"):
            self.exchange.trading_paused = False
            return web.json_response({"status": "ok", "trading_paused": False})
        return web.json_response({"status": "err", "error": "trading_paused not available"})

    async def handle_set_leverage(self, request):
        lev_str = request.query.get("leverage", "3")
        try:
            lev = int(lev_str)
        except ValueError:
            lev = 3
        if hasattr(self.exchange, "configure_all_leverage"):
            res = await self.exchange.configure_all_leverage(leverage=lev)
            self.exchange.leverage = float(lev)
            if self.app_ref:
                self.app_ref.leverage = float(lev)
            return web.json_response(res)
        return web.json_response({"status": "err", "error": "configure_all_leverage not available"})

    async def handle_set_size(self, request):
        size_str = request.query.get("size")
        dynamic_str = request.query.get("dynamic")
        size_pct_str = request.query.get("size_pct")
        min_size_str = request.query.get("min_size")
        max_size_str = request.query.get("max_size")

        if self.app_ref:
            if size_str is not None:
                try:
                    sz = float(size_str)
                    self.app_ref.size_usd = sz
                    self.app_ref.min_size_usd = min(self.app_ref.min_size_usd, sz)
                    self.app_ref.max_size_usd = max(self.app_ref.max_size_usd, sz * 2.0)
                except ValueError:
                    pass
            if dynamic_str is not None:
                self.app_ref.dynamic_size = dynamic_str.lower() in ("true", "1", "yes")
            if size_pct_str is not None:
                try:
                    self.app_ref.size_pct = float(size_pct_str)
                except ValueError:
                    pass
            if min_size_str is not None:
                try:
                    self.app_ref.min_size_usd = float(min_size_str)
                except ValueError:
                    pass
            if max_size_str is not None:
                try:
                    self.app_ref.max_size_usd = float(max_size_str)
                except ValueError:
                    pass

            return web.json_response({
                "status": "ok",
                "size_usd": self.app_ref.size_usd,
                "dynamic_size": self.app_ref.dynamic_size,
                "size_pct": self.app_ref.size_pct,
                "min_size_usd": self.app_ref.min_size_usd,
                "max_size_usd": self.app_ref.max_size_usd,
                "calculated_order_size": self.app_ref.calculate_order_size(),
            })
        return web.json_response({"status": "err", "error": "app_ref not available"})

    async def handle_set_max_positions(self, request):
        max_pos_str = request.query.get("max_positions", "6")
        try:
            max_pos = int(max_pos_str)
            if self.app_ref:
                self.app_ref.max_positions = max_pos
            return web.json_response({"status": "ok", "max_positions": max_pos})
        except ValueError:
            return web.json_response({"status": "err", "error": "Invalid max_positions value"})

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", self.port)
        await site.start()
        print(f"[WEB DASHBOARD] Servidor web actiu al port {self.port} (http://0.0.0.0:{self.port})")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
