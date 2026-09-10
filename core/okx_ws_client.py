"""Client WebSocket asíncron per a OKX V5 (CEX global de derivats perpètus)."""

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

logger = logging.getLogger("OkxWS")

def get_ssl_context() -> ssl.SSLContext:
    """Retorna un context SSL compatible amb entorns locals, Docker i Railway."""
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

class OkxWSClient:
    """
    Client WebSocket en temps real per al mercat de perpètus USDT d'OKX (V5).
    Subscriu als canals 'books5' (llibre d'ordres top 5 bid/ask) i 'funding-rate'.
    """

    DEFAULT_CT_VAL: Dict[str, float] = {
        "BTC": 0.01,
        "ETH": 0.1,
        "SOL": 1.0,
        "AVAX": 1.0,
        "LINK": 1.0,
        "NEAR": 10.0,
        "SUI": 10.0,
        "DOGE": 100.0,
        "PEPE": 10000000.0,
        "ARB": 10.0,
        "OP": 10.0,
        "TIA": 1.0,
        "SEI": 10.0,
        "APT": 1.0,
        "UNI": 1.0,
        "WIF": 1.0,
        "RENDER": 1.0,
        "INJ": 0.1,
        "ENA": 10.0,
    }

    DEFAULT_XPERP_CT_VAL: Dict[str, float] = {
        "BTC": 0.0001,
        "ETH": 0.001,
        "SOL": 0.01,
        "AVAX": 10.0,
        "LINK": 1.0,
        "NEAR": 1.0,
        "SUI": 1.0,
        "DOGE": 10.0,
        "ARB": 10.0,
        "OP": 1.0,
        "APT": 1.0,
        "SEI": 10.0,
        "INJ": 0.1,
        "UNI": 1.0,
        "HYPE": 0.1,
        "PEPE": 1000000.0,
        "PUMP": 1000.0,
        "WIF": 1.0,
        "RENDER": 1.0,
    }

    def __init__(
        self,
        coins: Optional[List[str]] = None,
        on_book_update: Optional[Callable[[OrderBookL2], None]] = None,
        on_funding_update: Optional[Callable[[Dict[str, float]], None]] = None,
        is_demo: bool = False,
    ):
        self.coins = coins or ["BTC", "ETH", "SOL", "AVAX", "LINK", "NEAR", "SUI", "DOGE"]
        self.on_book_update = on_book_update
        self.on_funding_update = on_funding_update
        self.is_demo = is_demo or (os.environ.get("OKX_IS_DEMO", "false").lower() in ("1", "true", "yes"))

        self.symbol_map: Dict[str, str] = {c.upper(): f"{c.upper()}-USDT-SWAP" for c in self.coins}
        self.reverse_map: Dict[str, str] = {f"{c.upper()}-USDT-SWAP": c.upper() for c in self.coins}

        self.order_books: Dict[str, OrderBookL2] = {}
        self.funding_rates_8h: Dict[str, float] = {}
        self.contract_specs: Dict[str, dict] = {}

        self.is_running = False
        self._ws_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._funding_poll_task: Optional[asyncio.Task] = None
        self._ssl_context = get_ssl_context()

        default_rest_url = "https://eea.okx.com" if os.environ.get("OKX_REGION", "eea").lower() == "eea" else "https://www.okx.com"
        self.rest_url = os.environ.get("OKX_REST_URL", default_rest_url).rstrip("/")

        if self.is_demo:
            self.ws_url = "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"
        else:
            self.ws_url = "wss://ws.okx.com:8443/ws/v5/public"

    async def start(self):
        """Inicia la connexió WebSocket i tasques de fons."""
        self.is_running = True
        await self._fetch_instrument_specs()
        self._ws_task = asyncio.create_task(self._run_ws_loop())
        self._funding_poll_task = asyncio.create_task(self._run_funding_poll_loop())

    async def stop(self):
        """Atura el client WebSocket."""
        self.is_running = False
        for task in (self._ws_task, self._ping_task, self._funding_poll_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _fetch_instrument_specs(self):
        """Descarrega les especificacions dels instruments (X-Perp a EEA o SWAP a Global) des d'OKX."""
        is_eea = ("eea.okx.com" in self.rest_url) or (os.environ.get("OKX_REGION", "eea").lower() == "eea")

        # 1. Si som a EEA, consultar primer instruments FUTURES per trobar els contractes X-Perps oficials
        if is_eea:
            url_fut = f"{self.rest_url}/api/v5/public/instruments?instType=FUTURES"
            try:
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self._ssl_context)) as session:
                    async with session.get(url_fut, timeout=aiohttp.ClientTimeout(total=6.0)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for item in data.get("data", []):
                                inst_id = item.get("instId", "")
                                if "XPERP" in inst_id and item.get("state") != "suspend":
                                    parts = inst_id.split("-")
                                    coin = parts[0].upper()
                                    if coin in self.coins:
                                        self.symbol_map[coin] = inst_id
                                        self.reverse_map[inst_id] = coin
                                        self.contract_specs[coin] = {
                                            "instId": inst_id,
                                            "ctVal": float(item.get("ctVal", self.DEFAULT_XPERP_CT_VAL.get(coin, 1.0))),
                                            "minSz": float(item.get("minSz", 1.0)),
                                            "lotSz": float(item.get("lotSz", 1.0)),
                                            "tickSz": float(item.get("tickSz", 0.01)),
                                        }
                            logger.info(f"Metadades X-Perp OKX WebSocket carregades per a {len(self.contract_specs)} monedes.")
            except Exception as e:
                logger.debug(f"Error consultant instruments FUTURES OKX WS: {e}")

        # 2. Descarregar instruments SWAP per a les monedes globals o restants
        url_swap = f"{self.rest_url}/api/v5/public/instruments?instType=SWAP"
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self._ssl_context)) as session:
                async with session.get(url_swap, timeout=aiohttp.ClientTimeout(total=6.0)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for item in data.get("data", []):
                            inst_id = item.get("instId", "")
                            parts = inst_id.split("-")
                            if len(parts) >= 3 and parts[1] == "USDT":
                                coin = parts[0].upper()
                                if coin in self.coins and coin not in self.contract_specs:
                                    self.symbol_map[coin] = inst_id
                                    self.reverse_map[inst_id] = coin
                                    self.contract_specs[coin] = {
                                        "instId": inst_id,
                                        "ctVal": float(item.get("ctVal", self.DEFAULT_CT_VAL.get(coin, 1.0))),
                                        "minSz": float(item.get("minSz", 1.0)),
                                        "lotSz": float(item.get("lotSz", 1.0)),
                                        "tickSz": float(item.get("tickSz", 0.01)),
                                    }
                        logger.info(f"Metadades de contractes OKX WebSocket carregades per a {len(self.contract_specs)} monedes.")
        except Exception as e:
            logger.debug(f"Error descarregant metadades de contractes OKX: {e}")

    def get_contract_val(self, coin: str) -> float:
        """Retorna el valor de 1 contracte en unitats de moneda base."""
        c = coin.upper()
        if c in self.contract_specs:
            return self.contract_specs[c]["ctVal"]
        is_eea = ("eea.okx.com" in self.rest_url) or (os.environ.get("OKX_REGION", "eea").lower() == "eea")
        if is_eea and c in self.DEFAULT_XPERP_CT_VAL:
            return self.DEFAULT_XPERP_CT_VAL[c]
        return self.DEFAULT_CT_VAL.get(c, 1.0)

    async def _run_ws_loop(self):
        retry_delay = 2.0
        while self.is_running:
            try:
                logger.info(f"Connectant a OKX V5 WebSocket: {self.ws_url}...")
                async with websockets.connect(
                    self.ws_url,
                    ssl=self._ssl_context,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    logger.info("Connexió WebSocket amb OKX V5 establerta amb èxit.")
                    retry_delay = 2.0

                    sub_args = []
                    for coin in self.coins:
                        inst_id = self.symbol_map.get(coin)
                        if inst_id:
                            sub_args.append({"channel": "books5", "instId": inst_id})
                            if "-SWAP" in inst_id:
                                sub_args.append({"channel": "funding-rate", "instId": inst_id})

                    if sub_args:
                        sub_msg = {"op": "subscribe", "args": sub_args}
                        await ws.send(json.dumps(sub_msg))

                    if self._ping_task:
                        self._ping_task.cancel()
                    self._ping_task = asyncio.create_task(self._send_pings(ws))

                    async for raw_msg in ws:
                        if not self.is_running:
                            break
                        if raw_msg == "pong":
                            continue
                        try:
                            msg = json.loads(raw_msg)
                            self._handle_message(msg)
                        except Exception as pe:
                            logger.debug(f"Error processant missatge OKX WS: {pe}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Desconnexió OKX WebSocket ({e}). Reintentant en {retry_delay:.1f}s...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, 30.0)

    async def _send_pings(self, ws):
        """Envia string 'ping' requerit per OKX cada 25 segons."""
        try:
            while self.is_running:
                await asyncio.sleep(25)
                await ws.send("ping")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def _handle_message(self, msg: dict):
        """Processa els missatges rebuts d'OKX WebSocket."""
        arg = msg.get("arg", {})
        channel = arg.get("channel")
        inst_id = arg.get("instId")
        if not channel or not inst_id:
            return

        coin = self.reverse_map.get(inst_id)
        if not coin:
            return

        data_list = msg.get("data", [])
        if not data_list:
            return

        data = data_list[0]

        if channel == "books5":
            raw_bids = data.get("bids", [])
            raw_asks = data.get("asks", [])
            ts = float(data.get("ts", time.time() * 1000)) / 1000.0

            ct_val = self.get_contract_val(coin)

            bids = [
                BookLevel(price=float(b[0]), size=float(b[1]) * ct_val)
                for b in raw_bids if len(b) >= 2 and float(b[1]) > 0
            ]
            asks = [
                BookLevel(price=float(a[0]), size=float(a[1]) * ct_val)
                for a in raw_asks if len(a) >= 2 and float(a[1]) > 0
            ]

            if bids and asks:
                book = OrderBookL2(
                    coin=coin,
                    timestamp=ts,
                    bids=bids,
                    asks=asks,
                )
                self.order_books[coin] = book
                if self.on_book_update:
                    self.on_book_update(book)

        elif channel == "funding-rate":
            fr_str = data.get("fundingRate")
            if fr_str is not None:
                rate_8h_pct = float(fr_str) * 100.0
                self.funding_rates_8h[coin] = rate_8h_pct
                if self.on_funding_update:
                    self.on_funding_update(self.funding_rates_8h)

    async def _run_funding_poll_loop(self):
        """Polling periòdic de fallback per a les taxes de finançament d'OKX."""
        url = f"{self.rest_url}/api/v5/public/funding-rate-current"
        while self.is_running:
            try:
                for coin in self.coins:
                    inst_id = self.symbol_map.get(coin)
                    if not inst_id:
                        continue
                    # Per a XPERP (EEA), l'endpoint funding-rate-current no existeix.
                    # Usem l'instrument SWAP equivalent per obtenir la taxa de funding.
                    if "-SWAP" in inst_id:
                        query_inst_id = inst_id
                    else:
                        query_inst_id = f"{coin.upper()}-USDT-SWAP"
                    full_url = f"{url}?instId={query_inst_id}"
                    try:
                        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self._ssl_context)) as session:
                            async with session.get(full_url, timeout=aiohttp.ClientTimeout(total=4.0)) as resp:
                                if resp.status == 200:
                                    res_json = await resp.json()
                                    d = res_json.get("data", [])
                                    if d:
                                        fr_str = d[0].get("fundingRate")
                                        if fr_str:
                                            self.funding_rates_8h[coin] = float(fr_str) * 100.0
                    except Exception as e:
                        logger.debug(f"Error polling funding rate per {coin} ({query_inst_id}): {e}")

                if self.funding_rates_8h and self.on_funding_update:
                    self.on_funding_update(self.funding_rates_8h)
            except Exception as e:
                logger.debug(f"Error en funding poll OKX: {e}")

            await asyncio.sleep(60.0)
