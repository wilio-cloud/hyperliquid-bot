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
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>⚡ Arbitratge Delta-Neutral <span class="badge">EN VIU</span></h1>
                <div style="font-size: 0.8rem; color: #94a3b8; margin-top: 4px;" id="header-sub">Hyperliquid DEX vs dYdX v4 • 100% Descentralitzat (DEX-to-DEX)</div>
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
                <div class="label">Comissions</div>
                <div class="val purple" id="fees">-</div>
                <div class="sub">Maker & Taker nets</div>
            </div>
        </div>

        <!-- Taula 1: Monitor de Spreads en Viu -->
        <div class="card-table">
            <div class="table-title">
                <span>📊 Monitor de Spreads i Funding en Temps Real</span>
                <span style="font-size: 0.8rem; font-weight: normal; color: #94a3b8;">Llindar mínim: <b style="color: #10b981;">±0.180%</b> (Marge Net Garantit)</span>
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

        <!-- Taula 3: Darreres Operacions Tancades -->
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

        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                if (!res.ok) {
                    document.getElementById('uptime').innerText = 'Servidor reiniciant (' + res.status + ')...';
                    return;
                }
                const data = await res.json();
                const m = data.metrics || {};
                
                const isDydx = (m.venue2_name || 'DYDX').toUpperCase() === 'DYDX';
                const v2Name = isDydx ? 'dYdX v4' : 'Binance';
                const v2Short = isDydx ? 'dYdX' : 'BN';

                document.title = `Arbitratge Delta-Neutral • Hyperliquid vs ${v2Name}`;
                const subEl = document.getElementById('header-sub');
                if (subEl) {
                    subEl.innerText = isDydx
                        ? 'Hyperliquid DEX vs dYdX v4 • 100% Descentralitzat (DEX-to-DEX)'
                        : 'Hyperliquid DEX vs Binance Futures • 0% Risc Direccional';
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
                if (tagEl) tagEl.innerText = `DELTA-NEUTRAL ARB (${m.leverage_str || '2x'})`;

                // Mètriques principals
                const totalBal = (typeof m.balance === 'number') ? m.balance : 1000.0;
                document.getElementById('balance').innerText = totalBal.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' $';
                if (m.hl_balance !== undefined && m.bn_balance !== undefined) {
                    document.getElementById('balance-sub').innerText = `HL: ${m.hl_balance.toFixed(2)}$ | ${v2Short}: ${m.bn_balance.toFixed(2)}$`;
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
                document.getElementById('fees').innerText = fmtNum(m.total_fees, 4) + ' $';
                document.getElementById('uptime').innerText = 'Temps actiu: ' + (data.uptime || '-');

                // Taula Spreads
                const spreadsBody = document.getElementById('spreads-body');
                if (data.spreads && data.spreads.length > 0) {
                    spreadsBody.innerHTML = data.spreads.map(s => {
                        const spreadVal = s.spread_pct || 0.0;
                        const spreadColor = Math.abs(spreadVal) >= 0.180 ? 'green' : (Math.abs(spreadVal) >= 0.120 ? 'yellow' : 'white');
                        let signalTag = '<span class="tag-neutral">NORMAL</span>';
                        if (spreadVal >= 0.180) {
                            signalTag = `<span class="tag-signal tag-buy">🔥 SELL HL / BUY ${v2Short}</span>`;
                        } else if (spreadVal <= -0.180) {
                            signalTag = `<span class="tag-signal tag-sell">🔥 BUY HL / SELL ${v2Short}</span>`;
                        } else if (Math.abs(spreadVal) >= 0.120) {
                            signalTag = '<span class="tag-signal" style="background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid #38bdf8;">⏳ APROP (' + Math.abs(spreadVal).toFixed(3) + '%)</span>';
                        } else if (Math.abs(s.annual_funding_diff_apr || 0) >= 15.0) {
                            signalTag = '<span class="tag-signal" style="background: rgba(250, 204, 21, 0.15); color: #facc15; border: 1px solid #facc15;">💰 HARVEST APR</span>';
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
                if (data.positions && data.positions.length > 0) {
                    posCount.innerText = `${data.positions.length} posició(ns)`;
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
                    posCount.innerText = "0 posicions";
                    posBody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: #64748b;">Sense posicions actives en curs</td></tr>';
                }

                // Taula Tancades
                const closedBody = document.getElementById('closed-body');
                if (data.recent_closed && data.recent_closed.length > 0) {
                    closedBody.innerHTML = data.recent_closed.slice().reverse().map(p => {
                        const pnlVal = p.realized_pnl || 0.0;
                        const pnlColor = pnlVal >= 0 ? 'green' : 'red';
                        const reasonColor = (p.exit_reason === 'CONVERGENCE_TARGET' || p.exit_reason === 'TAKE_PROFIT_TARGET') ? 'green' : 'yellow';
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

        if hasattr(self.app_ref, "get_dashboard_data"):
            data = self.app_ref.get_dashboard_data()
            spreads_data = data.get("spreads", [])
            positions_data = data.get("positions", [])
            closed_data = data.get("recent_closed", [])
            metrics = data.get("metrics", self.exchange.metrics)
        else:
            metrics = self.exchange.metrics
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
                    for p in self.exchange.closed_positions[-15:]
                ]

        return web.json_response({
            "uptime": uptime,
            "metrics": metrics,
            "spreads": spreads_data,
            "positions": positions_data,
            "recent_closed": closed_data,
        })

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", self.port)
        await site.start()
        print(f"[WEB DASHBOARD] Servidor web actiu al port {self.port} (http://0.0.0.0:{self.port})")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
