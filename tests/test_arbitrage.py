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
    assert reason in ("CONVERGENCE_TARGET", "TAKE_PROFIT_TARGET")

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

def test_profit_guard_blocks_unprofitable_convergence():
    """Verifica que el bot NO tanca per convergència si el benefici net projectat no cobreix comissions."""
    exchange = ArbitragePaperExchange(initial_hl_balance=5000.0, initial_bn_balance=5000.0)
    strat = CrossExchangeArbitrageStrategy(
        min_entry_spread_pct=0.180,
        target_exit_spread_pct=0.010,
        min_profit_usd=0.05,
    )

    # Simulem una entrada a spread estret de 0.120%
    # HL sell @ 100.12, BN buy @ 100.00
    signal = ArbitrageSignal(
        coin="SOL",
        direction=ArbitrageDirection.SELL_HL_BUY_BN,
        hl_price=100.12,
        bn_price=100.00,
        spread_pct=0.120,
    )
    pos = exchange.open_arbitrage_position(signal, size_usd=1000.0)
    assert pos is not None

    # Simulem convergència superficial: HL ask 100.01, BN bid 100.00 (spread 0.010% <= target_exit_spread_pct)
    # Gross captured: 0.120% - 0.010% = 0.110% = 1.10$.
    # Comissions totals = 1.50$.
    # Resultat net projectat: 1.10$ - 1.50$ = -0.40$ < 0.05$.
    hl_shallow = OrderBookL2(
        coin="SOL",
        timestamp=2000.0,
        bids=[BookLevel(price=100.00, size=10.0)],
        asks=[BookLevel(price=100.01, size=10.0)],
    )
    bn_shallow = OrderBookL2(
        coin="SOL",
        timestamp=2000.0,
        bids=[BookLevel(price=100.00, size=10.0)],
        asks=[BookLevel(price=100.01, size=10.0)],
    )
    strat.update_hl_book(hl_shallow)
    strat.update_bn_book(bn_shallow)

    # El bot HA DE BLOQUEJAR el tancament per evitar registrar una pèrdua
    exit_check = strat.check_exit(pos)
    assert exit_check is None, "El bot hauria d'haver bloquejat el tancament per PnL net negatiu!"

    # Ara simulem que el spread creua a inversió (HL ask 99.90, BN bid 100.05)
    # Gross captured = (100.12 - 99.90) + (100.05 - 100.00) = 0.22 + 0.05 = 0.27% = 2.70$
    # Net = 2.70$ - 1.50$ = +1.20$ >= 0.05$
    hl_deep = OrderBookL2(
        coin="SOL",
        timestamp=2010.0,
        bids=[BookLevel(price=99.89, size=10.0)],
        asks=[BookLevel(price=99.90, size=10.0)],
    )
    bn_deep = OrderBookL2(
        coin="SOL",
        timestamp=2010.0,
        bids=[BookLevel(price=100.05, size=10.0)],
        asks=[BookLevel(price=100.06, size=10.0)],
    )
    strat.update_hl_book(hl_deep)
    strat.update_bn_book(bn_deep)

    exit_check2 = strat.check_exit(pos)
    assert exit_check2 is not None
    assert exit_check2[0] in ("CONVERGENCE_TARGET", "TAKE_PROFIT_TARGET")

    closed = exchange.close_arbitrage_position(pos.pair_id, exit_check2[1], exit_check2[2], reason=exit_check2[0])
    assert closed.realized_pnl > 0.0
    print(f"  Guany net registrat amb profit guard: {closed.realized_pnl:+.3f}$")

def test_dydx_venue2_integration():
    """Verifica el funcionament de dYdX v4 com a segon exchange descentralitzat."""
    from core.dydx_ws_client import DydxV4WSClient
    client = DydxV4WSClient(coins=["BTC", "ETH", "SOL"])
    assert client.symbol_map["BTC"] == "BTC-USD"
    assert client.symbol_map["ETH"] == "ETH-USD"
    assert client.reverse_map["SOL-USD"] == "SOL"

    exchange = ArbitragePaperExchange(
        venue2_name="DYDX",
        initial_hl_balance=5000.0,
        initial_bn_balance=5000.0,
    )
    assert exchange.venue2_name == "DYDX"
    assert exchange.metrics["venue2_name"] == "DYDX"

    signal = ArbitrageSignal(
        coin="ETH",
        direction=ArbitrageDirection.SELL_HL_BUY_BN,
        hl_price=2500.0,
        bn_price=2495.0,
        spread_pct=0.20,
    )
    pos = exchange.open_arbitrage_position(signal, size_usd=1000.0)
    assert pos.leg_bn.venue == "DYDX"
    assert pos.leg_hl.venue == "HYPERLIQUID"

    closed = exchange.close_arbitrage_position(pos.pair_id, 2496.0, 2496.0, reason="CONVERGENCE_TARGET")
    assert closed.leg_bn.venue == "DYDX"
    assert exchange.metrics["venue2_name"] == "DYDX"
    print("  Integració de dYdX v4 verificada amb èxit.")

def test_book_spread_guard_blocks_illiquid_entry():
    """Verifica que el bot rebutja entrar si algun exchange té un forat intern de liquiditat."""
    strat = CrossExchangeArbitrageStrategy(
        min_entry_spread_pct=0.180,
        max_book_spread_pct=0.120,
    )
    # Llibre d'Hyperliquid és líquid (spread 0.02%)
    hl_book = OrderBookL2(
        coin="LINK",
        timestamp=3000.0,
        bids=[BookLevel(price=12.298, size=100.0)],
        asks=[BookLevel(price=12.300, size=100.0)],
    )
    # Llibre de dYdX és il·líquid (spread 0.94%: bid 12.34, ask 12.456)
    # Aparentment hi ha un spread global (12.34 - 12.30 = +0.32% > 0.18%)
    dydx_illiquid = OrderBookL2(
        coin="LINK",
        timestamp=3000.0,
        bids=[BookLevel(price=12.340, size=10.0)],
        asks=[BookLevel(price=12.456, size=10.0)],
    )
    strat.update_hl_book(hl_book)
    strat.update_bn_book(dydx_illiquid)

    # El filtre HA DE REBUTJAR l'entrada per evitar quedar atrapat al forat de liquiditat
    sig = strat.evaluate_entry("LINK")
    assert sig is None, "El bot hauria d'haver rebutjat l'entrada per forat de liquiditat intern (> 0.120%)!"

    # Ara simulem que dYdX té liquiditat estreta (spread 0.05%: bid 12.34, ask 12.346)
    dydx_liquid = OrderBookL2(
        coin="LINK",
        timestamp=3005.0,
        bids=[BookLevel(price=12.340, size=100.0)],
        asks=[BookLevel(price=12.346, size=100.0)],
    )
    strat.update_bn_book(dydx_liquid)
    sig2 = strat.evaluate_entry("LINK")
    assert sig2 is not None, "El bot hauria d'acceptar l'entrada quan el llibre és líquid!"
    assert sig2.direction == ArbitrageDirection.BUY_HL_SELL_BN
    print("  Filtre de salut del llibre d'ordres (Book Spread Guard) verificat amb èxit.")

if __name__ == "__main__":
    test_spread_calculation_and_signal()
    test_arbitrage_execution_and_pnl()
    test_funding_accrual()
    test_profit_guard_blocks_unprofitable_convergence()
    test_dydx_venue2_integration()
    test_book_spread_guard_blocks_illiquid_entry()
    print("✅ Tots els tests d'arbitratge han passat amb èxit!")
