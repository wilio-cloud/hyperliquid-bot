import time
import pytest

from core.arbitrage_models import ArbitrageDirection, ArbitrageLeg, ArbitragePosition, ArbitrageSignal
from core.arbitrage_paper_exchange import ArbitragePaperExchange
from core.models import BookLevel, OrderBookL2, OrderSide
from strategies.funding_carry import FundingCarryStrategy

def make_book(coin: str, bid: float, ask: float) -> OrderBookL2:
    mid = (bid + ask) / 2.0
    return OrderBookL2(
        coin=coin,
        bids=[BookLevel(price=bid, size=100.0)],
        asks=[BookLevel(price=ask, size=100.0)],
        best_bid=bid,
        best_ask=ask,
        mid_price=mid,
        spread_pct=((ask - bid) / mid) * 100.0,
        timestamp=time.time(),
    )

def test_funding_carry_entry_conditions():
    strat = FundingCarryStrategy(min_entry_apr=16.0, min_entry_spread_pct=-0.050)
    
    # 1. Update books for SOL
    hl_book = make_book("SOL", 150.05, 150.10)
    bn_book = make_book("SOL", 150.00, 150.05)
    strat.update_hl_book(hl_book)
    strat.update_bn_book(bn_book)
    
    # Case A: APR diff is low (e.g. 5% APR) -> No signal
    # HL funding hourly: 0.0010% -> 0.0010 * 24 * 365 = 8.76%
    # BN funding 8h: 0.0050% -> (0.0050 / 8) * 24 * 365 = 5.475% -> diff = +3.28%
    strat.update_hl_funding("SOL", 0.0010)
    strat.update_bn_funding({"SOL": 0.0050})
    sig = strat.evaluate_entry("SOL")
    assert sig is None

    # Case B: APR diff is high (+25.0% APR) and spread is positive (+0.033%) -> Signal
    # HL funding hourly: 0.0040% -> 0.0040 * 24 * 365 = 35.04%
    # BN funding 8h: 0.0080% -> (0.0080 / 8) * 24 * 365 = 8.76% -> diff = +26.28%
    strat.update_hl_funding("SOL", 0.0040)
    strat.update_bn_funding({"SOL": 0.0080})
    sig = strat.evaluate_entry("SOL")
    assert sig is not None
    assert sig.coin == "SOL"
    assert sig.direction == ArbitrageDirection.SELL_HL_BUY_BN
    assert sig.strategy_type == "FUNDING_CARRY"
    assert sig.net_funding_apr > 20.0

    # Case C: APR diff is high (+26%), but spread is deeply adverse (-0.15% < -0.05%) -> Filtered
    hl_bad_book = make_book("SOL", 149.80, 149.85)
    bn_bad_book = make_book("SOL", 150.10, 150.15)
    strat.update_hl_book(hl_bad_book)
    strat.update_bn_book(bn_bad_book)
    sig_adverse = strat.evaluate_entry("SOL")
    assert sig_adverse is None

def test_funding_carry_exit_inversion():
    strat = FundingCarryStrategy(min_entry_apr=16.0, min_exit_apr=4.0, min_holding_hours=1.0)
    coin = "NEAR"
    
    hl_book = make_book(coin, 5.00, 5.01)
    bn_book = make_book(coin, 4.99, 5.00)
    strat.update_hl_book(hl_book)
    strat.update_bn_book(bn_book)
    
    pos = ArbitragePosition(
        pair_id="carry_near_1",
        coin=coin,
        direction=ArbitrageDirection.SELL_HL_BUY_BN,
        leg_hl=ArbitrageLeg(
            venue="HYPERLIQUID", coin=coin, side=OrderSide.SELL,
            entry_price=5.00, size=100.0, size_usd=500.0, fee_rate=0.00035, fees_paid=0.175
        ),
        leg_bn=ArbitrageLeg(
            venue="AEVO", coin=coin, side=OrderSide.BUY,
            entry_price=5.00, size=100.0, size_usd=500.0, fee_rate=0.00050, fees_paid=0.250
        ),
        entry_spread_pct=0.05,
        entry_time=time.time() - 7200.0,  # 2 hours old (> 1.0h min_holding_hours)
        accumulated_funding=2.50,         # Accrued funding covers fees and small spread
        strategy_type="FUNDING_CARRY",
    )
    
    # When funding APR is still high (25% APR) -> No exit
    strat.update_hl_funding(coin, 0.0040)
    strat.update_bn_funding({coin: 0.0080})
    exit_eval = strat.check_exit(pos)
    assert exit_eval is None

    # When funding APR compresses to 2.0% (<= 4.0% min_exit_apr) -> Compression exit
    strat.update_hl_funding(coin, 0.0005)
    strat.update_bn_funding({coin: 0.0020})
    exit_eval = strat.check_exit(pos)
    assert exit_eval is not None
    assert "FUNDING_COMPRESSION_EXIT" in exit_eval[0]

    # When funding APR inverts to negative -> Inversion exit
    strat.update_hl_funding(coin, -0.0010)
    strat.update_bn_funding({coin: 0.0080})
    exit_eval_inv = strat.check_exit(pos)
    assert exit_eval_inv is not None
    assert "FUNDING_INVERSION_EXIT" in exit_eval_inv[0]

def test_funding_carry_windfall_take_profit():
    strat = FundingCarryStrategy(windfall_take_profit_pct=0.80)
    coin = "ETH"
    
    pos = ArbitragePosition(
        pair_id="carry_eth_1",
        coin=coin,
        direction=ArbitrageDirection.SELL_HL_BUY_BN,
        leg_hl=ArbitrageLeg(
            venue="HYPERLIQUID", coin=coin, side=OrderSide.SELL,
            entry_price=3000.0, size=0.1, size_usd=300.0, fee_rate=0.00035, fees_paid=0.105
        ),
        leg_bn=ArbitrageLeg(
            venue="BINANCE", coin=coin, side=OrderSide.BUY,
            entry_price=3000.0, size=0.1, size_usd=300.0, fee_rate=0.00040, fees_paid=0.120
        ),
        entry_spread_pct=0.02,
        entry_time=time.time() - 600.0,  # Only 10 min old
        strategy_type="FUNDING_CARRY",
    )
    
    # Basis moves dramatically in our favor: HL dropped to 2950, BN rose to 3020 -> huge gain
    hl_book = make_book(coin, 2950.0, 2951.0)
    bn_book = make_book(coin, 3019.0, 3020.0)
    strat.update_hl_book(hl_book)
    strat.update_bn_book(bn_book)
    
    exit_eval = strat.check_exit(pos)
    assert exit_eval is not None
    assert exit_eval[0] == "WINDFALL_TAKE_PROFIT"

def test_paper_exchange_records_carry_metadata():
    exchange = ArbitragePaperExchange(initial_hl_balance=1000.0, initial_bn_balance=1000.0)
    sig = ArbitrageSignal(
        coin="SOL",
        direction=ArbitrageDirection.SELL_HL_BUY_BN,
        hl_price=150.0,
        bn_price=150.0,
        spread_pct=0.05,
        net_funding_apr=28.5,
        strategy_type="FUNDING_CARRY",
        reason="Test Carry",
    )
    
    pos = exchange.open_arbitrage_position(sig, size_usd=200.0)
    assert pos is not None
    assert pos.strategy_type == "FUNDING_CARRY"
    assert pos.current_net_apr == 28.5
    assert pos.funding_payouts_count == 0
    
    # Simulate hourly funding payment
    # HL funding hourly: 0.0020% (Short receives)
    # BN funding 8h: 0.0080% (Long pays rate/8 = 0.0010%)
    # Net: 0.0010% of $200 = $0.002
    exchange.apply_hourly_funding("SOL", hl_funding_hourly_pct=0.0020, bn_funding_8h_pct=0.0080)
    assert pos.funding_payouts_count == 1
    assert pos.accumulated_funding > 0.0
