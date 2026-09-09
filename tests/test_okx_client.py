"""Tests unitaris complets per a la integració d'OKX V5 (REST, WebSocket i Arbitratge)."""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
try:
    import pytest
except ImportError:
    class _MockPytest:
        @staticmethod
        def approx(val, rel=1e-4):
            class _ApproxVal:
                def __eq__(self, other):
                    return abs(other - val) <= max(abs(val) * rel, 1e-6)
            return _ApproxVal()
    pytest = _MockPytest()

from core.arbitrage_live_exchange import ArbitrageLiveExchange
from core.arbitrage_models import ArbitrageDirection, ArbitragePosition, ArbitrageSignal
from core.models import BookLevel, OrderBookL2, OrderSide
from core.okx_live_client import OkxLiveClient
from core.okx_ws_client import OkxWSClient
from strategies.cross_arbitrage import CrossExchangeArbitrageStrategy


# =====================================================================
# 1. Tests d'Autenticació i Signatura HMAC d'OKX
# =====================================================================

def test_okx_hmac_signature():
    """Valida que la signatura HMAC-SHA256 coincideixi amb l'especificació d'OKX V5."""
    api_key = "test_key"
    api_secret = "test_secret_123"
    passphrase = "test_passphrase"
    client = OkxLiveClient(api_key=api_key, api_secret=api_secret, passphrase=passphrase)

    ts = "2026-09-09T12:00:00.000Z"
    method = "GET"
    request_path = "/api/v5/account/balance"
    body = ""

    expected_prehash = f"{ts}{method}{request_path}{body}"
    expected_sign = base64.b64encode(
        hmac.new(api_secret.encode("utf-8"), expected_prehash.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")

    sign = client._sign(ts, method, request_path, body)
    assert sign == expected_sign


def test_okx_headers_live_vs_demo():
    """Valida la inclusió correcta de headers d'autenticació i el flag de simulated trading."""
    client_live = OkxLiveClient("k", "s", "p", is_demo=False)
    headers_live = client_live._get_headers("GET", "/test")
    assert headers_live["OK-ACCESS-KEY"] == "k"
    assert headers_live["OK-ACCESS-PASSPHRASE"] == "p"
    assert "x-simulated-trading" not in headers_live

    client_demo = OkxLiveClient("k", "s", "p", is_demo=True)
    headers_demo = client_demo._get_headers("POST", "/test", '{"test": 1}')
    assert headers_demo["x-simulated-trading"] == "1"


# =====================================================================
# 2. Tests de Conversió de Contractes i Sizing (`ctVal`)
# =====================================================================

def test_okx_contract_sizing():
    """Comprova la conversió de mida base a contractes i l'arrodoniment delta-neutral."""
    # 1. Mode Global (SWAP)
    client_global = OkxLiveClient("k", "s", "p", base_url="https://www.okx.com")
    with patch.dict("os.environ", {"OKX_REGION": "global"}):
        assert client_global.get_contract_val("BTC") == 0.01
        assert client_global.to_contract_size("BTC", 0.024) == 2
        assert client_global.round_size("BTC", 0.024) == 0.02
        assert client_global.get_contract_val("ETH") == 0.1
        assert client_global.to_contract_size("ETH", 0.38) == 4
        assert client_global.round_size("ETH", 0.38) == 0.4
        assert client_global.get_contract_val("SOL") == 1.0

    # 2. Mode Europa / EEA (X-Perp per a minoristes)
    client_eea = OkxLiveClient("k", "s", "p", base_url="https://eea.okx.com")
    with patch.dict("os.environ", {"OKX_REGION": "eea"}):
        assert client_eea.get_contract_val("BTC") == 0.0001
        assert client_eea.get_contract_val("ETH") == 0.001
        assert client_eea.get_contract_val("SOL") == 0.01
        assert client_eea.to_contract_size("BTC", 0.000375) == 4
        assert client_eea.round_size("BTC", 0.000375) == 0.0004


def test_okx_price_rounding():
    """Comprova l'arrodoniment de preus segons tick size."""
    client = OkxLiveClient("k", "s", "p")
    # Per defecte tick_sz = 0.01 (2 decimals)
    assert client.round_price("BTC", 88123.456) == 88123.46
    assert client.round_price("SOL", 145.891) == 145.89


# =====================================================================
# 3. Tests de Mètodes REST (Balanç, Posicions, Ordres)
# =====================================================================

def test_okx_get_balance_and_positions():
    """Verifica la consulta de saldo i mapeig de posicions obertes a OKX."""
    async def _test():
        client = OkxLiveClient("k", "s", "p", base_url="https://www.okx.com")
        mock_bal_res = {
            "code": "0",
            "data": [{
                "totalEq": "450.50",
                "details": [{
                    "ccy": "USDT",
                    "availBal": "450.50",
                }]
            }]
        }

        mock_pos_res = {
            "code": "0",
            "data": [{
                "instId": "SOL-USDT-SWAP",
                "pos": "2",
                "posSide": "net",
                "avgPx": "140.50",
                "upl": "1.25",
            }]
        }

        client.account_config = {"posMode": "net_mode"}
        client._specs_initialized = True

        with patch.dict("os.environ", {"OKX_REGION": "global"}):
            with patch.object(client, "_request", side_effect=[mock_bal_res, mock_pos_res]):
                bal = await client.get_balance()
                assert bal == 450.50

                positions = await client.get_positions()
                assert len(positions) == 1
                pos = positions[0]
                assert pos["asset"] == "SOL"
                assert pos["side"] == "buy"
                assert pos["contracts"] == 2.0
                assert pos["amount"] == 2.0  # 2 contracts * 1.0 ctVal
                assert pos["avg_entry_price"] == 140.50

    asyncio.run(_test())


def test_okx_place_order_and_market_close():
    """Verifica l'enviament d'ordres límit/IOC i tancament a mercat a OKX."""
    async def _test():
        client = OkxLiveClient("k", "s", "p", base_url="https://www.okx.com")
        client._specs_initialized = True  # bypass instruments fetch
        client.account_config = {"posMode": "net_mode"}

        mock_order_res = {
            "code": "0",
            "data": [{
                "ordId": "123456789",
                "sCode": "0",
                "sMsg": "",
            }]
        }
        mock_check_res = {
            "code": "0",
            "data": [{
                "ordId": "123456789",
                "state": "filled",
                "accFillSz": "2",
                "avgPx": "145.0",
            }]
        }

        with patch.dict("os.environ", {"OKX_REGION": "global"}):
            with patch.object(client, "_request", side_effect=[mock_order_res, mock_check_res]) as mock_req:
                res = await client.place_order(
                    coin="SOL",
                    is_buy=True,
                    size=2.0,
                    price=145.0,
                    ioc=True,
                )
                assert res["status"] == "ok"
                assert res["ordId"] == "123456789"
                assert mock_req.call_count == 2
                call_kwargs = mock_req.call_args_list[0][1]
                assert call_kwargs["data"]["ordType"] == "ioc"
                assert call_kwargs["data"]["sz"] == "2"

        mock_pos_res = {
            "code": "0",
            "data": [{
                "instId": "SOL-USDT-SWAP",
                "pos": "2",
                "posSide": "net",
                "avgPx": "145.0",
            }]
        }
        mock_close_res = {"code": "0", "data": [{"instId": "SOL-USDT-SWAP"}]}
        with patch.object(client, "_request", side_effect=[mock_pos_res, mock_close_res]) as mock_req:
            res = await client.market_close("SOL", 2.0)
            assert res["status"] == "ok"

    asyncio.run(_test())


# =====================================================================
# 4. Tests de WebSocket d'OKX (`books5` i `funding-rate`)
# =====================================================================

def test_okx_ws_orderbook_and_funding():
    """Verifica el processament de missatges de llibre i de funding del WebSocket d'OKX."""
    async def _test():
        books_received = []
        funding_received = []

        def on_book(book: OrderBookL2):
            books_received.append(book)

        def on_funding(rates_dict: dict):
            funding_received.append(rates_dict)

        ws = OkxWSClient(coins=["SOL"], on_book_update=on_book, on_funding_update=on_funding)

        # Missatge de llibre d'ordres books5
        book_msg = {
            "arg": {"channel": "books5", "instId": "SOL-USDT-SWAP"},
            "data": [{
                "bids": [["145.10", "15", "0", "2"], ["145.00", "20", "0", "3"]],
                "asks": [["145.15", "10", "0", "1"], ["145.25", "30", "0", "4"]],
                "ts": "1710000000000",
            }]
        }
        ws._handle_message(book_msg)

        assert len(books_received) == 1
        b = books_received[0]
        assert b.coin == "SOL"
        assert b.best_bid == 145.10
        assert b.best_ask == 145.15
        assert b.mid_price == pytest.approx(145.125, rel=1e-4)

        # Missatge de funding rate
        funding_msg = {
            "arg": {"channel": "funding-rate", "instId": "SOL-USDT-SWAP"},
            "data": [{
                "instId": "SOL-USDT-SWAP",
                "fundingRate": "0.00015",  # 0.015%
            }]
        }
        ws._handle_message(funding_msg)

        assert len(funding_received) == 1
        assert funding_received[0]["SOL"] == 0.015

    asyncio.run(_test())


# =====================================================================
# 5. Tests d'Integració d'Arbitratge Real (HL + OKX)
# =====================================================================

def test_arbitrage_live_exchange_okx_fees_and_sizing():
    """Verifica que ArbitrageLiveExchange configuri les comissions d'OKX (0.02% Maker / 0.05% Taker) i alineï contractes."""
    async def _test():
        mock_hl = MagicMock()
        mock_hl.round_size = MagicMock(side_effect=lambda coin, sz: round(sz, 4))
        mock_hl.get_balance = AsyncMock(return_value=500.0)

        mock_okx = MagicMock()
        mock_okx.round_size = MagicMock(return_value=2.0)  # 2 SOL (2 contractes)
        mock_okx.get_balance = AsyncMock(return_value=500.0)
        mock_okx.get_positions = AsyncMock(return_value=[])

        exchange = ArbitrageLiveExchange(
            hl_client=mock_hl,
            venue2_client=mock_okx,
            venue2_name="OKX",
            initial_hl_balance=500.0,
            initial_bn_balance=500.0,
            state_file="/tmp/test_live_state_okx.json",
        )
        exchange.active_positions.clear()

        # Comissions OKX
        assert exchange.venue2_name == "OKX"
        assert exchange.bn_maker_fee == 0.00020
        assert exchange.bn_taker_fee == 0.00050

        # Test d'obertura amb alineament delta-neutral
        signal = ArbitrageSignal(
            coin="SOL",
            direction=ArbitrageDirection.BUY_HL_SELL_BN,
            hl_price=140.0,
            bn_price=140.5,
            spread_pct=0.35,
            strategy_name="CROSS_ARBITRAGE",
        )

        # Mock place_order
        mock_hl.place_order = AsyncMock(return_value={"status": "ok", "fill": {"avgPx": "140.0"}})
        mock_okx.place_order = AsyncMock(return_value={"status": "ok", "ordId": "999"})

        await exchange._execute_live_open(signal, size_usd=280.0, is_maker=False)

        assert len(exchange.active_positions) == 1
        pos = list(exchange.active_positions.values())[0]
        assert pos.coin == "SOL"
        # Comprovem que tant HL com OKX tenen la mateixa mida exacte (2.0 SOL)
        assert pos.leg_hl.size == 2.0
        assert pos.leg_bn.size == 2.0

    asyncio.run(_test())


# =====================================================================
# 6. Tests de Sortida per Temps / Rotació de Capital
# =====================================================================

def test_time_exit_rotation_and_deadlock_prevention():
    """Valida que les posicions obertes no quedin atrapades si un spread s'eixampla lleugerament."""
    strategy = CrossExchangeArbitrageStrategy(venue2_name="OKX")

    # Simulem llibres de mercat
    hl_book = OrderBookL2(
        coin="SOL",
        bids=[BookLevel(price=140.0, size=10.0)],
        asks=[BookLevel(price=140.02, size=10.0)],
        timestamp=time.time(),
    )
    okx_book = OrderBookL2(
        coin="SOL",
        bids=[BookLevel(price=140.01, size=10.0)],
        asks=[BookLevel(price=140.03, size=10.0)],
        timestamp=time.time(),
    )
    strategy.hl_books["SOL"] = hl_book
    strategy.bn_books["SOL"] = okx_book

    from core.arbitrage_models import ArbitrageLeg

    # Creem posició amb 65 minuts d'antiguitat
    now = time.time()
    leg_hl = ArbitrageLeg(
        venue="HYPERLIQUID",
        coin="SOL",
        side=OrderSide.BUY,
        entry_price=140.0,
        size=2.0,
        size_usd=280.0,
        current_price=140.0,
        fee_rate=0.00045,
    )
    leg_bn = ArbitrageLeg(
        venue="OKX",
        coin="SOL",
        side=OrderSide.SELL,
        entry_price=140.5,
        size=2.0,
        size_usd=281.0,
        current_price=140.03,
        fee_rate=0.00050,
    )
    pos = ArbitragePosition(
        pair_id="test_pair_1",
        coin="SOL",
        direction=ArbitrageDirection.BUY_HL_SELL_BN,
        leg_hl=leg_hl,
        leg_bn=leg_bn,
        entry_spread_pct=0.35,
        entry_time=now - 3700.0,  # > 60 min
    )
    pos.divergence_start_time = None
    pos.accumulated_funding = 0.0

    # L'exit ha de detectar rotació de capital i alliberar la ranura
    exit_decision = strategy.check_exit(pos)
    assert exit_decision is not None
    reason, _, _ = exit_decision
    assert reason in ("TIME_ROTATION_BREAKEVEN", "TIME_SLOT_FREE", "CONVERGENCE_TARGET", "TAKE_PROFIT_TARGET")


def test_trading_paused_prevents_new_positions():
    """Valida que el mode de pausa de trading bloquegi noves entrades per permetre transferència de fons."""
    from core.arbitrage_paper_exchange import ArbitragePaperExchange

    exchange = ArbitragePaperExchange(
        initial_hl_balance=500.0,
        initial_bn_balance=500.0,
    )
    assert exchange.trading_paused is False

    exchange.trading_paused = True
    sig = ArbitrageSignal(
        coin="SOL",
        direction=ArbitrageDirection.BUY_HL_SELL_BN,
        hl_price=140.0,
        bn_price=140.5,
        spread_pct=0.35,
        strategy_name="CROSS_ARBITRAGE",
    )
    pos = exchange.open_arbitrage_position(sig, size_usd=100.0)
    assert pos is None
    assert len(exchange.active_positions) == 0
    assert exchange.metrics["trading_paused"] is True


def test_okx_xperp_discovery_and_order():
    """Valida el mapeig dinàmic d'instruments X-Perp a Europa (EEA) i l'execució d'ordres amb get_inst_id."""
    async def _test():
        client = OkxLiveClient("k", "s", "p", base_url="https://eea.okx.com")

        mock_fut_res = {
            "code": "0",
            "data": [
                {
                    "instId": "NEAR-USD_UM_XPERP-310613",
                    "ctVal": "1",
                    "minSz": "1",
                    "lotSz": "1",
                    "tickSz": "0.001",
                    "state": "live",
                },
                {
                    "instId": "BTC-USD_UM_XPERP-310404",
                    "ctVal": "0.0001",
                    "minSz": "1",
                    "lotSz": "1",
                    "tickSz": "0.1",
                    "state": "live",
                },
            ],
        }
        mock_swap_res = {"code": "0", "data": []}
        client.account_config = {"posMode": "net_mode"}
        with patch.object(client, "_request", side_effect=[mock_fut_res, mock_swap_res]):
            await client.init_contract_specs()

            assert client.get_inst_id("NEAR") == "NEAR-USD_UM_XPERP-310613"
            assert client.get_inst_id("BTC") == "BTC-USD_UM_XPERP-310404"
            assert client.get_contract_val("NEAR") == 1.0
            assert client.get_contract_val("BTC") == 0.0001

            # Sizing per a micro-pressupost: 30$ a BTC @ 80.000$ = ~3 contractes (3 * 8$ = 24$)
            assert client.to_contract_size("BTC", 0.000375) == 4
            assert client.round_size("BTC", 0.000375) == 0.0004

        # Test d'ordre utilitzant l'instrument X-Perp
        mock_order_res = {
            "code": "0",
            "data": [{"ordId": "998877", "sCode": "0", "sMsg": ""}],
        }
        with patch.object(client, "_request", return_value=mock_order_res) as mock_req:
            res = await client.place_order(coin="NEAR", is_buy=True, size=10.0, price=2.58)
            assert res["status"] == "ok"
            call_kwargs = mock_req.call_args[1]
            assert call_kwargs["data"]["instId"] == "NEAR-USD_UM_XPERP-310613"
            assert call_kwargs["data"]["sz"] == "10"
            assert call_kwargs["data"]["px"] == "2.58"

    asyncio.run(_test())


def test_okx_balance_total_equity_vs_available():
    """Verifica que get_balance() retorna el patrimoni total (equity) i get_available_balance() el marge lliure."""
    async def _test():
        client = OkxLiveClient("k", "s", "p", base_url="https://www.okx.com")
        mock_bal_res = {
            "code": "0",
            "data": [{
                "totalEq": "450.40",
                "details": [{
                    "ccy": "USDC",
                    "availBal": "361.36",
                    "eq": "450.40",
                }]
            }]
        }
        with patch.object(client, "_request", return_value=mock_bal_res):
            total_bal = await client.get_balance()
            avail_bal = await client.get_available_balance()

            assert total_bal == 450.40
            assert avail_bal == 361.36

    asyncio.run(_test())
