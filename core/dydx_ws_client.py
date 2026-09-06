"""Client WebSocket asíncron per a dYdX v4 Indexer (DEX descentralitzat de perpetus)."""

import asyncio
import json
import logging
import os
import ssl
import time
from typing import Callable, Dict, List, Optional
import aiohttp
import certifi
import websockets

from core.models import BookLevel, OrderBookL2

logger = logging.getLogger("DydxV4WS")

def get_ssl_context() -> ssl.SSLContext:
    """Retorna un context SSL compatible amb entorns locals, Docker i proxies."""
    ctx = ssl.create_default_context()
    cert_file = os.environ.get("SSL_CERT_FILE")
    if cert_file and os.path.exists(cert_file):
        try:
            ctx.load_verify_locations(cafile=cert_file)
            return ctx
        except Exception:
            pass
    try:
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    return ctx

class DydxV4WSClient:
    def __init__(
        self,
        coins: Optional[List[str]] = None,
        on_book_update: Optional[Callable[[OrderBookL2], None]] = None,
        on_funding_update: Optional[Callable[[Dict[str, float]], None]] = None,
    ):
        self.coins = coins or ["BTC", "ETH", "SOL", "LINK", "NEAR", "SUI", "DOGE"]
        self.symbol_map = {c.upper(): f"{c.upper()}-USD" for c in self.coins}
        self.reverse_map = {f"{c.upper()}-USD": c.upper() for c in self.coins}

        self.on_book_update = on_book_update
        self.on_funding_update = on_funding_update

        # Emmagatzematge intern de llibre d'ordres complet {coin: {price: size}}
        self._raw_bids: Dict[str, Dict[float, float]] = {c: {} for c in self.coins}
        self._raw_asks: Dict[str, Dict[float, float]] = {c: {} for c in self.coins}

        self.order_books: Dict[str, OrderBookL2] = {}
        self.funding_rates_8h: Dict[str, float] = {}  # Percentatge 8h (ex: 0.0100 = 0.01%)
        self.oracle_prices: Dict[str, float] = {}

        self.is_running = False
        self._ws_task: Optional[asyncio.Task] = None
        self._funding_task: Optional[asyncio.Task] = None
        self._ssl_context = get_ssl_context()

        self.ws_url = "wss://indexer.dydx.trade/v4/ws"
        self.rest_url = "https://indexer.dydx.trade/v4/perpetualMarkets"

    async def start(self):
        """Inicia la connexió WebSocket i el polling de funding de dYdX v4."""
        self.is_running = True
        self._ws_task = asyncio.create_task(self._run_ws_loop())
        self._funding_task = asyncio.create_task(self._run_funding_poll_loop())

    async def stop(self):
        """Atura el client de dYdX."""
        self.is_running = False
        for task in (self._ws_task, self._funding_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _run_ws_loop(self):
        retry_delay = 2.0
        while self.is_running:
            try:
                logger.info(f"Connectant a dYdX v4 WebSocket: {self.ws_url}...")
                async with websockets.connect(
                    self.ws_url,
                    ssl=self._ssl_context,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    logger.info("Connexió WebSocket amb dYdX v4 establerta amb èxit.")
                    retry_delay = 2.0

                    # Subscripció a cadascuna de les monedes per al canal v4_orderbook
                    for coin in self.coins:
                        market_id = self.symbol_map.get(coin)
                        if market_id:
                            sub_msg = {
                                "type": "subscribe",
                                "channel": "v4_orderbook",
                                "id": market_id,
                                "batched": True,
                            }
                            await ws.send(json.dumps(sub_msg))

                    async for raw_msg in ws:
                        if not self.is_running:
                            break
                        try:
                            msg = json.loads(raw_msg)
                            self._handle_ws_message(msg)
                        except Exception as e:
                            logger.error(f"Error processant missatge WS dYdX: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Connexió WS dYdX caiguda: {e}. Reintentant en {retry_delay:.1f}s...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, 30.0)

    def _handle_ws_message(self, msg: dict):
        msg_type = msg.get("type")
        market_id = msg.get("id")
        coin = self.reverse_map.get(market_id)
        if not coin:
            return

        now_ts = time.time()

        if msg_type == "subscribed":
            # Snapshot inicial del llibre d'ordres
            contents = msg.get("contents", {})
            bids_dict = {}
            for b in contents.get("bids", []):
                try:
                    p, s = float(b["price"]), float(b["size"])
                    if s > 0:
                        bids_dict[p] = s
                except (ValueError, KeyError):
                    continue

            asks_dict = {}
            for a in contents.get("asks", []):
                try:
                    p, s = float(a["price"]), float(a["size"])
                    if s > 0:
                        asks_dict[p] = s
                except (ValueError, KeyError):
                    continue

            self._raw_bids[coin] = bids_dict
            self._raw_asks[coin] = asks_dict
            self._emit_orderbook(coin, now_ts)

        elif msg_type in ("channel_batch_data", "channel_data"):
            # Actualitzacions incrementals (deltas)
            contents = msg.get("contents", [])
            bids_dict = self._raw_bids[coin]
            asks_dict = self._raw_asks[coin]

            # En batched, contents és una llista de canvis; en no-batched, pot ser un sol diccionari
            items = contents if isinstance(contents, list) else [contents]
            for item in items:
                # Actualització de bids
                for b in item.get("bids", []):
                    try:
                        p, s = float(b[0]), float(b[1])
                        if s <= 0:
                            bids_dict.pop(p, None)
                        else:
                            bids_dict[p] = s
                    except (ValueError, IndexError):
                        continue

                # Actualització de asks
                for a in item.get("asks", []):
                    try:
                        p, s = float(a[0]), float(a[1])
                        if s <= 0:
                            asks_dict.pop(p, None)
                        else:
                            asks_dict[p] = s
                    except (ValueError, IndexError):
                        continue

            self._emit_orderbook(coin, now_ts)

    def _emit_orderbook(self, coin: str, timestamp: float):
        bids_dict = self._raw_bids.get(coin, {})
        asks_dict = self._raw_asks.get(coin, {})
        if not bids_dict or not asks_dict:
            return

        best_bid = max(bids_dict.keys())
        best_ask = min(asks_dict.keys())

        # Si el llibre està creuat momentàniament, prenem el mid
        if best_bid > best_ask:
            best_bid, best_ask = best_ask, best_bid

        bid_qty = bids_dict.get(best_bid, 1.0)
        ask_qty = asks_dict.get(best_ask, 1.0)

        order_book = OrderBookL2(
            coin=coin,
            timestamp=timestamp,
            bids=[BookLevel(price=best_bid, size=bid_qty, num_orders=1)],
            asks=[BookLevel(price=best_ask, size=ask_qty, num_orders=1)],
        )
        self.order_books[coin] = order_book

        if self.on_book_update:
            self.on_book_update(order_book)

    async def _run_funding_poll_loop(self):
        """Consulta periòdica de Funding Rates oficials de dYdX cada 20 segons."""
        while self.is_running:
            try:
                connector = aiohttp.TCPConnector(ssl=self._ssl_context)
                async with aiohttp.ClientSession(connector=connector) as session:
                    headers = {"User-Agent": "Mozilla/5.0"}
                    async with session.get(self.rest_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8.0)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            markets = data.get("markets", {})
                            for coin in self.coins:
                                market_id = self.symbol_map.get(coin)
                                if market_id in markets:
                                    m = markets[market_id]
                                    # dYdX reporta funding horari en decimal (ex: 0.0000125 = 0.00125% per 1h)
                                    # Convertim a taxa de 8h en percentatge per homogeneïtzar amb l'estratègia
                                    fr_1h = float(m.get("nextFundingRate", 0.0))
                                    rate_8h_pct = fr_1h * 8.0 * 100.0
                                    oracle_px = float(m.get("oraclePrice", 0.0))
                                    self.funding_rates_8h[coin] = rate_8h_pct
                                    self.oracle_prices[coin] = oracle_px

                            if self.on_funding_update:
                                self.on_funding_update(self.funding_rates_8h)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Error obtenint funding rates de dYdX: {e}")

            await asyncio.sleep(20.0)
