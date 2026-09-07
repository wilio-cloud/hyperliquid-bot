"""Orquestrador principal del Bot: Arbitratge Delta-Neutral i Scalper."""

import argparse
import asyncio
import datetime
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
from core.aevo_ws_client import AevoWSClient
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
        venue2: str = "aevo",
        min_spread: float = 0.120,
        weekend_min_spread: float = 0.100,
        min_profit_usd: float = 0.10,
        exit_spread: float = 0.010,
        size_usd: float = 250.0,
        headless: bool = False,
        max_positions: int = 4,
        max_book_spread: float = 0.350,
        initial_balance: float = 1000.0,
        leverage: float = 2.0,
        dynamic_size: bool = True,
        size_pct: float = 25.0,
        min_size_usd: float = 100.0,
        max_size_usd: float = 2500.0,
        state_file: Optional[str] = None,
    ):
        self.coins = coins
        self.venue2 = venue2.lower()
        if self.venue2 == "aevo":
            self.venue2_label = "Aevo DEX"
        elif self.venue2 == "dydx":
            self.venue2_label = "dYdX v4"
        else:
            self.venue2_label = "Binance"

        self.size_usd = size_usd
        self.headless = headless
        self.max_positions = max_positions
        self.leverage = leverage
        self.dynamic_size = dynamic_size
        self.size_pct = size_pct
        self.min_size_usd = min_size_usd
        self.max_size_usd = max_size_usd
        self.start_time = time.time()

        initial_hl = initial_balance / 2.0
        initial_bn = initial_balance / 2.0

        self.exchange = ArbitragePaperExchange(
            initial_hl_balance=initial_hl,
            initial_bn_balance=initial_bn,
            venue2_name=self.venue2,
            leverage=self.leverage,
            on_open_cb=self._on_pair_open,
            on_close_cb=self._on_pair_close,
            state_file=state_file,
        )
        self.strategy = CrossExchangeArbitrageStrategy(
            min_entry_spread_pct=min_spread,
            weekend_min_spread_pct=weekend_min_spread,
            min_profit_usd=min_profit_usd,
            target_exit_spread_pct=exit_spread,
            max_book_spread_pct=max_book_spread,
        )
        self.hl_ws = HyperliquidWSClient(
            coins=self.coins,
            on_book_update=self.handle_hl_book,
        )
        if self.venue2 == "aevo":
            self.venue2_ws = AevoWSClient(
                coins=self.coins,
                on_book_update=self.handle_venue2_book,
                on_funding_update=self.handle_venue2_funding,
            )
        elif self.venue2 == "dydx":
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

    def calculate_order_size(self) -> float:
        """
        Calcula la mida d'ordre dinàmica basada en interès compost.
        Si dynamic_size és True, usa el percentatge 'size_pct' del capital total disponible.
        Exemple: Amb 1.000$ de capital (500$ per pota) i 30% -> 300$ per pota.
        Si el capital creix a 1.500$ -> 450$ per pota.
        """
        if not self.dynamic_size:
            return self.size_usd

        min_bal = min(self.exchange.hl_balance_usd, self.exchange.bn_balance_usd)
        total_sym_equity = min_bal * 2.0
        target = total_sym_equity * (self.size_pct / 100.0)
        return max(self.min_size_usd, min(round(target, 1), self.max_size_usd))

    def _on_pair_open(self, pos: ArbitragePosition):
        if self.headless:
            print(
                f"  ⚡ [ARB OBERT] {pos.coin} {pos.direction.value} | "
                f"HL: {pos.leg_hl.entry_price:.2f} ({pos.leg_hl.side.value}) | "
                f"{pos.leg_bn.venue}: {pos.leg_bn.entry_price:.2f} ({pos.leg_bn.side.value}) | "
                f"Spread: {pos.entry_spread_pct:+.3f}% | Mida: {pos.leg_hl.size_usd:.0f}$ x 2"
            )

    def _on_pair_close(self, pos: ArbitragePosition, exit_reason: str, net_pnl: float):
        # Pausa de seguretat de 90 segons (1.5 minuts) per permetre flux sostingut de 8-10 op/h
        self.coin_cooldowns[pos.coin] = time.time() + 90.0
        if self.headless:
            icon = "✅" if net_pnl > 0 else "❌"
            res_str = "GUANY" if net_pnl > 0 else "PÈRDUA"
            print(
                f"  {icon} [ARB TANCAT {exit_reason}] {pos.coin} | {res_str}: {net_pnl:+.3f}$ | "
                f"Funding: {pos.accumulated_funding:+.4f}$ | Comissions: {pos.total_fees:.4f}$ | "
                f"Balanç Total: {self.exchange.total_balance_usd:.2f}$ (Cooldown 90s)"
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
                    # En tancaments ordenats (convergència, take profit o breakeven per temps), apliquem comissió passiva Maker (estalvi del 60% en sortida)
                    is_maker_exit = reason in ("CONVERGENCE_TARGET", "TAKE_PROFIT_TARGET", "TIME_BREAKEVEN", "TIME_QUICK_PROFIT")
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
        if not self.exchange.has_open_position(coin) and len(self.exchange.active_positions) < self.max_positions:
            sig = self.strategy.evaluate_entry(coin)
            if sig:
                order_sz = self.calculate_order_size()
                self.exchange.open_arbitrage_position(sig, size_usd=order_sz, is_maker=False)

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
                # 1. Spread mid real (% de diferència entre el preu mig de HL i el segon exchange)
                global_mid = (info.hl_mid + info.bn_mid) / 2.0
                mid_spread_pct = ((info.hl_mid - info.bn_mid) / global_mid) * 100.0 if global_mid > 0 else 0.0

                # 2. Spread executable net real (el màxim guany executable entre les dues direccions)
                best_exec_pct = max(info.spread_sell_hl_buy_bn_pct, info.spread_buy_hl_sell_bn_pct)

                # 3. Comprovació de llibre i senyal real
                hl_book = self.strategy.hl_books.get(coin)
                bn_book = self.strategy.bn_books.get(coin)
                hl_inner = ((hl_book.best_ask - hl_book.best_bid) / hl_book.mid_price) * 100.0 if (hl_book and hl_book.mid_price) else 0.0
                bn_inner = ((bn_book.best_ask - bn_book.best_bid) / bn_book.mid_price) * 100.0 if (bn_book and bn_book.mid_price) else 0.0
                max_inner = max(hl_inner, bn_inner)

                coin_eff_spread = self.strategy.get_effective_min_spread(coin)
                sig = self.strategy.evaluate_entry(coin)
                if sig:
                    signal_type = "SIGNAL"
                    dir_str = "SELL HL / BUY " if sig.direction == ArbitrageDirection.SELL_HL_BUY_BN else "BUY HL / SELL "
                    signal_status = f"🔥 {dir_str}{self.venue2_label}"
                elif max_inner > self.strategy.max_book_spread_pct:
                    signal_type = "BLOCKED"
                    signal_status = f"⚠️ LLIBRE AMPLI ({max_inner:.2f}%)"
                elif best_exec_pct >= (coin_eff_spread * 0.65):
                    signal_type = "APROP"
                    signal_status = f"⏳ APROP ({best_exec_pct:.3f}%)"
                elif abs(info.annual_funding_diff_apr) >= 15.0:
                    signal_type = "HARVEST"
                    signal_status = "💰 HARVEST APR"
                else:
                    signal_type = "NORMAL"
                    signal_status = "NORMAL"

                spreads.append({
                    "coin": coin,
                    "hl_price": info.hl_mid,
                    "bn_price": info.bn_mid,
                    "spread_pct": mid_spread_pct,
                    "exec_spread_pct": best_exec_pct,
                    "signal_status": signal_status,
                    "signal_type": signal_type,
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
            for p in self.exchange.closed_positions[-35:]
        ]
        metrics = self.exchange.metrics.copy()
        current_size = self.calculate_order_size()
        metrics["dynamic_size"] = self.dynamic_size
        metrics["current_order_size"] = current_size
        metrics["size_pct"] = self.size_pct
        metrics["max_positions"] = self.max_positions
        is_wk = self.strategy.is_weekend_regime
        eff_spread = self.strategy.effective_min_spread
        metrics["is_weekend"] = is_wk
        metrics["min_spread"] = eff_spread
        metrics["weekday_min_spread"] = self.strategy.min_entry_spread_pct
        metrics["weekend_min_spread"] = self.strategy.weekend_min_spread_pct
        metrics["regime_label"] = f"🗓️ CAP DE SETMANA ({eff_spread:.3f}%)" if is_wk else f"⚡ SETMANAL ({eff_spread:.3f}%)"
        metrics["max_book_spread"] = self.strategy.max_book_spread_pct

        # Càlcul del ritme horari global i mètriques detallades per actiu (Coin Analytics)
        now = time.time()
        uptime_sec = max(now - self.start_time, 60.0)

        # Obtenim els trades recents d'aquesta sessió (darreres 24 hores i època actual)
        valid_trades = [
            p for p in self.exchange.closed_positions
            if p.entry_time and (now - p.entry_time) <= 86400 and p.entry_time > 1780000000
        ]
        if valid_trades:
            first_trade_t = min(p.entry_time for p in valid_trades)
            elapsed_sec = max(uptime_sec, now - first_trade_t)
        else:
            elapsed_sec = uptime_sec

        # Mínim de 15 minuts (0.25h) per evitar divisions anòmales a l'inici
        elapsed_hours = max(elapsed_sec / 3600.0, 0.25)
        total_closed = len(self.exchange.closed_positions)
        trades_per_hour = round(total_closed / elapsed_hours, 1)
        metrics["trades_per_hour"] = trades_per_hour
        metrics["target_trades_per_hour"] = "8-10"

        realized_pnl_total = sum(p.realized_pnl for p in self.exchange.closed_positions)
        avg_trade_pnl = (realized_pnl_total / total_closed) if total_closed > 0 else 0.12
        live_pace = trades_per_hour if trades_per_hour > 0 else 8.0
        cur_hourly_rate = live_pace * avg_trade_pnl
        bal = self.exchange.total_balance_usd

        # Model de projeccions i simulador d'escenaris
        scenarios_dict = {
            "current_measured": {
                "id": "current_measured",
                "label": "Mesurat Real en Viu",
                "pace_h": live_pace,
                "avg_profit": round(avg_trade_pnl, 3),
                "hourly_rate": round(cur_hourly_rate, 3),
                "day_profit": round(cur_hourly_rate * 24, 2),
                "week_profit": round(cur_hourly_rate * 24 * 7, 2),
                "month_profit": round(cur_hourly_rate * 24 * 30, 2),
                "month_roi_pct": round((cur_hourly_rate * 24 * 30 / bal) * 100.0, 1),
            },
            "expected": {
                "id": "expected",
                "label": "Escenari Objectiu (8-10 op/h)",
                "pace_h": 9.0,
                "avg_profit": 0.12,
                "hourly_rate": 1.08,
                "day_profit": 25.92,
                "week_profit": 181.44,
                "month_profit": 777.60,
                "month_roi_pct": round((777.60 / bal) * 100.0, 1),
            },
            "conservative": {
                "id": "conservative",
                "label": "Escenari Conservador (5 op/h)",
                "pace_h": 5.0,
                "avg_profit": 0.08,
                "hourly_rate": 0.40,
                "day_profit": 9.60,
                "week_profit": 67.20,
                "month_profit": 288.00,
                "month_roi_pct": round((288.00 / bal) * 100.0, 1),
            },
            "ny_volatility": {
                "id": "ny_volatility",
                "label": "Obertura NY / Volatilitat (12-15 op/h)",
                "pace_h": 14.0,
                "avg_profit": 0.15,
                "hourly_rate": 2.10,
                "day_profit": 50.40,
                "week_profit": 352.80,
                "month_profit": 1512.00,
                "month_roi_pct": round((1512.00 / bal) * 100.0, 1),
            },
        }

        projections = {
            "avg_pnl_per_trade": round(avg_trade_pnl, 4),
            "current_pace_oph": live_pace,
            "scenarios": scenarios_dict,
            **scenarios_dict,
        }

        # Monitor de sessió d'obertura de Nova York (Wall Street / CME: 13:30 - 20:00 UTC)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        utc_min = now_utc.hour * 60 + now_utc.minute
        is_ny_active = 810 <= utc_min < 1200
        is_ny_premarket = 720 <= utc_min < 810
        mins_to_open = max(0, 810 - utc_min) if utc_min < 810 else 0

        ny_session = {
            "is_active": is_ny_active,
            "is_premarket": is_ny_premarket,
            "minutes_to_open": mins_to_open,
            "status_text": "🟢 SESSIÓ NY ACTIVA (Màxim Volum)" if is_ny_active else (
                f"⏳ PRE-MARKET NY (Obre en {mins_to_open} min)" if is_ny_premarket else "⏸️ FORA DE SESSIÓ NY"
            ),
            "volatility_boost_factor": 1.75 if is_ny_active else (1.30 if is_ny_premarket else 1.0),
        }

        coin_stats = []
        for coin in self.coins:
            coin_trades = [p for p in self.exchange.closed_positions if p.coin == coin]
            c_count = len(coin_trades)
            c_wins = len([p for p in coin_trades if p.realized_pnl > 0])
            c_losses = len([p for p in coin_trades if p.realized_pnl <= 0])
            c_winrate = (c_wins / c_count * 100.0) if c_count > 0 else 0.0
            c_pnl = sum(p.realized_pnl for p in coin_trades)
            c_avg_pnl = (c_pnl / c_count) if c_count > 0 else 0.0
            c_pace = round(c_count / elapsed_hours, 1)

            durations = [
                max(0.0, (p.exit_time - p.entry_time))
                for p in coin_trades
                if p.exit_time and p.entry_time and (0 < (p.exit_time - p.entry_time) < 86400)
            ]
            avg_dur_sec = (sum(durations) / len(durations)) if durations else 0.0
            avg_dur_min = round(avg_dur_sec / 60.0, 1)

            has_active = self.exchange.has_open_position(coin)

            # Diagnòstic de l'actiu per a calibració
            if has_active:
                diag = "⚡ Posició Oberta"
                diag_type = "ACTIVE"
            elif c_count >= 3 and c_winrate >= 90.0:
                diag = "🔥 Òptim (Freqüent)"
                diag_type = "OPTIMAL"
            elif c_count > 0:
                diag = "✅ Operant Bé"
                diag_type = "GOOD"
            else:
                hl_book = self.strategy.hl_books.get(coin)
                bn_book = self.strategy.bn_books.get(coin)
                hl_inner = ((hl_book.best_ask - hl_book.best_bid) / hl_book.mid_price) * 100.0 if (hl_book and hl_book.mid_price) else 0.0
                bn_inner = ((bn_book.best_ask - bn_book.best_bid) / bn_book.mid_price) * 100.0 if (bn_book and bn_book.mid_price) else 0.0
                max_inner = max(hl_inner, bn_inner)
                info = self.strategy.calculate_spread_info(coin)
                best_exec_pct = max(info.spread_sell_hl_buy_bn_pct, info.spread_buy_hl_sell_bn_pct) if info else 0.0

                coin_eff_spread = self.strategy.get_effective_min_spread(coin)
                if max_inner > self.strategy.max_book_spread_pct:
                    diag = "⚠️ Llibre Ampli (Filtre)"
                    diag_type = "PROTECTED"
                elif best_exec_pct >= (coin_eff_spread * 0.7):
                    diag = "⏳ A prop del llindar"
                    diag_type = "NEAR"
                elif coin == "BTC":
                    diag = "💎 Spread Estret (<0.03%)"
                    diag_type = "TIGHT"
                else:
                    diag = "💤 Poca Dislocació"
                    diag_type = "QUIET"

            coin_stats.append({
                "coin": coin,
                "trades_count": c_count,
                "trades_per_hour": c_pace,
                "wins": c_wins,
                "losses": c_losses,
                "winrate_pct": c_winrate,
                "realized_pnl": c_pnl,
                "avg_pnl": c_avg_pnl,
                "avg_duration_sec": avg_dur_sec,
                "avg_duration_min": avg_dur_min,
                "has_active": has_active,
                "diagnostic": diag,
                "diag_type": diag_type,
            })

        return {
            "metrics": metrics,
            "spreads": spreads,
            "positions": positions,
            "recent_closed": recent_closed,
            "coin_stats": coin_stats,
            "projections": projections,
            "ny_session": ny_session,
            "equity_history": getattr(self.exchange, "equity_history", []),
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
    min_spread_default = float(os.environ.get("MIN_SPREAD", "0.120"))
    weekend_min_spread_default = float(os.environ.get("WEEKEND_MIN_SPREAD", "0.100"))
    min_profit_default = float(os.environ.get("MIN_PROFIT", "0.10"))
    exit_spread_default = float(os.environ.get("EXIT_SPREAD", "0.010"))
    max_positions_default = int(os.environ.get("MAX_POSITIONS", "4"))
    max_book_spread_default = float(os.environ.get("MAX_BOOK_SPREAD", "0.350"))
    venue2_default = os.environ.get("VENUE2", "aevo").lower()
    initial_balance_default = float(os.environ.get("INITIAL_BALANCE", "1000.0"))
    leverage_default = float(os.environ.get("LEVERAGE", "2.0"))
    dynamic_size_default = os.environ.get("DYNAMIC_SIZE", "true").lower() in ("true", "1", "yes")
    size_pct_default = float(os.environ.get("SIZE_PCT", "25.0"))
    size_default = float(os.environ.get("SIZE", "250.0"))
    min_size_default = float(os.environ.get("MIN_SIZE", "100.0"))
    max_size_default = float(os.environ.get("MAX_SIZE", "2500.0"))

    parser = argparse.ArgumentParser(description="Bot d'Arbitratge Delta-Neutral i Scalping (Hyperliquid + Aevo / dYdX / Binance)")
    parser.add_argument("--mode", choices=["arbitrage", "scalper"], default="arbitrage", help="Mode d'operació: 'arbitrage' (recomanat) o 'scalper'")
    parser.add_argument("--venue2", choices=["aevo", "dydx", "binance"], default=venue2_default, help="Segon exchange per a l'arbitratge: 'aevo' (100%% DEX d'alta freqüència i liquiditat d'altcoins), 'dydx' o 'binance'")
    parser.add_argument("--coins", nargs="+", default=None, help="Monedes a operar (ex: BTC ETH SOL)")
    parser.add_argument("--initial-balance", type=float, default=initial_balance_default, help="Capital inicial total en dòlars (default: 1000.0$)")
    parser.add_argument("--leverage", type=float, default=leverage_default, help="Apalancament conservador per a l'arbitratge (default: 2.0x)")
    parser.add_argument("--size", type=float, default=size_default, help="Mida en dòlars per ordre/pota (default: 250.0$)")
    parser.add_argument("--dynamic-size", dest="dynamic_size", action="store_true", default=dynamic_size_default, help="Ajustar automàticament la mida per interès compost (default: True)")
    parser.add_argument("--no-dynamic-size", dest="dynamic_size", action="store_false", help="Desactivar mida dinàmica i utilitzar mida fixa")
    parser.add_argument("--size-pct", type=float, default=size_pct_default, help="Percentatge del capital total per a cada ordre (default: 25.0%%)")
    parser.add_argument("--min-size", type=float, default=min_size_default, help="Mida mínima d'ordre en dòlars (default: 100.0$)")
    parser.add_argument("--max-size", type=float, default=max_size_default, help="Límit màxim de mida per seguretat de llibre (default: 2500.0$)")
    parser.add_argument("--min-spread", type=float, default=min_spread_default, help="Spread mínim percentual d'entrada entre setmana (default: 0.120%%)")
    parser.add_argument("--weekend-min-spread", type=float, default=weekend_min_spread_default, help="Spread mínim percentual d'entrada en cap de setmana (default: 0.100%%)")
    parser.add_argument("--min-profit", type=float, default=min_profit_default, help="Benefici net mínim permès per trade tancat (default: 0.10$)")
    parser.add_argument("--exit-spread", type=float, default=exit_spread_default, help="Spread màxim percentual de sortida/convergència (default: 0.010%%)")
    parser.add_argument("--max-positions", type=int, default=max_positions_default, help="Nombre màxim de posicions simultànies (default: 4)")
    parser.add_argument("--max-book-spread", type=float, default=max_book_spread_default, help="Spread intern màxim del llibre de l'exchange per admetre entrada (default: 0.350%%)")
    parser.add_argument("--duration", type=int, default=0, help="Durada màxima d'execució en segons (0 = indefinit)")
    parser.add_argument("--headless", action="store_true", help="Executar sense el tauler visual Rich de terminal (recomanat per a Docker/Railway)")
    parser.add_argument("--no-burst", action="store_true", help="Desactivar Volume Burst (només scalper)")
    parser.add_argument("--maker-only", action="store_true", help="Només maker (només scalper)")
    args = parser.parse_args()

    if args.mode == "arbitrage":
        if args.venue2 == "dydx":
            default_coins = ["BTC", "ETH", "SOL"]
        elif args.venue2 == "aevo":
            default_coins = ["SOL", "HYPE", "NEAR", "PUMP", "SUI", "DOGE"]
        else:
            default_coins = ["BTC", "ETH", "SOL", "LINK", "NEAR", "SUI", "DOGE"]
        coins = args.coins or default_coins
        app = ArbitrageTradingBotApp(
            coins=coins,
            venue2=args.venue2,
            min_spread=args.min_spread,
            weekend_min_spread=args.weekend_min_spread,
            min_profit_usd=args.min_profit,
            exit_spread=args.exit_spread,
            size_usd=args.size,
            headless=args.headless,
            max_positions=args.max_positions,
            max_book_spread=args.max_book_spread,
            initial_balance=args.initial_balance,
            leverage=args.leverage,
            dynamic_size=args.dynamic_size,
            size_pct=args.size_pct,
            min_size_usd=args.min_size,
            max_size_usd=args.max_size,
            state_file=os.getenv("STATE_FILE", "paper_state.json"),
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
