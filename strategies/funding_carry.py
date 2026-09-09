"""Estratègia dedicada de Funding Rate Carry Trade Delta-Neutral (Hyperliquid vs Venue 2).

Aquesta estratègia no busca petits desajustos de preu (scalping ràpid de minuts), sinó
capturar de forma sistemàtica el diferencial de taxes de finançament (Funding Rate APR)
mantenint una posició 100% delta-neutral (Short en l'exchange que paga, Long en el que cobra menys).
"""

import logging
import time
from typing import Dict, Optional, Tuple

from core.arbitrage_models import (
    ArbitrageDirection,
    ArbitragePosition,
    ArbitrageSignal,
    CrossSpreadInfo,
)
from core.models import OrderBookL2

logger = logging.getLogger("FundingCarry")

class FundingCarryStrategy:
    def __init__(
        self,
        min_entry_apr: float = 16.0,          # APR net mínim anualitzat per obrir posició (+16.0%)
        min_exit_apr: float = 4.0,            # Llindar d'APR per sota del qual es tanca la posició (compressió/inversió de rendiment)
        min_entry_spread_pct: float = -0.050, # Preu de base mínim permissible a l'entrada (>= -0.05% per no començar amb base adversa)
        windfall_take_profit_pct: float = 0.80, # Tancament anticipat per guany extraordinari de base (>= +0.80% net sobre el valor de posició)
        max_basis_divergence_pct: float = 1.80, # Stop loss si el spread de base divergeix en contra més d'un 1.80%
        divergence_min_duration_sec: float = 120.0, # Requereix que la divergència adversa sigui sostinguda almenys 2 minuts
        min_holding_hours: float = 1.0,       # Temps mínim de retenció abans de permetre sortides per compressió de rendiment
        max_book_spread_pct: float = 0.220,   # Filtre de salut del llibre d'ordres (spread intern màxim)
        maker_first: bool = False,            # Execució Maker-First a Hyperliquid
    ):
        self.name = "FUNDING_CARRY"
        self.min_entry_apr = min_entry_apr
        self.min_exit_apr = min_exit_apr
        self.min_entry_spread_pct = min_entry_spread_pct
        self.windfall_take_profit_pct = windfall_take_profit_pct
        self.max_basis_divergence_pct = max_basis_divergence_pct
        self.divergence_min_duration_sec = divergence_min_duration_sec
        self.min_holding_hours = min_holding_hours
        self.max_book_spread_pct = max_book_spread_pct
        self.maker_first = maker_first

        self.hl_books: Dict[str, OrderBookL2] = {}
        self.bn_books: Dict[str, OrderBookL2] = {}
        self.hl_funding_hourly_pct: Dict[str, float] = {}
        self.bn_funding_8h_pct: Dict[str, float] = {}
        self.signals_count = 0

    def update_hl_book(self, book: OrderBookL2):
        self.hl_books[book.coin] = book

    def update_bn_book(self, book: OrderBookL2):
        self.bn_books[book.coin] = book

    def update_hl_funding(self, coin: str, funding_hourly_pct: float):
        self.hl_funding_hourly_pct[coin] = funding_hourly_pct

    def update_bn_funding(self, funding_dict: Dict[str, float]):
        self.bn_funding_8h_pct.update(funding_dict)

    def calculate_spread_info(self, coin: str) -> Optional[CrossSpreadInfo]:
        """Calcula l'estat del spread de llibre encreuat i diferencial de funding."""
        hl_book = self.hl_books.get(coin)
        bn_book = self.bn_books.get(coin)
        if not hl_book or not bn_book:
            return None

        hl_bid, hl_ask = hl_book.best_bid, hl_book.best_ask
        bn_bid, bn_ask = bn_book.best_bid, bn_book.best_ask
        if not (hl_bid and hl_ask and bn_bid and bn_ask):
            return None

        hl_mid = (hl_bid + hl_ask) / 2.0
        bn_mid = (bn_bid + bn_ask) / 2.0
        global_mid = (hl_mid + bn_mid) / 2.0
        if global_mid <= 0:
            return None

        # Cas 1: Preu HL > BN -> Venem HL (bid) i comprem BN (ask)
        spread_sell_hl_buy_bn = ((hl_bid - bn_ask) / global_mid) * 100.0

        # Cas 2: Preu BN > HL -> Venem BN (bid) i comprem HL (ask)
        spread_buy_hl_sell_bn = ((bn_bid - hl_ask) / global_mid) * 100.0

        hl_fund_h = self.hl_funding_hourly_pct.get(coin, 0.0)
        bn_fund_8h = self.bn_funding_8h_pct.get(coin, 0.0)

        # Càlcul d'APR anualitzat
        hl_apr = hl_fund_h * 24.0 * 365.0
        bn_apr = (bn_fund_8h / 8.0) * 24.0 * 365.0
        annual_diff_apr = hl_apr - bn_apr  # Positiu = HL paga més que BN

        return CrossSpreadInfo(
            coin=coin,
            hl_bid=hl_bid,
            hl_ask=hl_ask,
            bn_bid=bn_bid,
            bn_ask=bn_ask,
            hl_mid=hl_mid,
            bn_mid=bn_mid,
            spread_sell_hl_buy_bn_pct=spread_sell_hl_buy_bn,
            spread_buy_hl_sell_bn_pct=spread_buy_hl_sell_bn,
            hl_funding_8h_pct=hl_fund_h * 8.0,
            bn_funding_8h_pct=bn_fund_8h,
            annual_funding_diff_apr=annual_diff_apr,
            timestamp=time.time(),
        )

    def evaluate_entry(self, coin: str) -> Optional[ArbitrageSignal]:
        """
        Avalua si la diferència d'APR anualitzat supera el llindar de rendibilitat
        i el spread de preus d'entrada no és advers.
        """
        info = self.calculate_spread_info(coin)
        if not info:
            return None

        # 1. Filtre de salut del llibre intern
        hl_book = self.hl_books.get(coin)
        bn_book = self.bn_books.get(coin)
        if hl_book and hl_book.best_bid and hl_book.best_ask and hl_book.mid_price > 0:
            hl_inner = ((hl_book.best_ask - hl_book.best_bid) / hl_book.mid_price) * 100.0
            if hl_inner > self.max_book_spread_pct:
                return None
        if bn_book and bn_book.best_bid and bn_book.best_ask and bn_book.mid_price > 0:
            bn_inner = ((bn_book.best_ask - bn_book.best_bid) / bn_book.mid_price) * 100.0
            if bn_inner > self.max_book_spread_pct:
                return None

        # 2. Cas Principal: Hyperliquid Funding > Venue 2 Funding
        # Guanyem funding sent SHORT a HL i LONG a Venue 2
        # Requereix: APR diff >= min_entry_apr i spread no advers (>= min_entry_spread_pct)
        if info.annual_funding_diff_apr >= self.min_entry_apr and info.spread_sell_hl_buy_bn_pct >= self.min_entry_spread_pct:
            self.signals_count += 1
            return ArbitrageSignal(
                coin=coin,
                direction=ArbitrageDirection.SELL_HL_BUY_BN,
                hl_price=info.hl_bid,
                bn_price=info.bn_ask,
                spread_pct=info.spread_sell_hl_buy_bn_pct,
                hl_funding_8h_pct=info.hl_funding_8h_pct,
                bn_funding_8h_pct=info.bn_funding_8h_pct,
                net_funding_apr=info.annual_funding_diff_apr,
                strategy_type="FUNDING_CARRY",
                reason=(
                    f"Carry HL>Venue2 (+{info.annual_funding_diff_apr:.1f}% APR) | "
                    f"Spread Base: {info.spread_sell_hl_buy_bn_pct:+.3f}% >= {self.min_entry_spread_pct:+.3f}%"
                ),
            )

        # 3. Cas Secundari: Venue 2 Funding > Hyperliquid Funding
        # Guanyem funding sent LONG a HL i SHORT a Venue 2
        if -info.annual_funding_diff_apr >= self.min_entry_apr and info.spread_buy_hl_sell_bn_pct >= self.min_entry_spread_pct:
            self.signals_count += 1
            return ArbitrageSignal(
                coin=coin,
                direction=ArbitrageDirection.BUY_HL_SELL_BN,
                hl_price=info.hl_ask,
                bn_price=info.bn_bid,
                spread_pct=info.spread_buy_hl_sell_bn_pct,
                hl_funding_8h_pct=info.hl_funding_8h_pct,
                bn_funding_8h_pct=info.bn_funding_8h_pct,
                net_funding_apr=-info.annual_funding_diff_apr,
                strategy_type="FUNDING_CARRY",
                reason=(
                    f"Carry Venue2>HL (+{-info.annual_funding_diff_apr:.1f}% APR) | "
                    f"Spread Base: {info.spread_buy_hl_sell_bn_pct:+.3f}% >= {self.min_entry_spread_pct:+.3f}%"
                ),
            )

        return None

    def check_exit(self, pos: ArbitragePosition) -> Optional[Tuple[str, float, float]]:
        """
        Avalua si una posició de Funding Carry ha de tancar-se.
        Criteris de sortida:
        1. WINDFALL_TAKE_PROFIT: La base de preus es mou tan favorablement que el guany de capital supera dies de funding.
        2. FUNDING_INVERSION_EXIT: L'APR net ha comprimit per sota de `min_exit_apr` (la tesi de rendiment ha acabat).
        3. STOP_LOSS_DIVERGENCE: El spread de base divergeix negativament més enllà de `max_basis_divergence_pct`.
        """
        hl_book = self.hl_books.get(pos.coin)
        bn_book = self.bn_books.get(pos.coin)
        if not hl_book or not bn_book or not (hl_book.best_bid and hl_book.best_ask and bn_book.best_bid and bn_book.best_ask):
            return None

        # Protecció de liquiditat: no tancar si el llibre està eixamplat
        if hl_book.mid_price > 0 and ((hl_book.best_ask - hl_book.best_bid) / hl_book.mid_price) * 100.0 > self.max_book_spread_pct:
            return None
        if bn_book.mid_price > 0 and ((bn_book.best_ask - bn_book.best_bid) / bn_book.mid_price) * 100.0 > self.max_book_spread_pct:
            return None

        global_mid = (hl_book.mid_price + bn_book.mid_price) / 2.0
        pos_age_hours = (time.time() - pos.entry_time) / 3600.0

        # Càlcul de l'APR actual en temps real
        hl_fund_h = self.hl_funding_hourly_pct.get(pos.coin, 0.0)
        bn_fund_8h = self.bn_funding_8h_pct.get(pos.coin, 0.0)
        current_hl_apr = hl_fund_h * 24.0 * 365.0
        current_bn_apr = (bn_fund_8h / 8.0) * 24.0 * 365.0

        if pos.direction == ArbitrageDirection.SELL_HL_BUY_BN:
            current_net_apr = current_hl_apr - current_bn_apr
            hl_exit_px = hl_book.best_ask
            bn_exit_px = bn_book.best_bid
            current_spread_to_close = ((hl_exit_px - bn_exit_px) / global_mid) * 100.0
            pos.current_spread_pct = current_spread_to_close
            pos.current_net_apr = current_net_apr

            # PnL net projectat
            hl_gross = (pos.leg_hl.entry_price - hl_exit_px) * pos.leg_hl.size
            bn_gross = (bn_exit_px - pos.leg_bn.entry_price) * pos.leg_bn.size
            venue2_name = getattr(pos.leg_bn, "venue", "AEVO").upper()
            venue2_fee = 0.00050 if "AEVO" in venue2_name else (0.00020 if "VERTEX" in venue2_name else 0.00040)
            hl_exit_fee = pos.leg_hl.size * hl_exit_px * 0.00035
            bn_exit_fee = pos.leg_bn.size * bn_exit_px * venue2_fee
            projected_net_pnl = (hl_gross + bn_gross) + pos.accumulated_funding - (pos.total_fees + hl_exit_fee + bn_exit_fee)

            pos_size_usd = pos.leg_hl.size * pos.leg_hl.entry_price
            target_windfall_usd = max(0.50, pos_size_usd * (self.windfall_take_profit_pct / 100.0))

            # 1. Take Profit per Windfall de Base (El capital gain cobreix setmanes de funding)
            if projected_net_pnl >= target_windfall_usd:
                return ("WINDFALL_TAKE_PROFIT", hl_exit_px, bn_exit_px)

            # 2. Sortida per compressió/inversió de rendiment d'APR
            # A) Inversió total (APR <= 0%): tanquem perquè la posició començaria a pagar funding.
            # B) Compressió (APR <= min_exit_apr): tanquem un cop complert el temps mínim si cobreix comissions.
            if current_net_apr <= 0.0:
                return (f"FUNDING_INVERSION_EXIT (APR: {current_net_apr:.1f}%)", hl_exit_px, bn_exit_px)
            if (pos_age_hours >= self.min_holding_hours or pos.funding_payouts_count >= 1) and current_net_apr <= self.min_exit_apr:
                if projected_net_pnl >= 0.0:
                    return (f"FUNDING_COMPRESSION_EXIT (APR: {current_net_apr:.1f}%)", hl_exit_px, bn_exit_px)

            # 3. Stop loss de divergència extrema de base
            if current_spread_to_close >= (pos.entry_spread_pct + self.max_basis_divergence_pct):
                if pos.divergence_start_time is None:
                    pos.divergence_start_time = time.time()
                elif (time.time() - pos.divergence_start_time) >= self.divergence_min_duration_sec:
                    return ("STOP_LOSS_DIVERGENCE", hl_exit_px, bn_exit_px)
            else:
                pos.divergence_start_time = None

        else:  # BUY_HL_SELL_BN
            current_net_apr = current_bn_apr - current_hl_apr
            hl_exit_px = hl_book.best_bid
            bn_exit_px = bn_book.best_ask
            current_spread_to_close = ((bn_exit_px - hl_exit_px) / global_mid) * 100.0
            pos.current_spread_pct = current_spread_to_close
            pos.current_net_apr = current_net_apr

            hl_gross = (hl_exit_px - pos.leg_hl.entry_price) * pos.leg_hl.size
            bn_gross = (pos.leg_bn.entry_price - bn_exit_px) * pos.leg_bn.size
            venue2_name = getattr(pos.leg_bn, "venue", "AEVO").upper()
            venue2_fee = 0.00050 if "AEVO" in venue2_name else (0.00020 if "VERTEX" in venue2_name else 0.00040)
            hl_exit_fee = pos.leg_hl.size * hl_exit_px * 0.00035
            bn_exit_fee = pos.leg_bn.size * bn_exit_px * venue2_fee
            projected_net_pnl = (hl_gross + bn_gross) + pos.accumulated_funding - (pos.total_fees + hl_exit_fee + bn_exit_fee)

            pos_size_usd = pos.leg_hl.size * pos.leg_hl.entry_price
            target_windfall_usd = max(0.50, pos_size_usd * (self.windfall_take_profit_pct / 100.0))

            if projected_net_pnl >= target_windfall_usd:
                return ("WINDFALL_TAKE_PROFIT", hl_exit_px, bn_exit_px)

            if current_net_apr <= 0.0:
                return (f"FUNDING_INVERSION_EXIT (APR: {current_net_apr:.1f}%)", hl_exit_px, bn_exit_px)
            if (pos_age_hours >= self.min_holding_hours or pos.funding_payouts_count >= 1) and current_net_apr <= self.min_exit_apr:
                if projected_net_pnl >= 0.0:
                    return (f"FUNDING_COMPRESSION_EXIT (APR: {current_net_apr:.1f}%)", hl_exit_px, bn_exit_px)

            if current_spread_to_close >= (pos.entry_spread_pct + self.max_basis_divergence_pct):
                if pos.divergence_start_time is None:
                    pos.divergence_start_time = time.time()
                elif (time.time() - pos.divergence_start_time) >= self.divergence_min_duration_sec:
                    return ("STOP_LOSS_DIVERGENCE", hl_exit_px, bn_exit_px)
            else:
                pos.divergence_start_time = None

        return None

    def get_exit_diagnostic(self, pos: ArbitragePosition) -> str:
        """Retorna un diagnòstic de sortida per a posicions de Funding Carry."""
        apr = getattr(pos, "current_net_apr", 0.0)
        return f"Collita de Funding activa (APR: {apr:.1f}%)"
