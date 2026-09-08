"""Client WebSocket asíncron per a Hyperliquid."""

import asyncio
import json
import logging
import ssl
import time
from typing import Any, Callable, Dict, List, Optional
import certifi
import websockets

from config.settings import config
from core.models import BookLevel, OrderBookL2, OrderSide, Trade

logger = logging.getLogger("HyperliquidWS")

# Mapeig de símbols entre la denominació comuna i la de Hyperliquid
# Ex: PEPE a Hyperliquid cotitza com a kPEPE (on 1 contracte kPEPE = 1.000 tokens PEPE)
HL_COIN_MAPPINGS: Dict[str, Dict[str, Any]] = {
    "PEPE": {"hl_name": "kPEPE", "multiplier": 1000.0},
    "SHIB": {"hl_name": "kSHIB", "multiplier": 1000.0},
    "BONK": {"hl_name": "kBONK", "multiplier": 1000.0},
    "FLOKI": {"hl_name": "kFLOKI", "multiplier": 1000.0},
}
HL_REVERSE_MAPPINGS: Dict[str, tuple[str, float]] = {
    v["hl_name"]: (k, v["multiplier"]) for k, v in HL_COIN_MAPPINGS.items()
}

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
                        hl_coin = HL_COIN_MAPPINGS.get(coin.upper(), {}).get("hl_name", coin)
                        # Subscripció L2 Book
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "l2Book", "coin": hl_coin}
                        }))
                        # Subscripció Trades
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "trades", "coin": hl_coin}
                        }))
                        logger.info(f"Subscrit a l2Book i trades per a {coin} (HL: {hl_coin})")

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
            raw_coin = data.get("coin")
            if not raw_coin:
                return

            if raw_coin in HL_REVERSE_MAPPINGS:
                coin, mult = HL_REVERSE_MAPPINGS[raw_coin]
            else:
                coin, mult = raw_coin, 1.0
            
            levels = data.get("levels", [])
            if len(levels) < 2:
                return

            raw_bids = levels[0]
            raw_asks = levels[1]

            bids = [
                BookLevel(
                    price=float(item["px"]) / mult,
                    size=float(item["sz"]) * mult,
                    num_orders=int(item.get("n", 1))
                )
                for item in raw_bids
            ]
            asks = [
                BookLevel(
                    price=float(item["px"]) / mult,
                    size=float(item["sz"]) * mult,
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
                raw_coin = item.get("coin")
                if not raw_coin:
                    continue

                if raw_coin in HL_REVERSE_MAPPINGS:
                    coin, mult = HL_REVERSE_MAPPINGS[raw_coin]
                else:
                    coin, mult = raw_coin, 1.0
                
                # side: 'B' vol dir taker compra (preu puja / verd), 'A' taker ven (preu baixa / vermell)
                side = OrderSide.BUY if item.get("side") == "B" else OrderSide.SELL
                trade = Trade(
                    coin=coin,
                    side=side,
                    price=float(item["px"]) / mult,
                    size=float(item["sz"]) * mult,
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
