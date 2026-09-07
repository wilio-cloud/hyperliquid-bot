"""Client d'execució en real per a Hyperliquid basat en API Agents."""

import asyncio
import logging
from typing import Any, Dict, Optional
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

logger = logging.getLogger("HyperliquidLiveClient")

# Precisió de decimals per moneda a Hyperliquid
COIN_SZ_DECIMALS = {
    "SOL": 2,
    "HYPE": 2,
    "NEAR": 1,
    "SUI": 1,
    "DOGE": 0,
    "PUMP": 0,
}

class HyperliquidLiveClient:
    def __init__(
        self,
        wallet_address: str,
        agent_private_key: str,
        testnet: bool = False,
    ):
        self.wallet_address = wallet_address.lower()
        self.agent_private_key = agent_private_key
        self.testnet = testnet
        self.base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL

        # Creació del compte de signatura de l'Agent
        clean_key = agent_private_key if agent_private_key.startswith("0x") else f"0x{agent_private_key}"
        self.agent_account = Account.from_key(clean_key)
        
        # Inicialització del client d'Exchange i Info
        self.exchange = Exchange(
            self.agent_account,
            self.base_url,
            account_address=self.wallet_address,
        )
        self.info = Info(self.base_url, skip_ws=True)
        logger.info(
            f"HyperliquidLiveClient inicialitzat per a wallet {self.wallet_address[:8]}... "
            f"mitjançant Agent {self.agent_account.address[:8]}... (Testnet={self.testnet})"
        )

    def round_size(self, coin: str, size: float) -> float:
        """Ajusta la mida al nombre màxim de decimals admès per Hyperliquid."""
        decimals = COIN_SZ_DECIMALS.get(coin.upper(), 2)
        if decimals == 0:
            return float(int(size))
        return round(size, decimals)

    async def get_account_state(self) -> Dict[str, Any]:
        """Consulta l'estat complet del compte a Hyperliquid."""
        return await asyncio.to_thread(self.info.user_state, self.wallet_address)

    async def get_balance(self) -> float:
        """Retorna el saldo disponible de marge en USD (accountValue)."""
        try:
            state = await self.get_account_state()
            margin_summary = state.get("marginSummary", {})
            account_value = float(margin_summary.get("accountValue", 0.0))
            return account_value
        except Exception as e:
            logger.error(f"Error consultant balanç a Hyperliquid: {e}")
            return 0.0

    async def place_order(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        price: float,
        post_only: bool = False,
        ioc: bool = False,
    ) -> Dict[str, Any]:
        """
        Envia una ordre a Hyperliquid.
        - post_only: TIF = 'Alo' (Add Liquidity Only / Maker)
        - ioc: TIF = 'Ioc' (Immediate Or Cancel / Taker immediat)
        - per defecte: TIF = 'Gtc'
        """
        rounded_sz = self.round_size(coin, size)
        if rounded_sz <= 0:
            return {"status": "err", "error": f"Mida invàlida per a {coin}: {size}"}

        tif = "Alo" if post_only else ("Ioc" if ioc else "Gtc")
        order_type = {"limit": {"tif": tif}}

        def _execute():
            return self.exchange.order(
                name=coin.upper(),
                is_buy=is_buy,
                sz=rounded_sz,
                limit_px=price,
                order_type=order_type,
            )

        try:
            res = await asyncio.to_thread(_execute)
            logger.info(f"Ordre Hyperliquid enviada {coin} {'BUY' if is_buy else 'SELL'} {rounded_sz} @ {price}: {res}")
            return res
        except Exception as e:
            logger.error(f"Error executant ordre Hyperliquid: {e}")
            return {"status": "err", "error": str(e)}

    async def market_open(self, coin: str, is_buy: bool, size: float, slippage: float = 0.01) -> Dict[str, Any]:
        """Obre posició a mercat amb un slippage màxim (per defecte 1.0%)."""
        rounded_sz = self.round_size(coin, size)
        def _execute():
            return self.exchange.market_open(
                name=coin.upper(),
                is_buy=is_buy,
                sz=rounded_sz,
                slippage=slippage,
            )
        try:
            return await asyncio.to_thread(_execute)
        except Exception as e:
            logger.error(f"Error market_open Hyperliquid: {e}")
            return {"status": "err", "error": str(e)}

    async def market_close(self, coin: str, size: Optional[float] = None, slippage: float = 0.02) -> Dict[str, Any]:
        """Tanca immediatament qualsevol posició oberta a mercat (Rollback de seguretat)."""
        rounded_sz = self.round_size(coin, size) if size is not None else None
        def _execute():
            return self.exchange.market_close(
                coin=coin.upper(),
                sz=rounded_sz,
                slippage=slippage,
            )
        try:
            return await asyncio.to_thread(_execute)
        except Exception as e:
            logger.error(f"Error market_close Hyperliquid: {e}")
            return {"status": "err", "error": str(e)}

    async def cancel_order(self, coin: str, oid: int) -> Dict[str, Any]:
        """Cancel·la una ordre pendent."""
        def _execute():
            return self.exchange.cancel(coin.upper(), oid)
        try:
            return await asyncio.to_thread(_execute)
        except Exception as e:
            logger.error(f"Error cancel_order Hyperliquid: {e}")
            return {"status": "err", "error": str(e)}
