"""Client WebSocket asíncron per a Hyperliquid."""

import asyncio
import json
import logging
import ssl
import time
from typing import Callable, Dict, List, Optional
import certifi
import websockets

from config.settings import config
from core.models import BookLevel, OrderBookL2, OrderSide, Trade

logger = logging.getLogger("HyperliquidWS")

class HyperliquidWSClient:
    def __init__(
        self,
        coins: Optional[List[str]] = None,
        on_book_update: Optional[Callable[[OrderBookL2], None]] = None,
        on_trade_update: Optional[Callable[[Trade], None]] = None,
    ):
        self.coins = coins or config.coins
        self.ws_url = config.testnet_ws_url if config.use_testnet else config.mainnet_ws_url
        self.on_book_update = on_book_update
        self.on_trade_update = on_trade_update
        
        # Estat intern dels llibres i darrers trades
        self.order_books: Dict[str, OrderBookL2] = {}
        self.recent_trades: Dict[str, List[Trade]] = {coin: [] for coin in self.coins}
        
        self.is_running = False
        self._ws_task: Optional[asyncio.Task] = None
        self._ssl_context = ssl.create_default_context(cafile=certifi.where())

    async def start(self):
        """Inicia el cicle de connexió en segon pla."""
        self.is_running = True
        self._ws_task = asyncio.create_task(self._run_loop())

    async def stop(self):
        """Atura el client WebSocket."""
        self.is_running = False
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self):
        retry_delay = 2.0
        while self.is_running:
            try:
                logger.info(f"Connectant a {self.ws_url}...")
                async with websockets.connect(
                    self.ws_url,
                    ssl=self._ssl_context,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    logger.info("Connexió WebSocket establerta amb èxit.")
                    retry_delay = 2.0  # Reset retry delay
                    
                    # Subscriure's a l2Book i trades per a cada moneda
                    for coin in self.coins:
                        # Subscripció L2 Book
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "l2Book", "coin": coin}
                        }))
                        # Subscripció Trades
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "trades", "coin": coin}
                        }))
                        logger.info(f"Subscrit a l2Book i trades per a {coin}")

                    # Escolta de missatges
                    async for raw_msg in ws:
                        if not self.is_running:
                            break
                        try:
                            msg = json.loads(raw_msg)
                            self._handle_message(msg)
                        except Exception as e:
                            logger.error(f"Error processant missatge WS: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Connexió WS caiguda: {e}. Reintentant en {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, 30.0)

    def _handle_message(self, msg: dict):
        channel = msg.get("channel")
        data = msg.get("data")
        if not channel or not data:
            return

        if channel == "l2Book":
            coin = data.get("coin")
            if not coin:
                return
            
            levels = data.get("levels", [])
            if len(levels) < 2:
                return

            raw_bids = levels[0]
            raw_asks = levels[1]

            bids = [
                BookLevel(
                    price=float(item["px"]),
                    size=float(item["sz"]),
                    num_orders=int(item.get("n", 1))
                )
                for item in raw_bids
            ]
            asks = [
                BookLevel(
                    price=float(item["px"]),
                    size=float(item["sz"]),
                    num_orders=int(item.get("n", 1))
                )
                for item in raw_asks
            ]

            ts = float(data.get("time", time.time() * 1000)) / 1000.0
            order_book = OrderBookL2(
                coin=coin,
                timestamp=ts,
                bids=bids,
                asks=asks
            )
            self.order_books[coin] = order_book

            if self.on_book_update:
                self.on_book_update(order_book)

        elif channel == "trades":
            if not isinstance(data, list):
                return
            
            for item in data:
                coin = item.get("coin")
                if not coin:
                    continue
                
                # side: 'B' vol dir taker compra (preu puja / verd), 'A' taker ven (preu baixa / vermell)
                side = OrderSide.BUY if item.get("side") == "B" else OrderSide.SELL
                trade = Trade(
                    coin=coin,
                    side=side,
                    price=float(item["px"]),
                    size=float(item["sz"]),
                    timestamp=float(item.get("time", time.time() * 1000)) / 1000.0
                )

                if coin not in self.recent_trades:
                    self.recent_trades[coin] = []
                self.recent_trades[coin].append(trade)
                
                # Guardem només els últims 500 trades per moneda
                if len(self.recent_trades[coin]) > 500:
                    self.recent_trades[coin].pop(0)

                if self.on_trade_update:
                    self.on_trade_update(trade)
