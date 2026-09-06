"""Interfície base per a totes les estratègies de scalping."""

from abc import ABC, abstractmethod
from typing import Optional
from core.models import OrderBookL2, Signal, Trade

class BaseStrategy(ABC):
    def __init__(self, name: str, enabled: bool = True):
        self.name = name
        self.enabled = enabled
        self.signals_count: int = 0

    @abstractmethod
    def on_book_update(self, book: OrderBookL2) -> Optional[Signal]:
        """S'executa cada vegada que arriba una actualització del llibre d'ordres L2."""
        pass

    @abstractmethod
    def on_trade(self, trade: Trade) -> Optional[Signal]:
        """S'executa cada vegada que un trade de mercat té lloc."""
        pass

    def record_signal(self, signal: Signal):
        self.signals_count += 1
