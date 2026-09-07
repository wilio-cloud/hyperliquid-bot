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
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>⚡ Arbitratge Delta-Neutral <span class="badge">EN VIU</span></h1>
                <div style="font-size: 0.8rem; color: #94a3b8; margin-top: 4px;" id="header-sub">Hyperliquid DEX vs Aevo DEX • 100% Descentralitzat (DEX-to-DEX)</div>
            </div>
            <div style="text-align: right;">
                <span class="badge-strategy" id="mode-tag">DELTA-NEUTRAL ARB (2x)</span>
                <div style="font-size: 0.8rem; color: #94a3b8; margin-top: 4px;" id="uptime">Carregant...</div>
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
                <div class="label">Ritme Horari</div>
                <div class="val" id="pace">-</div>
                <div class="sub" id="pace-sub">Objectiu: 4-6 op/h</div>
            </div>
            <div class="card">
                <div class="label">Comissions</div>
                <div class="val purple" id="fees">-</div>
                <div class="sub">Maker & Taker nets</div>
            </div>
        </div>

        <!-- Gràfica de Simulació d'Escenaris i Equitat en Viu -->
        <div class="card-chart">
            <div class="chart-header">
                <div>
                    <h2 style="font-size: 1.05rem; color: #38bdf8; display: flex; align-items: center; gap: 8px;">
                        📈 Trajectòria d'Equitat en Viu i Simulador de Creixement
                    </h2>
                    <div style="font-size: 0.78rem; color: #94a3b8; margin-top: 3px;">
                        Model compost a 25% de capital per ordre • Llindars reals ≥0.28% • Marge net: +0.22$ a +0.45$/trade (6 parells elit)
                    </div>
                </div>
                <div id="ny-session-badge" class="ny-badge ny-pre">
                    🔔 Pre-Market NY: Obertura en --m
                </div>
            </div>

            <!-- Controls d'Horitzó i Toggles de Corbes -->
            <div class="chart-controls">
                <div class="btn-group" id="horizon-selector">
                    <button class="btn-horizon active" onclick="setHorizon(24)">24 Hores (1D)</button>
                    <button class="btn-horizon" onclick="setHorizon(72)">3 Dies</button>
                    <button class="btn-horizon" onclick="setHorizon(168)">7 Dies (1S)</button>
                    <button class="btn-horizon" onclick="setHorizon(720)">30 Dies (1M)</button>
                </div>
                <div class="toggles-group">
                    <span class="toggle-chip chip-real" id="chip-real" onclick="toggleCurve('real')">🟣 Equitat Real</span>
                    <span class="toggle-chip chip-actual" id="chip-actual" onclick="toggleCurve('actual')">🟢 Ritme Actual (<span id="lbl-pace-act">4.0</span> op/h)</span>
                    <span class="toggle-chip chip-target" id="chip-target" onclick="toggleCurve('target')">🔵 Objectiu (4.5 op/h)</span>
                    <span class="toggle-chip chip-cons" id="chip-cons" onclick="toggleCurve('conservative')">🟡 Conservador (2.5 op/h)</span>
                    <span class="toggle-chip chip-ny" id="chip-ny" onclick="toggleCurve('ny')">🟠 Volatilitat NY (7.5 op/h)</span>
                    <span class="toggle-chip chip-custom" id="chip-custom" onclick="toggleCurve('custom')">⚪ Slider Personalitzat</span>
                </div>
            </div>

            <!-- Slider Interactiu de Ritme -->
            <div class="slider-container">
                <span style="font-weight: 600; white-space: nowrap;">⚡ Simular Ritme:</span>
                <input type="range" id="sim-slider" min="0.5" max="15" step="0.5" value="4.0" oninput="onSliderInput(this.value)">
                <span id="slider-label" style="font-weight: bold; color: #38bdf8; min-width: 70px;">4.0 op/h</span>
                <span id="slider-gain-est" style="font-weight: 600; color: #10b981; margin-left: auto;">...</span>
            </div>

            <!-- Canvas de Chart.js -->
            <div style="position: relative; height: 320px; width: 100%;">
                <canvas id="growthChart"></canvas>
            </div>

            <!-- Mini KPIs de Projecció -->
            <div class="kpi-row">
                <div class="kpi-box">
                    <div class="kpi-lbl">Balanç & PnL Real</div>
                    <div class="kpi-val purple" id="kpi-real-bal">1,001.05 $</div>
                    <div class="kpi-sub" id="kpi-real-sub">+1.05$ (100% winrate)</div>
                </div>
                <div class="kpi-box">
                    <div class="kpi-lbl">Projecció 24 Hores</div>
                    <div class="kpi-val green" id="kpi-24h-val">~1,023 $</div>
                    <div class="kpi-sub" id="kpi-24h-sub">+2.2% ritme mesurat</div>
                </div>
                <div class="kpi-box">
                    <div class="kpi-lbl">Projecció 7 Dies</div>
                    <div class="kpi-val green" id="kpi-7d-val">~1,171 $</div>
                    <div class="kpi-sub" id="kpi-7d-sub">+17.0% compost</div>
                </div>
                <div class="kpi-box">
                    <div class="kpi-lbl">Projecció 30 Dies</div>
                    <div class="kpi-val green" id="kpi-30d-val">~1,960 $</div>
                    <div class="kpi-sub" id="kpi-30d-sub">+95.9% compost mensual</div>
                </div>
                <div class="kpi-box">
                    <div class="kpi-lbl">Sessió Wall St (NY)</div>
                    <div class="kpi-val" style="color: #fb923c;" id="kpi-ny-val">Pre-Market</div>
                    <div class="kpi-sub" id="kpi-ny-sub">Factor x1.40 oportunitats</div>
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
                    </tr>
                </thead>
                <tbody id="positions-body">
                    <tr><td colspan="7" style="text-align: center; color: #64748b;">Sense posicions d'arbitratge actives actualment</td></tr>
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
        let currentHorizon = 24; // 24, 72, 168, 720 hores
        let customSliderPace = 4.0;
        let curveVisibility = {
            real: true,
            actual: true,
            target: true,
            conservative: true,
            ny: true,
            custom: true
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
                            label: 'Equitat Real (En Viu)',
                            data: [],
                            borderColor: '#c084fc',
                            backgroundColor: 'rgba(192, 132, 252, 0.08)',
                            fill: true,
                            borderWidth: 3,
                            pointRadius: 3,
                            pointHoverRadius: 5,
                            tension: 0.1,
                            spanGaps: false
                        },
                        {
                            label: 'Ritme Actual Mesurat',
                            data: [],
                            borderColor: '#10b981',
                            borderWidth: 2.5,
                            pointRadius: 2,
                            tension: 0.2,
                            spanGaps: false
                        },
                        {
                            label: 'Objectiu (4.5 op/h • +0.25$)',
                            data: [],
                            borderColor: '#38bdf8',
                            borderWidth: 2,
                            borderDash: [5, 5],
                            pointRadius: 0,
                            tension: 0.2,
                            spanGaps: false
                        },
                        {
                            label: 'Conservador (2.5 op/h • +0.20$)',
                            data: [],
                            borderColor: '#facc15',
                            borderWidth: 2,
                            borderDash: [4, 4],
                            pointRadius: 0,
                            tension: 0.2,
                            spanGaps: false
                        },
                        {
                            label: 'Volatilitat NY (7.5 op/h • +0.30$)',
                            data: [],
                            borderColor: '#f97316',
                            borderWidth: 2,
                            borderDash: [3, 3],
                            pointRadius: 0,
                            tension: 0.2,
                            spanGaps: false
                        },
                        {
                            label: 'Simulació Slider',
                            data: [],
                            borderColor: '#ffffff',
                            borderWidth: 2,
                            pointRadius: 2,
                            tension: 0.2,
                            spanGaps: false
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
                                    let l = context.dataset.label || '';
                                    if (context.parsed.y !== null && context.parsed.y !== undefined) {
                                        return ` ${l}: ${context.parsed.y.toFixed(2)} $`;
                                    }
                                    return null;
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            grid: { color: 'rgba(255, 255, 255, 0.04)' },
                            ticks: { color: '#94a3b8', font: { size: 11 } }
                        },
                        y: {
                            grid: { color: 'rgba(255, 255, 255, 0.04)' },
                            ticks: {
                                color: '#94a3b8',
                                font: { size: 11 },
                                callback: function(v) { return v.toFixed(0) + ' $'; }
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
            const curBal = (typeof m.balance === 'number') ? m.balance : 1000.0;
            const initialBal = (typeof m.initial_balance === 'number' && m.initial_balance > 0) ? m.initial_balance : curBal;
            const curPace = (typeof m.trades_per_hour === 'number' && m.trades_per_hour > 0) ? m.trades_per_hour : 4.0;
            const avgProfit = (data.projections && data.projections.avg_pnl_per_trade && data.projections.avg_pnl_per_trade > 0) ? data.projections.avg_pnl_per_trade : 0.25;

            const profitPerTrade = avgProfit;
            const ratePerTrade = profitPerTrade / curBal;

            // 1. Punts històrics reals (evitem la caiguda fictícia des de 1000$)
            const hist = data.equity_history || [];
            const recentHist = hist.length > 8 ? hist.slice(-8) : hist;
            
            let labels = [];
            let realData = [];

            if (recentHist.length > 0) {
                recentHist.forEach((pt, idx) => {
                    const isLast = idx === recentHist.length - 1;
                    const lbl = isLast ? 'Ara' : (pt.trades ? `T${pt.trades}` : `H${idx}`);
                    labels.push(lbl);
                    realData.push(pt.equity);
                });
            } else {
                labels.push('Inici', 'Ara');
                realData.push(initialBal, curBal);
            }

            const junctionIdx = labels.length - 1;

            // 2. Passos temporals futurs segons horitzó
            let stepHours = 2;
            if (currentHorizon === 72) stepHours = 6;
            else if (currentHorizon === 168) stepHours = 12;
            else if (currentHorizon === 720) stepHours = 48;

            const numSteps = Math.round(currentHorizon / stepHours);
            let futureHours = [];
            for (let i = 1; i <= numSteps; i++) {
                const h = i * stepHours;
                futureHours.push(h);
                if (currentHorizon <= 24) {
                    labels.push(`+${h}h`);
                } else if (currentHorizon <= 72) {
                    labels.push(`+${h}h`);
                } else if (currentHorizon <= 168) {
                    const d = (h / 24).toFixed(1);
                    labels.push(`+${d}d`);
                } else {
                    const d = Math.round(h / 24);
                    labels.push(`+${d}d`);
                }
                realData.push(null);
            }

            // Funció per calcular la corba composta amb paràmetres específics per escenari
            function calcCurve(pace, tradePnl) {
                let arr = new Array(junctionIdx).fill(null);
                arr.push(curBal); // unió exacta a 'Ara'
                const pft = (typeof tradePnl === 'number') ? tradePnl : profitPerTrade;
                const rate = pft / curBal;
                for (let i = 0; i < futureHours.length; i++) {
                    const h = futureHours[i];
                    const numTrades = pace * h;
                    const bal = curBal * Math.pow(1.0 + rate, numTrades);
                    arr.push(bal);
                }
                return arr;
            }

            const actualCurve = calcCurve(curPace, profitPerTrade);
            const targetCurve = calcCurve(4.5, 0.25);
            const consCurve = calcCurve(2.5, 0.20);
            const nyCurve = calcCurve(7.5, 0.30);
            const customCurve = calcCurve(customSliderPace, profitPerTrade);

            chartInstance.data.labels = labels;
            chartInstance.data.datasets[0].data = realData;
            chartInstance.data.datasets[1].data = actualCurve;
            chartInstance.data.datasets[2].data = targetCurve;
            chartInstance.data.datasets[3].data = consCurve;
            chartInstance.data.datasets[4].data = nyCurve;
            chartInstance.data.datasets[5].data = customCurve;

            // Visibilitat de datasets
            chartInstance.data.datasets[0].hidden = !curveVisibility.real;
            chartInstance.data.datasets[1].hidden = !curveVisibility.actual;
            chartInstance.data.datasets[2].hidden = !curveVisibility.target;
            chartInstance.data.datasets[3].hidden = !curveVisibility.conservative;
            chartInstance.data.datasets[4].hidden = !curveVisibility.ny;
            chartInstance.data.datasets[5].hidden = !curveVisibility.custom;

            chartInstance.update('none');

            // Actualització badge slider
            const sliderFinalBal = customCurve[customCurve.length - 1];
            const sliderGain = sliderFinalBal - curBal;
            const sliderGainPct = (sliderGain / curBal) * 100;
            const horizonLbl = currentHorizon === 24 ? '24h' : (currentHorizon === 72 ? '3 dies' : (currentHorizon === 168 ? '7 dies' : '30 dies'));
            const sliderGainEl = document.getElementById('slider-gain-est');
            if (sliderGainEl) {
                sliderGainEl.innerHTML = `Est. a ${horizonLbl}: <b style="color: #34d399;">${sliderFinalBal.toFixed(1)}$ (+${sliderGainPct.toFixed(1)}%)</b> <span style="color: #94a3b8; font-size: 0.78rem;">(~${(customSliderPace * profitPerTrade).toFixed(2)}$/h net)</span>`;
            }

            // Actualització KPIs de simulació
            const kpiRealBal = document.getElementById('kpi-real-bal');
            if (kpiRealBal) kpiRealBal.innerText = `${curBal.toFixed(2)} $`;
            const kpiRealSub = document.getElementById('kpi-real-sub');
            if (kpiRealSub) {
                const totalTrades = m.total_trades || 0;
                if (totalTrades === 0) {
                    kpiRealSub.innerText = `${(m.realized_pnl || 0) >= 0 ? '+' : ''}${(m.realized_pnl || 0).toFixed(2)}$ (Monitoritzant en viu)`;
                } else {
                    kpiRealSub.innerText = `${(m.realized_pnl || 0) >= 0 ? '+' : ''}${(m.realized_pnl || 0).toFixed(2)}$ (${fmtNum(m.winrate_pct, 0)}% winrate)`;
                }
            }

            const est24 = curBal * Math.pow(1.0 + ratePerTrade, curPace * 24);
            const kpi24 = document.getElementById('kpi-24h-val');
            if (kpi24) kpi24.innerText = `~${est24.toFixed(1)} $`;
            const kpi24Sub = document.getElementById('kpi-24h-sub');
            if (kpi24Sub) kpi24Sub.innerText = `+${((est24 - curBal) / curBal * 100).toFixed(1)}% (${curPace.toFixed(1)} op/h • ~${profitPerTrade.toFixed(2)}$)`;

            const est7d = curBal * Math.pow(1.0 + ratePerTrade, curPace * 168);
            const kpi7d = document.getElementById('kpi-7d-val');
            if (kpi7d) kpi7d.innerText = `~${est7d.toFixed(1)} $`;
            const kpi7dSub = document.getElementById('kpi-7d-sub');
            if (kpi7dSub) kpi7dSub.innerText = `+${((est7d - curBal) / curBal * 100).toFixed(1)}% compost (7 dies)`;

            const est30d = curBal * Math.pow(1.0 + ratePerTrade, curPace * 720);
            const kpi30d = document.getElementById('kpi-30d-val');
            if (kpi30d) kpi30d.innerText = `~${est30d.toFixed(0)} $`;
            const kpi30dSub = document.getElementById('kpi-30d-sub');
            if (kpi30dSub) kpi30dSub.innerText = `+${((est30d - curBal) / curBal * 100).toFixed(0)}% compost mensual`;

            // Estat Sessió NY
            const ny = data.ny_session || {};
            const nyBadge = document.getElementById('ny-session-badge');
            const nyKpiVal = document.getElementById('kpi-ny-val');
            const nyKpiSub = document.getElementById('kpi-ny-sub');
            if (nyBadge) {
                if (ny.is_open) {
                    nyBadge.className = 'ny-badge ny-open';
                    nyBadge.innerText = '🟢 Sessió NY Oberta (Màxima Volatilitat)';
                    if (nyKpiVal) { nyKpiVal.innerText = 'Oberta 🟢'; nyKpiVal.className = 'kpi-val green'; }
                    if (nyKpiSub) nyKpiSub.innerText = 'Spreads màxims en curs';
                } else if (ny.is_premarket) {
                    nyBadge.className = 'ny-badge ny-pre';
                    nyBadge.innerText = `🔔 Pre-Market NY (${ny.status_text || 'Obertura propera'})`;
                    if (nyKpiVal) { nyKpiVal.innerText = `${ny.minutes_to_open || 30} min`; nyKpiVal.className = 'kpi-val yellow'; }
                    if (nyKpiSub) nyKpiSub.innerText = 'Obertura Wall St 15:30 CET';
                } else {
                    nyBadge.className = 'ny-badge ny-closed';
                    nyBadge.innerText = '🌙 Sessió NY Tancada';
                    if (nyKpiVal) { nyKpiVal.innerText = 'Tancada'; nyKpiVal.className = 'kpi-val'; }
                    if (nyKpiSub) nyKpiSub.innerText = 'Règim normal DEX-to-DEX';
                }
            }
        }

        function setHorizon(h) {
            currentHorizon = h;
            const btns = document.querySelectorAll('.btn-horizon');
            btns.forEach(b => b.classList.remove('active'));
            if (h === 24 && btns[0]) btns[0].classList.add('active');
            else if (h === 72 && btns[1]) btns[1].classList.add('active');
            else if (h === 168 && btns[2]) btns[2].classList.add('active');
            else if (h === 720 && btns[3]) btns[3].classList.add('active');
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

        function onSliderInput(val) {
            customSliderPace = parseFloat(val);
            const lbl = document.getElementById('slider-label');
            if (lbl) lbl.innerText = `${customSliderPace.toFixed(1)} op/h`;
            updateChartData();
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
                
                const venueRaw = (m.venue2_name || 'AEVO').toUpperCase();
                let v2Name = 'Aevo DEX';
                let v2Short = 'Aevo';
                let subText = 'Hyperliquid DEX vs Aevo DEX • 100% Descentralitzat (DEX-to-DEX)';

                if (venueRaw === 'DYDX') {
                    v2Name = 'dYdX v4';
                    v2Short = 'dYdX';
                    subText = 'Hyperliquid DEX vs dYdX v4 • 100% Descentralitzat (DEX-to-DEX)';
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

                // Ritme horari (Trades/h)
                const paceEl = document.getElementById('pace');
                const paceVal = (typeof m.trades_per_hour === 'number') ? m.trades_per_hour : 0.0;
                if (paceEl) {
                    paceEl.innerText = paceVal.toFixed(1) + ' op/h';
                    if (paceVal >= 8.0) {
                        paceEl.className = 'val green';
                    } else if (paceVal >= 5.0) {
                        paceEl.className = 'val yellow';
                    } else {
                        paceEl.className = 'val';
                    }
                }
                const paceSub = document.getElementById('pace-sub');
                if (paceSub) {
                    paceSub.innerText = `Objectiu: ${m.target_trades_per_hour || '4-6'} op/h`;
                }
                const paceSumm = document.getElementById('pace-summary');
                if (paceSumm) {
                    const paceColor = paceVal >= 4.0 ? '#10b981' : (paceVal >= 2.0 ? '#facc15' : '#94a3b8');
                    paceSumm.innerHTML = `Ritme actual: <b style="color: ${paceColor};">${paceVal.toFixed(1)} op/h</b> (Objectiu: ${m.target_trades_per_hour || '4-6'} op/h)`;
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
                    const regDesc = m.is_weekend ? 'Mode Cap de Setmana' : 'Mode Setmanal';
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

                        return `<tr>
                            <td style="font-weight: bold; color: #facc15;">${p.coin}</td>
                            <td style="font-weight: bold; color: #38bdf8;">${p.direction}</td>
                            <td>${hlSide} @ ${hlPx} (${hlSz}$)</td>
                            <td>${bnSide} @ ${bnPx} (${bnSz}$)</td>
                            <td style="color: #10b981; font-weight: bold;">0.00 (Neutral)</td>
                            <td style="color: #c084fc;">${((p.accumulated_funding || 0) >= 0 ? '+' : '') + fmtNum(p.accumulated_funding, 4)}$</td>
                            <td class="${pnlColor}" style="font-weight: bold;">${(pnlVal >= 0 ? '+' : '') + pnlVal.toFixed(3)}$</td>
                        </tr>`;
                    }).join('');
                } else {
                    posCount.innerText = `0 / ${maxPos} posicions`;
                    posBody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: #64748b;">Sense posicions actives en curs</td></tr>';
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
        self.app.router.add_get("/api/set_leverage", self.handle_set_leverage)
        self.app.router.add_post("/api/set_leverage", self.handle_set_leverage)
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
                    }
                    for p in self.exchange.closed_positions[-35:]
                ]

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
        if os.path.exists(log_file):
            try:
                with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                    all_lines = f.readlines()
                    lines = all_lines[-150:]
            except Exception as e:
                lines = [f"Error llegint logs: {e}\n"]
        else:
            lines = ["Cap fitxer trading_bot.log trobat encara.\n"]
        return web.Response(text="".join(lines), content_type="text/plain")

    async def handle_diag(self, request):
        diag = {}
        try:
            if hasattr(self.exchange, "aevo_client"):
                diag["aevo_account"] = await self.exchange.aevo_client.get_account()
                diag["aevo_portfolio"] = await self.exchange.aevo_client.get_account_state()
                diag["aevo_positions"] = await self.exchange.aevo_client.get_positions()
                diag["aevo_orders"] = await self.exchange.aevo_client.get_open_orders()
            if hasattr(self.exchange, "hl_client"):
                diag["hl_account"] = await self.exchange.hl_client.get_account_state()
                diag["hl_orders"] = await self.exchange.hl_client.get_open_orders()
        except Exception as e:
            diag["error"] = str(e)
        return web.json_response(diag)

    async def handle_close_all(self, request):
        if hasattr(self.exchange, "close_all_live_positions"):
            res = await self.exchange.close_all_live_positions()
            return web.json_response(res)
        return web.json_response({"status": "err", "error": "close_all_live_positions not available"})

    async def handle_set_leverage(self, request):
        lev_str = request.query.get("leverage", "2")
        try:
            lev = int(lev_str)
        except ValueError:
            lev = 2
        if hasattr(self.exchange, "configure_all_leverage"):
            res = await self.exchange.configure_all_leverage(leverage=lev)
            return web.json_response(res)
        return web.json_response({"status": "err", "error": "configure_all_leverage not available"})

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", self.port)
        await site.start()
        print(f"[WEB DASHBOARD] Servidor web actiu al port {self.port} (http://0.0.0.0:{self.port})")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
