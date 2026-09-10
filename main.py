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
from core.arbitrage_live_exchange import ArbitrageLiveExchange
from core.arbitrage_models import ArbitrageDirection, ArbitragePosition, ArbitrageSignal
from core.arbitrage_paper_exchange import ArbitragePaperExchange
from core.hyperliquid_live_client import HyperliquidLiveClient
from core.aevo_live_client import AevoLiveClient
from core.dydx_live_client import DydxLiveClient
from core.okx_live_client import OkxLiveClient
from core.binance_ws_client import BinanceFuturesWSClient, get_ssl_context
from core.dydx_ws_client import DydxV4WSClient
from core.okx_ws_client import OkxWSClient
from core.vertex_ws_client import VertexWSClient
from core.models import OrderBookL2, OrderSide, Signal, Trade
from core.paper_exchange import PaperExchange
from core.risk_manager import RiskManager
from core.web_server import WebDashboardServer
from core.ws_client import HyperliquidWSClient
from strategies.base_strategy import BaseStrategy
from strategies.cross_arbitrage import CrossExchangeArbitrageStrategy
from strategies.funding_carry import FundingCarryStrategy
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
        min_spread: float = 0.280,
        weekend_min_spread: float = 0.240,
        min_profit_usd: float = 0.25,
        exit_spread: float = 0.010,
        size_usd: float = 150.0,
        headless: bool = False,
        max_positions: int = 6,
        max_book_spread: Optional[float] = None,
        initial_balance: float = 1000.0,
        leverage: float = 3.0,
        dynamic_size: bool = True,
        size_pct: float = 16.6,
        min_size_usd: float = 100.0,
        max_size_usd: float = 2500.0,
        state_file: Optional[str] = None,
        execution_mode: str = "paper",
        maker_first: bool = False,
        port: int = 8080,
        strategy_mode: str = "arbitrage",
        enable_carry: bool = False,
        min_carry_apr: float = 16.0,
        min_exit_apr: float = 4.0,
        carry_slots: int = 2,
    ):
        self.coins = coins
        self.port = port
        self.strategy_mode = strategy_mode.lower()
        self.enable_carry = enable_carry or (self.strategy_mode == "funding_carry")
        self.min_carry_apr = min_carry_apr
        self.min_exit_apr = min_exit_apr
        self.carry_slots = carry_slots
        self.venue2 = venue2.lower()
        self.maker_first = maker_first
        if self.venue2 == "aevo":
            self.venue2_label = "Aevo DEX"
        elif self.venue2 == "dydx":
            self.venue2_label = "dYdX v4"
        elif self.venue2 == "okx":
            self.venue2_label = "OKX Perpetuals (USDT-M)"
        elif self.venue2 == "vertex":
            self.venue2_label = "Vertex Protocol (0% Maker)"
        else:
            self.venue2_label = "Binance"

        if max_book_spread is None:
            env_book_spread = os.environ.get("MAX_BOOK_SPREAD")
            if env_book_spread:
                max_book_spread = float(env_book_spread)
            else:
                max_book_spread = 1.200 if self.venue2 == "dydx" else 0.220

        self.size_usd = size_usd
        self.headless = headless
        self.max_positions = max_positions
        self.leverage = leverage
        self.dynamic_size = dynamic_size
        self.size_pct = size_pct
        self.min_size_usd = min_size_usd
        self.max_size_usd = max_size_usd
        self.start_time = time.time()
        self.execution_mode = execution_mode.lower()

        initial_hl = initial_balance / 2.0
        initial_bn = initial_balance / 2.0

        if self.execution_mode == "live":
            wallet_address = os.getenv("WALLET_ADDRESS", "").strip()
            hl_agent_key = os.getenv("HL_AGENT_PRIVATE_KEY", "").strip()
            hl_ref_code = os.getenv("HL_REFERRAL_CODE", config.hl_referral_code).strip()
            hl_testnet = os.getenv("HL_TESTNET", "false").lower() == "true"

            missing = []
            if not wallet_address:
                missing.append("WALLET_ADDRESS")
            if not hl_agent_key:
                missing.append("HL_AGENT_PRIVATE_KEY")

            venue2_client = None
            v2 = self.venue2.lower()

            if v2 == "dydx":
                dydx_address = os.getenv("DYDX_ADDRESS", "").strip()
                dydx_mnemonic = os.getenv("DYDX_MNEMONIC", "").strip()
                dydx_private_key = (os.getenv("DYDX_PRIVATE_KEY") or os.getenv("DYDX_PRIVATE") or "").strip()
                dydx_node_url = os.getenv("DYDX_NODE_URL", "dydx-grpc.publicnode.com:443").strip()
                dydx_env = os.getenv("DYDX_ENV", "mainnet").strip()

                if not (dydx_mnemonic or dydx_private_key):
                    missing.append("DYDX_MNEMONIC (o DYDX_PRIVATE_KEY)")

                if missing:
                    err_msg = f"Falten credencials per al mode LIVE amb dYdX: {', '.join(missing)}"
                    logger.error(err_msg)
                    raise ValueError(err_msg)

                venue2_client = DydxLiveClient(
                    address=dydx_address or None,
                    mnemonic=dydx_mnemonic or None,
                    private_key=dydx_private_key or None,
                    node_url=dydx_node_url,
                    env=dydx_env,
                )
            elif v2 == "okx":
                okx_key = os.getenv("OKX_API_KEY", "").strip()
                okx_secret = os.getenv("OKX_API_SECRET", "").strip()
                okx_passphrase = os.getenv("OKX_PASSPHRASE", "").strip()
                okx_is_demo = os.getenv("OKX_IS_DEMO", "false").lower() in ("true", "1", "yes")

                if not okx_key:
                    missing.append("OKX_API_KEY")
                if not okx_secret:
                    missing.append("OKX_API_SECRET")
                if not okx_passphrase:
                    missing.append("OKX_PASSPHRASE")

                if missing:
                    err_msg = f"Falten credencials per al mode LIVE amb OKX: {', '.join(missing)}"
                    logger.error(err_msg)
                    raise ValueError(err_msg)

                venue2_client = OkxLiveClient(
                    api_key=okx_key,
                    api_secret=okx_secret,
                    passphrase=okx_passphrase,
                    is_demo=okx_is_demo,
                )
            else:
                aevo_key = os.getenv("AEVO_API_KEY", "").strip()
                aevo_secret = os.getenv("AEVO_API_SECRET", "").strip()
                aevo_signing_key = os.getenv("AEVO_SIGNING_KEY", "").strip()
                aevo_env = os.getenv("AEVO_ENV", "mainnet").strip()

                if not aevo_key:
                    missing.append("AEVO_API_KEY")
                if not aevo_secret:
                    missing.append("AEVO_API_SECRET")
                if not aevo_signing_key:
                    missing.append("AEVO_SIGNING_KEY")

                if missing:
                    err_msg = f"Falten credencials per al mode LIVE amb Aevo: {', '.join(missing)}"
                    logger.error(err_msg)
                    raise ValueError(err_msg)

                venue2_client = AevoLiveClient(
                    wallet_address=wallet_address,
                    api_key=aevo_key,
                    api_secret=aevo_secret,
                    signing_key=aevo_signing_key,
                    env=aevo_env,
                )

            hl_client = HyperliquidLiveClient(
                wallet_address=wallet_address,
                agent_private_key=hl_agent_key,
                testnet=hl_testnet,
                referral_code=hl_ref_code or None,
            )
            self.exchange = ArbitrageLiveExchange(
                hl_client=hl_client,
                venue2_client=venue2_client,
                venue2_name=self.venue2.upper(),
                initial_hl_balance=initial_hl,
                initial_bn_balance=initial_bn,
                leverage=self.leverage,
                maker_first=self.maker_first,
                on_open_cb=self._on_pair_open,
                on_close_cb=self._on_pair_close,
                state_file=state_file or "live_state.json",
            )
            logger.info(
                f"⚡ [MODE REAL ACTIVAT] ArbitrageLiveExchange inicialitzat "
                f"(Hyperliquid + {self.venue2_label}, MakerFirst={self.maker_first})."
            )
        else:
            self.exchange = ArbitragePaperExchange(
                initial_hl_balance=initial_hl,
                initial_bn_balance=initial_bn,
                venue2_name=self.venue2,
                leverage=self.leverage,
                maker_first=self.maker_first,
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
            maker_first=self.maker_first,
            venue2_name=self.venue2.upper(),
        )
        self.carry_strategy = FundingCarryStrategy(
            min_entry_apr=self.min_carry_apr,
            min_exit_apr=self.min_exit_apr,
            min_entry_spread_pct=-0.030,         # Permetre petit cost de base a l'entrada
            windfall_take_profit_pct=0.60,        # TP per guany de base extraordinari
            max_basis_divergence_pct=2.00,        # SL per divergència de base
            divergence_min_duration_sec=180.0,    # 3 min sostingut
            min_holding_hours=1.0,                # Mínim 1h abans de tancar per compressió
            max_book_spread_pct=max_book_spread,
            maker_first=True,                     # SEMPRE Maker a HL per carry
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
        elif self.venue2 == "okx":
            self.venue2_ws = OkxWSClient(
                coins=self.coins,
                on_book_update=self.handle_venue2_book,
                on_funding_update=self.handle_venue2_funding,
                is_demo=os.getenv("OKX_IS_DEMO", "false").lower() in ("true", "1", "yes"),
            )
        elif self.venue2 == "vertex":
            self.venue2_ws = VertexWSClient(
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
            port=self.port,
            app_ref=self,
        )
        self.coin_cooldowns: Dict[str, float] = {}
        self.is_running = False
        self._sync_task: Optional[asyncio.Task] = None
        self._funding_accrual_task: Optional[asyncio.Task] = None
        self._balance_sync_task: Optional[asyncio.Task] = None

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
        # Cooldown adaptatiu: 1 hora per carry (evitar re-entrades en funding comprimit), 90s per scalp
        cooldown_sec = 3600.0 if getattr(pos, "strategy_type", "SPREAD_SCALP") == "FUNDING_CARRY" else 90.0
        self.coin_cooldowns[pos.coin] = time.time() + cooldown_sec
        if self.headless:
            icon = "✅" if net_pnl > 0 else "❌"
            res_str = "GUANY" if net_pnl > 0 else "PÈRDUA"
            cd_label = f"{cooldown_sec/60:.0f}min"
            print(
                f"  {icon} [ARB TANCAT {exit_reason}] {pos.coin} | {res_str}: {net_pnl:+.3f}$ | "
                f"Funding: {pos.accumulated_funding:+.4f}$ | Comissions: {pos.total_fees:.4f}$ | "
                f"Balanç Total: {self.exchange.total_balance_usd:.2f}$ (Cooldown {cd_label})"
            )

    def handle_hl_book(self, book: OrderBookL2):
        self.strategy.update_hl_book(book)
        self.carry_strategy.update_hl_book(book)
        self.exchange.on_hl_book(book)
        self._check_coin_state(book.coin)

    def handle_venue2_book(self, book: OrderBookL2):
        self.strategy.update_bn_book(book)
        self.carry_strategy.update_bn_book(book)
        self.exchange.on_bn_book(book)
        self._check_coin_state(book.coin)

    def handle_venue2_funding(self, funding_dict: Dict[str, float]):
        self.strategy.update_bn_funding(funding_dict)
        self.carry_strategy.update_bn_funding(funding_dict)

    def _check_coin_state(self, coin: str):
        # 1. Comprova tancaments de posicions obertes
        for pos in list(self.exchange.active_positions.values()):
            if pos.coin == coin:
                if getattr(pos, "strategy_type", "SPREAD_SCALP") == "FUNDING_CARRY":
                    exit_eval = self.carry_strategy.check_exit(pos)
                else:
                    exit_eval = self.strategy.check_exit(pos)
                if exit_eval:
                    reason, hl_px, bn_px = exit_eval
                    # En tancaments reals, per seguretat atòmica d'execució i evitar resting orders penjades,
                    # les sortides s'executen per Taker IOC (reduce_only=True). Apliquem comissió Taker ultra-realista (is_maker=False).
                    self.exchange.close_arbitrage_position(
                        pair_id=pos.pair_id,
                        hl_exit_price=hl_px,
                        bn_exit_price=bn_px,
                        reason=reason,
                        is_maker=False,
                    )

        # 2. Comprova cooldown de seguretat
        if time.time() < self.coin_cooldowns.get(coin, 0.0):
            return

        # 3. Avalua noves oportunitats d'entrada si no tenim posició en aquest parell
        if not self.exchange.has_open_position(coin) and len(self.exchange.active_positions) < self.max_positions:
            # Filtre EEA: saltar monedes sense instrument XPERP a OKX
            if hasattr(self.exchange, 'aevo_client') and hasattr(self.exchange.aevo_client, 'is_tradeable'):
                if not self.exchange.aevo_client.is_tradeable(coin):
                    return
            sig = None
            if self.strategy_mode == "funding_carry":
                sig = self.carry_strategy.evaluate_entry(coin)
            else:
                sig = self.strategy.evaluate_entry(coin)
                if not sig and self.enable_carry:
                    carry_count = sum(
                        1 for p in self.exchange.active_positions.values()
                        if getattr(p, "strategy_type", "SPREAD_SCALP") == "FUNDING_CARRY"
                    )
                    if carry_count < self.carry_slots:
                        sig = self.carry_strategy.evaluate_entry(coin)

            if sig:
                order_sz = self.calculate_order_size()
                # Per carry, forçar Maker entry per minimitzar comissions
                use_maker = sig.strategy_type == "FUNDING_CARRY"
                self.exchange.open_arbitrage_position(sig, size_usd=order_sz, is_maker=use_maker)

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
                                raw_c = meta["name"]
                                c = "PEPE" if raw_c == "kPEPE" else raw_c
                                if c in self.coins:
                                    funding_h = float(asset_ctxs[i]["funding"]) * 100.0
                                    self.strategy.update_hl_funding(c, funding_h)
                                    self.carry_strategy.update_hl_funding(c, funding_h)
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
                "strategy_type": getattr(p, "strategy_type", "SPREAD_SCALP"),
                "exit_diagnostic": (
                    self.carry_strategy.get_exit_diagnostic(p)
                    if getattr(p, "strategy_type", "SPREAD_SCALP") == "FUNDING_CARRY"
                    else self.strategy.get_exit_diagnostic(p)
                ),
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
                "strategy_type": getattr(p, "strategy_type", "SPREAD_SCALP"),
            }
            for p in self.exchange.closed_positions[-35:]
        ]
        metrics = self.exchange.metrics.copy()
        metrics["execution_mode"] = self.execution_mode
        metrics["maker_first"] = self.maker_first
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
        if self.maker_first:
            reg_label = f"⚡ MAKER-FIRST ({eff_spread:.3f}%)"
        elif is_wk:
            reg_label = f"🗓️ CAP DE SETMANA ({eff_spread:.3f}%)"
        else:
            reg_label = f"💎 RENDIBILITAT REAL ({eff_spread:.3f}%)"
        metrics["regime_label"] = reg_label
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
        metrics["target_trades_per_hour"] = "4-6"

        realized_pnl_total = sum(p.realized_pnl for p in self.exchange.closed_positions)
        avg_trade_pnl = (realized_pnl_total / total_closed) if total_closed > 0 else 0.25
        live_pace = trades_per_hour if trades_per_hour > 0 else 4.0
        cur_hourly_rate = live_pace * avg_trade_pnl
        bal = self.exchange.total_balance_usd

        # Model de projeccions i simulador d'escenaris (Nova Realitat: spreads >=0.28%, ~0.25$/trade net)
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
                "label": "Escenari Objectiu (4-6 op/h)",
                "pace_h": 4.5,
                "avg_profit": 0.25,
                "hourly_rate": round(4.5 * 0.25, 2),
                "day_profit": round(4.5 * 0.25 * 24, 2),
                "week_profit": round(4.5 * 0.25 * 24 * 7, 2),
                "month_profit": round(4.5 * 0.25 * 24 * 30, 2),
                "month_roi_pct": round((4.5 * 0.25 * 24 * 30 / bal) * 100.0, 1),
            },
            "conservative": {
                "id": "conservative",
                "label": "Escenari Conservador (2-3 op/h)",
                "pace_h": 2.5,
                "avg_profit": 0.20,
                "hourly_rate": round(2.5 * 0.20, 2),
                "day_profit": round(2.5 * 0.20 * 24, 2),
                "week_profit": round(2.5 * 0.20 * 24 * 7, 2),
                "month_profit": round(2.5 * 0.20 * 24 * 30, 2),
                "month_roi_pct": round((2.5 * 0.20 * 24 * 30 / bal) * 100.0, 1),
            },
            "ny_volatility": {
                "id": "ny_volatility",
                "label": "Obertura NY / Volatilitat (7-9 op/h)",
                "pace_h": 7.5,
                "avg_profit": 0.30,
                "hourly_rate": round(7.5 * 0.30, 2),
                "day_profit": round(7.5 * 0.30 * 24, 2),
                "week_profit": round(7.5 * 0.30 * 24 * 7, 2),
                "month_profit": round(7.5 * 0.30 * 24 * 30, 2),
                "month_roi_pct": round((7.5 * 0.30 * 24 * 30 / bal) * 100.0, 1),
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

    async def _run_balance_sync_loop(self):
        """Sincronitza els saldos reals periòdicament cada 60s en mode LIVE."""
        while self.is_running:
            await asyncio.sleep(60.0)
            if isinstance(self.exchange, ArbitrageLiveExchange):
                await self.exchange.sync_real_balances()

    async def run(self, duration_sec: int = 0):
        self.is_running = True
        # Iniciar el servidor web immediatament per respondre a l'instant al healthcheck de Railway
        await self.web_server.start()

        if isinstance(self.exchange, ArbitrageLiveExchange):
            logger.info("⚡ [MODE REAL] Verificant i sincronitzant saldos reals amb la blockchain...")
            await self.exchange.sync_real_balances()
            logger.info(f"⚡ [MODE REAL] Configurant palanquejament a {int(self.leverage)}x a Hyperliquid i {self.venue2_label}...")
            try:
                await self.exchange.configure_all_leverage(leverage=int(self.leverage), coins=self.coins)
            except Exception as e:
                logger.error(f"Error configurant palanquejament inicial: {e}")
            self._balance_sync_task = asyncio.create_task(self._run_balance_sync_loop())

        await self.hl_ws.start()
        await self.venue2_ws.start()
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
        for t in (self._sync_task, self._funding_accrual_task, self._balance_sync_task):
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
    min_spread_default = float(os.environ.get("MIN_SPREAD", "0.280"))
    weekend_min_spread_default = float(os.environ.get("WEEKEND_MIN_SPREAD", "0.240"))
    min_profit_default = float(os.environ.get("MIN_PROFIT", "0.25"))
    exit_spread_default = float(os.environ.get("EXIT_SPREAD", "0.010"))
    max_positions_default = int(os.environ.get("MAX_POSITIONS", "6"))
    venue2_default = os.environ.get("VENUE2", "aevo").lower()
    default_book_spread = "1.200" if venue2_default == "dydx" else "0.220"
    max_book_spread_default = float(os.environ.get("MAX_BOOK_SPREAD", default_book_spread))
    initial_balance_default = float(os.environ.get("INITIAL_BALANCE", "1000.0"))
    env_lev = os.environ.get("LEVERAGE")
    leverage_default = float(env_lev) if (env_lev and env_lev != "2.0") else 3.0
    dynamic_size_default = os.environ.get("DYNAMIC_SIZE", "true").lower() in ("true", "1", "yes")
    env_sz_pct = os.environ.get("SIZE_PCT")
    size_pct_default = float(env_sz_pct) if (env_sz_pct and env_sz_pct != "30") else 16.6
    size_default = float(os.environ.get("SIZE", "150.0"))
    env_min_sz = os.environ.get("MIN_SIZE")
    min_size_default = float(env_min_sz) if (env_min_sz and env_min_sz != "30") else 100.0
    env_max_sz = os.environ.get("MAX_SIZE")
    max_size_default = float(env_max_sz) if (env_max_sz and float(env_max_sz) > 50.0) else 2500.0

    parser = argparse.ArgumentParser(description="Bot d'Arbitratge Delta-Neutral i Scalping (Hyperliquid + Aevo / Vertex / dYdX / Binance)")
    mode_default = os.environ.get("BOT_MODE", "funding_carry")
    parser.add_argument("--mode", choices=["arbitrage", "funding_carry", "scalper"], default=mode_default, help="Mode d'operació: 'arbitrage' (spread scalping), 'funding_carry' (carry trade passiu de funding), o 'scalper'")
    parser.add_argument("--venue2", choices=["aevo", "vertex", "dydx", "binance", "okx"], default=venue2_default, help="Segon exchange per a l'arbitratge: 'aevo', 'okx' (CEX institucional), 'vertex', 'dydx' o 'binance'")
    parser.add_argument("--coins", nargs="+", default=None, help="Monedes a operar (ex: BTC ETH SOL)")
    parser.add_argument("--maker-first", action="store_true", default=os.getenv("MAKER_FIRST", "true").lower() in ("true", "1", "yes"), help="Activar execució Maker-First a Hyperliquid per minimitzar comissions d'arbitratge")
    parser.add_argument("--initial-balance", type=float, default=initial_balance_default, help="Capital inicial total en dòlars (default: 1000.0$)")
    parser.add_argument("--leverage", type=float, default=leverage_default, help="Apalancament conservador per a l'arbitratge (default: 3.0x)")
    parser.add_argument("--size", type=float, default=size_default, help="Mida en dòlars per ordre/pota (default: 150.0$)")
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
    parser.add_argument("--max-book-spread", type=float, default=None, help="Spread intern màxim del llibre de l'exchange per admetre entrada (default: 0.500%% per a dYdX, 0.220%% per a Aevo/altres)")
    parser.add_argument("--min-carry-apr", type=float, default=float(os.environ.get("MIN_CARRY_APR", "18.0")), help="APR mínim per entrar en Funding Carry Trade (default: 18.0%%)")
    parser.add_argument("--min-exit-apr", type=float, default=float(os.environ.get("MIN_EXIT_APR", "3.0")), help="APR mínim per sortir de Funding Carry per compressió (default: 3.0%%)")
    parser.add_argument("--enable-carry", action="store_true", default=os.getenv("ENABLE_CARRY", "false").lower() in ("true", "1", "yes"), help="Habilitar entrades híbrides de Funding Carry dins el mode d'arbitratge")
    parser.add_argument("--carry-slots", type=int, default=int(os.environ.get("CARRY_SLOTS", "3")), help="Ranures màximes reservades per a carry trade en mode híbrid (default: 3)")
    parser.add_argument("--duration", type=int, default=0, help="Durada màxima d'execució en segons (0 = indefinit)")
    parser.add_argument("--headless", action="store_true", help="Executar sense el tauler visual Rich de terminal (recomanat per a Docker/Railway)")
    parser.add_argument("--live", action="store_true", help="Activar mode d'execució en real a Hyperliquid i Aevo")
    parser.add_argument("--execution-mode", type=str, default=os.getenv("EXECUTION_MODE", "paper"), choices=["paper", "live"], help="Mode d'execució: 'paper' o 'live' (default: paper o via EXECUTION_MODE env)")
    parser.add_argument("--no-burst", action="store_true", help="Desactivar Volume Burst (només scalper)")
    parser.add_argument("--maker-only", action="store_true", help="Només maker (només scalper)")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", 8080)), help="Port del servidor web dashboard (default: 8080)")
    args = parser.parse_args()

    execution_mode = "live" if (args.live or args.execution_mode == "live") else "paper"

    if args.mode in ("arbitrage", "funding_carry"):
        env_coins = os.getenv("COINS")
        if args.coins:
            coins = args.coins
        elif env_coins:
            coins = [c.strip().upper() for c in env_coins.split(",") if c.strip()]
        elif args.venue2 == "dydx":
            default_coins = [
                "ETH", "BTC", "SOL", "SUI", "NEAR", "LINK", "AVAX",
                "ARB", "OP", "APT", "SEI", "TIA", "RENDER", "INJ",
                "ENA", "DOGE", "WIF", "AAVE", "UNI"
            ]
            coins = default_coins
        elif args.venue2 == "okx":
            default_coins = [
                "HYPE", "PUMP", "PEPE", "WIF", "SUI", "NEAR", "DOGE",
                "ARB", "OP", "APT", "SEI", "INJ", "UNI", "RENDER",
                "LINK", "AVAX", "SOL", "ETH", "BTC"
            ]
            coins = default_coins
        elif args.venue2 == "vertex":
            default_coins = ["BTC", "ETH", "SOL", "ARB", "SUI", "LINK", "AVAX"]
            coins = default_coins
        elif args.venue2 == "aevo":
            default_coins = ["SOL", "HYPE", "NEAR", "PUMP", "SUI", "ZEC"]
            coins = default_coins
        else:
            default_coins = ["BTC", "ETH", "SOL", "LINK", "NEAR", "SUI", "DOGE"]
            coins = default_coins
        default_state_file = "live_state.json" if execution_mode == "live" else "paper_state.json"
        state_file = os.getenv("STATE_FILE", default_state_file)
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
            state_file=state_file,
            execution_mode=execution_mode,
            maker_first=args.maker_first,
            port=args.port,
            strategy_mode=args.mode,
            enable_carry=args.enable_carry,
            min_carry_apr=args.min_carry_apr,
            min_exit_apr=args.min_exit_apr,
            carry_slots=args.carry_slots,
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
