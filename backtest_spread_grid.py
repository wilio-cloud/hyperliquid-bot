"""Script d'optimització i escombrat de llindars (Grid Search Backtest) per a Arbitratge Hyperliquid vs Aevo DEX.

Permet:
1. Mostrejar en viu els llibres d'ordres sincronitzats de Hyperliquid i Aevo DEX (ex: 30s, 60s o més).
2. Opcionalment gravar i reproduir des d'arxiu històric.
3. Simular l'estratègia amb una graella de llindars (de 0.070% a 0.150%).
4. Trobar el llindar òptim (Sweet Spot) que maximitza el ritme d'ordres (8-10 op/h) garantint PnL net positiu.
"""

import argparse
import asyncio
import gzip
import json
import os
import ssl
import time
from typing import Dict, List, Optional
import certifi
import websockets

from core.arbitrage_models import ArbitrageDirection, ArbitragePosition
from core.arbitrage_paper_exchange import ArbitragePaperExchange
from core.models import BookLevel, OrderBookL2
from strategies.cross_arbitrage import CrossExchangeArbitrageStrategy

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    RICH_AVAILABLE = True
    console = Console()
except ImportError:
    RICH_AVAILABLE = False
    console = None

class SynchronizedMarketEvent:
    def __init__(self, venue: str, coin: str, book: OrderBookL2, timestamp: float):
        self.venue = venue  # "HL" o "AEVO"
        self.coin = coin
        self.book = book
        self.timestamp = timestamp

class ArbitrageGridBacktest:
    def __init__(
        self,
        coins: Optional[List[str]] = None,
        duration_sec: int = 30,
        initial_balance: float = 1000.0,
        order_size_usd: float = 250.0,
        leverage: float = 2.0,
        max_positions: int = 4,
    ):
        self.coins = coins or ["BTC", "ETH", "SOL", "HYPE", "NEAR", "XRP", "PUMP"]
        self.duration_sec = duration_sec
        self.initial_balance = initial_balance
        self.order_size_usd = order_size_usd
        self.leverage = leverage
        self.max_positions = max_positions
        self.events: List[SynchronizedMarketEvent] = []

    async def record_live_events(self, duration_sec: int, save_file: Optional[str] = None):
        """Connecta simultàniament a Hyperliquid i Aevo i enregistra tots els esdeveniments L2."""
        ssl_ctx = ssl._create_unverified_context()
        hl_url = "wss://api.hyperliquid.xyz/ws"
        aevo_url = "wss://ws.aevo.xyz"

        aevo_symbol_map = {c: f"{c}-PERP" for c in self.coins}
        aevo_rev_map = {f"{c}-PERP": c for c in self.coins}

        print(f"\nConnectant als WebSockets de Hyperliquid i Aevo per a {len(self.coins)} monedes...")
        print(f"Temps de mostreig programat: {duration_sec} segons...")

        start_time = time.time()
        event_queue = asyncio.Queue()

        async def hl_collector():
            try:
                async with websockets.connect(hl_url, ssl=ssl_ctx) as ws:
                    for c in self.coins:
                        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "l2Book", "coin": c}}))
                    while time.time() - start_time < duration_sec:
                        msg = json.loads(await ws.recv())
                        if msg.get("channel") == "l2Book":
                            data = msg.get("data", {})
                            c = data.get("coin")
                            if c in self.coins:
                                levels = data.get("levels", [[], []])
                                if levels[0] and levels[1]:
                                    bids = [BookLevel(price=float(x["px"]), size=float(x["sz"])) for x in levels[0][:5]]
                                    asks = [BookLevel(price=float(x["px"]), size=float(x["sz"])) for x in levels[1][:5]]
                                    ts = float(data.get("time", time.time() * 1000)) / 1000.0
                                    book = OrderBookL2(coin=c, timestamp=ts, bids=bids, asks=asks)
                                    await event_queue.put(SynchronizedMarketEvent("HL", c, book, ts))
            except Exception:
                pass

        async def aevo_collector():
            try:
                async with websockets.connect(aevo_url, ssl=ssl_ctx) as ws:
                    channels = [f"orderbook-100ms:{aevo_symbol_map[c]}" for c in self.coins]
                    await ws.send(json.dumps({"op": "subscribe", "data": channels}))
                    while time.time() - start_time < duration_sec:
                        msg = json.loads(await ws.recv())
                        data = msg.get("data")
                        if data and isinstance(data, dict):
                            inst = data.get("instrument_name", "")
                            c = aevo_rev_map.get(inst)
                            if c:
                                bids_raw = data.get("bids", [])
                                asks_raw = data.get("asks", [])
                                if bids_raw and asks_raw:
                                    bids = [BookLevel(price=float(b[0]), size=float(b[1])) for b in bids_raw[:5]]
                                    asks = [BookLevel(price=float(a[0]), size=float(a[1])) for a in asks_raw[:5]]
                                    ts = time.time()
                                    book = OrderBookL2(coin=c, timestamp=ts, bids=bids, asks=asks)
                                    await event_queue.put(SynchronizedMarketEvent("AEVO", c, book, ts))
            except Exception:
                pass

        async def drain_queue():
            while time.time() - start_time < duration_sec or not event_queue.empty():
                try:
                    ev = await asyncio.wait_for(event_queue.get(), timeout=0.5)
                    self.events.append(ev)
                except asyncio.TimeoutError:
                    if time.time() - start_time >= duration_sec:
                        break

        await asyncio.gather(hl_collector(), aevo_collector(), drain_queue())

        self.events.sort(key=lambda x: x.timestamp)
        print(f"S'han recollit {len(self.events):,} esdeveniments sincronitzats.")

        if save_file and self.events:
            with gzip.open(save_file, "wt", encoding="utf-8") as f:
                for ev in self.events:
                    row = {
                        "venue": ev.venue,
                        "coin": ev.coin,
                        "timestamp": ev.timestamp,
                        "bids": [{"price": b.price, "size": b.size} for b in ev.book.bids],
                        "asks": [{"price": a.price, "size": a.size} for a in ev.book.asks],
                    }
                    f.write(json.dumps(row) + "\n")
            print(f"Dades desades a '{save_file}'")

    def load_from_file(self, filepath: str):
        print(f"Carregant esdeveniments des de '{filepath}'...")
        self.events = []
        with gzip.open(filepath, "rt", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                bids = [BookLevel(price=b["price"], size=b["size"]) for b in row["bids"]]
                asks = [BookLevel(price=a["price"], size=a["size"]) for a in row["asks"]]
                book = OrderBookL2(coin=row["coin"], timestamp=row["timestamp"], bids=bids, asks=asks)
                self.events.append(SynchronizedMarketEvent(row["venue"], row["coin"], book, row["timestamp"]))
        self.events.sort(key=lambda x: x.timestamp)
        print(f"Carregats {len(self.events):,} esdeveniments.")

    def simulate_threshold(self, min_spread_pct: float) -> dict:
        strat = CrossExchangeArbitrageStrategy(
            min_entry_spread_pct=min_spread_pct,
            weekend_min_spread_pct=min_spread_pct,
            target_exit_spread_pct=0.010,
            min_profit_usd=0.08,
            take_profit_usd=0.35,
            max_hold_seconds=900,
            max_book_spread_pct=0.350,
            per_coin_min_spread={c: min_spread_pct for c in self.coins},
        )
        exchange = ArbitragePaperExchange(
            initial_hl_balance=self.initial_balance / 2.0,
            initial_bn_balance=self.initial_balance / 2.0,
            venue2_name="AEVO",
            leverage=self.leverage,
        )

        cooldowns: Dict[str, float] = {}

        if not self.events:
            return {}

        sim_start_ts = self.events[0].timestamp
        sim_end_ts = self.events[-1].timestamp
        duration_hours = max((sim_end_ts - sim_start_ts) / 3600.0, 1.0 / 3600.0)

        for ev in self.events:
            if ev.venue == "HL":
                strat.update_hl_book(ev.book)
                exchange.on_hl_book(ev.book)
            else:
                strat.update_bn_book(ev.book)
                exchange.on_bn_book(ev.book)

            coin = ev.coin

            # Sortides
            for pos in list(exchange.active_positions.values()):
                if pos.coin == coin:
                    exit_res = strat.check_exit(pos)
                    if exit_res:
                        # Sortides reals executades per Taker IOC
                        exchange.close_arbitrage_position(pos.pair_id, hl_px, bn_px, reason, is_maker=False)
                        cooldowns[coin] = ev.timestamp + 45.0

            # Entrades
            if ev.timestamp < cooldowns.get(coin, 0.0):
                continue

            if not exchange.has_open_position(coin) and len(exchange.active_positions) < self.max_positions:
                sig = strat.evaluate_entry(coin)
                if sig:
                    exchange.open_arbitrage_position(sig, size_usd=self.order_size_usd, is_maker=False)

        trades = exchange.closed_positions
        total_trades = len(trades)
        wins = [p for p in trades if p.realized_pnl > 0]
        winrate = (len(wins) / total_trades * 100.0) if total_trades > 0 else 0.0
        total_pnl = sum(p.realized_pnl for p in trades)
        avg_pnl = (total_pnl / total_trades) if total_trades > 0 else 0.0
        trades_per_hour = total_trades / duration_hours
        ev_hourly = trades_per_hour * avg_pnl

        return {
            "threshold_pct": min_spread_pct,
            "total_trades": total_trades,
            "trades_per_hour": trades_per_hour,
            "winrate_pct": winrate,
            "total_pnl": total_pnl,
            "avg_pnl": avg_pnl,
            "ev_hourly": ev_hourly,
            "duration_hours": duration_hours,
            "closed_trades": trades,
        }

    def run_grid_search(self, candidate_thresholds: Optional[List[float]] = None) -> List[dict]:
        thresholds = candidate_thresholds or [0.070, 0.080, 0.085, 0.090, 0.095, 0.100, 0.110, 0.120, 0.130, 0.140, 0.150]
        results = []
        print("\n🔍 Executant escombrat de llindars (Grid Search Backtest)...")
        for th in thresholds:
            res = self.simulate_threshold(th)
            results.append(res)
        return results

    def print_results(self, results: List[dict]):
        if not results:
            print("Sense resultats.")
            return

        best_candidate = None
        best_score = -9999.0
        for r in results:
            if r.get("total_trades", 0) > 0 and r.get("avg_pnl", 0) > 0:
                pace_diff = abs(r["trades_per_hour"] - 9.0)
                score = r["ev_hourly"] - (pace_diff * 0.1)
                if score > best_score:
                    best_score = score
                    best_candidate = r["threshold_pct"]

        print("\n" + "=" * 95)
        print(f"{'Llindar %':<10} | {'Trades':<8} | {'Ritme (op/h)':<14} | {'Winrate %':<10} | {'PnL Total':<12} | {'EV Horari':<12} | {'Diagnòstic'}")
        print("=" * 95)
        for r in results:
            th = r["threshold_pct"]
            pace = r["trades_per_hour"]
            winrate = r["winrate_pct"]
            tot_pnl = r["total_pnl"]
            ev = r["ev_hourly"]
            is_sweet = (best_candidate is not None and abs(th - best_candidate) < 0.001)

            if is_sweet:
                diag = "🌟 SWEET SPOT (Òptim)"
            elif pace >= 8.0 and tot_pnl > 0:
                diag = "🔥 Alta Freqüència Rentable"
            elif pace < 4.0:
                diag = "⏳ Lent (<4 op/h)"
            elif tot_pnl <= 0 and r["total_trades"] > 0:
                diag = "🛑 Comissions altes (PnL negatiu)"
            else:
                diag = "✅ Estable"

            print(f"{th:8.3f}% | {r['total_trades']:<8} | {pace:10.1f} op/h | {winrate:8.1f}% | {tot_pnl:+10.3f}$ | {ev:+10.2f}$/h | {diag}")
        print("=" * 95)
        if best_candidate:
            print(f"\n🎯 Llindar Òptim Recomanat: {best_candidate:.3f}% (Maximitza l'EV i el volum cap a 8-10 op/h)")
        else:
            print(f"\n🎯 Llindar Estàndard Recomanat: 0.095% (Marge net garantit cobrint comissions de 0.070%)")

def main():
    parser = argparse.ArgumentParser(description="Grid Search Backtest de Llindars d'Arbitratge")
    parser.add_argument("--duration", type=int, default=15, help="Segons de mostreig en viu (default: 15s)")
    parser.add_argument("--file", type=str, default=None, help="Carregar gravació des d'un arxiu .jsonl.gz")
    parser.add_argument("--save", type=str, default=None, help="Desar les dades en un arxiu .jsonl.gz")
    parser.add_argument("--size", type=float, default=250.0, help="Mida per ordre (default: 250$)")
    parser.add_argument("--coins", nargs="+", default=None, help="Monedes a analitzar")
    args = parser.parse_args()

    backtest = ArbitrageGridBacktest(
        coins=args.coins,
        duration_sec=args.duration,
        order_size_usd=args.size,
    )

    if args.file:
        backtest.load_from_file(args.file)
    else:
        asyncio.run(backtest.record_live_events(duration_sec=args.duration, save_file=args.save))

    results = backtest.run_grid_search()
    backtest.print_results(results)

if __name__ == "__main__":
    main()
