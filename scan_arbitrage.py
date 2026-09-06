"""Escàner de mercat en viu per detectar oportunitats d'arbitratge (Hyperliquid vs Binance)."""

import argparse
import asyncio
import json
import logging
import ssl
import time
from typing import Dict, List
import aiohttp
from rich.console import Console
from rich.live import Live
from rich.table import Table

from core.binance_ws_client import get_ssl_context

console = Console()
logging.getLogger().setLevel(logging.WARNING)

WATCHLIST = ["BTC", "ETH", "SOL", "DOGE", "SUI", "AVAX", "LINK", "XRP", "ADA", "NEAR"]

async def fetch_hl_meta_and_prices(session: aiohttp.ClientSession, ssl_ctx: ssl.SSLContext) -> Dict[str, dict]:
    url = "https://api.hyperliquid.xyz/info"
    payload = {"type": "metaAndAssetCtxs"}
    async with session.post(url, json=payload, ssl=ssl_ctx, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
        if resp.status == 200:
            data = await resp.json()
            universe = data[0]["universe"]
            asset_ctxs = data[1]
            result = {}
            for i, meta in enumerate(universe):
                coin = meta["name"]
                if coin in WATCHLIST:
                    ctx = asset_ctxs[i]
                    result[coin] = {
                        "mark_price": float(ctx["markPx"]),
                        "funding_hourly_pct": float(ctx["funding"]) * 100.0,
                    }
            return result
    return {}

async def fetch_binance_premiums(session: aiohttp.ClientSession, ssl_ctx: ssl.SSLContext) -> Dict[str, dict]:
    url = "https://fapi.binance.com/fapi/v1/premiumIndex"
    headers = {"User-Agent": "Mozilla/5.0"}
    async with session.get(url, headers=headers, ssl=ssl_ctx, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
        if resp.status == 200:
            data = await resp.json()
            result = {}
            for item in data:
                sym = item.get("symbol", "")
                if sym.endswith("USDT"):
                    coin = sym[:-4]
                    if coin in WATCHLIST:
                        result[coin] = {
                            "mark_price": float(item["markPrice"]),
                            "funding_8h_pct": float(item["lastFundingRate"]) * 100.0,
                        }
            return result
    return {}

def render_table(hl_data: dict, bn_data: dict, min_spread_pct: float = 0.080) -> Table:
    table = Table(
        title="⚡ OPORTUNITATS D'ARBITRATGE EN VIU (Hyperliquid DEX vs Binance Futures)",
        title_style="bold cyan",
        header_style="bold magenta",
        border_style="dim white",
    )
    table.add_column("Moneda", justify="left", style="yellow bold")
    table.add_column("Preu HL ($)", justify="right")
    table.add_column("Preu BN ($)", justify="right")
    table.add_column("Spread % (HL vs BN)", justify="right")
    table.add_column("HL Fund (8h)", justify="right")
    table.add_column("BN Fund (8h)", justify="right")
    table.add_column("Dif. Funding (APR)", justify="right")
    table.add_column("Estat Arbitratge", justify="center")

    for coin in WATCHLIST:
        hl_item = hl_data.get(coin)
        bn_item = bn_data.get(coin)
        if not hl_item or not bn_item:
            continue

        hl_px = hl_item["mark_price"]
        bn_px = bn_item["mark_price"]
        spread_pct = ((hl_px - bn_px) / bn_px) * 100.0

        hl_fund_8h = hl_item["funding_hourly_pct"] * 8.0
        bn_fund_8h = bn_item["funding_8h_pct"]
        annual_diff_apr = (hl_fund_8h - bn_fund_8h) * 3.0 * 365.0

        spread_color = "green bold" if abs(spread_pct) >= min_spread_pct else "white"
        apr_color = "green" if annual_diff_apr > 10.0 else ("red" if annual_diff_apr < -10.0 else "dim white")

        # Motiu o acció recomanada
        if spread_pct >= min_spread_pct:
            status = "[bold green]🔥 SELL HL / BUY BN[/]"
        elif spread_pct <= -min_spread_pct:
            status = "[bold green]🔥 BUY HL / SELL BN[/]"
        elif abs(annual_diff_apr) >= 15.0:
            status = "[bold yellow]💰 HARVEST FUNDING[/]"
        else:
            status = "[dim]NORMAL[/]"

        # Format del preu segons la magnitud
        px_fmt = "{:,.4f}" if hl_px < 1.0 else ("{:,.2f}" if hl_px < 1000.0 else "{:,.1f}")

        table.add_row(
            coin,
            px_fmt.format(hl_px),
            px_fmt.format(bn_px),
            f"[{spread_color}]{spread_pct:+.4f}%[/{spread_color}]",
            f"{hl_fund_8h:+.4f}%",
            f"{bn_fund_8h:+.4f}%",
            f"[{apr_color}]{annual_diff_apr:+.1f}%[/{apr_color}]",
            status,
        )

    return table

async def run_scanner(once: bool = False, min_spread_pct: float = 0.080):
    ssl_ctx = get_ssl_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(connector=connector) as session:
        if once:
            hl_data = await fetch_hl_meta_and_prices(session, ssl_ctx)
            bn_data = await fetch_binance_premiums(session, ssl_ctx)
            console.print(render_table(hl_data, bn_data, min_spread_pct=min_spread_pct))
            return

        with Live(console=console, refresh_per_second=1) as live:
            while True:
                try:
                    hl_data = await fetch_hl_meta_and_prices(session, ssl_ctx)
                    bn_data = await fetch_binance_premiums(session, ssl_ctx)
                    live.update(render_table(hl_data, bn_data, min_spread_pct=min_spread_pct))
                except Exception as e:
                    console.print(f"[red]Error a l'escàner: {e}[/red]")
                await asyncio.sleep(2.0)

def main():
    parser = argparse.ArgumentParser(description="Hyperliquid vs Binance Arbitrage Scanner")
    parser.add_argument("--once", action="store_true", help="Executa un sol escaneig i surt")
    parser.add_argument("--min-spread", type=float, default=0.080, help="Spread mínim percentual")
    args = parser.parse_args()

    try:
        asyncio.run(run_scanner(once=args.once, min_spread_pct=args.min_spread))
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
