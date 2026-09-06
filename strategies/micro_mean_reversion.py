"""Estratègia 3: Micro Reversió a la Mitjana (Micro-VWAP)."""

import math
import time
from collections import deque
from typing import Dict, Optional
from config.settings import config
from core.models import OrderBookL2, Signal, Trade
from strategies.base_strategy import BaseStrategy

class MicroMeanReversionStrategy(BaseStrategy):
    def __init__(
        self,
        window_seconds: int = config.mean_rev_window,
        dev_threshold_pct: float = config.mean_rev_dev_pct,
        min_signal_interval_sec: float = 15.0,
    ):
        super().__init__(name="Micro_MeanRev", enabled=True)
        self.window_seconds = window_seconds
        self.dev_threshold_pct = dev_threshold_pct
        self.min_signal_interval_sec = min_signal_interval_sec
        
        # Historial de trades: deque de (timestamp, price, size)
        self.history: Dict[str, deque] = {}
        self.last_signal_time: Dict[str, float] = {}

    def on_trade(self, trade: Trade) -> Optional[Signal]:
        coin = trade.coin
        now = time.time()

        if coin not in self.history:
            self.history[coin] = deque()

        self.history[coin].append((now, trade.price, trade.size))

        # Neteja de dades fora de la finestra temporal
        while self.history[coin] and (now - self.history[coin][0][0] > self.window_seconds):
            self.history[coin].popleft()

        return None

    def on_book_update(self, book: OrderBookL2) -> Optional[Signal]:
        if not self.enabled:
            return None

        coin = book.coin
        if coin not in self.history or len(self.history[coin]) < 20:
            return None

        now = time.time()
        if now - self.last_signal_time.get(coin, 0) < self.min_signal_interval_sec:
            return None

        # Càlcul de VWAP en la finestra
        sum_pv = sum(p * sz for _, p, sz in self.history[coin])
        sum_v = sum(sz for _, _, sz in self.history[coin])

        if sum_v <= 0:
            return None

        vwap = sum_pv / sum_v
        mid = book.mid_price
        if not mid or not book.best_bid or not book.best_ask:
            return None

        deviation_pct = (mid - vwap) / vwap

        # 1. Preu per sota del VWAP (Sobrevenda micro): esperar rebot
        if deviation_pct <= -self.dev_threshold_pct:
            self.last_signal_time[coin] = now
            sig = Signal(
                coin=coin,
                action="BUY",
                price=book.best_bid,
                strategy_name=self.name,
                reason=f"Micro Mean-Rev Oversold: Preu {mid:.2f} sota VWAP {vwap:.2f} ({deviation_pct*100:+.3f}%)",
            )
            self.record_signal(sig)
            return sig

        # 2. Preu per sobre del VWAP (Sobrecompra micro): esperar caiguda
        elif deviation_pct >= self.dev_threshold_pct:
            self.last_signal_time[coin] = now
            sig = Signal(
                coin=coin,
                action="SELL",
                price=book.best_ask,
                strategy_name=self.name,
                reason=f"Micro Mean-Rev Overbought: Preu {mid:.2f} sobre VWAP {vwap:.2f} ({deviation_pct*100:+.3f}%)",
            )
            self.record_signal(sig)
            return sig

        return None
