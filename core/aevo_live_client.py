"""Client d'execució en real per a Aevo basat en REST API i signatura EIP-712."""

import asyncio
import logging
import random
import time
from typing import Any, Dict, List, Optional
import aiohttp
from eth_account import Account
from eth_account.messages import encode_typed_data
import certifi
import ssl

logger = logging.getLogger("AevoLiveClient")

# Mapatge oficial d'instrument_id per a cada moneda perpètua a Aevo Mainnet
AEVO_INSTRUMENTS = {
    "SOL": {"id": 5197, "amount_decimals": 1, "price_decimals": 3},
    "HYPE": {"id": 49760, "amount_decimals": 1, "price_decimals": 4},
    "NEAR": {"id": 22661, "amount_decimals": 0, "price_decimals": 3},
    "PUMP": {"id": 82182, "amount_decimals": 0, "price_decimals": 6},
    "SUI": {"id": 17791, "amount_decimals": 1, "price_decimals": 4},
    "DOGE": {"id": 11969, "amount_decimals": 0, "price_decimals": 5},
}

AEVO_DOMAINS = {
    "mainnet": {
        "rest_url": "https://api.aevo.xyz",
        "domain": {
            "name": "Aevo Mainnet",
            "version": "1",
            "chainId": 1,
        },
    },
    "testnet": {
        "rest_url": "https://api-testnet.aevo.xyz",
        "domain": {
            "name": "Aevo Testnet",
            "version": "1",
            "chainId": 11155111,
        },
    },
}

EIP712_ORDER_TYPES = {
    "Order": [
        {"name": "maker", "type": "address"},
        {"name": "isBuy", "type": "bool"},
        {"name": "limitPrice", "type": "uint256"},
        {"name": "amount", "type": "uint256"},
        {"name": "salt", "type": "uint256"},
        {"name": "instrument", "type": "uint256"},
        {"name": "timestamp", "type": "uint256"},
    ]
}

class AevoLiveClient:
    def __init__(
        self,
        wallet_address: str,
        api_key: str,
        api_secret: str,
        signing_key: str,
        env: str = "mainnet",
    ):
        self.wallet_address = wallet_address.lower()
        self.api_key = api_key
        self.api_secret = api_secret
        clean_key = signing_key if signing_key.startswith("0x") else f"0x{signing_key}"
        self.signing_key = clean_key
        self.env = env
        
        cfg = AEVO_DOMAINS.get(env, AEVO_DOMAINS["mainnet"])
        self.rest_url = cfg["rest_url"]
        self.signing_domain = cfg["domain"]
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

        # Headers d'autenticació estàndard d'Aevo
        self.headers = {
            "AEVO-KEY": self.api_key,
            "AEVO-SECRET": self.api_secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "HyperliquidArbitrageBot/2.0",
        }
        logger.info(
            f"AevoLiveClient inicialitzat per a wallet {self.wallet_address[:8]}... "
            f"amb API Key {self.api_key[:8]}... (Env={self.env})"
        )

    def round_size(self, coin: str, size: float) -> float:
        """Ajusta la mida al nombre de decimals admès per Aevo."""
        meta = AEVO_INSTRUMENTS.get(coin.upper(), {})
        decimals = meta.get("amount_decimals", 1)
        if decimals == 0:
            return float(int(size))
        return round(size, decimals)

    def round_price(self, coin: str, price: float) -> float:
        meta = AEVO_INSTRUMENTS.get(coin.upper(), {})
        decimals = meta.get("price_decimals", 4)
        return round(price, decimals)

    def get_instrument_id(self, coin: str) -> Optional[int]:
        meta = AEVO_INSTRUMENTS.get(coin.upper())
        return meta["id"] if meta else None

    async def get_account_state(self) -> Dict[str, Any]:
        """Consulta l'estat del compte i portfolio a Aevo."""
        url = f"{self.rest_url}/portfolio"
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self.ssl_context)) as session:
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=8.0)) as resp:
                if resp.status == 200:
                    return await resp.json()
                text = await resp.text()
                logger.error(f"Error consultant portfolio Aevo ({resp.status}): {text}")
                return {}

    async def get_balance(self) -> float:
        """Retorna el marge/balanç disponible en USD a Aevo."""
        try:
            state = await self.get_account_state()
            # Aevo portfolio retorna 'balance', 'collateral', 'user_margin'
            balance_str = state.get("collateral") or state.get("balance") or "0"
            return float(balance_str)
        except Exception as e:
            logger.error(f"Error consultant balanç Aevo: {e}")
            return 0.0

    def sign_order_payload(
        self,
        instrument_id: int,
        is_buy: bool,
        limit_price: float,
        quantity: float,
        post_only: bool = False,
        reduce_only: bool = False,
        close_position: bool = False,
    ) -> Dict[str, Any]:
        """Genera i signa criptogràficament el payload EIP-712 d'Aevo."""
        salt = random.randint(100000, 10**10)
        timestamp = int(time.time())

        # En el protocol d'Aevo, preu i quantitat es multipliquen per 10^6 en el struct signat
        price_int = int(round(limit_price * 10**6))
        amount_int = int(round(quantity * 10**6))

        order_struct = {
            "maker": self.wallet_address,
            "isBuy": is_buy,
            "limitPrice": price_int,
            "amount": amount_int,
            "salt": salt,
            "instrument": instrument_id,
            "timestamp": timestamp,
        }

        signable_msg = encode_typed_data(
            domain_data=self.signing_domain,
            message_types=EIP712_ORDER_TYPES,
            message_data=order_struct,
        )
        signed = Account.sign_message(signable_msg, private_key=self.signing_key)
        signature = signed.signature.hex()

        payload = {
            "maker": self.wallet_address,
            "is_buy": is_buy,
            "instrument": instrument_id,
            "limit_price": str(price_int),
            "amount": str(amount_int),
            "salt": str(salt),
            "signature": signature if signature.startswith("0x") else f"0x{signature}",
            "post_only": post_only,
            "reduce_only": reduce_only,
            "close_position": close_position,
            "timestamp": timestamp,
        }
        return payload

    async def place_order(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        price: float,
        post_only: bool = False,
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        """Envia una ordre signada a Aevo."""
        inst_id = self.get_instrument_id(coin)
        if not inst_id:
            return {"status": "err", "error": f"Moneda no suportada a Aevo: {coin}"}

        rounded_sz = self.round_size(coin, size)
        rounded_px = self.round_price(coin, price)
        if rounded_sz <= 0:
            return {"status": "err", "error": f"Mida invàlida per a {coin}: {size}"}

        payload = self.sign_order_payload(
            instrument_id=inst_id,
            is_buy=is_buy,
            limit_price=rounded_px,
            quantity=rounded_sz,
            post_only=post_only,
            reduce_only=reduce_only,
        )

        url = f"{self.rest_url}/orders"
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self.ssl_context)) as session:
                async with session.post(url, json=payload, headers=self.headers, timeout=aiohttp.ClientTimeout(total=8.0)) as resp:
                    data = await resp.json()
                    if resp.status in (200, 201):
                        logger.info(f"Ordre Aevo enviada amb èxit {coin} {'BUY' if is_buy else 'SELL'} {rounded_sz} @ {rounded_px}: {data}")
                        return {"status": "ok", "data": data}
                    logger.error(f"Error enviant ordre Aevo ({resp.status}): {data}")
                    return {"status": "err", "error": data, "http_status": resp.status}
        except Exception as e:
            logger.error(f"Excepció enviant ordre Aevo: {e}")
            return {"status": "err", "error": str(e)}

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel·la una ordre existent a Aevo."""
        url = f"{self.rest_url}/orders/{order_id}"
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self.ssl_context)) as session:
                async with session.delete(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=6.0)) as resp:
                    return await resp.json()
        except Exception as e:
            logger.error(f"Error cancel·lant ordre Aevo {order_id}: {e}")
            return {"status": "err", "error": str(e)}

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Consulta les posicions obertes reals a Aevo."""
        url = f"{self.rest_url}/positions"
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self.ssl_context)) as session:
                async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=6.0)) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return []
        except Exception as e:
            logger.error(f"Error obtenint posicions Aevo: {e}")
            return []

    async def market_close(self, coin: str, size: Optional[float] = None) -> Dict[str, Any]:
        """Tanca a mercat qualsevol posició oberta a Aevo per a aquesta moneda (Rollback de seguretat)."""
        inst_id = self.get_instrument_id(coin)
        if not inst_id:
            return {"status": "err", "error": f"Instrument desconegut: {coin}"}

        positions = await self.get_positions()
        for pos in positions:
            if pos.get("instrument_id") == inst_id or pos.get("asset") == coin.upper():
                sz = float(pos.get("amount", 0))
                side = pos.get("side", "")
                if sz > 0:
                    # Si estàvem BUY (Long), venem per tancar. Si estàvem SELL (Short), comprem per tancar.
                    is_buy_to_close = side.lower() == "sell" or side.lower() == "short"
                    # Preu extrem per omplir a mercat immediat
                    limit_px = 9999999.0 if is_buy_to_close else 0.000001
                    close_sz = size if size is not None else sz
                    return await self.place_order(
                        coin=coin,
                        is_buy=is_buy_to_close,
                        size=close_sz,
                        price=limit_px,
                        post_only=False,
                        reduce_only=True,
                    )
        return {"status": "ok", "message": f"Cap posició oberta trobada per a {coin} a Aevo"}
