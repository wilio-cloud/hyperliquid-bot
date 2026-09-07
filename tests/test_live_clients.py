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
        state_file="/tmp/test_live_state_clean.json",
    )
    exchange.active_positions.clear()
    exchange.closed_positions.clear()

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
    exchange.active_positions.clear()
    exchange.closed_positions.clear()

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

def test_hyperliquid_order_error_detection():
    with patch("core.hyperliquid_live_client.Exchange") as mock_ex, patch("core.hyperliquid_live_client.Info") as mock_info:
        client = HyperliquidLiveClient(
            wallet_address=DUMMY_ACCOUNT.address,
            agent_private_key=DUMMY_KEY,
            testnet=True,
        )
        # Simulem que Exchange.order retorna status 'ok' però amb error intern a statuses
        client.exchange.order = MagicMock(return_value={
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [{"error": "Insufficient margin"}]
                }
            }
        })

        async def _test():
            res = await client.place_order("HYPE", is_buy=True, size=1.0, price=85.0)
            assert res["status"] == "err"
            assert "Insufficient margin" in res["error"]

        asyncio.run(_test())

def test_live_exchange_sequential_abort_when_hl_fails():
    """Verifica que si Hyperliquid falla o expira, Aevo MAI és tocat (0 exposició direccional)."""
    hl_mock = MagicMock(spec=HyperliquidLiveClient)
    hl_mock.round_size.side_effect = lambda c, s: round(s, 2)
    # Hyperliquid falla (ex: IOC expirat o tick size invàlid)
    hl_mock.place_order = AsyncMock(return_value={"status": "err", "error": "Order expired IOC"})
    hl_mock.market_close = AsyncMock(return_value={"status": "ok"})

    aevo_mock = MagicMock(spec=AevoLiveClient)
    aevo_mock.round_size.side_effect = lambda c, s: round(s, 2)
    aevo_mock.place_order = AsyncMock(return_value={"status": "ok"})
    aevo_mock.market_close = AsyncMock(return_value={"status": "ok"})

    exchange = ArbitrageLiveExchange(
        hl_client=hl_mock,
        aevo_client=aevo_mock,
        initial_hl_balance=500.0,
        initial_bn_balance=500.0,
        leverage=2.0,
        state_file="/tmp/test_live_state_abort.json",
    )
    exchange.active_positions.clear()

    sig = ArbitrageSignal(
        coin="NEAR",
        direction=ArbitrageDirection.SELL_HL_BUY_BN,
        spread_pct=0.180,
        hl_price=5.2,
        bn_price=5.18,
        timestamp=1000.0,
    )

    async def _run():
        await exchange._execute_live_open(sig, size_usd=200.0, is_maker=False)

    asyncio.run(_run())

    # Verificacions estrictes:
    # 1. Hyperliquid s'ha intentat
    assert hl_mock.place_order.called
    # 2. Aevo NO S'HA TOCAT MAI!
    assert not aevo_mock.place_order.called
    # 3. Cap posició oberta
    assert len(exchange.active_positions) == 0

def test_live_exchange_reconcile_positions_clears_when_no_real_positions():
    """Verifica que reconcile_active_positions neteja posicions fantasmes quan els comptes reals estan plans."""
    hl_mock = MagicMock(spec=HyperliquidLiveClient)
    hl_mock.get_account_state = AsyncMock(return_value={"assetPositions": []})

    aevo_mock = MagicMock(spec=AevoLiveClient)
    aevo_mock.get_positions = AsyncMock(return_value=[])

    exchange = ArbitrageLiveExchange(
        hl_client=hl_mock,
        aevo_client=aevo_mock,
        initial_hl_balance=500.0,
        initial_bn_balance=500.0,
        leverage=2.0,
        state_file="/tmp/test_live_state_reconcile.json",
    )
    # Simulem una posició fantasma en memòria
    exchange.active_positions["DUMMY_POS"] = MagicMock()
    assert len(exchange.active_positions) == 1

    async def _run():
        await exchange.reconcile_active_positions()

    asyncio.run(_run())

    # Ha de quedar netejat perquè ni HL ni Aevo tenen posicions
    assert len(exchange.active_positions) == 0

def test_hyperliquid_set_leverage():
    with patch("core.hyperliquid_live_client.Exchange") as mock_ex, patch("core.hyperliquid_live_client.Info") as mock_info:
        client = HyperliquidLiveClient(
            wallet_address=DUMMY_ACCOUNT.address,
            agent_private_key=DUMMY_KEY,
            testnet=True,
        )
        client.exchange.update_leverage = MagicMock(return_value={"status": "ok"})
        res = asyncio.run(client.set_leverage("HYPE", leverage=2, is_cross=True))
        assert res.get("status") == "ok"
        client.exchange.update_leverage.assert_called_once_with(leverage=2, name="HYPE", is_cross=True)

def test_configure_all_leverage():
    hl_mock = MagicMock(spec=HyperliquidLiveClient)
    hl_mock.set_leverage = AsyncMock(return_value={"status": "ok"})
    aevo_mock = MagicMock(spec=AevoLiveClient)
    aevo_mock.set_leverage = AsyncMock(return_value={"status": "ok"})

    exchange = ArbitrageLiveExchange(
        hl_client=hl_mock,
        aevo_client=aevo_mock,
        initial_hl_balance=500.0,
        initial_bn_balance=500.0,
        leverage=2.0,
        state_file="/tmp/test_live_state_lev.json",
    )
    res = asyncio.run(exchange.configure_all_leverage(leverage=2))
    assert res.get("status") == "ok"
    assert res.get("leverage") == 2
    assert hl_mock.set_leverage.call_count == 6
    assert aevo_mock.set_leverage.call_count == 6
