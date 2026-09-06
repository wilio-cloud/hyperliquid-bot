"""Estratègia 4: Captura de Spread / Market Making Pur (100% Maker - 0% Fees)."""

import time
from typing import Dict, Optional
from config.settings import config
from core.models import OrderBookL2, OrderSide, Signal, Trade
from strategies.base_strategy import BaseStrategy

class SpreadMarketMakerStrategy(BaseStrategy):
    def __init__(
        self,
        min_spread_pct: float = 0.003,   # Spread mínim del 0.003% per justificar l'operació
        max_spread_pct: float = 0.025,   # No operar si el spread és massa obert (risc de salt)
        max_holding_sec: float = 45.0,    # Temps màxim per tancar la sortida de spread
    ):
        super().__init__(name="Spread_Maker", enabled=True)
        self.min_spread_pct = min_spread_pct
        self.max_spread_pct = max_spread_pct
        self.max_holding_sec = max_holding_sec
        
        # Estat d'inventari per moneda: None, 'BUY_PLACED', 'HOLDING_INVENTORY'
        self.inventory_state: Dict[str, str] = {}
        self.last_quote_time: Dict[str, float] = {}

    def on_book_update(self, book: OrderBookL2) -> Optional[Signal]:
        if not self.enabled:
            return None

        if not book.best_bid or not book.best_ask or not book.mid_price:
            return None

        coin = book.coin
        spread_pct = book.spread_pct

        # 1. Comprovació de condicions òptimes de spread
        if spread_pct < self.min_spread_pct or spread_pct > self.max_spread_pct:
            return None

        # 2. Avaluar equilibri del book per evitar adverse selection
        # Si un dels dos costats està buit (< 20% del volum), no entrem
        top_bid_vol = sum(lvl.size * lvl.price for lvl in book.bids[:3])
        top_ask_vol = sum(lvl.size * lvl.price for lvl in book.asks[:3])

        if top_bid_vol <= 0 or top_ask_vol <= 0:
            return None

        imbalance = top_bid_vol / top_ask_vol

        # Si el book està equilibrat (sense perill de crash imminent):
        # Generem ordre de compra al Best Bid exacta per esperar l'execució com a Maker
        now = time.time()
        if now - self.last_quote_time.get(coin, 0) < 3.0:
            return None

        # Si el bid té un suport lleugerament superior (1.1x a 2.0x), comprem al bid per vendre a l'ask
        if 1.05 <= imbalance <= 2.2:
            self.last_quote_time[coin] = now
            # Take profit és el Best Ask exacte (captura d'spread pur!)
            tp_target = book.best_ask
            sl_target = book.best_bid * (1.0 - 0.0010)  # Stop loss molt estret

            sig = Signal(
                coin=coin,
                action="BUY",
                price=book.best_bid,
                strategy_name=self.name,
                reason=f"Spread Capture: Bid {book.best_bid:.2f} -> Ask {book.best_ask:.2f} (Spread: {spread_pct:.3f}%)",
                take_profit=tp_target,
                stop_loss=sl_target,
            )
            self.record_signal(sig)
            return sig

        return None

    def on_trade(self, trade: Trade) -> Optional[Signal]:
        return None
