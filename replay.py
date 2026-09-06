"""Reproductor de gravacions (Replay) a alta velocitat per a backtests exactes de tick i L2."""

import argparse
import gzip
import json
import time
from core.models import BookLevel, OrderBookL2, OrderSide, Trade
from core.paper_exchange import PaperExchange
from core.risk_manager import RiskManager
from strategies.micro_mean_reversion import MicroMeanReversionStrategy
from strategies.orderbook_imbalance import OrderBookImbalanceStrategy
from strategies.volume_burst import VolumeBurstStrategy
from rich.console import Console

console = Console()

def replay_file(filename: str, speed_multiplier: float = 0.0):
    console.print(f"[bold cyan]Carregant gravació històrica des de '{filename}'...[/bold cyan]")
    
    exchange = PaperExchange()
    risk_manager = RiskManager()
    books = {}
    strategies = [
        OrderBookImbalanceStrategy(),
        VolumeBurstStrategy(),
        MicroMeanReversionStrategy(),
    ]

    total_events = 0
    start_time = time.time()

    with gzip.open(filename, "rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            total_events += 1
            msg = json.loads(line)
            channel = msg.get("channel")
            data = msg.get("data")
            if not channel or not data:
                continue

            if channel == "l2Book":
                coin = data.get("coin")
                levels = data.get("levels", [])
                if len(levels) >= 2:
                    bids = [BookLevel(price=float(x["px"]), size=float(x["sz"]), num_orders=int(x.get("n", 1))) for x in levels[0]]
                    asks = [BookLevel(price=float(x["px"]), size=float(x["sz"]), num_orders=int(x.get("n", 1))) for x in levels[1]]
                    ts = float(data.get("time", time.time() * 1000)) / 1000.0
                    book = OrderBookL2(coin=coin, timestamp=ts, bids=bids, asks=asks)
                    books[coin] = book

                    exchange.on_book_update(book)

                    # Check time exits
                    expired = risk_manager.check_time_exits(exchange.positions, books)
                    for c in expired:
                        if c in books and books[c].mid_price:
                            exchange.close_position(c, books[c].mid_price, "TIME_LIMIT", is_maker=True)

                    for strat in strategies:
                        sig = strat.on_book_update(book)
                        if sig:
                            has_pos = exchange.get_position(sig.coin) is not None
                            can_open, _ = risk_manager.can_open_position(sig.coin, exchange.balance_usd, exchange.initial_balance, len(exchange.positions), has_pos)
                            if can_open:
                                side = OrderSide.BUY if sig.action == "BUY" else OrderSide.SELL
                                exchange.place_order(sig.coin, side, sig.price, 500.0, post_only=True, strategy_name=sig.strategy_name, current_book=book)

            elif channel == "trades":
                if isinstance(data, list):
                    for item in data:
                        coin = item.get("coin")
                        side = OrderSide.BUY if item.get("side") == "B" else OrderSide.SELL
                        trade = Trade(coin=coin, side=side, price=float(item["px"]), size=float(item["sz"]), timestamp=float(item.get("time", 0))/1000.0)
                        exchange.on_trade(trade)

                        for strat in strategies:
                            sig = strat.on_trade(trade)
                            if sig and trade.coin in books:
                                has_pos = exchange.get_position(sig.coin) is not None
                                can_open, _ = risk_manager.can_open_position(sig.coin, exchange.balance_usd, exchange.initial_balance, len(exchange.positions), has_pos)
                                if can_open:
                                    side = OrderSide.BUY if sig.action == "BUY" else OrderSide.SELL
                                    exchange.place_order(sig.coin, side, sig.price, 500.0, post_only=True, strategy_name=sig.strategy_name, current_book=books[trade.coin])

    elapsed = time.time() - start_time
    console.print(f"\n[green]Replay completat en {elapsed:.2f}s ({total_events:,} esdeveniments processats)![/green]")

    metrics = exchange.metrics
    console.print(f"PnL Net: {metrics['net_pnl']:+.2f}$ | Trades: {metrics['total_trades']} | Winrate: {metrics['winrate_pct']:.1f}% | Fees: {metrics['total_fees']:.2f}$")

def main():
    parser = argparse.ArgumentParser(description="Replay recorded market data")
    parser.add_argument("--file", type=str, default="data_recording.jsonl.gz")
    args = parser.parse_args()
    replay_file(args.file)

if __name__ == "__main__":
    main()
