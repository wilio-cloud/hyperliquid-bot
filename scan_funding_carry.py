"""Escàner en viu per detectar i monitoritzar oportunitats de Funding Rate Carry Trade.

Compara en temps real les taxes de finançament entre Hyperliquid i exchanges secundaris,
calcula l'APR net anualitzat, el spread de base inicial i la projecció de beneficis en dòlars.
"""

import argparse
import asyncio
import ssl
from typing import Dict, List
import aiohttp
from rich.console import Console
from rich.live import Live
from rich.table import Table

from core.binance_ws_client import get_ssl_context

console = Console()

WATCHLIST = ["BTC", "ETH", "SOL", "NEAR", "SUI", "DOGE", "AVAX", "LINK", "XRP", "ADA", "ARB", "OP", "PEPE", "WIF"]

async def fetch_hl_meta_and_funding(session: aiohttp.ClientSession, ssl_ctx: ssl.SSLContext) -> Dict[str, dict]:
    url = "https://api.hyperliquid.xyz/info"
    payload = {"type": "metaAndAssetCtxs"}
    async with session.post(url, json=payload, ssl=ssl_ctx, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
        if resp.status == 200:
            data = await resp.json()
            universe = data[0]["universe"]
            asset_ctxs = data[1]
            result = {}
            for i, meta in enumerate(universe):
                raw_c = meta["name"]
                c = "PEPE" if raw_c == "kPEPE" else raw_c
                if c in WATCHLIST:
                    ctx = asset_ctxs[i]
                    hourly_pct = float(ctx["funding"]) * 100.0
                    result[c] = {
                        "mark_price": float(ctx["markPx"]),
                        "funding_hourly_pct": hourly_pct,
                        "funding_8h_pct": hourly_pct * 8.0,
                        "funding_apr": hourly_pct * 24.0 * 365.0,
                    }
            return result
    return {}

async def fetch_binance_funding(session: aiohttp.ClientSession, ssl_ctx: ssl.SSLContext) -> Dict[str, dict]:
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
                        rate_8h_pct = float(item["lastFundingRate"]) * 100.0
                        result[coin] = {
                            "mark_price": float(item["markPrice"]),
                            "funding_8h_pct": rate_8h_pct,
                            "funding_apr": (rate_8h_pct / 8.0) * 24.0 * 365.0,
                        }
            return result
    return {}

def render_carry_table(hl_data: dict, bn_data: dict, min_apr: float = 16.0, position_size_usd: float = 1000.0) -> Table:
    table = Table(
        title="🌾 ESCÀNER DE FUNDING RATE CARRY TRADE (Hyperliquid vs Binance Futures)",
        title_style="bold cyan",
        header_style="bold magenta",
        border_style="dim white",
    )
    table.add_column("Moneda", justify="left", style="yellow bold")
    table.add_column("Preu HL ($)", justify="right")
    table.add_column("Spread Base (%)", justify="right")
    table.add_column("HL Fund (8h)", justify="right")
    table.add_column("BN Fund (8h)", justify="right")
    table.add_column("Diferència Net APR", justify="right")
    table.add_column(f"Rendiment/Dia ({position_size_usd:.0f}$)", justify="right")
    table.add_column(f"Rendiment/Any ({position_size_usd:.0f}$)", justify="right")
    table.add_column("Estat del Carry", justify="center")

    sorted_coins = []
    for coin in WATCHLIST:
        hl_item = hl_data.get(coin)
        bn_item = bn_data.get(coin)
        if not hl_item or not bn_item:
            continue
        net_apr = hl_item["funding_apr"] - bn_item["funding_apr"]
        sorted_coins.append((coin, abs(net_apr), net_apr, hl_item, bn_item))

    # Ordenat de més atractiu a menys atractiu
    sorted_coins.sort(key=lambda x: x[1], reverse=True)

    for coin, abs_apr, net_apr, hl_item, bn_item in sorted_coins:
        hl_px = hl_item["mark_price"]
        bn_px = bn_item["mark_price"]
        base_spread_pct = ((hl_px - bn_px) / bn_px) * 100.0

        daily_usd = position_size_usd * (abs_apr / 100.0) / 365.0
        annual_usd = position_size_usd * (abs_apr / 100.0)

        # Avaluació de qualitat
        if net_apr >= min_apr and base_spread_pct >= -0.05:
            apr_color = "bold green"
            status = "[bold green]🔥 SHORT HL / LONG BN[/]"
        elif -net_apr >= min_apr and base_spread_pct <= 0.05:
            apr_color = "bold green"
            status = "[bold green]🔥 LONG HL / SHORT BN[/]"
        elif abs_apr >= min_apr:
            apr_color = "yellow"
            status = "[yellow]⚠️ BASE ADVERSA[/]"
        elif abs_apr >= 10.0:
            apr_color = "dim white"
            status = "[dim]⏳ MODERAT[/]"
        else:
            apr_color = "dim"
            status = "[dim]BAIX[/]"

        px_fmt = "{:,.4f}" if hl_px < 1.0 else ("{:,.2f}" if hl_px < 1000.0 else "{:,.1f}")

        table.add_row(
            coin,
            px_fmt.format(hl_px),
            f"{base_spread_pct:+.3f}%",
            f"{hl_item['funding_8h_pct']:+.4f}%",
            f"{bn_item['funding_8h_pct']:+.4f}%",
            f"[{apr_color}]{net_apr:+.1f}% APR[/{apr_color}]",
            f"{daily_usd:+.2f}$/d",
            f"{annual_usd:+.1f}$/any",
            status,
        )

    return table

async def run_scanner(once: bool = False, min_apr: float = 16.0, size_usd: float = 1000.0):
    ssl_ctx = get_ssl_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(connector=connector) as session:
        if once:
            hl_data = await fetch_hl_meta_and_funding(session, ssl_ctx)
            bn_data = await fetch_binance_funding(session, ssl_ctx)
            console.print(render_carry_table(hl_data, bn_data, min_apr=min_apr, position_size_usd=size_usd))
            return

        with Live(console=console, refresh_per_second=1) as live:
            while True:
                try:
                    hl_data = await fetch_hl_meta_and_funding(session, ssl_ctx)
                    bn_data = await fetch_binance_funding(session, ssl_ctx)
                    live.update(render_carry_table(hl_data, bn_data, min_apr=min_apr, position_size_usd=size_usd))
                except Exception as e:
                    console.print(f"[red]Error a l'escàner: {e}[/red]")
                await asyncio.sleep(3.0)

def main():
    parser = argparse.ArgumentParser(description="Hyperliquid vs Binance Funding Rate Carry Scanner")
    parser.add_argument("--once", action="store_true", help="Executa una sola lectura i surt")
    parser.add_argument("--min-apr", type=float, default=16.0, help="APR mínim per considerar senyal òptim (default: 16.0%)")
    parser.add_argument("--size", type=float, default=1000.0, help="Mida de posició en dòlars per calcular rendiments (default: 1000$)")
    args = parser.parse_args()

    try:
        asyncio.run(run_scanner(once=args.once, min_apr=args.min_apr, size_usd=args.size))
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
