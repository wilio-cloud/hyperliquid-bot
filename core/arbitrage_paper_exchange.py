"""Simulador d'execució dual per a arbitratge creuat (Hyperliquid + Binance)."""

import logging
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
        initial_hl_balance: float = 5000.0,
        initial_bn_balance: float = 5000.0,
        venue2_name: str = "DYDX",
        hl_maker_fee: float = 0.00010,  # 0.010%
        hl_taker_fee: float = 0.00035,  # 0.035%
        bn_maker_fee: float = 0.00020,  # 0.020% (dYdX maker)
        bn_taker_fee: float = 0.00040,  # 0.040% (dYdX taker)
        on_open_cb: Optional[Callable[[ArbitragePosition], None]] = None,
        on_close_cb: Optional[Callable[[ArbitragePosition, str, float], None]] = None,
    ):
        self.venue2_name = venue2_name.upper()
        self.initial_hl_balance = initial_hl_balance
        self.initial_bn_balance = initial_bn_balance
        self.initial_total_balance = initial_hl_balance + initial_bn_balance

        self.hl_balance_usd = initial_hl_balance
        self.bn_balance_usd = initial_bn_balance

        self.hl_maker_fee = hl_maker_fee
        self.hl_taker_fee = hl_taker_fee
        self.bn_maker_fee = bn_maker_fee
        self.bn_taker_fee = bn_taker_fee

        self.on_open_cb = on_open_cb
        self.on_close_cb = on_close_cb

        self.active_positions: Dict[str, ArbitragePosition] = {}
        self.closed_positions: List[ArbitragePosition] = []
        
        self.total_funding_collected = 0.0
        self.total_fees_paid = 0.0

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
        size_usd: float = 1000.0,
        is_maker: bool = False,
    ) -> Optional[ArbitragePosition]:
        """Obre simultàniament les dues potes d'arbitratge delta-neutral."""
        if self.has_open_position(signal.coin):
            logger.debug(f"Ja hi ha una posició oberta per {signal.coin}. Omissió.")
            return None

        # Comprovació de capital disponible
        required_margin_per_leg = size_usd  # 1x leverage o cobertura completa
        if self.hl_balance_usd < required_margin_per_leg * 0.2:  # requereix mínim 20% de marge lliure
            logger.warning("Saldo insuficient a Hyperliquid per cobrir la posició d'arbitratge.")
            return None
        if self.bn_balance_usd < required_margin_per_leg * 0.2:
            logger.warning("Saldo insuficient a Binance per cobrir la posició d'arbitratge.")
            return None

        pair_id = f"arb_{signal.coin}_{int(time.time() * 1000)}"
        hl_fee_rate = self.hl_maker_fee if is_maker else self.hl_taker_fee
        bn_fee_rate = self.bn_maker_fee if is_maker else self.bn_taker_fee

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
        )
        position.update_pnl()
        self.active_positions[pair_id] = position

        logger.info(
            f"[ARB OBERT] {signal.coin} {signal.direction.value} | "
            f"HL: {signal.hl_price:.2f} | BN: {signal.bn_price:.2f} | Spread: {signal.spread_pct:+.3f}%"
        )
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
            self.total_funding_collected += net_hour_funding

            self.hl_balance_usd += hl_payment
            self.bn_balance_usd += bn_payment

            pos.update_pnl()
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
        if self.on_close_cb:
            self.on_close_cb(pos, reason, net_pnl)

        return pos

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
        }
