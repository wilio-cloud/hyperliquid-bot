"""Orquestrador principal del Bot d'Hyperliquid (Paper Trading & Multi-Estratègia)."""

import argparse
import asyncio
import logging
import signal
import sys
import time
from typing import Dict, List

from rich.console import Console
from rich.live import Live

from config.settings import config
from core.models import OrderBookL2, OrderSide, Signal, Trade
from core.paper_exchange import PaperExchange
from core.risk_manager import RiskManager
from core.ws_client import HyperliquidWSClient
from strategies.base_strategy import BaseStrategy
from strategies.micro_mean_reversion import MicroMeanReversionStrategy
from strategies.orderbook_imbalance import OrderBookImbalanceStrategy
from strategies.spread_scalper import SpreadMarketMakerStrategy
from strategies.volume_burst import VolumeBurstStrategy
from ui.dashboard import generate_dashboard
from core.web_server import WebDashboardServer

# Configuració de logging (desat a fitxer per no trencar el dashboard Rich a la terminal)
logging.basicConfig(
    filename="trading_bot.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("Main")
console = Console()

class TradingBotApp:
    def __init__(self, coins: List[str], strategies: List[BaseStrategy], headless: bool = False):
        self.coins = coins
        self.strategies = strategies
        self.headless = headless
        self.start_time = time.time()
        
        self.exchange = PaperExchange(
            initial_balance=config.initial_balance_usd,
            on_fill_cb=self._on_exchange_fill,
            on_close_cb=self._on_exchange_close,
        )
        self.risk_manager = RiskManager()
        self.books: Dict[str, OrderBookL2] = {}
        
        self.ws_client = HyperliquidWSClient(
            coins=self.coins,
            on_book_update=self.handle_book_update,
            on_trade_update=self.handle_trade_update,
        )
        self.web_server = WebDashboardServer(self.exchange, self.start_time)
        self.is_running = False

    def _on_exchange_fill(self, pos, order, is_maker: bool):
        if self.headless:
            maker_str = "MAKER (0.01% fee)" if is_maker else "TAKER (0.035% fee)"
            print(f"  ⚡ [FILLED ENTRADA] {pos.side.value} {pos.coin} @ {pos.entry_price:.2f} ({pos.size_usd:.0f}$) | TP: {pos.take_profit:.2f} | SL: {pos.stop_loss:.2f} [{maker_str}]")

    def _on_exchange_close(self, pos, exit_reason: str, net_pnl: float):
        self.risk_manager.record_closed_position(pos)
        if self.headless:
            icon = "✅" if net_pnl > 0 else "❌"
            color_res = "GUANY" if net_pnl > 0 else "PÈRDUA"
            print(f"  {icon} [TANCADA {exit_reason}] {pos.coin} @ {pos.exit_price:.2f} | {color_res}: {net_pnl:+.3f}$ (Fees: {pos.fees_paid:.4f}$) | Nou Balanç: {self.exchange.balance_usd:.2f}$")

    def handle_book_update(self, book: OrderBookL2):
        self.books[book.coin] = book

        # 1. Actualitza preus i comprova TP/SL al simulador
        self.exchange.on_book_update(book)

        # 2. Comprova tancament per temps de seguretat (Time-Exits)
        expired_coins = self.risk_manager.check_time_exits(self.exchange.positions, self.books)
        for coin in expired_coins:
            if coin in self.books and self.books[coin].mid_price:
                self.exchange.close_position(
                    coin=coin,
                    exit_price=self.books[coin].mid_price,
                    exit_reason="TIME_LIMIT",
                    is_maker=True,
                )

        # 3. Executa les estratègies basades en book
        for strat in self.strategies:
            sig = strat.on_book_update(book)
            if sig:
                self._process_signal(sig, book)

    def handle_trade_update(self, trade: Trade):
        # 1. Avança cues del simulador d'ordres límit
        self.exchange.on_trade(trade)

        # 2. Executa les estratègies basades en flux de trades
        for strat in self.strategies:
            sig = strat.on_trade(trade)
            if sig and trade.coin in self.books:
                self._process_signal(sig, self.books[trade.coin])

    def _process_signal(self, sig: Signal, book: OrderBookL2):
        has_pos = self.exchange.get_position(sig.coin) is not None
        can_open, reason = self.risk_manager.can_open_position(
            coin=sig.coin,
            current_balance=self.exchange.balance_usd,
            initial_balance=self.exchange.initial_balance,
            open_positions_count=len(self.exchange.positions),
            has_existing_coin_position=has_pos,
        )

        if not can_open:
            logger.debug(f"[SENYAL REBUTJAT PER RISC] {sig.coin} {sig.action} de {sig.strategy_name}: {reason}")
            return

        side = OrderSide.BUY if sig.action == "BUY" else OrderSide.SELL
        order = self.exchange.place_order(
            coin=sig.coin,
            side=side,
            price=sig.price,
            size_usd=config.position_size_usd,
            post_only=True,
            strategy_name=sig.strategy_name,
            current_book=book,
        )

        if order and self.headless:
            print(f"[{sig.strategy_name}] Ordre enviada: {side.value} {sig.coin} @ {sig.price:.2f} ({sig.reason})")

    async def run(self, duration_sec: int = 0):
        self.is_running = True
        await self.ws_client.start()
        await self.web_server.start()
        strat_names = [s.name for s in self.strategies]

        try:
            if not self.headless:
                with Live(generate_dashboard(self.exchange, self.books, self.start_time, strat_names), refresh_per_second=4, console=console) as live:
                    while self.is_running:
                        await asyncio.sleep(0.25)
                        live.update(generate_dashboard(self.exchange, self.books, self.start_time, strat_names))
                        if duration_sec > 0 and (time.time() - self.start_time) >= duration_sec:
                            break
            else:
                print(f"Bot iniciat en mode Headless. Monitoritzant {self.coins}...")
                while self.is_running:
                    await asyncio.sleep(1.0)
                    if duration_sec > 0 and (time.time() - self.start_time) >= duration_sec:
                        break

        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await self.shutdown()

    async def shutdown(self):
        self.is_running = False
        await self.web_server.stop()
        await self.ws_client.stop()
        self.print_summary()

    def print_summary(self):
        console.print("\n[bold cyan]═══ RESUM FINAL DE LA SESSIÓ DE TRADING ═══[/bold cyan]")
        metrics = self.exchange.metrics
        pnl = metrics["net_pnl"]
        pnl_color = "green" if pnl >= 0 else "red"
        
        console.print(f"Balanç Inicial: [white]{self.exchange.initial_balance:,.2f}$[/white]")
        console.print(f"Balanç Final:   [white]{metrics['balance']:,.2f}$[/white]")
        console.print(f"PnL Net Total:  [{pnl_color}]{pnl:+,.3f}$[/]")
        console.print(f"Total Operacions: {metrics['total_trades']} (Guanyades: {metrics['wins']}, Perdudes: {metrics['losses']})")
        console.print(f"Taxa d'Èxit (Winrate): [bold]{metrics['winrate_pct']:.2f}%[/bold]")
        console.print(f"Profit Factor: {metrics['profit_factor']:.2f}")
        console.print(f"Total Comissions: [magenta]{metrics['total_fees']:.4f}$[/magenta] (Maker: {metrics['maker_ratio_pct']:.1f}%)")
        
        # Desglossament per estratègies
        console.print("\n[bold]Rendiment per Estratègia:[/bold]")
        for strat in self.strategies:
            strat_trades = [p for p in self.exchange.closed_positions if p.strategy_name == strat.name]
            s_wins = len([p for p in strat_trades if p.realized_pnl > 0])
            s_pnl = sum(p.realized_pnl for p in strat_trades)
            s_wr = (s_wins / len(strat_trades) * 100.0) if strat_trades else 0.0
            console.print(f" • {strat.name:15}: {len(strat_trades)} trades | Winrate: {s_wr:5.1f}% | PnL: {s_pnl:+,.3f}$ | Senyals: {strat.signals_count}")

        if self.exchange.positions:
            console.print("\n[bold yellow]Posicions obertes en curs en aturar la sessió:[/bold yellow]")
            for coin, pos in self.exchange.positions.items():
                pnl_color = "green" if pos.unrealized_pnl >= 0 else "red"
                console.print(
                    f" • [{pos.side.value}] {pos.coin} @ {pos.entry_price:,.2f} "
                    f"| Mida: {pos.size_usd:.1f}$ | TP: {pos.take_profit:,.2f} | SL: {pos.stop_loss:,.2f} "
                    f"| PnL No Realitzat: [{pnl_color}]{pos.unrealized_pnl:+.3f}$ ({pos.unrealized_pnl_pct:+.2f}%)[/{pnl_color}]"
                )

def main():
    parser = argparse.ArgumentParser(description="Hyperliquid High-Frequency Scalping Bot")
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL"], help="Monedes a operar (ex: BTC ETH SOL)")
    parser.add_argument("--duration", type=int, default=0, help="Durada màxima d'execució en segons (0 = indefinit)")
    parser.add_argument("--headless", action="store_true", help="Executar sense el tauler visual Rich (només logs)")
    parser.add_argument("--no-burst", action="store_true", help="Desactivar Volume Burst (recomanat)")
    parser.add_argument("--maker-only", action="store_true", help="Executar només l'estratègia de captura de spread")
    args = parser.parse_args()

    # Instanciació de les estratègies
    if args.maker_only:
        strategies: List[BaseStrategy] = [SpreadMarketMakerStrategy()]
    else:
        strategies: List[BaseStrategy] = [
            SpreadMarketMakerStrategy(),
            OrderBookImbalanceStrategy(),
            MicroMeanReversionStrategy(),
        ]
        if not args.no_burst:
            strategies.append(VolumeBurstStrategy())

    app = TradingBotApp(coins=args.coins, strategies=strategies, headless=args.headless)

    def handle_sigint(sig, frame):
        print("\nSenyal d'aturada rebut. Tancant connexions...")
        app.is_running = False

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        asyncio.run(app.run(duration_sec=args.duration))
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
