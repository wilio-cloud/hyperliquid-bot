"""Orquestrador principal del Bot: Arbitratge Delta-Neutral i Scalper."""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from typing import Dict, List, Optional
import aiohttp
from rich.console import Console
from rich.live import Live

from config.settings import config
from core.arbitrage_models import ArbitrageDirection, ArbitragePosition, ArbitrageSignal
from core.arbitrage_paper_exchange import ArbitragePaperExchange
from core.binance_ws_client import BinanceFuturesWSClient, get_ssl_context
from core.dydx_ws_client import DydxV4WSClient
from core.models import OrderBookL2, OrderSide, Signal, Trade
from core.paper_exchange import PaperExchange
from core.risk_manager import RiskManager
from core.web_server import WebDashboardServer
from core.ws_client import HyperliquidWSClient
from strategies.base_strategy import BaseStrategy
from strategies.cross_arbitrage import CrossExchangeArbitrageStrategy
from strategies.micro_mean_reversion import MicroMeanReversionStrategy
from strategies.orderbook_imbalance import OrderBookImbalanceStrategy
from strategies.spread_scalper import SpreadMarketMakerStrategy
from strategies.volume_burst import VolumeBurstStrategy
from ui.dashboard import generate_arbitrage_dashboard, generate_dashboard

logging.basicConfig(
    filename="trading_bot.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

logger = logging.getLogger("Main")
console = Console()

class ArbitrageTradingBotApp:
    def __init__(
        self,
        coins: List[str],
        venue2: str = "dydx",
        min_spread: float = 0.180,
        exit_spread: float = 0.010,
        size_usd: float = 1000.0,
        headless: bool = False,
    ):
        self.coins = coins
        self.venue2 = venue2.lower()
        self.venue2_label = "dYdX v4" if self.venue2 == "dydx" else "Binance"
        self.size_usd = size_usd
        self.headless = headless
        self.start_time = time.time()

        self.exchange = ArbitragePaperExchange(
            initial_hl_balance=5000.0,
            initial_bn_balance=5000.0,
            venue2_name=self.venue2,
            on_open_cb=self._on_pair_open,
            on_close_cb=self._on_pair_close,
        )
        self.strategy = CrossExchangeArbitrageStrategy(
            min_entry_spread_pct=min_spread,
            target_exit_spread_pct=exit_spread,
        )
        self.hl_ws = HyperliquidWSClient(
            coins=self.coins,
            on_book_update=self.handle_hl_book,
        )
        if self.venue2 == "dydx":
            self.venue2_ws = DydxV4WSClient(
                coins=self.coins,
                on_book_update=self.handle_venue2_book,
                on_funding_update=self.handle_venue2_funding,
            )
        else:
            self.venue2_ws = BinanceFuturesWSClient(
                coins=self.coins,
                on_book_update=self.handle_venue2_book,
                on_funding_update=self.handle_venue2_funding,
            )

        self.web_server = WebDashboardServer(
            exchange=self.exchange,
            start_time=self.start_time,
            app_ref=self,
        )
        self.coin_cooldowns: Dict[str, float] = {}
        self.is_running = False
        self._sync_task: Optional[asyncio.Task] = None
        self._funding_accrual_task: Optional[asyncio.Task] = None

    def _on_pair_open(self, pos: ArbitragePosition):
        if self.headless:
            print(
                f"  ⚡ [ARB OBERT] {pos.coin} {pos.direction.value} | "
                f"HL: {pos.leg_hl.entry_price:.2f} ({pos.leg_hl.side.value}) | "
                f"{pos.leg_bn.venue}: {pos.leg_bn.entry_price:.2f} ({pos.leg_bn.side.value}) | "
                f"Spread: {pos.entry_spread_pct:+.3f}% | Mida: {pos.leg_hl.size_usd:.0f}$ x 2"
            )

    def _on_pair_close(self, pos: ArbitragePosition, exit_reason: str, net_pnl: float):
        # Pausa de seguretat de 3 minuts per evitar bucles d'alta freqüència
        self.coin_cooldowns[pos.coin] = time.time() + 180.0
        if self.headless:
            icon = "✅" if net_pnl > 0 else "❌"
            res_str = "GUANY" if net_pnl > 0 else "PÈRDUA"
            print(
                f"  {icon} [ARB TANCAT {exit_reason}] {pos.coin} | {res_str}: {net_pnl:+.3f}$ | "
                f"Funding: {pos.accumulated_funding:+.4f}$ | Comissions: {pos.total_fees:.4f}$ | "
                f"Balanç Total: {self.exchange.total_balance_usd:.2f}$ (Cooldown 3m)"
            )

    def handle_hl_book(self, book: OrderBookL2):
        self.strategy.update_hl_book(book)
        self.exchange.on_hl_book(book)
        self._check_coin_state(book.coin)

    def handle_venue2_book(self, book: OrderBookL2):
        self.strategy.update_bn_book(book)
        self.exchange.on_bn_book(book)
        self._check_coin_state(book.coin)

    def handle_venue2_funding(self, funding_dict: Dict[str, float]):
        self.strategy.update_bn_funding(funding_dict)

    def _check_coin_state(self, coin: str):
        # 1. Comprova tancaments de posicions obertes
        for pos in list(self.exchange.active_positions.values()):
            if pos.coin == coin:
                exit_eval = self.strategy.check_exit(pos)
                if exit_eval:
                    reason, hl_px, bn_px = exit_eval
                    # En tancaments ordenats (convergència o take profit), apliquem comissió passiva Maker (estalvi del 60% en sortida)
                    is_maker_exit = reason in ("CONVERGENCE_TARGET", "TAKE_PROFIT_TARGET")
                    self.exchange.close_arbitrage_position(
                        pair_id=pos.pair_id,
                        hl_exit_price=hl_px,
                        bn_exit_price=bn_px,
                        reason=reason,
                        is_maker=is_maker_exit,
                    )

        # 2. Comprova cooldown de seguretat
        if time.time() < self.coin_cooldowns.get(coin, 0.0):
            return

        # 3. Avalua noves oportunitats d'entrada si no tenim posició en aquest parell
        if not self.exchange.has_open_position(coin) and len(self.exchange.active_positions) < 3:
            sig = self.strategy.evaluate_entry(coin)
            if sig:
                self.exchange.open_arbitrage_position(sig, size_usd=self.size_usd, is_maker=False)

    async def _run_hl_meta_sync_loop(self):
        """Sincronitza Funding Rates oficials de Hyperliquid cada 30 segons."""
        ssl_ctx = get_ssl_context()
        while self.is_running:
            try:
                connector = aiohttp.TCPConnector(ssl=ssl_ctx)
                async with aiohttp.ClientSession(connector=connector) as session:
                    url = "https://api.hyperliquid.xyz/info"
                    payload = {"type": "metaAndAssetCtxs"}
                    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=6.0)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            universe = data[0]["universe"]
                            asset_ctxs = data[1]
                            for i, meta in enumerate(universe):
                                c = meta["name"]
                                if c in self.coins:
                                    funding_h = float(asset_ctxs[i]["funding"]) * 100.0
                                    self.strategy.update_hl_funding(c, funding_h)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Error sincronitzant meta Hyperliquid: {e}")

            await asyncio.sleep(30.0)

    async def _run_hourly_funding_loop(self):
        """Simula l'acreditació de funding cada 1 hora per a posicions mantingudes."""
        while self.is_running:
            await asyncio.sleep(3600.0)
            for coin in self.coins:
                hl_f = self.strategy.hl_funding_hourly_pct.get(coin, 0.0)
                bn_f = self.strategy.bn_funding_8h_pct.get(coin, 0.0)
                self.exchange.apply_hourly_funding(coin, hl_f, bn_f)

    def get_dashboard_data(self) -> dict:
        spreads = []
        for coin in self.coins:
            info = self.strategy.calculate_spread_info(coin)
            if info:
                spread_val = info.spread_sell_hl_buy_bn_pct if abs(info.spread_sell_hl_buy_bn_pct) >= abs(info.spread_buy_hl_sell_bn_pct) else -info.spread_buy_hl_sell_bn_pct
                spreads.append({
                    "coin": coin,
                    "hl_price": info.hl_mid,
                    "bn_price": info.bn_mid,
                    "spread_pct": spread_val,
                    "hl_funding_8h": info.hl_funding_8h_pct,
                    "bn_funding_8h": info.bn_funding_8h_pct,
                    "annual_funding_diff_apr": info.annual_funding_diff_apr,
                })
        positions = [
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
        recent_closed = [
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
        return {
            "metrics": self.exchange.metrics,
            "spreads": spreads,
            "positions": positions,
            "recent_closed": recent_closed,
        }

    async def run(self, duration_sec: int = 0):
        self.is_running = True
        await self.hl_ws.start()
        await self.venue2_ws.start()
        await self.web_server.start()
        self._sync_task = asyncio.create_task(self._run_hl_meta_sync_loop())
        self._funding_accrual_task = asyncio.create_task(self._run_hourly_funding_loop())

        try:
            if not self.headless:
                with Live(generate_arbitrage_dashboard(self, self.start_time), refresh_per_second=3, console=console) as live:
                    while self.is_running:
                        await asyncio.sleep(0.33)
                        live.update(generate_arbitrage_dashboard(self, self.start_time))
                        if duration_sec > 0 and (time.time() - self.start_time) >= duration_sec:
                            break
            else:
                print(f"Bot d'Arbitratge Delta-Neutral en marxa (Hyperliquid vs {self.venue2_label}) [Headless]. Monedes: {self.coins}...")
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
        for t in (self._sync_task, self._funding_accrual_task):
            if t:
                t.cancel()
        await self.web_server.stop()
        await self.hl_ws.stop()
        await self.venue2_ws.stop()
        self.print_summary()

    def print_summary(self):
        console.print(f"\n[bold cyan]═══ RESUM FINAL SESSIÓ ARBITRATGE DELTA-NEUTRAL (HL vs {self.venue2_label}) ═══[/bold cyan]")
        m = self.exchange.metrics
        pnl = m["net_pnl"]
        pnl_color = "green" if pnl >= 0 else "red"
        v2_name = m.get("venue2_name", "VENUE2")
        console.print(f"Balanç Inicial: [white]{m['initial_balance']:,.2f}$[/white]")
        console.print(f"Balanç Final:   [white]{m['balance']:,.2f}$[/white] (HL: {m['hl_balance']:,.2f}$ | {v2_name}: {m['bn_balance']:,.2f}$)")
        console.print(f"PnL Net Total:  [{pnl_color}]{pnl:+,.3f}$[/]")
        console.print(f"Funding Cobrat: [bold green]{m['total_funding']:+,.4f}$[/bold green]")
        console.print(f"Total Trades:   {m['total_trades']} (Guanyats: {m['wins']}, Perduts: {m['losses']})")
        console.print(f"Winrate:        [bold]{m['winrate_pct']:.1f}%[/bold]")
        console.print(f"Comissions:     [magenta]{m['total_fees']:.4f}$[/magenta]")


class DirectionalScalperApp:
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
        self.exchange.on_book_update(book)

        expired_coins = self.risk_manager.check_time_exits(self.exchange.positions, self.books)
        for coin in expired_coins:
            if coin in self.books and self.books[coin].mid_price:
                self.exchange.close_position(
                    coin=coin,
                    exit_price=self.books[coin].mid_price,
                    exit_reason="TIME_LIMIT",
                    is_maker=True,
                )

        for strat in self.strategies:
            sig = strat.on_book_update(book)
            if sig:
                self._process_signal(sig, book)

    def handle_trade_update(self, trade: Trade):
        self.exchange.on_trade(trade)
        for strat in self.strategies:
            sig = strat.on_trade(trade)
            if sig and trade.coin in self.books:
                self._process_signal(sig, self.books[trade.coin])

    def _process_signal(self, sig: Signal, book: OrderBookL2):
        has_pos = (self.exchange.get_position(sig.coin) is not None) or self.exchange.has_open_orders(sig.coin)
        can_open, reason = self.risk_manager.can_open_position(
            coin=sig.coin,
            current_balance=self.exchange.balance_usd,
            initial_balance=self.exchange.initial_balance,
            open_positions_count=len(self.exchange.positions),
            has_existing_coin_position=has_pos,
        )

        if not can_open:
            return

        side = OrderSide.BUY if sig.action == "BUY" else OrderSide.SELL
        self.exchange.place_order(
            coin=sig.coin,
            side=side,
            price=sig.price,
            size_usd=config.position_size_usd,
            post_only=True,
            strategy_name=sig.strategy_name,
            current_book=book,
            take_profit=sig.take_profit,
            stop_loss=sig.stop_loss,
        )

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
                print(f"Scalper iniciat en mode Headless. Monitoritzant {self.coins}...")
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

def main():
    min_spread_default = float(os.environ.get("MIN_SPREAD", "0.180"))
    exit_spread_default = float(os.environ.get("EXIT_SPREAD", "0.010"))
    venue2_default = os.environ.get("VENUE2", "dydx").lower()
    parser = argparse.ArgumentParser(description="Bot d'Arbitratge Delta-Neutral i Scalping (Hyperliquid + dYdX / Binance)")
    parser.add_argument("--mode", choices=["arbitrage", "scalper"], default="arbitrage", help="Mode d'operació: 'arbitrage' (recomanat) o 'scalper'")
    parser.add_argument("--venue2", choices=["dydx", "binance"], default=venue2_default, help="Segon exchange per a l'arbitratge: 'dydx' (100%% DEX descentralitzat, legal a la UE/Espanya) o 'binance'")
    parser.add_argument("--coins", nargs="+", default=None, help="Monedes a operar (ex: BTC ETH SOL LINK NEAR SUI DOGE)")
    parser.add_argument("--min-spread", type=float, default=min_spread_default, help="Spread mínim percentual d'entrada per a l'arbitratge (default: 0.180%%)")
    parser.add_argument("--exit-spread", type=float, default=exit_spread_default, help="Spread màxim percentual de sortida/convergència (default: 0.010%%)")
    parser.add_argument("--size", type=float, default=1000.0, help="Mida en dòlars per ordre/pota")
    parser.add_argument("--duration", type=int, default=0, help="Durada màxima d'execució en segons (0 = indefinit)")
    parser.add_argument("--headless", action="store_true", help="Executar sense el tauler visual Rich de terminal (recomanat per a Docker/Railway)")
    parser.add_argument("--no-burst", action="store_true", help="Desactivar Volume Burst (només scalper)")
    parser.add_argument("--maker-only", action="store_true", help="Només maker (només scalper)")
    args = parser.parse_args()

    if args.mode == "arbitrage":
        coins = args.coins or ["BTC", "ETH", "SOL", "LINK", "NEAR", "SUI", "DOGE"]
        app = ArbitrageTradingBotApp(
            coins=coins,
            venue2=args.venue2,
            min_spread=args.min_spread,
            exit_spread=args.exit_spread,
            size_usd=args.size,
            headless=args.headless,
        )
    else:
        coins = args.coins or ["BTC"]
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
        app = DirectionalScalperApp(coins=coins, strategies=strategies, headless=args.headless)

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
