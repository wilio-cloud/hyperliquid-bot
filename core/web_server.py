"""Servidor web lleuger per monitoritzar el bot des del mòbil o navegador al núvol."""

import os
import time
from aiohttp import web
from core.paper_exchange import PaperExchange

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ca">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hyperliquid Bot Dashboard</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
        body { background: #0f172a; color: #f8fafc; padding: 16px; }
        .container { max-width: 900px; margin: 0 auto; }
        header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; border-bottom: 1px solid #334155; padding-bottom: 12px; }
        h1 { font-size: 1.25rem; color: #38bdf8; display: flex; align-items: center; gap: 8px; }
        .badge { background: #10b981; color: #fff; font-size: 0.75rem; padding: 3px 8px; border-radius: 999px; font-weight: bold; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; margin-bottom: 20px; }
        .card { background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 12px; }
        .card .label { font-size: 0.75rem; color: #94a3b8; margin-bottom: 4px; }
        .card .val { font-size: 1.2rem; font-weight: bold; }
        .green { color: #10b981; }
        .red { color: #ef4444; }
        .card-table { background: #1e293b; border: 1px solid #334155; border-radius: 8px; margin-bottom: 20px; overflow-x: auto; }
        .table-title { padding: 12px 16px; font-weight: bold; font-size: 0.95rem; border-bottom: 1px solid #334155; color: #cbd5e1; }
        table { width: 100%; border-collapse: collapse; font-size: 0.85rem; text-align: left; }
        th, td { padding: 10px 14px; border-bottom: 1px solid #334155; }
        th { color: #94a3b8; font-weight: 600; }
        tr:last-child td { border-bottom: none; }
        .footer { text-align: center; font-size: 0.75rem; color: #64748b; margin-top: 20px; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>⚡ Hyperliquid Scalper <span class="badge">EN VIU</span></h1>
            <div style="font-size: 0.8rem; color: #94a3b8;" id="uptime">Carregant...</div>
        </header>

        <div class="grid">
            <div class="card">
                <div class="label">Balanç</div>
                <div class="val" id="balance">-</div>
            </div>
            <div class="card">
                <div class="label">PnL Net</div>
                <div class="val" id="pnl">-</div>
            </div>
            <div class="card">
                <div class="label">Winrate</div>
                <div class="val green" id="winrate">-</div>
            </div>
            <div class="card">
                <div class="label">Operacions</div>
                <div class="val" id="trades">-</div>
            </div>
            <div class="card">
                <div class="label">Comissions</div>
                <div class="val" id="fees" style="color: #c084fc;">-</div>
            </div>
        </div>

        <div class="card-table">
            <div class="table-title">Posicions Obertes Actuals</div>
            <table>
                <thead>
                    <tr>
                        <th>Moneda</th>
                        <th>Costat</th>
                        <th>Entrada</th>
                        <th>Preu Actual</th>
                        <th>PnL No Realitzat</th>
                        <th>Estratègia</th>
                    </tr>
                </thead>
                <tbody id="positions-body">
                    <tr><td colspan="6" style="text-align: center; color: #64748b;">Sense posicions obertes</td></tr>
                </tbody>
            </table>
        </div>

        <div class="card-table">
            <div class="table-title">Darreres 5 Minioperacions Tancades</div>
            <table>
                <thead>
                    <tr>
                        <th>Moneda</th>
                        <th>Costat</th>
                        <th>Entrada</th>
                        <th>Sortida</th>
                        <th>Motiu</th>
                        <th>PnL Net ($)</th>
                    </tr>
                </thead>
                <tbody id="closed-body">
                    <tr><td colspan="6" style="text-align: center; color: #64748b;">Esperant trades...</td></tr>
                </tbody>
            </table>
        </div>

        <div class="footer">Actualització automàtica cada 2s • Hyperliquid Multi-Strategy Bot</div>
    </div>

    <script>
        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                
                document.getElementById('balance').innerText = data.metrics.balance.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' $';
                
                const pnlEl = document.getElementById('pnl');
                const pnl = data.metrics.net_pnl;
                pnlEl.innerText = (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + ' $';
                pnlEl.className = 'val ' + (pnl >= 0 ? 'green' : 'red');

                document.getElementById('winrate').innerText = data.metrics.winrate_pct.toFixed(1) + ' %';
                document.getElementById('trades').innerText = `${data.metrics.total_trades} (${data.metrics.wins}W / ${data.metrics.losses}L)`;
                document.getElementById('fees').innerText = data.metrics.total_fees.toFixed(4) + ' $';
                document.getElementById('uptime').innerText = 'Temps actiu: ' + data.uptime;

                // Posicions
                const posBody = document.getElementById('positions-body');
                if (data.positions.length === 0) {
                    posBody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #64748b;">Sense posicions obertes</td></tr>';
                } else {
                    posBody.innerHTML = data.positions.map(p => {
                        const sideColor = p.side === 'BUY' ? 'green' : 'red';
                        const pnlColor = p.unrealized_pnl >= 0 ? 'green' : 'red';
                        return `<tr>
                            <td style="font-weight: bold; color: #facc15;">${p.coin}</td>
                            <td class="${sideColor}" style="font-weight: bold;">${p.side}</td>
                            <td>${p.entry_price.toFixed(2)}</td>
                            <td>${p.current_price.toFixed(2)}</td>
                            <td class="${pnlColor}" style="font-weight: bold;">${(p.unrealized_pnl >= 0 ? '+' : '') + p.unrealized_pnl.toFixed(3)}$ (${p.unrealized_pnl_pct.toFixed(2)}%)</td>
                            <td style="color: #38bdf8;">${p.strategy_name}</td>
                        </tr>`;
                    }).join('');
                }

                // Tancades
                const closedBody = document.getElementById('closed-body');
                if (data.recent_closed.length === 0) {
                    closedBody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #64748b;">Cap operació tancada encara</td></tr>';
                } else {
                    closedBody.innerHTML = data.recent_closed.slice().reverse().map(p => {
                        const sideColor = p.side === 'BUY' ? 'green' : 'red';
                        const pnlColor = p.realized_pnl >= 0 ? 'green' : 'red';
                        const reasonColor = p.exit_reason === 'TAKE_PROFIT' || p.exit_reason === 'SPREAD_CAPTURED' ? 'green' : 'red';
                        return `<tr>
                            <td style="font-weight: bold; color: #facc15;">${p.coin}</td>
                            <td class="${sideColor}">${p.side}</td>
                            <td>${p.entry_price.toFixed(2)}</td>
                            <td>${p.exit_price ? p.exit_price.toFixed(2) : '-'}</td>
                            <td class="${reasonColor}" style="font-weight: bold;">${p.exit_reason}</td>
                            <td class="${pnlColor}" style="font-weight: bold;">${(p.realized_pnl >= 0 ? '+' : '') + p.realized_pnl.toFixed(3)}$</td>
                        </tr>`;
                    }).join('');
                }
            } catch (e) {
                console.error(e);
            }
        }
        setInterval(fetchStatus, 2000);
        fetchStatus();
    </script>
</body>
</html>
"""

class WebDashboardServer:
    def __init__(self, exchange: PaperExchange, start_time: float, port: int = 8080):
        self.exchange = exchange
        self.start_time = start_time
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

        positions = [
            {
                "coin": p.coin,
                "side": p.side.value,
                "entry_price": p.entry_price,
                "current_price": p.current_price,
                "unrealized_pnl": p.unrealized_pnl,
                "unrealized_pnl_pct": p.unrealized_pnl_pct,
                "strategy_name": p.strategy_name,
            }
            for p in self.exchange.positions.values()
        ]

        recent_closed = [
            {
                "coin": p.coin,
                "side": p.side.value,
                "entry_price": p.entry_price,
                "exit_price": p.exit_price,
                "exit_reason": p.exit_reason,
                "realized_pnl": p.realized_pnl,
            }
            for p in self.exchange.closed_positions[-10:]
        ]

        return web.json_response({
            "uptime": uptime,
            "metrics": self.exchange.metrics,
            "positions": positions,
            "recent_closed": recent_closed,
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
