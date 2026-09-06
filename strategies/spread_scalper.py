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

        # Target de TP del +0.065% (supera de sobres la comissió) i SL del -0.180% (marge contra el soroll)
        min_tp_pct = 0.00065
        min_sl_pct = 0.00180

        # 1. Si el bid té un suport lleugerament superior: comprem al Bid per sortir per sobre
        if 1.05 <= imbalance <= 2.2:
            self.last_quote_time[coin] = now
            tp_target = max(book.best_ask, book.best_bid * (1.0 + min_tp_pct))
            sl_target = book.best_bid * (1.0 - min_sl_pct)

            sig = Signal(
                coin=coin,
                action="BUY",
                price=book.best_bid,
                strategy_name=self.name,
                reason=f"Spread Scalp BUY: Bid {book.best_bid:.2f} -> TP {tp_target:.2f}",
                take_profit=tp_target,
                stop_loss=sl_target,
            )
            self.record_signal(sig)
            return sig

        # 2. Si l'ask té un suport lleugerament superior: venem a l'Ask per sortir per sota
        ask_bid_ratio = top_ask_vol / top_bid_vol
        if 1.05 <= ask_bid_ratio <= 2.2:
            self.last_quote_time[coin] = now
            tp_target = min(book.best_bid, book.best_ask * (1.0 - min_tp_pct))
            sl_target = book.best_ask * (1.0 + min_sl_pct)

            sig = Signal(
                coin=coin,
                action="SELL",
                price=book.best_ask,
                strategy_name=self.name,
                reason=f"Spread Scalp SELL: Ask {book.best_ask:.2f} -> TP {tp_target:.2f}",
                take_profit=tp_target,
                stop_loss=sl_target,
            )
            self.record_signal(sig)
            return sig

        return None

    def on_trade(self, trade: Trade) -> Optional[Signal]:
        return None
