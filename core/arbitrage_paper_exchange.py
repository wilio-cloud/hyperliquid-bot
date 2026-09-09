import json
import logging
import os
import time
from typing import Callable, Dict, List, Optional

from core.arbitrage_models import (
    ArbitrageDirection,
    ArbitrageLeg,
    ArbitragePosition,
    ArbitrageSignal,
)
from core.models import OrderBookL2, OrderSide

logger = logging.getLogger("ArbitrageExchange")

class ArbitragePaperExchange:
    def __init__(
        self,
        initial_hl_balance: float = 500.0,
        initial_bn_balance: float = 500.0,
        venue2_name: str = "AEVO",
        leverage: float = 2.0,
        hl_maker_fee: float = 0.00015,  # 0.015% (Tier 0 base Hyperliquid)
        hl_taker_fee: float = 0.00045,  # 0.045%
        bn_maker_fee: Optional[float] = None,
        bn_taker_fee: Optional[float] = None,
        maker_first: bool = False,
        on_open_cb: Optional[Callable[[ArbitragePosition], None]] = None,
        on_close_cb: Optional[Callable[[ArbitragePosition, str, float], None]] = None,
        state_file: Optional[str] = None,
    ):
        self.venue2_name = venue2_name.upper()
        self.leverage = leverage
        self.maker_first = maker_first
        self.initial_hl_balance = initial_hl_balance
        self.initial_bn_balance = initial_bn_balance
        self.initial_total_balance = initial_hl_balance + initial_bn_balance

        self.hl_balance_usd = initial_hl_balance
        self.bn_balance_usd = initial_bn_balance

        self.hl_maker_fee = hl_maker_fee
        self.hl_taker_fee = hl_taker_fee

        if bn_maker_fee is not None:
            self.bn_maker_fee = bn_maker_fee
        elif self.venue2_name == "VERTEX":
            self.bn_maker_fee = 0.00000  # 0.000% Maker a Vertex Protocol
        elif self.venue2_name == "AEVO":
            self.bn_maker_fee = 0.00030  # 0.030% Maker a Aevo
        elif self.venue2_name == "DYDX":
            self.bn_maker_fee = 0.00010  # 0.010% Maker a dYdX v4
        else:
            self.bn_maker_fee = 0.00020  # 0.020% Binance

        if bn_taker_fee is not None:
            self.bn_taker_fee = bn_taker_fee
        elif self.venue2_name == "VERTEX":
            self.bn_taker_fee = 0.00020  # 0.020% Taker a Vertex Protocol
        elif self.venue2_name == "AEVO":
            self.bn_taker_fee = 0.00050  # 0.050% Taker real a Aevo
        elif self.venue2_name == "DYDX":
            self.bn_taker_fee = 0.00050  # 0.050% Taker estàndard Tier 1 a dYdX v4 (<1M$ volum)
        else:
            self.bn_taker_fee = 0.00040  # 0.040% Binance

        self.on_open_cb = on_open_cb
        self.on_close_cb = on_close_cb

        self.active_positions: Dict[str, ArbitragePosition] = {}
        self.closed_positions: List[ArbitragePosition] = []
        
        self.total_funding_collected = 0.0
        self.total_fees_paid = 0.0
        self.equity_history: List[dict] = []
        self.state_file: Optional[str] = state_file

        # Carrega l'estat previ si s'ha definit un fitxer d'estat
        if self.state_file:
            self.load_state()

    @property
    def total_balance_usd(self) -> float:
        return self.hl_balance_usd + self.bn_balance_usd

    @property
    def total_unrealized_pnl(self) -> float:
        return sum(pos.unrealized_pnl for pos in self.active_positions.values())

    @property
    def net_pnl(self) -> float:
        return (self.total_balance_usd + self.total_unrealized_pnl) - self.initial_total_balance

    def has_open_position(self, coin: str) -> bool:
        return any(pos.coin == coin for pos in self.active_positions.values())

    def open_arbitrage_position(
        self,
        signal: ArbitrageSignal,
        size_usd: float = 250.0,
        is_maker: bool = False,
        maker_first: Optional[bool] = None,
    ) -> Optional[ArbitragePosition]:
        """Simula l'obertura simultània (o Maker-First) de les dues potes d'arbitratge."""
        if self.has_open_position(signal.coin):
            logger.debug(f"Ja hi ha una posició oberta per {signal.coin}. Omissió.")
            return None

        # Comprovació de capital disponible segons apalancament
        required_margin_per_leg = size_usd / self.leverage
        if self.hl_balance_usd < required_margin_per_leg:
            logger.warning("Saldo insuficient a Hyperliquid per cobrir la posició d'arbitratge.")
            return None
        if self.bn_balance_usd < required_margin_per_leg:
            logger.warning(f"Saldo insuficient a {self.venue2_name} per cobrir la posició d'arbitratge.")
            return None

        pair_id = f"arb_{signal.coin}_{int(time.time() * 1000)}"
        effective_maker_first = self.maker_first if maker_first is None else maker_first
        if is_maker:
            hl_fee_rate = self.hl_maker_fee
            bn_fee_rate = self.bn_maker_fee
        elif effective_maker_first:
            # En mode Maker-First, Hyperliquid entra com a Maker passiu (0.015% o 0%) i Venue2 cobreix com a Taker
            hl_fee_rate = self.hl_maker_fee
            bn_fee_rate = self.bn_taker_fee
        else:
            hl_fee_rate = self.hl_taker_fee
            bn_fee_rate = self.bn_taker_fee

        hl_size = size_usd / signal.hl_price
        bn_size = size_usd / signal.bn_price

        hl_entry_fee = size_usd * hl_fee_rate
        bn_entry_fee = size_usd * bn_fee_rate

        self.hl_balance_usd -= hl_entry_fee
        self.bn_balance_usd -= bn_entry_fee
        self.total_fees_paid += (hl_entry_fee + bn_entry_fee)

        if signal.direction == ArbitrageDirection.SELL_HL_BUY_BN:
            leg_hl = ArbitrageLeg(
                venue="HYPERLIQUID",
                coin=signal.coin,
                side=OrderSide.SELL,
                entry_price=signal.hl_price,
                size=hl_size,
                size_usd=size_usd,
                current_price=signal.hl_price,
                fee_rate=hl_fee_rate,
                fees_paid=hl_entry_fee,
            )
            leg_bn = ArbitrageLeg(
                venue=self.venue2_name,
                coin=signal.coin,
                side=OrderSide.BUY,
                entry_price=signal.bn_price,
                size=bn_size,
                size_usd=size_usd,
                current_price=signal.bn_price,
                fee_rate=bn_fee_rate,
                fees_paid=bn_entry_fee,
            )
        else:
            leg_hl = ArbitrageLeg(
                venue="HYPERLIQUID",
                coin=signal.coin,
                side=OrderSide.BUY,
                entry_price=signal.hl_price,
                size=hl_size,
                size_usd=size_usd,
                current_price=signal.hl_price,
                fee_rate=hl_fee_rate,
                fees_paid=hl_entry_fee,
            )
            leg_bn = ArbitrageLeg(
                venue=self.venue2_name,
                coin=signal.coin,
                side=OrderSide.SELL,
                entry_price=signal.bn_price,
                size=bn_size,
                size_usd=size_usd,
                current_price=signal.bn_price,
                fee_rate=bn_fee_rate,
                fees_paid=bn_entry_fee,
            )

        position = ArbitragePosition(
            pair_id=pair_id,
            coin=signal.coin,
            direction=signal.direction,
            leg_hl=leg_hl,
            leg_bn=leg_bn,
            entry_spread_pct=signal.spread_pct,
            entry_time=time.time(),
            current_spread_pct=signal.spread_pct,
            strategy_type=getattr(signal, "strategy_type", "SPREAD_SCALP"),
            current_net_apr=getattr(signal, "net_funding_apr", 0.0),
        )
        position.update_pnl()
        self.active_positions[pair_id] = position

        logger.info(
            f"[ARB OBERT] {signal.coin} {signal.direction.value} | "
            f"HL: {signal.hl_price:.2f} | BN: {signal.bn_price:.2f} | Spread: {signal.spread_pct:+.3f}%"
        )
        self.record_equity_point("OPEN")
        self.save_state()

        if self.on_open_cb:
            self.on_open_cb(position)

        return position

    def on_hl_book(self, book: OrderBookL2):
        """Actualitza el preu de referència d'Hyperliquid per a les posicions obertes."""
        if not book.mid_price:
            return
        for pos in self.active_positions.values():
            if pos.coin == book.coin:
                # Per a Short volem comprar al ask per tancar, per a Long volem vendre al bid per tancar
                close_px = book.best_ask if pos.leg_hl.side == OrderSide.SELL else book.best_bid
                pos.leg_hl.update_price(close_px or book.mid_price)
                pos.update_pnl()

    def on_bn_book(self, book: OrderBookL2):
        """Actualitza el preu de referència de Binance per a les posicions obertes."""
        if not book.mid_price:
            return
        for pos in self.active_positions.values():
            if pos.coin == book.coin:
                close_px = book.best_ask if pos.leg_bn.side == OrderSide.SELL else book.best_bid
                pos.leg_bn.update_price(close_px or book.mid_price)
                pos.update_pnl()

    def apply_hourly_funding(self, coin: str, hl_funding_hourly_pct: float, bn_funding_8h_pct: float):
        """
        Aplica el cobrament o pagament horari de les taxes de finançament.
        - Hyperliquid aplica pagament cada 1 hora.
        - Binance aplica pagament cada 8 hores (per hora equival a rate_8h / 8).
        """
        bn_funding_hourly_pct = bn_funding_8h_pct / 8.0

        for pos in self.active_positions.values():
            if pos.coin != coin or pos.is_closed:
                continue

            hl_payment = 0.0
            bn_payment = 0.0

            # Pota Hyperliquid: Short rep si funding > 0, paga si < 0. Long a la inversa.
            hl_rate = hl_funding_hourly_pct / 100.0
            if pos.leg_hl.side == OrderSide.SELL:
                hl_payment = pos.leg_hl.size_usd * hl_rate
            else:
                hl_payment = -pos.leg_hl.size_usd * hl_rate

            # Pota Binance
            bn_rate = bn_funding_hourly_pct / 100.0
            if pos.leg_bn.side == OrderSide.SELL:
                bn_payment = pos.leg_bn.size_usd * bn_rate
            else:
                bn_payment = -pos.leg_bn.size_usd * bn_rate

            net_hour_funding = hl_payment + bn_payment
            pos.accumulated_funding += net_hour_funding
            pos.funding_payouts_count += 1
            self.total_funding_collected += net_hour_funding

            self.hl_balance_usd += hl_payment
            self.bn_balance_usd += bn_payment

            pos.update_pnl()
            self.record_equity_point("FUNDING")
            self.save_state()
            logger.info(
                f"[FUNDING PAGAT] {coin} {pos.direction.value}: HL {hl_payment:+.4f}$ + BN {bn_payment:+.4f}$ = Net {net_hour_funding:+.4f}$"
            )

    def close_arbitrage_position(
        self,
        pair_id: str,
        hl_exit_price: float,
        bn_exit_price: float,
        reason: str = "SPREAD_CONVERGED",
        is_maker: bool = False,
    ) -> Optional[ArbitragePosition]:
        """Tanca simultàniament les dues potes de la posició d'arbitratge."""
        pos = self.active_positions.get(pair_id)
        if not pos or pos.is_closed:
            return None

        hl_fee_rate = self.hl_maker_fee if is_maker else self.hl_taker_fee
        bn_fee_rate = self.bn_maker_fee if is_maker else self.bn_taker_fee

        pos.leg_hl.close(hl_exit_price, exit_fee_rate=hl_fee_rate)
        pos.leg_bn.close(bn_exit_price, exit_fee_rate=bn_fee_rate)

        # Actualitza balanços amb els PnL realitzats bruts (sense comptar comissions d'entrada ja descomptades)
        # Nota: leg.close ja ha calculat realized_pnl = gross_pnl - exit_fee
        if pos.leg_hl.side == OrderSide.BUY:
            hl_gross = (hl_exit_price - pos.leg_hl.entry_price) * pos.leg_hl.size
        else:
            hl_gross = (pos.leg_hl.entry_price - hl_exit_price) * pos.leg_hl.size
        hl_exit_fee = pos.leg_hl.size * hl_exit_price * hl_fee_rate
        self.hl_balance_usd += (hl_gross - hl_exit_fee)

        if pos.leg_bn.side == OrderSide.BUY:
            bn_gross = (bn_exit_price - pos.leg_bn.entry_price) * pos.leg_bn.size
        else:
            bn_gross = (pos.leg_bn.entry_price - bn_exit_price) * pos.leg_bn.size
        bn_exit_fee = pos.leg_bn.size * bn_exit_price * bn_fee_rate
        self.bn_balance_usd += (bn_gross - bn_exit_fee)

        self.total_fees_paid += (hl_exit_fee + bn_exit_fee)

        pos.is_closed = True
        pos.exit_time = time.time()
        pos.exit_reason = reason
        pos.update_pnl()

        net_pnl = pos.realized_pnl
        del self.active_positions[pair_id]
        self.closed_positions.append(pos)

        logger.info(
            f"[ARB TANCAT {reason}] {pos.coin} {pos.direction.value} | PnL Net: {net_pnl:+.3f}$ "
            f"(Funding: {pos.accumulated_funding:+.4f}$, Fees Totals: {pos.total_fees:.4f}$)"
        )
        self.record_equity_point(f"CLOSE_{reason}")
        self.save_state()

        if self.on_close_cb:
            self.on_close_cb(pos, reason, net_pnl)

        return pos

    def record_equity_point(self, event_type: str = "TICK"):
        """Enregistra un punt cronològic en la corba històrica d'equity."""
        now = time.time()
        point = {
            "t": round(now, 1),
            "equity": round(self.total_balance_usd + self.total_unrealized_pnl, 3),
            "balance": round(self.total_balance_usd, 3),
            "realized_pnl": round(sum(p.realized_pnl for p in self.closed_positions), 3),
            "unrealized_pnl": round(self.total_unrealized_pnl, 3),
            "trades": len(self.closed_positions),
            "event": event_type,
        }
        self.equity_history.append(point)
        if len(self.equity_history) > 600:
            self.equity_history = self.equity_history[-600:]

    def save_state(self, filepath: Optional[str] = None):
        """Desa l'estat actual en format JSON a disc per protegir les dades en redeploys."""
        path = filepath or self.state_file
        if not path:
            return
        try:
            active_list = []
            for p in self.active_positions.values():
                d = p.model_dump() if hasattr(p, "model_dump") else p.dict()
                active_list.append(d)

            closed_list = []
            for p in self.closed_positions:
                d = p.model_dump() if hasattr(p, "model_dump") else p.dict()
                closed_list.append(d)

            data = {
                "hl_balance_usd": self.hl_balance_usd,
                "bn_balance_usd": self.bn_balance_usd,
                "initial_total_balance": self.initial_total_balance,
                "total_fees_paid": self.total_fees_paid,
                "total_funding_collected": self.total_funding_collected,
                "active_positions": active_list,
                "closed_positions": closed_list,
                "equity_history": self.equity_history,
                "last_saved": time.time(),
            }
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, path)
        except Exception as e:
            logger.debug(f"Error desant paper_state: {e}")

    def load_state(self, filepath: Optional[str] = None) -> bool:
        """Carrega l'estat de l'exchange si existeix el fitxer JSON."""
        path = filepath or self.state_file
        if not path or not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.hl_balance_usd = float(data.get("hl_balance_usd", self.hl_balance_usd))
            self.bn_balance_usd = float(data.get("bn_balance_usd", self.bn_balance_usd))
            self.initial_total_balance = float(data.get("initial_total_balance", self.initial_total_balance))
            self.total_fees_paid = float(data.get("total_fees_paid", self.total_fees_paid))
            raw_equity = data.get("equity_history", [])
            self.equity_history = []
            for pt in raw_equity:
                if pt.get("t", 0) < 1780000000:
                    pt["t"] += 31545000.0
                self.equity_history.append(pt)

            self.active_positions.clear()
            for p_dict in data.get("active_positions", []):
                if p_dict.get("entry_time") and p_dict["entry_time"] < 1780000000:
                    p_dict["entry_time"] += 31545000.0
                if p_dict.get("exit_time") and p_dict["exit_time"] < 1780000000:
                    p_dict["exit_time"] += 31545000.0
                pos = ArbitragePosition(**p_dict)
                self.active_positions[pos.pair_id] = pos

            self.closed_positions.clear()
            for p_dict in data.get("closed_positions", []):
                if p_dict.get("entry_time") and p_dict["entry_time"] < 1780000000:
                    p_dict["entry_time"] += 31545000.0
                if p_dict.get("exit_time") and p_dict["exit_time"] < 1780000000:
                    p_dict["exit_time"] += 31545000.0
                if p_dict.get("exit_time") and p_dict.get("entry_time") and (p_dict["exit_time"] - p_dict["entry_time"] > 86400):
                    p_dict["entry_time"] = p_dict["exit_time"] - 180.0
                pos = ArbitragePosition(**p_dict)
                pos.update_pnl()
                self.closed_positions.append(pos)

            logger.info(
                f"[STATE RESTORED] {len(self.closed_positions)} operacions tancades restaurades, "
                f"{len(self.active_positions)} obertes | Balanç: {self.total_balance_usd:.2f}$"
            )
            return True
        except Exception as e:
            logger.error(f"Error carregant paper_state: {e}")
            return False

    @property
    def metrics(self) -> dict:
        wins = len([p for p in self.closed_positions if p.realized_pnl > 0])
        losses = len([p for p in self.closed_positions if p.realized_pnl <= 0])
        total = len(self.closed_positions)
        winrate = (wins / total * 100.0) if total > 0 else 0.0

        gross_profit = sum(p.realized_pnl for p in self.closed_positions if p.realized_pnl > 0)
        gross_loss = abs(sum(p.realized_pnl for p in self.closed_positions if p.realized_pnl < 0))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 1.0)

        return {
            "venue2_name": self.venue2_name,
            "leverage": self.leverage,
            "leverage_str": f"{self.leverage:g}x",
            "hl_balance": self.hl_balance_usd,
            "bn_balance": self.bn_balance_usd,
            "balance": self.total_balance_usd,
            "initial_balance": self.initial_total_balance,
            "net_pnl": self.net_pnl,
            "unrealized_pnl": self.total_unrealized_pnl,
            "realized_pnl": sum(p.realized_pnl for p in self.closed_positions),
            "total_funding": self.total_funding_collected,
            "total_fees": self.total_fees_paid,
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "winrate_pct": winrate,
            "profit_factor": profit_factor,
            "active_positions_count": len(self.active_positions),
            "equity_history": self.equity_history,
            "execution_mode": "paper",
            "maker_first": getattr(self, "maker_first", False),
        }
