"""Proves unitàries per a DydxLiveClient i la integració amb ArbitrageLiveExchange."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from core.arbitrage_models import ArbitrageDirection, ArbitrageSignal
from core.arbitrage_live_exchange import ArbitrageLiveExchange
from core.dydx_live_client import DydxLiveClient
from core.hyperliquid_live_client import HyperliquidLiveClient
from core.models import OrderSide

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


def test_dydx_live_client_address_derivation():
    client = DydxLiveClient(mnemonic=TEST_MNEMONIC)
    assert client.address.startswith("dydx1")
    assert len(client.address) > 20


def test_dydx_live_client_hex_private_key_with_0x():
    raw_hex = "4f3edf983ac636a65a842ce7c78d9aa706d3b113bce9c46f30d7d21715b23b1d"
    client1 = DydxLiveClient(private_key=raw_hex)
    client2 = DydxLiveClient(private_key=f"0x{raw_hex}")
    assert client1.private_key == raw_hex
    assert client2.private_key == raw_hex
    assert client1.address == client2.address
    assert client2.address.startswith("dydx1")


def test_dydx_live_client_rounding():
    client = DydxLiveClient()

    # SOL (stepSize: 0.1, tickSize: 0.01)
    assert client.round_size("SOL", 2.456) == 2.4
    assert client.round_price("SOL", 103.116) == 103.12

    # SUI (stepSize: 10, tickSize: 0.0001)
    assert client.round_size("SUI", 275.4) == 270.0
    assert client.round_price("SUI", 1.23456) == 1.2346

    # DOGE (stepSize: 100, tickSize: 0.00001)
    assert client.round_size("DOGE", 2789.2) == 2700.0
    assert client.round_price("DOGE", 0.091234) == 0.09123

    # NEAR (stepSize: 1.0, tickSize: 0.001)
    assert client.round_size("NEAR", 55.8) == 55.0
    assert client.round_price("NEAR", 3.4567) == 3.457

    # WIF (stepSize: 1.0, tickSize: 0.00001)
    assert client.round_size("WIF", 15.8) == 15.0
    assert client.round_price("WIF", 0.223456) == 0.22346


def test_dydx_live_client_place_order_success():
    client = DydxLiveClient(mnemonic=TEST_MNEMONIC)
    client._is_initialized = True

    mock_node = MagicMock()
    mock_node.latest_block_height = AsyncMock(return_value=1000000)

    mock_tx_resp = MagicMock()
    mock_tx_resp.code = 0
    mock_tx_resp.txhash = "0xABCDEF123456"
    mock_tx_resp.height = 1000001
    mock_node.place_order = AsyncMock(return_value=mock_tx_resp)

    client.node_client = mock_node
    client.wallet = MagicMock()
    client.wallet.address = client.address

    async def _run():
        res = await client.place_order(
            coin="SOL",
            is_buy=True,
            size=2.45,
            price=103.11,
            post_only=False,
            reduce_only=False,
            ioc=True,
        )
        return res

    res = asyncio.run(_run())
    assert res["status"] == "ok"
    assert res["txhash"] == "0xABCDEF123456"
    assert res["data"]["side"] == "BUY"
    assert res["data"]["size"] == 2.4
    assert res["data"]["price"] == 103.11
    assert mock_node.place_order.called


def test_dydx_live_client_place_order_chain_error():
    client = DydxLiveClient(mnemonic=TEST_MNEMONIC)
    client._is_initialized = True

    mock_node = MagicMock()
    mock_node.latest_block_height = AsyncMock(return_value=1000000)

    mock_tx_resp = MagicMock()
    mock_tx_resp.code = 4
    mock_tx_resp.raw_log = "insufficient funds for fee"
    mock_tx_resp.txhash = "0xFAIL"
    mock_node.place_order = AsyncMock(return_value=mock_tx_resp)

    client.node_client = mock_node
    client.wallet = MagicMock()
    client.wallet.address = client.address

    async def _run():
        return await client.place_order(
            coin="SOL",
            is_buy=False,
            size=1.0,
            price=100.0,
            ioc=True,
        )

    res = asyncio.run(_run())
    assert res["status"] == "err"
    assert res["code"] == 4
    assert "insufficient funds" in res["error"]


def test_dydx_live_client_get_positions_and_balance():
    client = DydxLiveClient(address="dydx1testaddress")

    sample_state = {
        "subaccount": {
            "equity": "520.50",
            "freeCollateral": "495.20",
            "openPerpetualPositions": {
                "SOL-USD": {
                    "market": "SOL-USD",
                    "side": "LONG",
                    "size": "2.4",
                    "entryPrice": "103.12",
                    "unrealizedPnl": "1.50",
                },
                "SUI-USD": {
                    "market": "SUI-USD",
                    "side": "SHORT",
                    "size": "-200",
                    "entryPrice": "1.25",
                    "unrealizedPnl": "-0.30",
                }
            }
        }
    }

    client.get_account_state = AsyncMock(return_value=sample_state)

    async def _run():
        bal = await client.get_balance()
        pos = await client.get_positions()
        return bal, pos

    balance, positions = asyncio.run(_run())
    assert balance == 495.20
    assert len(positions) == 2

    sol_pos = next(p for p in positions if p["asset"] == "SOL")
    assert sol_pos["amount"] == 2.4
    assert sol_pos["side"] == "buy"
    assert sol_pos["avg_entry_price"] == 103.12

    sui_pos = next(p for p in positions if p["asset"] == "SUI")
    assert sui_pos["amount"] == 200.0
    assert sui_pos["side"] == "sell"
    assert sui_pos["avg_entry_price"] == 1.25


def test_dydx_live_client_market_close():
    client = DydxLiveClient(address="dydx1testaddress")
    client.get_positions = AsyncMock(return_value=[
        {
            "asset": "SOL",
            "market": "SOL-USD",
            "amount": 2.4,
            "side": "buy",
            "avg_entry_price": 100.0,
            "mark_price": 105.0,
        }
    ])
    client.place_order = AsyncMock(return_value={"status": "ok", "txhash": "0xCLOSE"})

    async def _run():
        return await client.market_close("SOL")

    res = asyncio.run(_run())
    assert res["status"] == "ok"
    assert client.place_order.called
    kwargs = client.place_order.call_args.kwargs
    assert kwargs["coin"] == "SOL"
    assert kwargs["is_buy"] is False  # Opposite of LONG is SELL
    assert kwargs["size"] == 2.4
    assert kwargs["reduce_only"] is True
    assert kwargs["ioc"] is True


def test_arbitrage_live_exchange_with_dydx():
    hl_mock = MagicMock(spec=HyperliquidLiveClient)
    hl_mock.round_size.side_effect = lambda c, s: round(s, 2)
    hl_mock.place_order = AsyncMock(return_value={"status": "ok", "response": {"type": "order"}})
    hl_mock.market_close = AsyncMock(return_value={"status": "ok"})

    dydx_mock = MagicMock(spec=DydxLiveClient)
    dydx_mock.round_size.side_effect = lambda c, s: round(s, 1)
    dydx_mock.place_order = AsyncMock(return_value={"status": "ok", "txhash": "0xDYDX123"})
    dydx_mock.market_close = AsyncMock(return_value={"status": "ok"})

    exchange = ArbitrageLiveExchange(
        hl_client=hl_mock,
        venue2_client=dydx_mock,
        venue2_name="DYDX",
        initial_hl_balance=500.0,
        initial_bn_balance=500.0,
        leverage=2.0,
        state_file="/tmp/test_live_state_dydx.json",
    )
    exchange.active_positions.clear()
    exchange.closed_positions.clear()

    # Comprovar que s'apliquen les comissions de dYdX (0.010% Maker, 0.050% Taker)
    assert exchange.venue2_name == "DYDX"
    assert exchange.bn_maker_fee == 0.00010
    assert exchange.bn_taker_fee == 0.00050

    sig = ArbitrageSignal(
        coin="SOL",
        direction=ArbitrageDirection.SELL_HL_BUY_BN,
        spread_pct=0.150,
        hl_price=105.0,
        bn_price=104.8,
        timestamp=1000.0,
    )

    async def _run():
        await exchange._execute_live_open(sig, size_usd=200.0, is_maker=False)

    asyncio.run(_run())

    assert len(exchange.active_positions) == 1
    pos = list(exchange.active_positions.values())[0]
    assert pos.coin == "SOL"
    assert pos.leg_bn.venue == "DYDX"
    assert hl_mock.place_order.called
    assert dydx_mock.place_order.called


def test_arbitrage_live_exchange_anti_unhedged_with_dydx():
    hl_mock = MagicMock(spec=HyperliquidLiveClient)
    hl_mock.round_size.side_effect = lambda c, s: round(s, 2)
    hl_mock.place_order = AsyncMock(return_value={"status": "ok", "response": {"type": "order"}})
    hl_mock.market_close = AsyncMock(return_value={"status": "ok"})

    dydx_mock = MagicMock(spec=DydxLiveClient)
    dydx_mock.round_size.side_effect = lambda c, s: round(s, 1)
    # Simulem error a dYdX per comprovar que Hyperliquid fa rollback immediat
    dydx_mock.place_order = AsyncMock(return_value={"status": "err", "error": "Order rejected by validator"})
    dydx_mock.market_close = AsyncMock(return_value={"status": "ok"})

    exchange = ArbitrageLiveExchange(
        hl_client=hl_mock,
        venue2_client=dydx_mock,
        venue2_name="DYDX",
        initial_hl_balance=500.0,
        initial_bn_balance=500.0,
        leverage=2.0,
        state_file="/tmp/test_live_state_dydx_rollback.json",
    )
    exchange.active_positions.clear()

    sig = ArbitrageSignal(
        coin="SOL",
        direction=ArbitrageDirection.SELL_HL_BUY_BN,
        spread_pct=0.150,
        hl_price=105.0,
        bn_price=104.8,
        timestamp=1000.0,
    )

    async def _run():
        await exchange._execute_live_open(sig, size_usd=200.0, is_maker=False)

    asyncio.run(_run())

    # La posició NO s'ha d'obrir
    assert len(exchange.active_positions) == 0
    # Hyperliquid ha d'haver rebut ordre de market_close per neutralitzar el risc
    assert hl_mock.market_close.called
    kwargs = hl_mock.market_close.call_args.kwargs
    assert kwargs["coin"] == "SOL"


def test_adaptive_max_book_spread_for_dydx():
    import os
    from main import ArbitrageTradingBotApp

    # Per defecte a dYdX ha de ser 1.200%
    app_dydx = ArbitrageTradingBotApp(coins=["SOL"], venue2="dydx", headless=True)
    assert app_dydx.strategy.max_book_spread_pct == 1.200

    # Per defecte a Aevo ha de ser 0.220%
    app_aevo = ArbitrageTradingBotApp(coins=["SOL"], venue2="aevo", headless=True)
    assert app_aevo.strategy.max_book_spread_pct == 0.220

    # Si es passa explícit, s'ha de respectar
    app_custom = ArbitrageTradingBotApp(coins=["SOL"], venue2="dydx", max_book_spread=0.350, headless=True)
    assert app_custom.strategy.max_book_spread_pct == 0.350

    # Si es defineix variable d'entorn MAX_BOOK_SPREAD, s'ha de respectar
    os.environ["MAX_BOOK_SPREAD"] = "0.420"
    try:
        app_env = ArbitrageTradingBotApp(coins=["SOL"], venue2="dydx", headless=True)
        assert app_env.strategy.max_book_spread_pct == 0.420
    finally:
        del os.environ["MAX_BOOK_SPREAD"]

