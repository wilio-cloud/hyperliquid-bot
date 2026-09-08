"""Models de dades per a l'estratègia d'arbitratge creuat (Hyperliquid vs Binance)."""

from enum import Enum
import time
from typing import Optional
from pydantic import BaseModel, Field
from core.models import OrderSide

class ArbitrageDirection(str, Enum):
    SELL_HL_BUY_BN = "SELL_HL_BUY_BN"  # Preu HL > BN: Shortem HL (car) + Longem BN (barat)
    BUY_HL_SELL_BN = "BUY_HL_SELL_BN"  # Preu BN > HL: Longem HL (barat) + Shortem BN (car)

class ArbitrageSignal(BaseModel):
    coin: str
    direction: ArbitrageDirection
    hl_price: float
    bn_price: float
    spread_pct: float
    hl_funding_8h_pct: float = 0.0
    bn_funding_8h_pct: float = 0.0
    net_funding_apr: float = 0.0
    strategy_type: str = "SPREAD_SCALP"  # "SPREAD_SCALP" o "FUNDING_CARRY"
    reason: str = ""
    timestamp: float = Field(default_factory=time.time)

class ArbitrageLeg(BaseModel):
    venue: str  # "HYPERLIQUID" o "BINANCE"
    coin: str
    side: OrderSide  # BUY o SELL
    entry_price: float
    size: float
    size_usd: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    fee_rate: float = 0.0
    fees_paid: float = 0.0
    exit_price: Optional[float] = None

    def update_price(self, price: float):
        self.current_price = price
        if self.side == OrderSide.BUY:
            self.unrealized_pnl = (self.current_price - self.entry_price) * self.size
        else:
            self.unrealized_pnl = (self.entry_price - self.current_price) * self.size

    def close(self, exit_price: float, exit_fee_rate: Optional[float] = None):
        self.exit_price = exit_price
        self.current_price = exit_price
        fee_rate = exit_fee_rate if exit_fee_rate is not None else self.fee_rate
        exit_fee = self.size * exit_price * fee_rate
        self.fees_paid += exit_fee
        if self.side == OrderSide.BUY:
            gross_pnl = (exit_price - self.entry_price) * self.size
        else:
            gross_pnl = (self.entry_price - exit_price) * self.size
        self.realized_pnl = gross_pnl - self.fees_paid
        self.unrealized_pnl = 0.0

class ArbitragePosition(BaseModel):
    pair_id: str
    coin: str
    direction: ArbitrageDirection
    leg_hl: ArbitrageLeg
    leg_bn: ArbitrageLeg
    entry_spread_pct: float
    target_exit_spread_pct: float = 0.015  # Sortida quan el spread convergeix a <= 0.015%
    stop_loss_spread_pct: float = 0.35      # Stop de seguretat si el spread divergeix excessivament
    entry_time: float = Field(default_factory=time.time)
    current_spread_pct: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    total_fees: float = 0.0
    accumulated_funding: float = 0.0
    is_closed: bool = False
    exit_time: Optional[float] = None
    exit_reason: Optional[str] = None
    divergence_start_time: Optional[float] = None
    strategy_type: str = "SPREAD_SCALP"  # "SPREAD_SCALP" o "FUNDING_CARRY"
    current_net_apr: float = 0.0
    funding_payouts_count: int = 0

    def update_pnl(self):
        """Actualitza el PnL combinat de totes dues potes i afegeix funding."""
        self.total_fees = self.leg_hl.fees_paid + self.leg_bn.fees_paid
        if not self.is_closed:
            # Comissions estimades de sortida per mostrar el PnL Net REAL exacte
            hl_px = self.leg_hl.current_price or self.leg_hl.entry_price
            bn_px = self.leg_bn.current_price or self.leg_bn.entry_price
            est_exit_fees = (self.leg_hl.size * hl_px * self.leg_hl.fee_rate) + (self.leg_bn.size * bn_px * self.leg_bn.fee_rate)
            self.unrealized_pnl = (
                self.leg_hl.unrealized_pnl + self.leg_bn.unrealized_pnl
                + self.accumulated_funding - (self.total_fees + est_exit_fees)
            )
        else:
            self.realized_pnl = (
                self.leg_hl.realized_pnl + self.leg_bn.realized_pnl
                + self.accumulated_funding
            )

class CrossSpreadInfo(BaseModel):
    coin: str
    hl_bid: float
    hl_ask: float
    bn_bid: float
    bn_ask: float
    hl_mid: float
    bn_mid: float
    # Spread executant compra a BN i venda a HL (ex: preu HL > BN)
    spread_sell_hl_buy_bn_pct: float
    # Spread executant compra a HL i venda a BN (ex: preu BN > HL)
    spread_buy_hl_sell_bn_pct: float
    hl_funding_8h_pct: float = 0.0
    bn_funding_8h_pct: float = 0.0
    annual_funding_diff_apr: float = 0.0
    timestamp: float = Field(default_factory=time.time)
