"""Suite de tests unitaris per al motor de paper trading, risc i estratègies."""

import time
from core.models import BookLevel, OrderBookL2, OrderSide, OrderType, Trade
from core.paper_exchange import PaperExchange
from core.risk_manager import RiskManager
from strategies.orderbook_imbalance import OrderBookImbalanceStrategy
from strategies.volume_burst import VolumeBurstStrategy
from strategies.micro_mean_reversion import MicroMeanReversionStrategy

def make_sample_book(coin="BTC", mid=60000.0, spread=2.0, bid_sz=10.0, ask_sz=10.0):
    best_bid = mid - (spread / 2.0)
    best_ask = mid + (spread / 2.0)
    bids = [
        BookLevel(price=best_bid, size=bid_sz),
        BookLevel(price=best_bid - 1.0, size=bid_sz),
        BookLevel(price=best_bid - 2.0, size=bid_sz),
        BookLevel(price=best_bid - 3.0, size=bid_sz),
        BookLevel(price=best_bid - 4.0, size=bid_sz),
    ]
    asks = [
        BookLevel(price=best_ask, size=ask_sz),
        BookLevel(price=best_ask + 1.0, size=ask_sz),
        BookLevel(price=best_ask + 2.0, size=ask_sz),
        BookLevel(price=best_ask + 3.0, size=ask_sz),
        BookLevel(price=best_ask + 4.0, size=ask_sz),
    ]
    return OrderBookL2(coin=coin, timestamp=time.time(), bids=bids, asks=asks)

def test_paper_exchange_post_only_rejection():
    exchange = PaperExchange(initial_balance=10000.0)
    book = make_sample_book(coin="BTC", mid=60000.0, spread=2.0)
    
    # Intentar comprar per sobre o igual al best ask com a post-only ha de ser REJECTED
    order = exchange.place_order(
        coin="BTC",
        side=OrderSide.BUY,
        price=book.best_ask,
        size_usd=500.0,
        post_only=True,
        current_book=book,
    )
    assert order is not None
    assert order.status.value == "REJECTED"

def test_paper_exchange_order_queue_and_fill():
    exchange = PaperExchange(initial_balance=10000.0)
    book = make_sample_book(coin="BTC", mid=60000.0, spread=2.0, bid_sz=1.0)
    
    # Comprem al best bid (59999.0). Hi ha 1.0 BTC de cua davant nostre
    order = exchange.place_order(
        coin="BTC",
        side=OrderSide.BUY,
        price=book.best_bid,
        size_usd=600.0,
        post_only=True,
        strategy_name="TestStrat",
        current_book=book,
    )
    assert order.status.value == "OPEN"
    assert order.order_id in exchange.open_orders
    assert exchange.open_orders[order.order_id].queue_ahead == 1.0

    # Arriba un trade que ven només 0.5 BTC -> la cua es redueix però no s'omple
    trade1 = Trade(coin="BTC", side=OrderSide.SELL, price=book.best_bid, size=0.5, timestamp=time.time())
    exchange.on_trade(trade1)
    assert order.order_id in exchange.open_orders
    assert abs(exchange.open_orders[order.order_id].queue_ahead - 0.5) < 1e-5

    # Arriba un altre trade de 0.6 BTC -> s'omple l'ordre i s'obre la posició
    trade2 = Trade(coin="BTC", side=OrderSide.SELL, price=book.best_bid, size=0.6, timestamp=time.time())
    exchange.on_trade(trade2)
    assert order.order_id not in exchange.open_orders
    assert "BTC" in exchange.positions
    pos = exchange.positions["BTC"]
    assert pos.entry_price == book.best_bid
    assert pos.strategy_name == "TestStrat"
    assert exchange.total_maker_orders == 1

def test_paper_exchange_take_profit():
    exchange = PaperExchange(initial_balance=10000.0)
    book = make_sample_book(coin="BTC", mid=60000.0, spread=2.0, bid_sz=0.1)
    
    order = exchange.place_order(
        coin="BTC",
        side=OrderSide.BUY,
        price=book.best_bid,
        size_usd=600.0,
        post_only=True,
        current_book=book,
    )
    # Fill order
    exchange.on_trade(Trade(coin="BTC", side=OrderSide.SELL, price=book.best_bid, size=1.0, timestamp=time.time()))
    pos = exchange.positions["BTC"]
    tp_target = pos.take_profit

    # El mercat puja fins al Take Profit
    higher_book = make_sample_book(coin="BTC", mid=tp_target, spread=1.0)
    exchange.on_book_update(higher_book)

    # La posició ha d'haver tancat en positiu
    assert "BTC" not in exchange.positions
    assert len(exchange.closed_positions) == 1
    assert exchange.closed_positions[0].realized_pnl > 0
    assert exchange.closed_positions[0].exit_reason == "TAKE_PROFIT"
    assert exchange.balance_usd > 10000.0

def test_risk_manager_circuit_breaker():
    rm = RiskManager()
    # Pèrdua acumulada de 250$ quan el límit és 200$
    can_open, reason = rm.can_open_position(
        coin="ETH",
        current_balance=9750.0,
        initial_balance=10000.0,
        open_positions_count=0,
        has_existing_coin_position=False,
    )
    assert can_open is False
    assert "CIRCUIT BREAKER" in reason

def test_orderbook_imbalance_strategy():
    strat = OrderBookImbalanceStrategy(imbalance_ratio=2.5, min_signal_interval_sec=0.0)
    
    # Book amb fort desequilibri de compra (30 BTC a bids vs 5 BTC a asks)
    book = make_sample_book(coin="BTC", mid=60000.0, spread=1.0, bid_sz=30.0, ask_sz=5.0)
    sig = strat.on_book_update(book)
    
    assert sig is not None
    assert sig.action == "BUY"
    assert sig.coin == "BTC"
    assert "Bullish Ratio" in sig.reason

def test_micro_mean_reversion_trend_filter():
    strat = MicroMeanReversionStrategy(min_signal_interval_sec=0.0, dev_threshold_pct=0.001)
    
    # Simulem 25 trades a 60000.0 per alimentar la història
    now = time.time()
    for i in range(25):
        strat.on_trade(Trade(coin="BTC", price=60000.0, size=1.0, side=OrderSide.BUY, timestamp=now))
    
    # 1. Preu cau per sota de VWAP (59900.0 vs 60000.0, dev -0.16%)
    # Però l'EMA és 60000.0 i el preu actual és 59900.0 (per sota de la macro EMA) -> Ganivet que cau! Ha de ser descartat
    book_knife = make_sample_book(coin="BTC", mid=59900.0, spread=2.0)
    sig_knife = strat.on_book_update(book_knife)
    assert sig_knife is None, "Hauria de rebutjar comprar si estem sota l'EMA (ganivet que cau)"

    # 2. Ara simulem que la macro-tendència és alcista (EMA pujava des de 59000.0)
    strat.ema_trend["BTC"] = 59800.0  # El preu (59900.0) està per sobre de l'EMA de 59800 -> Pullback en tendència alcista!
    sig_valid = strat.on_book_update(book_knife)
    assert sig_valid is not None
    assert sig_valid.action == "BUY"
    assert "Trend Bullish" in sig_valid.reason

def test_vertex_ws_client_and_fees():
    from core.vertex_ws_client import VertexWSClient
    from core.arbitrage_paper_exchange import ArbitragePaperExchange
    
    # 1. Verificació de product IDs
    client = VertexWSClient(coins=["BTC", "ETH", "SOL", "SUI"])
    assert client.coin_to_pid["BTC"] == 2
    assert client.coin_to_pid["ETH"] == 4
    assert client.coin_to_pid["SOL"] == 12
    assert client.coin_to_pid["SUI"] == 28

    # 2. Verificació de 0% maker fee per a Vertex
    exchange = ArbitragePaperExchange(venue2_name="VERTEX")
    assert exchange.bn_maker_fee == 0.00000, "Vertex ha de tenir 0% maker fee"
    assert exchange.bn_taker_fee == 0.00020, "Vertex ha de tenir 0.02% taker fee"

def test_maker_first_fee_savings():
    from core.arbitrage_models import ArbitrageDirection, ArbitrageSignal
    from core.arbitrage_paper_exchange import ArbitragePaperExchange
    
    # Mode estàndard Taker
    ex_taker = ArbitragePaperExchange(venue2_name="AEVO", maker_first=False)
    sig = ArbitrageSignal(
        coin="BTC",
        direction=ArbitrageDirection.SELL_HL_BUY_BN,
        hl_price=60100.0,
        bn_price=60000.0,
        spread_pct=0.166,
    )
    pos_taker = ex_taker.open_arbitrage_position(sig, size_usd=1000.0, is_maker=False)
    assert pos_taker is not None
    # HL Taker: 1000 * 0.00045 = 0.45$, Aevo Taker: 1000 * 0.00050 = 0.50$ -> Total = 0.95$
    assert abs(pos_taker.leg_hl.fees_paid - 0.45) < 1e-4

    # Mode Maker-First
    ex_maker = ArbitragePaperExchange(venue2_name="AEVO", maker_first=True)
    pos_maker = ex_maker.open_arbitrage_position(sig, size_usd=1000.0, is_maker=False)
    assert pos_maker is not None
    # HL Maker: 1000 * 0.00015 = 0.15$ (estalvi del 66% a la pota HL!)
    assert abs(pos_maker.leg_hl.fees_paid - 0.15) < 1e-4
    assert pos_maker.leg_hl.fees_paid < pos_taker.leg_hl.fees_paid

def test_cross_arbitrage_maker_first_spread_calibration():
    from strategies.cross_arbitrage import CrossExchangeArbitrageStrategy

    strat_taker = CrossExchangeArbitrageStrategy(maker_first=False)
    strat_maker = CrossExchangeArbitrageStrategy(maker_first=True)

    spread_taker = strat_taker.get_effective_min_spread("ETH")
    spread_maker = strat_maker.get_effective_min_spread("ETH")

    assert spread_maker < spread_taker
    assert spread_maker <= 0.110, "En mode Maker-First el spread mínim ha de ser <= 0.110%"

if __name__ == "__main__":
    test_paper_exchange_post_only_rejection()
    test_paper_exchange_order_queue_and_fill()
    test_paper_exchange_take_profit()
    test_risk_manager_circuit_breaker()
    test_orderbook_imbalance_strategy()
    test_micro_mean_reversion_trend_filter()
    test_vertex_ws_client_and_fees()
    test_maker_first_fee_savings()
    test_cross_arbitrage_maker_first_spread_calibration()
    print("Tots els tests unitaris han passat correctament!")
