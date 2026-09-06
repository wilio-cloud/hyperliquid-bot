"""Proves unitàries per al sistema d'arbitratge creuat (Hyperliquid vs Binance)."""

import math
from core.arbitrage_models import ArbitrageDirection, ArbitrageLeg, ArbitragePosition, ArbitrageSignal
from core.arbitrage_paper_exchange import ArbitragePaperExchange
from core.models import BookLevel, OrderBookL2, OrderSide
from strategies.cross_arbitrage import CrossExchangeArbitrageStrategy

def test_spread_calculation_and_signal():
    strat = CrossExchangeArbitrageStrategy(min_entry_spread_pct=0.100, target_exit_spread_pct=0.015)
    
    # Simulem preus on Hyperliquid és més car: HL mid 80160, BN mid 80000
    # Spread ~ 0.20%
    hl_book = OrderBookL2(
        coin="BTC",
        timestamp=1000.0,
        bids=[BookLevel(price=80159.0, size=1.0)],
        asks=[BookLevel(price=80161.0, size=1.0)],
    )
    bn_book = OrderBookL2(
        coin="BTC",
        timestamp=1000.0,
        bids=[BookLevel(price=79999.0, size=1.0)],
        asks=[BookLevel(price=80001.0, size=1.0)],
    )

    strat.update_hl_book(hl_book)
    strat.update_bn_book(bn_book)
    strat.update_hl_funding("BTC", 0.0025)  # 0.0025% per hora
    strat.update_bn_funding({"BTC": 0.0100})  # 0.0100% per 8h

    info = strat.calculate_spread_info("BTC")
    assert info is not None
    # HL bid (80159) vs BN ask (80001) -> diferència positiva = 158$ (~0.197%)
    assert info.spread_sell_hl_buy_bn_pct > 0.100

    signal = strat.evaluate_entry("BTC")
    assert signal is not None
    assert signal.direction == ArbitrageDirection.SELL_HL_BUY_BN
    assert signal.hl_price == 80159.0
    assert signal.bn_price == 80001.0

def test_arbitrage_execution_and_pnl():
    exchange = ArbitragePaperExchange(
        initial_hl_balance=5000.0,
        initial_bn_balance=5000.0,
        hl_taker_fee=0.00035,
        bn_taker_fee=0.00040,
    )
    strat = CrossExchangeArbitrageStrategy(target_exit_spread_pct=0.015)

    signal = ArbitrageSignal(
        coin="BTC",
        direction=ArbitrageDirection.SELL_HL_BUY_BN,
        hl_price=80160.0,
        bn_price=80000.0,
        spread_pct=0.200,
        hl_funding_8h_pct=0.020,
        bn_funding_8h_pct=0.010,
        net_funding_apr=15.0,
    )

    # Obrim posició de 1.000$ per pota
    pos = exchange.open_arbitrage_position(signal, size_usd=1000.0, is_maker=False)
    assert pos is not None
    assert len(exchange.active_positions) == 1
    assert pos.leg_hl.side == OrderSide.SELL
    assert pos.leg_bn.side == OrderSide.BUY

    # Comprovem comissions deduïdes: 0.35$ (HL) + 0.40$ (BN) = 0.75$
    expected_fees = 1000.0 * 0.00035 + 1000.0 * 0.00040
    assert abs(exchange.total_fees_paid - expected_fees) < 1e-4

    # Simulem convergència dels preus a 80050 (spread 0.5$ entre bid i ask)
    hl_converged = OrderBookL2(
        coin="BTC",
        timestamp=1005.0,
        bids=[BookLevel(price=80049.5, size=1.0)],
        asks=[BookLevel(price=80050.5, size=1.0)],
    )
    bn_converged = OrderBookL2(
        coin="BTC",
        timestamp=1005.0,
        bids=[BookLevel(price=80049.5, size=1.0)],
        asks=[BookLevel(price=80050.5, size=1.0)],
    )
    strat.update_hl_book(hl_converged)
    strat.update_bn_book(bn_converged)

    exit_check = strat.check_exit(pos)
    assert exit_check is not None
    reason, hl_px, bn_px = exit_check
    assert reason == "CONVERGENCE_TARGET"

    # Tancament
    closed_pos = exchange.close_arbitrage_position(pos.pair_id, hl_px, bn_px, reason=reason, is_maker=False)
    assert closed_pos is not None
    assert closed_pos.is_closed is True
    assert len(exchange.active_positions) == 0
    assert len(exchange.closed_positions) == 1

    # Comprovem que s'ha obtingut un guany net (spread 0.20% = ~2.00$ brut - ~1.50$ fees = ~+0.50$)
    assert closed_pos.realized_pnl > 0.0
    assert exchange.metrics["wins"] == 1
    assert exchange.metrics["balance"] > 10000.0

def test_funding_accrual():
    exchange = ArbitragePaperExchange(initial_hl_balance=5000.0, initial_bn_balance=5000.0)
    signal = ArbitrageSignal(
        coin="ETH",
        direction=ArbitrageDirection.SELL_HL_BUY_BN,
        hl_price=2500.0,
        bn_price=2495.0,
        spread_pct=0.20,
    )
    pos = exchange.open_arbitrage_position(signal, size_usd=1000.0)
    assert pos is not None

    # Simulem cobrament de funding: HL té funding positiu (0.002% per hora), BN té 0.005% per 8h
    # HL Short rep (+0.002% * 1000 = +0.02$)
    # BN Long paga (- (0.005% / 8) * 1000 = -0.00625$)
    # Net per hora: +0.01375$
    exchange.apply_hourly_funding("ETH", hl_funding_hourly_pct=0.002, bn_funding_8h_pct=0.005)
    assert pos.accumulated_funding > 0.0
    assert exchange.total_funding_collected > 0.0

if __name__ == "__main__":
    test_spread_calculation_and_signal()
    test_arbitrage_execution_and_pnl()
    test_funding_accrual()
    print("✅ Tots els tests d'arbitratge han passat amb èxit!")
