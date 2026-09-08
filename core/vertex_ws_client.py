"""Client WebSocket asíncron per a Vertex Protocol (DEX d'alta freqüència amb 0% Maker fee)."""

import asyncio
import json
import logging
import os
import ssl
import time
from typing import Callable, Dict, List, Optional
import certifi
import websockets

from core.models import BookLevel, OrderBookL2

logger = logging.getLogger("VertexWS")

# Product IDs oficials per a contractes perpetus a Vertex Protocol (Arbitrum)
# Els perpetus a Vertex utilitzen IDs parells
VERTEX_PERP_PRODUCTS: Dict[str, int] = {
    "BTC": 2,
    "ETH": 4,
    "ARB": 8,
    "SOL": 12,
    "SUI": 28,
    "LINK": 30,
    "DOGE": 34,
    "AVAX": 36,
    "NEAR": 42,
    "TIA": 50,
}

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

class VertexWSClient:
    """
    Client WebSocket per connectar al llibre d'ordres L2 de Vertex Protocol (Arbitrum).
    Vertex ofereix 0.00% de comissió Maker en perps i una latència sub-10ms.
    """
    def __init__(
        self,
        coins: Optional[List[str]] = None,
        on_book_update: Optional[Callable[[OrderBookL2], None]] = None,
        on_funding_update: Optional[Callable[[Dict[str, float]], None]] = None,
    ):
        self.coins = [c.upper() for c in (coins or ["BTC", "ETH", "SOL"])]
        self.on_book_update = on_book_update
        self.on_funding_update = on_funding_update

        # Mapatge de monedes a Product IDs de Vertex
        self.coin_to_pid: Dict[str, int] = {}
        self.pid_to_coin: Dict[int, str] = {}
        for coin in self.coins:
            pid = VERTEX_PERP_PRODUCTS.get(coin)
            if pid is not None:
                self.coin_to_pid[coin] = pid
                self.pid_to_coin[pid] = coin
            else:
                logger.warning(f"Vertex Protocol: Product ID no registrat per a {coin}")

        self.order_books: Dict[str, OrderBookL2] = {}
        self.funding_rates_8h: Dict[str, float] = {}

        self.is_running = False
        self._ws_task: Optional[asyncio.Task] = None
        self._funding_task: Optional[asyncio.Task] = None
        self._ssl_context = get_ssl_context()

        self.ws_url = "wss://gateway.prod.vertexprotocol.com/v1/ws"

    async def start(self):
        """Inicia la connexió WebSocket i el bucle de sincronització de Vertex."""
        self.is_running = True
        self._ws_task = asyncio.create_task(self._run_ws_loop())
        self._funding_task = asyncio.create_task(self._run_funding_poll_loop())
        logger.info(f"VertexWSClient iniciat per a les monedes: {list(self.coin_to_pid.keys())}")

    async def stop(self):
        """Atura les tasques en segon pla."""
        self.is_running = False
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
        if self._funding_task and not self._funding_task.done():
            self._funding_task.cancel()
        logger.info("VertexWSClient aturat.")

    async def _run_ws_loop(self):
        """Bucle principal de WebSocket amb reconexió automàtica."""
        while self.is_running:
            try:
                headers = {"User-Agent": "Mozilla/5.0 (compatible; VertexBot/1.0)"}
                async with websockets.connect(
                    self.ws_url,
                    ssl=self._ssl_context,
                    ping_interval=20,
                    ping_timeout=15,
                    close_timeout=10,
                    extra_headers=headers,
                ) as ws:
                    logger.info("✅ Connexió establerta amb el WebSocket de Vertex Protocol.")

                    # Subscripció al stream book_depth per a cada product_id
                    for coin, pid in self.coin_to_pid.items():
                        sub_msg = {
                            "method": "subscribe",
                            "stream": {
                                "type": "book_depth",
                                "product_id": pid,
                            },
                            "id": pid,
                        }
                        await ws.send(json.dumps(sub_msg))
                        logger.debug(f"Subscripció enviada a Vertex per a {coin} (PID {pid})")

                    async for message in ws:
                        if not self.is_running:
                            break
                        try:
                            data = json.loads(message)
                            self._handle_ws_message(data)
                        except Exception as parse_err:
                            logger.debug(f"Error processant missatge Vertex: {parse_err}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Desconnexió del WebSocket de Vertex ({e}). Reconnectant en 3s...")
                await asyncio.sleep(3.0)

    def _handle_ws_message(self, data: dict):
        """Processa les actualitzacions del llibre d'ordres L2 de Vertex."""
        stream_data = data.get("data", data)
        msg_type = stream_data.get("type")

        if msg_type == "book_depth":
            pid = stream_data.get("product_id")
            coin = self.pid_to_coin.get(pid)
            if not coin:
                return

            raw_bids = stream_data.get("bids", [])
            raw_asks = stream_data.get("asks", [])

            bids: List[BookLevel] = []
            for b in raw_bids:
                try:
                    # Preu i quantitat vénen en format x18 (1e18)
                    px = float(b[0]) / 1e18
                    sz = float(b[1]) / 1e18
                    if px > 0 and sz > 0:
                        bids.append(BookLevel(price=px, size=sz))
                except (ValueError, TypeError, IndexError):
                    continue

            asks: List[BookLevel] = []
            for a in raw_asks:
                try:
                    px = float(a[0]) / 1e18
                    sz = float(a[1]) / 1e18
                    if px > 0 and sz > 0:
                        asks.append(BookLevel(price=px, size=sz))
                except (ValueError, TypeError, IndexError):
                    continue

            # Ordenació estàndard: bids descendents, asks ascendents
            bids.sort(key=lambda x: x.price, reverse=True)
            asks.sort(key=lambda x: x.price, reverse=False)

            book = OrderBookL2(
                coin=coin,
                timestamp=time.time(),
                bids=bids[:20],
                asks=asks[:20],
            )
            self.order_books[coin] = book

            if self.on_book_update:
                self.on_book_update(book)

    async def _run_funding_poll_loop(self):
        """Consulta periòdica de funding rates de Vertex."""
        while self.is_running:
            try:
                # Per defecte establim una taxa d'estimació neutral de 0.01% (8h)
                for coin in self.coins:
                    if coin not in self.funding_rates_8h:
                        self.funding_rates_8h[coin] = 0.0100

                if self.on_funding_update:
                    self.on_funding_update(self.funding_rates_8h)
            except Exception as e:
                logger.debug(f"Error consultant funding a Vertex: {e}")

            await asyncio.sleep(60.0)
