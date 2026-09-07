"""Proves unitàries per als clients d'execució en real i protecció anti-unhedged."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from eth_account import Account
from core.arbitrage_models import ArbitrageDirection, ArbitrageSignal
from core.arbitrage_live_exchange import ArbitrageLiveExchange
from core.hyperliquid_live_client import HyperliquidLiveClient
from core.aevo_live_client import AevoLiveClient

DUMMY_KEY = "0x" + "1" * 64
DUMMY_ACCOUNT = Account.from_key(DUMMY_KEY)

def test_hyperliquid_live_client_rounding_and_types():
    with patch("core.hyperliquid_live_client.Exchange") as mock_ex, patch("core.hyperliquid_live_client.Info") as mock_info:
        client = HyperliquidLiveClient(
            wallet_address=DUMMY_ACCOUNT.address,
            agent_private_key=DUMMY_KEY,
            testnet=True,
        )
        assert client.round_size("PUMP", 12345.67) == 12345.0
        assert client.round_size("DOGE", 555.99) == 555.0
        assert client.round_size("SOL", 1.23456) == 1.23
        assert client.round_size("SUI", 89.123) == 89.1
        assert client.round_size("HYPE", 3.456) == 3.46

def test_aevo_live_client_eip712_signing_and_instruments():
    client = AevoLiveClient(
        wallet_address=DUMMY_ACCOUNT.address,
        api_key="test_api_key",
        api_secret="test_api_secret",
        signing_key=DUMMY_KEY,
        env="mainnet",
    )
    # Verificació d'instruments coneguts
    assert client.get_instrument_id("SOL") == 5197
    assert client.get_instrument_id("HYPE") == 49760
    assert client.get_instrument_id("PUMP") == 82182

    # Verificació de generació de signatura EIP-712
    payload = client.sign_order_payload(
        instrument_id=5197,
        is_buy=True,
        limit_price=105.50,
        quantity=2.5,
        post_only=False,
    )
    assert payload["maker"] == DUMMY_ACCOUNT.address.lower()
    assert payload["is_buy"] is True
    assert payload["instrument"] == 5197
    assert payload["limit_price"] == str(int(105.50 * 10**6))
    assert payload["amount"] == str(int(2.5 * 10**6))
    assert "signature" in payload
    assert payload["signature"].startswith("0x")
    assert len(payload["signature"]) > 60

def test_live_exchange_atomic_open_both_legs_success():
    hl_mock = MagicMock(spec=HyperliquidLiveClient)
    hl_mock.round_size.side_effect = lambda c, s: round(s, 2)
    hl_mock.place_order = AsyncMock(return_value={"status": "ok", "response": {"type": "order"}})
    hl_mock.market_close = AsyncMock(return_value={"status": "ok"})

    aevo_mock = MagicMock(spec=AevoLiveClient)
    aevo_mock.round_size.side_effect = lambda c, s: round(s, 2)
    aevo_mock.place_order = AsyncMock(return_value={"status": "ok", "data": {"order_id": "12345"}})
    aevo_mock.market_close = AsyncMock(return_value={"status": "ok"})

    exchange = ArbitrageLiveExchange(
        hl_client=hl_mock,
        aevo_client=aevo_mock,
        initial_hl_balance=500.0,
        initial_bn_balance=500.0,
        leverage=2.0,
        state_file="/tmp/test_live_state.json",
    )

    sig = ArbitrageSignal(
        coin="SOL",
        direction=ArbitrageDirection.SELL_HL_BUY_BN,
        spread_pct=0.150,
        hl_price=105.0,
        bn_price=104.8,
        timestamp=1000.0,
    )

    async def _run_test():
        await exchange._execute_live_open(sig, size_usd=200.0, is_maker=False)

    asyncio.run(_run_test())

    # Comprovació que s'ha obert la posició delta-neutral
    assert len(exchange.active_positions) == 1
    pos = list(exchange.active_positions.values())[0]
    assert pos.coin == "SOL"
    assert pos.direction == ArbitrageDirection.SELL_HL_BUY_BN
    assert hl_mock.place_order.called
    assert aevo_mock.place_order.called
    assert not hl_mock.market_close.called
    assert not aevo_mock.market_close.called

def test_live_exchange_anti_unhedged_rollback_when_aevo_fails():
    hl_mock = MagicMock(spec=HyperliquidLiveClient)
    hl_mock.round_size.side_effect = lambda c, s: round(s, 2)
    hl_mock.place_order = AsyncMock(return_value={"status": "ok"})
    hl_mock.market_close = AsyncMock(return_value={"status": "ok"})

    aevo_mock = MagicMock(spec=AevoLiveClient)
    aevo_mock.round_size.side_effect = lambda c, s: round(s, 2)
    # Aevo falla (ex: saldo o rebuig del llibre)
    aevo_mock.place_order = AsyncMock(return_value={"status": "err", "error": "Insufficient margin"})
    aevo_mock.market_close = AsyncMock(return_value={"status": "ok"})

    exchange = ArbitrageLiveExchange(
        hl_client=hl_mock,
        aevo_client=aevo_mock,
        initial_hl_balance=500.0,
        initial_bn_balance=500.0,
        leverage=2.0,
        state_file="/tmp/test_live_state_rollback.json",
    )

    sig = ArbitrageSignal(
        coin="HYPE",
        direction=ArbitrageDirection.BUY_HL_SELL_BN,
        spread_pct=0.120,
        hl_price=85.0,
        bn_price=85.2,
        timestamp=1000.0,
    )

    async def _run_rollback_test():
        await exchange._execute_live_open(sig, size_usd=200.0, is_maker=False)

    asyncio.run(_run_rollback_test())

    # Cap posició oberta perquè s'ha cancel·lat/neutralitzat immediatament
    assert len(exchange.active_positions) == 0
    # Hyperliquid ha d'haver estat tancat a mercat immediatament (Rollback de seguretat)
    assert hl_mock.market_close.called
    print("Anti-Unhedged Guard verificat: rollback de seguretat executat amb èxit!")
