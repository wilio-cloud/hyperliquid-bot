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
        self.ema_trend: Dict[str, float] = {}  # Filtre de macro-tendència per evitar ganivets que cauen

    def on_trade(self, trade: Trade) -> Optional[Signal]:
        coin = trade.coin
        now = time.time()

        if coin not in self.history:
            self.history[coin] = deque()

        self.history[coin].append((now, trade.price, trade.size))

        # Actualitza la tendència EMA (200 ticks de memòria)
        if coin not in self.ema_trend:
            self.ema_trend[coin] = trade.price
        else:
            alpha = 2.0 / (200 + 1)
            self.ema_trend[coin] = alpha * trade.price + (1.0 - alpha) * self.ema_trend[coin]

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
        macro_ema = self.ema_trend.get(coin, mid)

        # 1. Preu per sota del VWAP (Sobrevenda micro):
        # Filtre de seguretat: Només comprem si estem en tendència alcista (preu >= macro_ema)
        if deviation_pct <= -self.dev_threshold_pct and mid >= macro_ema:
            self.last_signal_time[coin] = now
            tp_target = book.best_bid * (1.0 + config.default_take_profit_pct)
            sl_target = book.best_bid * (1.0 - config.default_stop_loss_pct)
            sig = Signal(
                coin=coin,
                action="BUY",
                price=book.best_bid,
                strategy_name=self.name,
                reason=f"Micro Mean-Rev Oversold (Trend Bullish): Preu {mid:.2f} sota VWAP {vwap:.2f} ({deviation_pct*100:+.3f}%)",
                take_profit=tp_target,
                stop_loss=sl_target,
            )
            self.record_signal(sig)
            return sig

        # 2. Preu per sobre del VWAP (Sobrecompra micro):
        # Filtre de seguretat: Només venem si estem en tendència baixista (preu <= macro_ema)
        elif deviation_pct >= self.dev_threshold_pct and mid <= macro_ema:
            self.last_signal_time[coin] = now
            tp_target = book.best_ask * (1.0 - config.default_take_profit_pct)
            sl_target = book.best_ask * (1.0 + config.default_stop_loss_pct)
            sig = Signal(
                coin=coin,
                action="SELL",
                price=book.best_ask,
                strategy_name=self.name,
                reason=f"Micro Mean-Rev Overbought (Trend Bearish): Preu {mid:.2f} sobre VWAP {vwap:.2f} ({deviation_pct*100:+.3f}%)",
                take_profit=tp_target,
                stop_loss=sl_target,
            )
            self.record_signal(sig)
            return sig

        return None
