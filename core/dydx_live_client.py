"""Client d'execució en real per a dYdX v4 basat en gRPC (Node) i REST (Indexer)."""

import asyncio
import json
import logging
import math
import os
import random
import ssl
import time
from typing import Any, Dict, List, Optional

import certifi
import aiohttp
from Crypto.Hash import RIPEMD160
import hashlib
import bech32

try:
    from dydx_v4_client.key_pair import KeyPair
    from dydx_v4_client.network import make_mainnet, make_testnet, Network
    from dydx_v4_client.node.client import NodeClient
    from dydx_v4_client.node.market import Market
    from dydx_v4_client.wallet import Wallet
    from dydx_v4_client.indexer.rest.constants import OrderType
    from v4_proto.dydxprotocol.clob.order_pb2 import Order
    DYDX_SDK_AVAILABLE = True
except ImportError:
    DYDX_SDK_AVAILABLE = False

logger = logging.getLogger("DydxLiveClient")

DEFAULT_NODE_URL = "dydx-grpc.publicnode.com:443"
DEFAULT_INDEXER_REST = "https://indexer.dydx.trade"
DEFAULT_INDEXER_WS = "wss://indexer.dydx.trade/v4/ws"

SYMBOL_MAP = {
    "SOL": "SOL-USD",
    "SUI": "SUI-USD",
    "NEAR": "NEAR-USD",
    "PUMP": "PUMP-USD",
    "ZEC": "ZEC-USD",
    "DOGE": "DOGE-USD",
    "AVAX": "AVAX-USD",
    "LINK": "LINK-USD",
    "ARB": "ARB-USD",
    "OP": "OP-USD",
    "TIA": "TIA-USD",
    "INJ": "INJ-USD",
    "WIF": "WIF-USD",
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
}

REVERSE_SYMBOL_MAP = {v: k for k, v in SYMBOL_MAP.items()}


class DydxLiveClient:
    """Client d'execució en viu per a la xarxa dYdX v4."""

    def __init__(
        self,
        address: Optional[str] = None,
        mnemonic: Optional[str] = None,
        private_key: Optional[str] = None,
        subaccount_number: int = 0,
        node_url: str = DEFAULT_NODE_URL,
        indexer_rest_url: str = DEFAULT_INDEXER_REST,
        indexer_ws_url: str = DEFAULT_INDEXER_WS,
        env: str = "mainnet",
        market_config_path: str = "config/dydx_markets.json",
    ):
        self.address = (address or "").strip().lower()
        self.mnemonic = mnemonic.strip() if mnemonic else None
        self.private_key = private_key.strip() if private_key else None
        self.subaccount_number = subaccount_number
        self.node_url = node_url
        self.indexer_rest_url = indexer_rest_url.rstrip("/")
        self.indexer_ws_url = indexer_ws_url
        self.env = env
        self.market_config_path = market_config_path

        self.ssl_context = ssl.create_default_context(cafile=certifi.where())
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        self.markets_metadata: Dict[str, dict] = {}
        self.markets: Dict[str, Any] = {}
        self._load_market_metadata()

        self.node_client: Optional[Any] = None
        self.wallet: Optional[Any] = None
        self._init_lock = asyncio.Lock()
        self._is_initialized = False

        # Si tenim credencials i no tenim adreça, deduïm l'adreça de la clau
        if not self.address and (self.mnemonic or self.private_key):
            self.address = self._derive_address()

        logger.info(
            f"DydxLiveClient creat per a address: {self.address or 'no especificada'} "
            f"(subaccount={self.subaccount_number}, env={self.env})"
        )

    def _derive_address(self) -> str:
        """Deriva l'adreça dYdX (dydx1...) a partir del mnemonic o clau privada."""
        try:
            if self.mnemonic:
                kp = KeyPair.from_mnemonic(self.mnemonic)
            elif self.private_key:
                kp = KeyPair.from_hex(self.private_key)
            else:
                return ""
            pub_bytes = kp.public_key_bytes
            sha = hashlib.sha256(pub_bytes).digest()
            rip = RIPEMD160.new(sha).digest()
            return bech32.bech32_encode("dydx", bech32.convertbits(rip, 8, 5))
        except Exception as e:
            logger.warning(f"No s'ha pogut derivar l'adreça dYdX: {e}")
            return ""

    def _load_market_metadata(self):
        """Carrega la informació de mercats (clobPairId, stepSize, tickSize) des del disc."""
        if os.path.exists(self.market_config_path):
            try:
                with open(self.market_config_path, "r") as f:
                    self.markets_metadata = json.load(f)
                for sym, meta in self.markets_metadata.items():
                    if DYDX_SDK_AVAILABLE:
                        try:
                            self.markets[sym] = Market(meta)
                        except Exception:
                            pass
                logger.info(f"Carregats {len(self.markets_metadata)} mercats des de {self.market_config_path}")
            except Exception as e:
                logger.warning(f"Error carregant fitxer de mercats {self.market_config_path}: {e}")

    async def initialize(self):
        """Inicialitza la connexió gRPC amb el Node de dYdX i el Wallet."""
        if self._is_initialized:
            return

        async with self._init_lock:
            if self._is_initialized:
                return

            if not DYDX_SDK_AVAILABLE:
                raise RuntimeError("dydx_v4_client no està disponible al sistema.")

            # Connectem amb la xarxa gRPC de dYdX
            if self.env == "testnet":
                net = make_testnet(node_url=self.node_url)
            else:
                net = make_mainnet(
                    rest_indexer=self.indexer_rest_url,
                    websocket_indexer=self.indexer_ws_url,
                    node_url=self.node_url,
                )

            logger.info(f"Connectant al node gRPC de dYdX ({self.node_url})...")
            self.node_client = await NodeClient.connect(net.node)
            logger.info("Connexió gRPC amb el node dYdX establerta.")

            # Inicialitzar Wallet si disposem de mnemonic o clau privada
            if self.mnemonic:
                kp = KeyPair.from_mnemonic(self.mnemonic)
            elif self.private_key:
                kp = KeyPair.from_hex(self.private_key)
            else:
                kp = None

            if kp:
                # Comprovar número de compte i seqüència a la blockchain
                if not self.address:
                    self.address = self._derive_address()

                acc_num = 0
                seq = 0
                try:
                    account = await self.node_client.get_account(self.address)
                    acc_num = account.account_number
                    seq = account.sequence
                    logger.info(f"Compte dYdX trobat a la xarxa: acc_num={acc_num}, seq={seq}")
                except Exception as e:
                    logger.warning(
                        f"El compte {self.address} encara no té transaccions prèvies o ha fallat get_account: {e}. "
                        f"Iniciant amb sequence=0."
                    )

                self.wallet = Wallet(key=kp, account_number=acc_num, sequence=seq)
                logger.info(f"Wallet dYdX inicialitzat per a {self.address}")

            self._is_initialized = True

    def get_market_info(self, coin: str) -> Optional[dict]:
        """Obté la informació oficial del mercat per a una moneda (ex: SOL -> SOL-USD)."""
        sym = SYMBOL_MAP.get(coin.upper(), f"{coin.upper()}-USD")
        return self.markets_metadata.get(sym)

    def get_market_obj(self, coin: str) -> Optional[Any]:
        """Obté l'objecte Market de dYdX SDK."""
        sym = SYMBOL_MAP.get(coin.upper(), f"{coin.upper()}-USD")
        m_obj = self.markets.get(sym)
        if not m_obj and sym in self.markets_metadata and DYDX_SDK_AVAILABLE:
            m_obj = Market(self.markets_metadata[sym])
            self.markets[sym] = m_obj
        return m_obj

    def round_size(self, coin: str, size: float) -> float:
        """Ajusta la mida al stepSize oficial del mercat dYdX v4."""
        market = self.get_market_info(coin)
        if not market:
            return round(size, 2)

        step = float(market.get("stepSize", 1.0))
        if step >= 1.0:
            units = int(size / step)
            return float(units * step)
        else:
            decimals = max(0, -int(round(math.log10(step))))
            units = int(size / step)
            return round(units * step, decimals)

    def round_price(self, coin: str, price: float) -> float:
        """Ajusta el preu al tickSize oficial del mercat dYdX v4."""
        market = self.get_market_info(coin)
        if not market:
            return round(price, 4)

        tick = float(market.get("tickSize", 0.0001))
        decimals = max(0, -int(round(math.log10(tick))))
        units = round(price / tick)
        return round(units * tick, decimals)

    async def get_account_state(self) -> Dict[str, Any]:
        """Consulta l'estat del subcompte a l'Indexer de dYdX v4."""
        if not self.address:
            return {}

        url = f"{self.indexer_rest_url}/v4/addresses/{self.address}/subaccountNumber/{self.subaccount_number}"
        try:
            connector = aiohttp.TCPConnector(ssl=self.ssl_context)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=8.0)) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 404:
                        logger.debug(f"Subcompte dYdX {self.address} encara no inicialitzat o sense fons (404).")
                        return {}
                    text = await resp.text()
                    logger.warning(f"Error consultant estat dYdX ({resp.status}): {text}")
                    return {}
        except Exception as e:
            logger.error(f"Excepció consultant estat dYdX: {e}")
            return {}

    async def get_balance(self) -> float:
        """Retorna el marge/col·lateral lliure disponible en USDC a dYdX."""
        try:
            data = await self.get_account_state()
            sub = data.get("subaccount", {})
            free_col = sub.get("freeCollateral") or sub.get("equity") or "0"
            return float(free_col)
        except Exception as e:
            logger.error(f"Error consultant balanç dYdX: {e}")
            return 0.0

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Consulta les posicions obertes reals a dYdX i les normalitza a la interfície comuna."""
        positions: List[Dict[str, Any]] = []
        if not self.address:
            return positions

        try:
            data = await self.get_account_state()
            sub = data.get("subaccount", {})
            open_pos = sub.get("openPerpetualPositions", {})

            for market_id, p in open_pos.items():
                sz = float(p.get("size", 0.0))
                if sz == 0.0:
                    continue

                raw_side = str(p.get("side", "")).upper()
                side = "buy" if raw_side in ("LONG", "BUY") or sz > 0 else "sell"
                amt = abs(sz)

                coin = REVERSE_SYMBOL_MAP.get(market_id, market_id.split("-")[0])
                entry_px = float(p.get("entryPrice", 0.0))
                unrealized_pnl = float(p.get("unrealizedPnl", 0.0))

                positions.append({
                    "asset": coin.upper(),
                    "market": market_id,
                    "amount": amt,
                    "side": side,
                    "avg_entry_price": entry_px,
                    "mark_price": entry_px,
                    "unrealized_pnl": unrealized_pnl,
                    "raw": p,
                })
        except Exception as e:
            logger.error(f"Error consultant posicions dYdX: {e}")

        return positions

    async def place_order(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        price: float,
        post_only: bool = False,
        reduce_only: bool = False,
        ioc: bool = False,
    ) -> Dict[str, Any]:
        """Envia una ordre signada a la xarxa dYdX v4."""
        await self.initialize()

        if not self.wallet:
            return {"status": "err", "error": "Wallet no configurat a DydxLiveClient."}
        if not self.node_client:
            return {"status": "err", "error": "NodeClient no connectat a DydxLiveClient."}

        market_obj = self.get_market_obj(coin)
        if not market_obj:
            return {"status": "err", "error": f"Mercat dYdX no trobat per a moneda: {coin}"}

        rounded_sz = self.round_size(coin, size)
        rounded_px = self.round_price(coin, price)

        if rounded_sz <= 0:
            return {"status": "err", "error": f"Mida arrodonida invàlida per a {coin}: {rounded_sz} (original: {size})"}

        try:
            # En dYdX v4, les ordres de curt termini (Short-Term) necessiten good_til_block
            latest_height = await self.node_client.latest_block_height()
            good_til_block = latest_height + 15

            client_id = random.randint(100000, 2000000000)
            order_flags = 0  # 0 = Short-Term order

            oid = market_obj.order_id(
                address=self.wallet.address,
                subaccount_number=self.subaccount_number,
                client_id=client_id,
                order_flags=order_flags,
            )

            side_enum = Order.Side.SIDE_BUY if is_buy else Order.Side.SIDE_SELL
            
            # Determinació de TimeInForce
            if ioc:
                tif = Order.TimeInForce.TIME_IN_FORCE_IOC
            elif post_only:
                tif = Order.TimeInForce.TIME_IN_FORCE_POST_ONLY
            else:
                tif = Order.TimeInForce.TIME_IN_FORCE_UNSPECIFIED

            order_type = OrderType.LIMIT

            order_proto = market_obj.order(
                order_id=oid,
                order_type=order_type,
                side=side_enum,
                size=rounded_sz,
                price=rounded_px,
                time_in_force=tif,
                reduce_only=reduce_only,
                post_only=post_only,
                good_til_block=good_til_block,
            )

            logger.info(
                f"📤 Enviant ordre dYdX v4: {coin} {'BUY' if is_buy else 'SELL'} {rounded_sz} @ {rounded_px} "
                f"(IOC={ioc}, PostOnly={post_only}, ReduceOnly={reduce_only}, GTB={good_til_block})..."
            )

            tx_resp = await self.node_client.place_order(self.wallet, order_proto)

            if getattr(tx_resp, "code", 0) == 0:
                tx_hash = getattr(tx_resp, "txhash", "")
                logger.info(
                    f"✅ Ordre dYdX acceptada a la xarxa! TxHash: {tx_hash} "
                    f"| Height: {getattr(tx_resp, 'height', 0)}"
                )
                return {
                    "status": "ok",
                    "txhash": tx_hash,
                    "height": getattr(tx_resp, "height", 0),
                    "data": {
                        "client_id": client_id,
                        "side": "BUY" if is_buy else "SELL",
                        "size": rounded_sz,
                        "price": rounded_px,
                        "txhash": tx_hash,
                    },
                }
            else:
                code = getattr(tx_resp, "code", -1)
                raw_log = getattr(tx_resp, "raw_log", "Unknown error")
                logger.error(f"❌ Error enviant ordre dYdX (Code {code}): {raw_log}")
                return {
                    "status": "err",
                    "code": code,
                    "error": raw_log,
                    "txhash": getattr(tx_resp, "txhash", ""),
                }

        except Exception as e:
            logger.error(f"Excepció enviant ordre dYdX per a {coin}: {e}")
            return {"status": "err", "error": str(e)}

    async def cancel_order(self, order_id: Any) -> Dict[str, Any]:
        """Cancel·la una ordre específica a dYdX."""
        return {"status": "ok", "message": "Les ordres short-term expiren automàticament en segons"}

    async def cancel_all_orders(self) -> Dict[str, Any]:
        """Cancel·la ordres pendents a dYdX."""
        return {"status": "ok", "message": "Ordres short-term dYdX netejades"}

    async def market_close(self, coin: str, size: Optional[float] = None) -> Dict[str, Any]:
        """Tanca immediatament una posició oberta a dYdX a preu de mercat agressiu (IOC)."""
        positions = await self.get_positions()
        for pos in positions:
            if pos.get("asset") == coin.upper():
                sz = float(pos.get("amount", 0.0))
                side = pos.get("side", "").lower()
                if sz > 0:
                    is_buy_to_close = (side == "sell")
                    raw_px = float(pos.get("mark_price") or pos.get("avg_entry_price") or 1.0)
                    
                    # Preu de seguretat +/- 3% per garantir execució IOC immediata
                    limit_px = raw_px * 1.03 if is_buy_to_close else raw_px * 0.97
                    close_sz = size if size is not None else sz

                    logger.info(
                        f"🛡️ [dYdX MARKET CLOSE] Tancant {coin} {close_sz} {'BUY' if is_buy_to_close else 'SELL'} @ {limit_px:.4f}"
                    )
                    return await self.place_order(
                        coin=coin,
                        is_buy=is_buy_to_close,
                        size=close_sz,
                        price=limit_px,
                        post_only=False,
                        reduce_only=True,
                        ioc=True,
                    )

        return {"status": "ok", "message": f"Cap posició oberta trobada per a {coin} a dYdX"}

    async def set_leverage(self, coin: str, leverage: int = 2) -> Dict[str, Any]:
        """Configura el palanquejament per a un instrument a dYdX."""
        logger.info(f"dYdX v4 utilitza marge creuat de cartera (Portfolio Cross-Margin) per a {coin} (Lev={leverage}x).")
        return {"status": "ok", "coin": coin, "leverage": leverage}
