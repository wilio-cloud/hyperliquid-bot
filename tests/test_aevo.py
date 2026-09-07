"""Proves unitàries per al connector d'Aevo DEX i la seva integració d'arbitratge."""

from core.aevo_ws_client import AevoWSClient
from core.arbitrage_paper_exchange import ArbitragePaperExchange
from core.arbitrage_models import ArbitrageDirection, ArbitrageSignal
from core.models import OrderBookL2
from main import ArbitrageTradingBotApp

def test_aevo_symbol_mapping():
    coins = ["BTC", "ETH", "SOL", "NEAR", "SUI", "DOGE", "LINK"]
    client = AevoWSClient(coins=coins)
    assert client.symbol_map["BTC"] == "BTC-PERP"
    assert client.symbol_map["SOL"] == "SOL-PERP"
    assert client.symbol_map["NEAR"] == "NEAR-PERP"
    assert client.reverse_map["BTC-PERP"] == "BTC"
    assert client.reverse_map["SOL-PERP"] == "SOL"
    print("  test_aevo_symbol_mapping passat.")

def test_aevo_orderbook_snapshot_and_delta():
    received_books = []
    def on_book(b: OrderBookL2):
        received_books.append(b)

    client = AevoWSClient(coins=["SOL"], on_book_update=on_book)

    # 1. Simular Snapshot
    snapshot_msg = {
        "channel": "orderbook-100ms:SOL-PERP",
        "data": {
            "type": "snapshot",
            "instrument_name": "SOL-PERP",
            "bids": [["105.00", "10.0"], ["104.90", "5.0"]],
            "asks": [["105.10", "12.0"], ["105.20", "8.0"]],
        }
    }
    client._handle_ws_message(snapshot_msg)

    assert len(received_books) == 1
    book = received_books[-1]
    assert book.coin == "SOL"
    assert book.bids[0].price == 105.00
    assert book.asks[0].price == 105.10

    # 2. Simular Update (delta: nou millor bid a 105.05)
    update_msg = {
        "channel": "orderbook-100ms:SOL-PERP",
        "data": {
            "type": "update",
            "instrument_name": "SOL-PERP",
            "bids": [["105.05", "15.0"]],
            "asks": [],
        }
    }
    client._handle_ws_message(update_msg)

    assert len(received_books) == 2
    updated_book = received_books[-1]
    assert updated_book.bids[0].price == 105.05
    assert updated_book.asks[0].price == 105.10
    print("  test_aevo_orderbook_snapshot_and_delta passat.")

def test_aevo_paper_exchange_fees():
    exchange = ArbitragePaperExchange(
        initial_hl_balance=500.0,
        initial_bn_balance=500.0,
        venue2_name="AEVO",
        leverage=2.0,
    )
    assert exchange.venue2_name == "AEVO"
    assert exchange.bn_maker_fee == 0.00000  # 0.00% maker a Aevo
    assert exchange.bn_taker_fee == 0.00025  # 0.025% taker a Aevo
    print("  test_aevo_paper_exchange_fees passat.")

def test_main_app_initialization_with_aevo():
    app = ArbitrageTradingBotApp(
        coins=["BTC", "ETH", "SOL", "NEAR", "SUI", "DOGE", "LINK"],
        venue2="aevo",
        headless=True,
    )
    assert app.venue2 == "aevo"
    assert app.venue2_label == "Aevo DEX"
    assert isinstance(app.venue2_ws, AevoWSClient)
    assert len(app.coins) == 7
    print("  test_main_app_initialization_with_aevo passat.")

if __name__ == "__main__":
    test_aevo_symbol_mapping()
    test_aevo_orderbook_snapshot_and_delta()
    test_aevo_paper_exchange_fees()
    test_main_app_initialization_with_aevo()
    print("✅ Tots els tests d'Aevo han passat amb èxit!")
