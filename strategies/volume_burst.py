"""Estratègia 2: Pic de Volum / Ràfega de Tapes (Volume Burst Momentum)."""

import time
from collections import deque
from typing import Dict, Optional
from config.settings import config
from core.models import OrderBookL2, OrderSide, Signal, Trade
from strategies.base_strategy import BaseStrategy

class VolumeBurstStrategy(BaseStrategy):
    def __init__(
        self,
        burst_window_sec: float = config.volume_burst_window_sec,
        burst_multiplier: float = config.volume_burst_multiplier,
        min_direction_ratio: float = 0.75,  # 75% del volum en una sola direcció
        min_signal_interval_sec: float = 10.0,
    ):
        super().__init__(name="Volume_Burst", enabled=True)
        self.burst_window_sec = burst_window_sec
        self.burst_multiplier = burst_multiplier
        self.min_direction_ratio = min_direction_ratio
        self.min_signal_interval_sec = min_signal_interval_sec
        
        # Historial de trades recents per moneda: deque de (timestamp, size_usd, side)
        self.trades_history: Dict[str, deque] = {}
        self.last_signal_time: Dict[str, float] = {}

    def on_book_update(self, book: OrderBookL2) -> Optional[Signal]:
        return None

    def on_trade(self, trade: Trade) -> Optional[Signal]:
        if not self.enabled:
            return None

        coin = trade.coin
        now = time.time()

        if coin not in self.trades_history:
            self.trades_history[coin] = deque()

        trade_usd = trade.price * trade.size
        self.trades_history[coin].append((now, trade_usd, trade.side))

        # Neteja de trades antics (> 60 segons per mantenir la línia base)
        while self.trades_history[coin] and (now - self.trades_history[coin][0][0] > 60.0):
            self.trades_history[coin].popleft()

        # Comprovació d'interval mínim entre senyals
        if now - self.last_signal_time.get(coin, 0) < self.min_signal_interval_sec:
            return None

        # Càlcul de volum en la finestra de ràfega (ex: darrers 5 segons)
        burst_buys = 0.0
        burst_sells = 0.0
        baseline_total = 0.0

        for ts, usd, side in self.trades_history[coin]:
            baseline_total += usd
            if now - ts <= self.burst_window_sec:
                if side == OrderSide.BUY:
                    burst_buys += usd
                else:
                    burst_sells += usd

        burst_total = burst_buys + burst_sells
        if burst_total <= 0:
            return None

        # Volum mitjà esperat en 5 segons basat en l'últim minut
        expected_burst_vol = (baseline_total / 60.0) * self.burst_window_sec
        if expected_burst_vol <= 0:
            return None

        vol_ratio = burst_total / expected_burst_vol

        # 1. Ràfega compradora agressiva
        if vol_ratio >= self.burst_multiplier and (burst_buys / burst_total) >= self.min_direction_ratio:
            self.last_signal_time[coin] = now
            sig = Signal(
                coin=coin,
                action="BUY",
                price=trade.price,
                strategy_name=self.name,
                reason=f"Volume Burst {vol_ratio:.1f}x ({burst_buys:,.0f}$ compres en {self.burst_window_sec:.0f}s)",
            )
            self.record_signal(sig)
            return sig

        # 2. Ràfega venedora agressiva
        elif vol_ratio >= self.burst_multiplier and (burst_sells / burst_total) >= self.min_direction_ratio:
            self.last_signal_time[coin] = now
            sig = Signal(
                coin=coin,
                action="SELL",
                price=trade.price,
                strategy_name=self.name,
                reason=f"Volume Dump {vol_ratio:.1f}x ({burst_sells:,.0f}$ vendes en {self.burst_window_sec:.0f}s)",
            )
            self.record_signal(sig)
            return sig

        return None
