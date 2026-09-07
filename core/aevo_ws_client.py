"""Client WebSocket asíncron per a Aevo DEX (DEX descentralitzat de perpetuals d'alta freqüència)."""

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

logger = logging.getLogger("AevoWS")

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

class AevoWSClient:
    """
    Client WebSocket per connectar al llibre d'ordres L2 i obtenir taxes de finançament
    d'Aevo DEX (Arbitrum / Optimism / Ethereum).
    """
    def __init__(
        self,
        coins: Optional[List[str]] = None,
        on_book_update: Optional[Callable[[OrderBookL2], None]] = None,
        on_funding_update: Optional[Callable[[Dict[str, float]], None]] = None,
    ):
        self.coins = coins or ["BTC", "ETH", "SOL", "HYPE", "AVAX", "DOGE", "NEAR", "SUI"]
        self.symbol_map = {c.upper(): f"{c.upper()}-PERP" for c in self.coins}
        self.reverse_map = {f"{c.upper()}-PERP": c.upper() for c in self.coins}

        self.on_book_update = on_book_update
        self.on_funding_update = on_funding_update

        # Emmagatzematge intern de llibre d'ordres {coin: {price: size}}
        self._raw_bids: Dict[str, Dict[float, float]] = {c: {} for c in self.coins}
        self._raw_asks: Dict[str, Dict[float, float]] = {c: {} for c in self.coins}

        self.order_books: Dict[str, OrderBookL2] = {}
        self.funding_rates_8h: Dict[str, float] = {}  # Percentatge 8h (ex: 0.0100 = 0.01%)

        self.is_running = False
        self._ws_task: Optional[asyncio.Task] = None
        self._funding_task: Optional[asyncio.Task] = None
        self._ssl_context = get_ssl_context()

        self.ws_url = "wss://ws.aevo.xyz"
        self.rest_funding_url = "https://api.aevo.xyz/funding"

    async def start(self):
        """Inicia la connexió WebSocket i el bucle de funding d'Aevo."""
        self.is_running = True
        self._ws_task = asyncio.create_task(self._run_ws_loop())
        self._funding_task = asyncio.create_task(self._run_funding_poll_loop())

    async def stop(self):
        """Atura el client d'Aevo."""
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
                logger.info(f"Connectant a Aevo DEX WebSocket: {self.ws_url}...")
                async with websockets.connect(
                    self.ws_url,
                    ssl=self._ssl_context,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    logger.info("Connexió WebSocket amb Aevo DEX establerta amb èxit.")
                    retry_delay = 2.0

                    # Subscripció a orderbook-100ms per a cadascuna de les monedes
                    sub_channels = [f"orderbook-100ms:{self.symbol_map[c]}" for c in self.coins if c in self.symbol_map]
                    sub_msg = {
                        "op": "subscribe",
                        "data": sub_channels,
                    }
                    await ws.send(json.dumps(sub_msg))
                    logger.info(f"Subscrit als canals Aevo: {sub_channels}")

                    async for raw_msg in ws:
                        if not self.is_running:
                            break
                        try:
                            msg = json.loads(raw_msg)
                            self._handle_ws_message(msg)
                        except Exception as e:
                            logger.error(f"Error processant missatge WS Aevo: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Connexió WS Aevo caiguda: {e}. Reintentant en {retry_delay:.1f}s...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, 30.0)

    def _handle_ws_message(self, msg: dict):
        channel = msg.get("channel", "")
        data = msg.get("data")
        if not data or not isinstance(data, dict):
            return

        instrument_name = data.get("instrument_name")
        if not instrument_name and ":" in channel:
            instrument_name = channel.split(":")[-1]

        coin = self.reverse_map.get(instrument_name)
        if not coin:
            return

        now_ts = time.time()
        msg_type = data.get("type")
        bids = data.get("bids", [])
        asks = data.get("asks", [])

        if msg_type == "snapshot":
            # Snapshot complet inicial
            bids_dict = {}
            for b in bids:
                try:
                    p, s = float(b[0]), float(b[1])
                    if s > 0:
                        bids_dict[p] = s
                except (ValueError, IndexError):
                    continue

            asks_dict = {}
            for a in asks:
                try:
                    p, s = float(a[0]), float(a[1])
                    if s > 0:
                        asks_dict[p] = s
                except (ValueError, IndexError):
                    continue

            self._raw_bids[coin] = bids_dict
            self._raw_asks[coin] = asks_dict
            self._emit_orderbook(coin, now_ts)

        elif msg_type == "update":
            # Actualització incremental (delta)
            bids_dict = self._raw_bids[coin]
            asks_dict = self._raw_asks[coin]

            for b in bids:
                try:
                    p, s = float(b[0]), float(b[1])
                    if s <= 0:
                        bids_dict.pop(p, None)
                    else:
                        bids_dict[p] = s
                except (ValueError, IndexError):
                    continue

            for a in asks:
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
        """Consulta periòdica de Funding Rates oficials d'Aevo cada 20 segons."""
        while self.is_running:
            try:
                connector = aiohttp.TCPConnector(ssl=self._ssl_context)
                async with aiohttp.ClientSession(connector=connector) as session:
                    headers = {"User-Agent": "Mozilla/5.0"}
                    for coin in self.coins:
                        instrument_name = self.symbol_map.get(coin)
                        if not instrument_name:
                            continue
                        url = f"{self.rest_funding_url}?instrument_name={instrument_name}"
                        try:
                            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    fr_1h = float(data.get("funding_rate", 0.0))
                                    # Taxa de 8h en percentatge (%) per homogeneïtzar amb l'estratègia
                                    rate_8h_pct = fr_1h * 8.0 * 100.0
                                    self.funding_rates_8h[coin] = rate_8h_pct
                        except Exception as e:
                            logger.debug(f"Error obtenint funding Aevo per a {coin}: {e}")

                    if self.on_funding_update and self.funding_rates_8h:
                        self.on_funding_update(self.funding_rates_8h)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Error al bucle de funding rates d'Aevo: {e}")

            await asyncio.sleep(20.0)
