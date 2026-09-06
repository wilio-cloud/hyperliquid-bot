"""Estratègia 1: Desequilibri del Llibre d'Ordres (Order Book Imbalance - OBI)."""

import time
from typing import Optional
from config.settings import config
from core.models import OrderBookL2, Signal, Trade
from strategies.base_strategy import BaseStrategy

class OrderBookImbalanceStrategy(BaseStrategy):
    def __init__(
        self,
        imbalance_ratio: float = config.obi_imbalance_ratio,
        depth_levels: int = config.obi_depth_levels,
        max_spread_pct: float = 0.04,  # No operar si el spread supera el 0.04%
        min_signal_interval_sec: float = 5.0,
    ):
        super().__init__(name="OBI_Scalper", enabled=True)
        self.imbalance_ratio = imbalance_ratio
        self.depth_levels = depth_levels
        self.max_spread_pct = max_spread_pct
        self.min_signal_interval_sec = min_signal_interval_sec
        self.last_signal_time: dict[str, float] = {}

    def on_book_update(self, book: OrderBookL2) -> Optional[Signal]:
        if not self.enabled:
            return None

        # Filtre de spread: mai operar si el spread és massa obert
        if book.spread_pct > self.max_spread_pct or book.best_bid is None or book.best_ask is None:
            return None

        now = time.time()
        if now - self.last_signal_time.get(book.coin, 0) < self.min_signal_interval_sec:
            return None

        levels_bid = book.bids[:self.depth_levels]
        levels_ask = book.asks[:self.depth_levels]

        if not levels_bid or not levels_ask:
            return None

        bid_vol_usd = sum(lvl.price * lvl.size for lvl in levels_bid)
        ask_vol_usd = sum(lvl.price * lvl.size for lvl in levels_ask)

        if ask_vol_usd <= 0 or bid_vol_usd <= 0:
            return None

        ratio_bid_ask = bid_vol_usd / ask_vol_usd
        ratio_ask_bid = ask_vol_usd / bid_vol_usd

        # 1. Senyal de Compra: Si hi ha una forta pressió compradora al book
        if ratio_bid_ask >= self.imbalance_ratio:
            self.last_signal_time[book.coin] = now
            sig = Signal(
                coin=book.coin,
                action="BUY",
                price=book.best_bid,  # Ordre límit com a Maker al millor Bid
                strategy_name=self.name,
                reason=f"OBI Bullish Ratio: {ratio_bid_ask:.2f}x (Bid: {bid_vol_usd:,.0f}$ vs Ask: {ask_vol_usd:,.0f}$)",
                take_profit=book.best_bid * (1.0 + 0.0008),
                stop_loss=book.best_bid * (1.0 - 0.0018),
            )
            self.record_signal(sig)
            return sig

        # 2. Senyal de Venda (Short): Si hi ha una forta pressió venedora al book
        elif ratio_ask_bid >= self.imbalance_ratio:
            self.last_signal_time[book.coin] = now
            sig = Signal(
                coin=book.coin,
                action="SELL",
                price=book.best_ask,  # Ordre límit com a Maker al millor Ask
                strategy_name=self.name,
                reason=f"OBI Bearish Ratio: {ratio_ask_bid:.2f}x (Ask: {ask_vol_usd:,.0f}$ vs Bid: {bid_vol_usd:,.0f}$)",
                take_profit=book.best_ask * (1.0 - 0.0008),
                stop_loss=book.best_ask * (1.0 + 0.0018),
            )
            self.record_signal(sig)
            return sig

        return None

    def on_trade(self, trade: Trade) -> Optional[Signal]:
        # Aquesta estratègia es basa purament en l'estructura del book L2
        return None
