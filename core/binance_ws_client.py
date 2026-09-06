"""Client WebSocket asíncron per a Binance Futures (USD-M)."""

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

logger = logging.getLogger("BinanceFuturesWS")

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

class BinanceFuturesWSClient:
    def __init__(
        self,
        coins: Optional[List[str]] = None,
        on_book_update: Optional[Callable[[OrderBookL2], None]] = None,
        on_funding_update: Optional[Callable[[Dict[str, float]], None]] = None,
    ):
        # Monedes en format base (ex: ["BTC", "ETH", "SOL"])
        self.coins = coins or ["BTC", "ETH", "SOL", "LINK", "NEAR", "SUI", "DOGE"]
        self.symbol_map = {c.upper(): f"{c.lower()}usdt" for c in self.coins}
        self.reverse_map = {f"{c.upper()}USDT": c.upper() for c in self.coins}
        
        self.on_book_update = on_book_update
        self.on_funding_update = on_funding_update
        
        self.order_books: Dict[str, OrderBookL2] = {}
        self.funding_rates_8h: Dict[str, float] = {}  # Percentatge (ex: 0.0100 = 0.01%)
        self.mark_prices: Dict[str, float] = {}
        
        self.is_running = False
        self._ws_task: Optional[asyncio.Task] = None
        self._funding_task: Optional[asyncio.Task] = None
        self._ssl_context = get_ssl_context()

    @property
    def stream_url(self) -> str:
        streams = [f"{self.symbol_map[c]}@bookTicker" for c in self.coins if c in self.symbol_map]
        return f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"

    async def start(self):
        """Inicia el cicle de connexió WebSocket i polling de funding en segon pla."""
        self.is_running = True
        self._ws_task = asyncio.create_task(self._run_ws_loop())
        self._funding_task = asyncio.create_task(self._run_funding_poll_loop())

    async def stop(self):
        """Atura el client WebSocket."""
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
                url = self.stream_url
                logger.info(f"Connectant a Binance Futures WS: {url[:80]}...")
                async with websockets.connect(
                    url,
                    ssl=self._ssl_context,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    logger.info("Connexió WebSocket amb Binance Futures establerta amb èxit.")
                    retry_delay = 2.0
                    
                    async for raw_msg in ws:
                        if not self.is_running:
                            break
                        try:
                            msg = json.loads(raw_msg)
                            self._handle_ws_message(msg)
                        except Exception as e:
                            logger.error(f"Error processant missatge WS Binance: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Connexió WS Binance caiguda: {e}. Reintentant en {retry_delay:.1f}s...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, 30.0)

    def _handle_ws_message(self, msg: dict):
        data = msg.get("data")
        if not data or data.get("e") != "bookTicker":
            return

        symbol = data.get("s", "")
        coin = self.reverse_map.get(symbol)
        if not coin:
            return

        try:
            best_bid = float(data["b"])
            bid_qty = float(data["B"])
            best_ask = float(data["a"])
            ask_qty = float(data["A"])
            ts = float(data.get("T", time.time() * 1000)) / 1000.0

            order_book = OrderBookL2(
                coin=coin,
                timestamp=ts,
                bids=[BookLevel(price=best_bid, size=bid_qty, num_orders=1)],
                asks=[BookLevel(price=best_ask, size=ask_qty, num_orders=1)],
            )
            self.order_books[coin] = order_book

            if self.on_book_update:
                self.on_book_update(order_book)
        except (ValueError, KeyError) as err:
            logger.debug(f"Error parsejant bookTicker Binance ({symbol}): {err}")

    async def _run_funding_poll_loop(self):
        """Consulta periòdica de Funding Rates oficials de Binance cada 20 segons."""
        funding_url = "https://fapi.binance.com/fapi/v1/premiumIndex"
        while self.is_running:
            try:
                connector = aiohttp.TCPConnector(ssl=self._ssl_context)
                async with aiohttp.ClientSession(connector=connector) as session:
                    headers = {"User-Agent": "Mozilla/5.0"}
                    async with session.get(funding_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8.0)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for item in data:
                                sym = item.get("symbol")
                                if sym in self.reverse_map:
                                    coin = self.reverse_map[sym]
                                    rate_8h = float(item.get("lastFundingRate", 0.0)) * 100.0
                                    mark_px = float(item.get("markPrice", 0.0))
                                    self.funding_rates_8h[coin] = rate_8h
                                    self.mark_prices[coin] = mark_px
                            
                            if self.on_funding_update:
                                self.on_funding_update(self.funding_rates_8h)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Error obtenint funding rates de Binance: {e}")

            await asyncio.sleep(20.0)
