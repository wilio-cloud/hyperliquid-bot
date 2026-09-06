"""Mòdul de gestió de risc i protecció de capital."""

import logging
import time
from typing import Dict, Optional
from config.settings import config
from core.models import OrderBookL2, Position

logger = logging.getLogger("RiskManager")

class RiskManager:
    def __init__(self):
        self.cooldown_until: Dict[str, float] = {}  # coin -> timestamp
        self.consecutive_losses: int = 0
        self.daily_start_time = time.time()
        self.max_holding_time_seconds = 300.0  # 5 minuts màxim per operació de scalping

    def can_open_position(
        self,
        coin: str,
        current_balance: float,
        initial_balance: float,
        open_positions_count: int,
        has_existing_coin_position: bool,
    ) -> tuple[bool, str]:
        """Avalua si el bot té permís per obrir una nova posició."""
        # 1. Comprovació de posició existent en aquest actiu
        if has_existing_coin_position:
            return False, f"Ja hi ha una posició activa per a {coin}"

        # 2. Límit màxim de posicions simultànies
        if open_positions_count >= config.max_open_positions:
            return False, f"Límit de posicions simultànies assolit ({open_positions_count}/{config.max_open_positions})"

        # 3. Circuit Breaker de pèrdua màxima diària
        total_pnl = current_balance - initial_balance
        if total_pnl <= -config.max_daily_loss_usd:
            return False, f"CIRCUIT BREAKER: Pèrdua acumulada ({total_pnl:.2f}$) supera el límit permès (-{config.max_daily_loss_usd:.2f}$)"

        # 4. Pèrdues consecutives
        if self.consecutive_losses >= config.max_consecutive_losses:
            return False, f"PAUSA DE SEGURETAT: {self.consecutive_losses} pèrdues consecutives detectades"

        # 5. Cooldown específic de l'actiu
        now = time.time()
        if coin in self.cooldown_until and now < self.cooldown_until[coin]:
            remaining = int(self.cooldown_until[coin] - now)
            return False, f"Cooldown actiu per a {coin} ({remaining}s restants)"

        return True, "OK"

    def record_closed_position(self, pos: Position):
        """Registra el resultat d'una posició per ajustar cooldowns i comptadors de ratxa."""
        now = time.time()
        self.cooldown_until[pos.coin] = now + config.cooldown_seconds

        if pos.realized_pnl > 0:
            self.consecutive_losses = 0
            logger.info(f"[RISK] Guany registrat a {pos.coin}. Racha de pèrdues reiniciada.")
        else:
            self.consecutive_losses += 1
            logger.warning(f"[RISK] Pèrdua a {pos.coin}. Racha actual: {self.consecutive_losses} pèrdues consecutives.")

    def check_time_exits(self, positions: Dict[str, Position], order_books: Dict[str, OrderBookL2]) -> list[str]:
        """Detecta posicions que han superat el temps màxim permès per a scalping sense matar trades prematurament."""
        to_close = []
        now = time.time()
        for coin, pos in positions.items():
            duration = now - pos.entry_time
            # Gestió intel·ligent del temps:
            # 1. Si supera 15 minuts (900s), forcem tancament de seguretat
            if duration >= 900.0:
                logger.info(f"[RISK TIME EXIT] {coin} ha superat 15m ({duration:.1f}s). Tancant.")
                to_close.append(coin)
            # 2. Si dura més de 5 minuts (300s), només tanquem si ja cobreix comissions amb guany net (+0.25$) o pèrdua creixent (<-0.80$)
            elif duration >= 300.0:
                if pos.unrealized_pnl >= 0.25:
                    logger.info(f"[RISK TIME PROFIT EXIT] {coin} tancant amb guany net ({pos.unrealized_pnl:+.3f}$) als {duration:.1f}s.")
                    to_close.append(coin)
                elif pos.unrealized_pnl <= -0.80:
                    logger.info(f"[RISK TIME DEFENSE EXIT] {coin} tancant per pèrdua ({pos.unrealized_pnl:+.3f}$) als {duration:.1f}s.")
                    to_close.append(coin)
        return to_close
