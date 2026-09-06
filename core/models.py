"""Models de dades per a Hyperliquid bot."""

from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field
import time

class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"

class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

class BookLevel(BaseModel):
    price: float
    size: float
    num_orders: int = 1

class OrderBookL2(BaseModel):
    coin: str
    timestamp: float
    bids: List[BookLevel] = Field(default_factory=list)
    asks: List[BookLevel] = Field(default_factory=list)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return None

    @property
    def spread(self) -> float:
        if self.best_bid is not None and self.best_ask is not None:
            return max(0.0, self.best_ask - self.best_bid)
        return 0.0

    @property
    def spread_pct(self) -> float:
        mid = self.mid_price
        if mid and mid > 0:
            return (self.spread / mid) * 100.0
        return 0.0

class Trade(BaseModel):
    coin: str
    side: OrderSide  # BUY (taker buy into ask) o SELL (taker sell into bid)
    price: float
    size: float
    timestamp: float

class Signal(BaseModel):
    coin: str
    action: str  # "BUY", "SELL", "CLOSE", "HOLD"
    price: float
    strategy_name: str
    reason: str
    timestamp: float = Field(default_factory=time.time)
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None

class Order(BaseModel):
    order_id: str
    coin: str
    side: OrderSide
    order_type: OrderType
    price: float
    size: float
    post_only: bool = True
    status: OrderStatus = OrderStatus.PENDING
    created_at: float = Field(default_factory=time.time)
    filled_at: Optional[float] = None
    fee_paid: float = 0.0
    strategy_name: str = ""

class Position(BaseModel):
    position_id: str
    coin: str
    side: OrderSide
    entry_price: float
    size: float
    size_usd: float
    entry_time: float = Field(default_factory=time.time)
    take_profit: float
    stop_loss: float
    strategy_name: str
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    is_closed: bool = False
    exit_price: Optional[float] = None
    exit_time: Optional[float] = None
    exit_reason: Optional[str] = None
