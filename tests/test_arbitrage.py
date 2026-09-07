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
    exchange = ArbitragePaperExchange(
        initial_hl_balance=5000.0,
        initial_bn_balance=5000.0,
        venue2_name="BINANCE",
    )
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

def test_dynamic_position_sizing():
    """Verifica que la mida d'ordre s'ajusta automàticament segons l'interès compost."""
    from main import ArbitrageTradingBotApp
    app = ArbitrageTradingBotApp(
        coins=["BTC", "ETH", "SOL"],
        initial_balance=1000.0,
        leverage=2.0,
        dynamic_size=True,
        size_pct=30.0,
        min_size_usd=100.0,
        max_size_usd=2500.0,
    )
    # Amb 1.000$ (500$ a cada exchange), mida = 1.000 * 0.30 = 300.0$
    assert app.calculate_order_size() == 300.0

    # Simulem creixement a 1.500$ (750$ a cada exchange)
    app.exchange.hl_balance_usd = 750.0
    app.exchange.bn_balance_usd = 750.0
    assert app.calculate_order_size() == 450.0

    # Simulem asimetria (600$ a HL, 800$ a dYdX) -> usa min_balance (600$) * 2 = 1200 * 0.30 = 360.0$
    app.exchange.hl_balance_usd = 600.0
    app.exchange.bn_balance_usd = 800.0
    assert app.calculate_order_size() == 360.0

    # Simulem límit màxim de liquiditat (10.000$ -> 3.000$ calculat, però limitat a max_size 2.500$)
    app.exchange.hl_balance_usd = 5000.0
    app.exchange.bn_balance_usd = 5000.0
    assert app.calculate_order_size() == 2500.0

    print("  Dynamic Position Sizing (interès compost) verificat amb èxit.")

def test_dashboard_signal_status_accuracy():
    """Verifica que el tauler no mostra senyals falses de foc si el spread executable és insuficient."""
    from main import ArbitrageTradingBotApp
    app = ArbitrageTradingBotApp(
        coins=["SOL"],
        initial_balance=1000.0,
        min_spread=0.150,
        max_book_spread=0.160,
    )
    # HL té Bid 105.71, Ask 105.73 (Mid 105.72)
    # dYdX té Bid 105.78, Ask 105.88 (Mid 105.83)
    # Mid diff = -0.104%
    # Executable BUY HL / SELL dYdX = (105.78 - 105.73) / 105.775 = +0.047% (INSUFICIENT per a 0.150%)
    # Executable SELL HL / BUY dYdX = (105.71 - 105.88) = -0.160% (Pèrdua)
    app.strategy.update_hl_book(OrderBookL2(coin="SOL", timestamp=1.0, bids=[BookLevel(price=105.71, size=10)], asks=[BookLevel(price=105.73, size=10)]))
    app.strategy.update_bn_book(OrderBookL2(coin="SOL", timestamp=1.0, bids=[BookLevel(price=105.78, size=10)], asks=[BookLevel(price=105.88, size=10)]))

    data = app.get_dashboard_data()
    sol_data = data["spreads"][0]

    # No ha de mostrar senyal de foc falsa
    assert sol_data["signal_type"] != "SIGNAL", f"No hauria de ser SIGNAL: {sol_data}"
    assert "🔥" not in sol_data["signal_status"], f"No hauria de contenir foc: {sol_data['signal_status']}"
    assert sol_data["exec_spread_pct"] < 0.150
    print("  Precisió de senyals del tauler verificada amb èxit.")

def test_weekend_regime_and_funding_harvest():
    """Verifica l'adaptació de cap de setmana (0.120%) i la collita de funding rates."""
    from strategies.cross_arbitrage import CrossExchangeArbitrageStrategy
    strat = CrossExchangeArbitrageStrategy(
        min_entry_spread_pct=0.150,
        weekend_min_spread_pct=0.120,
        auto_weekend_adjust=True,
        min_funding_harvest_apr=8.0,
    )
    assert isinstance(strat.is_weekend_regime, bool)
    if strat.is_weekend_regime:
        assert strat.effective_min_spread == 0.120
    else:
        assert strat.effective_min_spread == 0.150

    # Simulem llibres equilibrats (sense spread de preu, 0.0%)
    strat.update_hl_book(OrderBookL2(coin="BTC", timestamp=1.0, bids=[BookLevel(price=80000.0, size=1.0)], asks=[BookLevel(price=80001.0, size=1.0)]))
    strat.update_bn_book(OrderBookL2(coin="BTC", timestamp=1.0, bids=[BookLevel(price=80000.0, size=1.0)], asks=[BookLevel(price=80001.0, size=1.0)]))
    
    # Sense diferencial de funding, no hi ha senyal
    assert strat.evaluate_entry("BTC") is None

    # Ara simulem Funding disparat a HL (+0.002% per hora = +17.5% APR) i 0% a dYdX
    strat.update_hl_funding("BTC", 0.0020) # 0.002% * 24 * 365 = 17.52% APR
    sig = strat.evaluate_entry("BTC")
    assert sig is not None, "Hauria de generar senyal de Funding Harvest quan l'APR supera el llindar!"
    assert sig.direction == ArbitrageDirection.SELL_HL_BUY_BN
    assert "Funding Harvest" in sig.reason
    print("  Règim de cap de setmana i Funding Harvest verificats amb èxit.")

def test_dynamic_proportional_take_profit():
    """Verifica que el TP s'escala proporcionalment a la mida d'ordre (com ahir)."""
    exchange = ArbitragePaperExchange(
        initial_hl_balance=500.0,
        initial_bn_balance=500.0,
        venue2_name="AEVO",
    )
    strat = CrossExchangeArbitrageStrategy(
        min_entry_spread_pct=0.120,
        target_exit_spread_pct=0.010,
    )

    # Ordre de 300$ a SOL
    signal = ArbitrageSignal(
        coin="SOL",
        direction=ArbitrageDirection.SELL_HL_BUY_BN,
        hl_price=100.20,
        bn_price=100.00,
        spread_pct=0.200,
    )
    pos = exchange.open_arbitrage_position(signal, size_usd=300.0)
    assert pos is not None

    # Simulem que l'spread es redueix ràpidament: HL ask 100.05, BN bid 100.00 (queda 0.050% spread)
    # Gross = (100.20 - 100.05)*3.0 = 0.45$
    # Fees anada i tornada a Aevo = ~0.21$
    # Net = 0.45$ - 0.21$ = +0.24$ >= target_tp (0.15$)
    hl_book = OrderBookL2(coin="SOL", timestamp=2000.0, bids=[BookLevel(price=100.04, size=10.0)], asks=[BookLevel(price=100.05, size=10.0)])
    bn_book = OrderBookL2(coin="SOL", timestamp=2000.0, bids=[BookLevel(price=100.00, size=10.0)], asks=[BookLevel(price=100.01, size=10.0)])
    strat.update_hl_book(hl_book)
    strat.update_bn_book(bn_book)

    exit_res = strat.check_exit(pos)
    assert exit_res is not None
    assert exit_res[0] == "TAKE_PROFIT_TARGET", "Hauria d'haver executat TAKE_PROFIT_TARGET immediatament!"
    print("  Take Profit dinàmic proporcional verificat amb èxit.")

if __name__ == "__main__":
    test_spread_calculation_and_signal()
    test_arbitrage_execution_and_pnl()
    test_funding_accrual()
    test_profit_guard_blocks_unprofitable_convergence()
    test_dydx_venue2_integration()
    test_book_spread_guard_blocks_illiquid_entry()
    test_dynamic_position_sizing()
    test_dashboard_signal_status_accuracy()
    test_weekend_regime_and_funding_harvest()
    test_dynamic_proportional_take_profit()
    print("✅ Tots els tests d'arbitratge han passat amb èxit!")
