"""Estratègia d'Arbitratge Delta-Neutral Creuat (Hyperliquid vs Binance Futures)."""

import datetime
import logging
import time
from typing import Dict, List, Optional, Tuple

from core.arbitrage_models import (
    ArbitrageDirection,
    ArbitragePosition,
    ArbitrageSignal,
    CrossSpreadInfo,
)
from core.models import OrderBookL2

logger = logging.getLogger("CrossArbitrage")

class CrossExchangeArbitrageStrategy:
    def __init__(
        self,
        min_entry_spread_pct: float = 0.150,   # Dislocació mínima d'entrada (+0.150% per cobrir comissions i garantir guany net)
        weekend_min_spread_pct: float = 0.120, # Llindar dinàmic per a caps de setmana (volum més tranquil)
        auto_weekend_adjust: bool = True,       # Ajust automàtic segons calendari UTC
        min_funding_harvest_apr: float = 8.0,  # Llindar d'APR per obrir collita de funding passiu (ex: +8.0%)
        target_exit_spread_pct: float = 0.010, # Convergència de sortida (<= +0.010%)
        min_profit_usd: float = 0.05,          # Benefici net mínim permes a la convergència (+0.05$)
        take_profit_usd: float = 0.50,         # Tancament automàtic per benefici substancial (+0.50$)
        max_divergence_pct: float = 2.50,      # Stop de divergència (+2.50% addicional per a deslligaments reals, no metxes de 1 cèntim)
        divergence_min_duration_sec: float = 60.0, # Requereix que la divergència sigui sostinguda almenys 60 segons
        max_hold_seconds: int = 43200,         # 12 hores màxim per posició (permet collir funding passiu)
        max_book_spread_pct: float = 0.160,    # Llindar màxim d'spread intern (apte per a BTC/ETH/SOL a dYdX i HL, bloqueja il·líquids)
    ):
        self.name = "CROSS_ARBITRAGE"
        self.min_entry_spread_pct = min_entry_spread_pct
        self.weekend_min_spread_pct = weekend_min_spread_pct
        self.auto_weekend_adjust = auto_weekend_adjust
        self.min_funding_harvest_apr = min_funding_harvest_apr
        self.target_exit_spread_pct = target_exit_spread_pct
        self.min_profit_usd = min_profit_usd
        self.take_profit_usd = take_profit_usd
        self.max_divergence_pct = max_divergence_pct
        self.divergence_min_duration_sec = divergence_min_duration_sec
        self.max_hold_seconds = max_hold_seconds
        self.max_book_spread_pct = max_book_spread_pct

        self.hl_books: Dict[str, OrderBookL2] = {}
        self.bn_books: Dict[str, OrderBookL2] = {}
        
        # Funding rates: HL en hourly (ex: 0.00125%), BN en 8h (ex: 0.0100%)
        self.hl_funding_hourly_pct: Dict[str, float] = {}
        self.bn_funding_8h_pct: Dict[str, float] = {}

        self.signals_count = 0

    @property
    def is_weekend_regime(self) -> bool:
        """Determina si estem en règim de cap de setmana (divendres 21:00 UTC a diumenge 22:00 UTC)."""
        now = datetime.datetime.now(datetime.timezone.utc)
        w = now.weekday()
        if w == 4 and now.hour >= 21:
            return True
        if w == 5:
            return True
        if w == 6 and now.hour < 22:
            return True
        return False

    @property
    def effective_min_spread(self) -> float:
        """Retorna el llindar efectiu: 0.120% en cap de setmana, 0.150% entre setmana."""
        if self.auto_weekend_adjust and self.is_weekend_regime:
            return self.weekend_min_spread_pct
        return self.min_entry_spread_pct

    def update_hl_book(self, book: OrderBookL2):
        self.hl_books[book.coin] = book

    def update_bn_book(self, book: OrderBookL2):
        self.bn_books[book.coin] = book

    def update_hl_funding(self, coin: str, funding_hourly_pct: float):
        self.hl_funding_hourly_pct[coin] = funding_hourly_pct

    def update_bn_funding(self, funding_dict: Dict[str, float]):
        self.bn_funding_8h_pct.update(funding_dict)

    def calculate_spread_info(self, coin: str) -> Optional[CrossSpreadInfo]:
        """Calcula l'estat del spread de llibre encreuat i diferència de funding."""
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

        # Cas 1: Preu HL > BN -> Venem a Bid HL i comprem a Ask BN
        spread_sell_hl_buy_bn = ((hl_bid - bn_ask) / global_mid) * 100.0

        # Cas 2: Preu BN > HL -> Venem a Bid BN i comprem a Ask HL
        spread_buy_hl_sell_bn = ((bn_bid - hl_ask) / global_mid) * 100.0

        hl_fund_h = self.hl_funding_hourly_pct.get(coin, 0.0)
        bn_fund_8h = self.bn_funding_8h_pct.get(coin, 0.0)
        
        # Càlcul de rendiment anualitzat (APR) de la diferència de funding
        hl_apr = hl_fund_h * 24.0 * 365.0
        bn_apr = (bn_fund_8h / 8.0) * 24.0 * 365.0
        annual_diff_apr = hl_apr - bn_apr

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
        """Avalua si el spread entre HL i BN supera el llindar d'entrada rendible."""
        info = self.calculate_spread_info(coin)
        if not info:
            return None

        # Filtre de salut del llibre d'ordres (Book Spread Guard):
        # Rebutja si el llibre intern de qualsevol exchange és buit o massa ampli (> max_book_spread_pct)
        hl_book = self.hl_books.get(coin)
        bn_book = self.bn_books.get(coin)
        if hl_book and hl_book.best_bid and hl_book.best_ask and hl_book.mid_price > 0:
            hl_inner_spread = ((hl_book.best_ask - hl_book.best_bid) / hl_book.mid_price) * 100.0
            if hl_inner_spread > self.max_book_spread_pct:
                return None
        if bn_book and bn_book.best_bid and bn_book.best_ask and bn_book.mid_price > 0:
            bn_inner_spread = ((bn_book.best_ask - bn_book.best_bid) / bn_book.mid_price) * 100.0
            if bn_inner_spread > self.max_book_spread_pct:
                return None

        target_spread = self.effective_min_spread

        # Cas 1: Preu HL supera BN (Dislocació de spread)
        if info.spread_sell_hl_buy_bn_pct >= target_spread:
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
                reason=f"Spread HL>BN: {info.spread_sell_hl_buy_bn_pct:+.3f}% >= {target_spread:.3f}% (APR Dif: {info.annual_funding_diff_apr:+.1f}%)",
            )

        # Cas 2: Preu BN supera HL (Dislocació de spread)
        if info.spread_buy_hl_sell_bn_pct >= target_spread:
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
                reason=f"Spread BN>HL: {info.spread_buy_hl_sell_bn_pct:+.3f}% >= {target_spread:.3f}% (APR Dif: {-info.annual_funding_diff_apr:+.1f}%)",
            )

        # Cas 3: Collita de Funding Rate (Carry Trade delta-neutral)
        # Permet entrada si la diferència de funding és molt atractiva i el spread de preu no ens suposa pèrdua (>= -0.040%)
        min_harvest_apr = self.min_funding_harvest_apr if self.is_weekend_regime else (self.min_funding_harvest_apr * 1.5)
        if info.annual_funding_diff_apr >= min_harvest_apr and info.spread_sell_hl_buy_bn_pct >= -0.040:
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
                reason=f"Funding Harvest HL>BN (+{info.annual_funding_diff_apr:.1f}% APR)",
            )
        elif -info.annual_funding_diff_apr >= min_harvest_apr and info.spread_buy_hl_sell_bn_pct >= -0.040:
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
                reason=f"Funding Harvest BN>HL (+{-info.annual_funding_diff_apr:.1f}% APR)",
            )

        return None

    def check_exit(self, pos: ArbitragePosition) -> Optional[Tuple[str, float, float]]:
        """
        Comprova si una posició activa ha assolit la convergència o el límit de temps.
        Retorna (motiu, preu_sortida_hl, preu_sortida_bn) si s'ha de tancar.
        """
        hl_book = self.hl_books.get(pos.coin)
        bn_book = self.bn_books.get(pos.coin)
        if not hl_book or not bn_book or not (hl_book.best_bid and hl_book.best_ask and bn_book.best_bid and bn_book.best_ask):
            return None

        global_mid = (hl_book.mid_price + bn_book.mid_price) / 2.0

        if pos.direction == ArbitrageDirection.SELL_HL_BUY_BN:
            # Entrada: Venem HL (bid), Comprem BN (ask).
            # Sortida: Comprem HL (ask), Venem BN (bid).
            hl_exit_px = hl_book.best_ask
            bn_exit_px = bn_book.best_bid
            # El spread actual per desfer la posició:
            current_spread_to_close = ((hl_exit_px - bn_exit_px) / global_mid) * 100.0
            pos.current_spread_pct = current_spread_to_close

            # Càlcul del PnL net projectat (utilitzant comissions Maker per a sortida ordenada per límit):
            hl_gross = (pos.leg_hl.entry_price - hl_exit_px) * pos.leg_hl.size
            bn_gross = (bn_exit_px - pos.leg_bn.entry_price) * pos.leg_bn.size
            hl_exit_fee = pos.leg_hl.size * hl_exit_px * 0.00010  # Maker exit 0.010%
            bn_exit_fee = pos.leg_bn.size * bn_exit_px * 0.00020  # Maker exit 0.020%
            projected_total_fees = pos.total_fees + hl_exit_fee + bn_exit_fee
            projected_net_pnl = (hl_gross + bn_gross) + pos.accumulated_funding - projected_total_fees

            # 1. Take profit anticipat si funding o inversió de spread generen un guany substancial
            if projected_net_pnl >= self.take_profit_usd:
                return ("TAKE_PROFIT_TARGET", hl_exit_px, bn_exit_px)

            # 2. Convergència reeixida NOMÉS si el benefici net és positiu (cobreix comissions + marge)
            if current_spread_to_close <= self.target_exit_spread_pct:
                if projected_net_pnl >= self.min_profit_usd:
                    return ("CONVERGENCE_TARGET", hl_exit_px, bn_exit_px)
                # Si no arriba al marge mínim, com que la posició és delta-neutral (0 risc direccional),
                # no tanquem amb pèrdua: mantenim per cobrar funding o esperar una oscil·lació millor

            # 3. Stop loss de divergència catastròfica amb filtre de persistència (evita tancar en metxes de soroll)
            if current_spread_to_close >= (pos.entry_spread_pct + self.max_divergence_pct):
                if pos.divergence_start_time is None:
                    pos.divergence_start_time = time.time()
                elif (time.time() - pos.divergence_start_time) >= self.divergence_min_duration_sec:
                    return ("STOP_LOSS_DIVERGENCE", hl_exit_px, bn_exit_px)
            else:
                pos.divergence_start_time = None

        else:  # BUY_HL_SELL_BN
            # Entrada: Comprem HL (ask), Venem BN (bid).
            # Sortida: Venem HL (bid), Comprem BN (ask).
            hl_exit_px = hl_book.best_bid
            bn_exit_px = bn_book.best_ask
            current_spread_to_close = ((bn_exit_px - hl_exit_px) / global_mid) * 100.0
            pos.current_spread_pct = current_spread_to_close

            # Càlcul del PnL net projectat:
            hl_gross = (hl_exit_px - pos.leg_hl.entry_price) * pos.leg_hl.size
            bn_gross = (pos.leg_bn.entry_price - bn_exit_px) * pos.leg_bn.size
            hl_exit_fee = pos.leg_hl.size * hl_exit_px * 0.00010
            bn_exit_fee = pos.leg_bn.size * bn_exit_px * 0.00020
            projected_total_fees = pos.total_fees + hl_exit_fee + bn_exit_fee
            projected_net_pnl = (hl_gross + bn_gross) + pos.accumulated_funding - projected_total_fees

            if projected_net_pnl >= self.take_profit_usd:
                return ("TAKE_PROFIT_TARGET", hl_exit_px, bn_exit_px)

            if current_spread_to_close <= self.target_exit_spread_pct:
                if projected_net_pnl >= self.min_profit_usd:
                    return ("CONVERGENCE_TARGET", hl_exit_px, bn_exit_px)

            if current_spread_to_close >= (pos.entry_spread_pct + self.max_divergence_pct):
                if pos.divergence_start_time is None:
                    pos.divergence_start_time = time.time()
                elif (time.time() - pos.divergence_start_time) >= self.divergence_min_duration_sec:
                    return ("STOP_LOSS_DIVERGENCE", hl_exit_px, bn_exit_px)
            else:
                pos.divergence_start_time = None

        # Límit de temps de seguretat (12 hores)
        if (time.time() - pos.entry_time) >= self.max_hold_seconds:
            return ("TIME_LIMIT", hl_exit_px, bn_exit_px)

        return None
