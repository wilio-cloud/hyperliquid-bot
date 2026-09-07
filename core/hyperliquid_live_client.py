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
        clean_key = agent_private_key.strip()
        if not clean_key.startswith("0x"):
            clean_key = f"0x{clean_key}"

        if len(clean_key) != 66:
            if len(clean_key) == 42:
                raise ValueError(
                    f"HL_AGENT_PRIVATE_KEY és una adreça pública ({clean_key}), no una clau privada! "
                    f"A Hyperliquid, quan crees un API Agent, et mostra la 'Agent Private Key' "
                    f"(64 caràcters hexadecimals / 32 bytes). Si us plau, genera un nou agent si no la vas copiar."
                )
            raise ValueError(
                f"HL_AGENT_PRIVATE_KEY té una llargada invàlida ({len(clean_key)} caràcters). "
                f"Una clau privada d'Ethereum ha de tenir 64 caràcters hexadecimals (o 66 amb '0x')."
            )

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

    def round_price(self, coin: str, price: float) -> float:
        """
        Arrodoneix el preu segons les especificacions d'Hyperliquid:
        - Màxim 5 xifres significatives
        - Màxim 6 decimals
        """
        if price <= 0:
            return price
        import math
        digits = 5
        decimals = max(0, digits - int(math.floor(math.log10(abs(price)))) - 1)
        decimals = min(decimals, 6)
        return round(price, decimals)

    async def get_account_state(self) -> Dict[str, Any]:
        """Consulta l'estat complet del compte a Hyperliquid."""
        return await asyncio.to_thread(self.info.user_state, self.wallet_address)

    async def get_balance(self) -> float:
        """Retorna el saldo total disponible de marge en USD a Hyperliquid (Perps + Spot/Unified)."""
        try:
            state = await self.get_account_state()
            margin_summary = state.get("marginSummary", {})
            account_value = float(margin_summary.get("accountValue", 0.0))
            withdrawable = float(state.get("withdrawable", 0.0))

            # Consulta de saldo Spot (spotClearinghouseState) per a comptes Unified o USDC a Spot
            spot_usdc = 0.0
            try:
                spot_state = await asyncio.to_thread(self.info.spot_user_state, self.wallet_address)
                for b in spot_state.get("balances", []):
                    if b.get("coin") == "USDC":
                        spot_usdc = float(b.get("total", 0.0))
                        break
            except Exception as e:
                logger.debug(f"No s'ha pogut consultar spot state a Hyperliquid: {e}")

            # Saldo total disponible per operar
            total_bal = max(account_value + spot_usdc, withdrawable, account_value)
            logger.info(
                f"Balanç Hyperliquid obtingut: Perps={account_value:.2f}$, "
                f"SpotUSDC={spot_usdc:.2f}$, Withdrawable={withdrawable:.2f}$ -> Total={total_bal:.2f}$"
            )
            return total_bal
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

        # Collar de seguretat de 0.05% per a ordres IOC per assegurar fill immediat al millor preu
        if ioc:
            collar_px = price * (1.0005 if is_buy else 0.9995)
            rounded_px = self.round_price(coin, collar_px)
        else:
            rounded_px = self.round_price(coin, price)

        tif = "Alo" if post_only else ("Ioc" if ioc else "Gtc")
        order_type = {"limit": {"tif": tif}}

        def _execute():
            return self.exchange.order(
                name=coin.upper(),
                is_buy=is_buy,
                sz=rounded_sz,
                limit_px=rounded_px,
                order_type=order_type,
            )

        try:
            res = await asyncio.to_thread(_execute)
            logger.info(f"Ordre Hyperliquid enviada {coin} {'BUY' if is_buy else 'SELL'} {rounded_sz} @ {rounded_px}: {res}")
            
            # Verificació estricta de la resposta del motor d'Hyperliquid
            if isinstance(res, dict):
                if res.get("status") == "ok":
                    resp_data = res.get("response", {})
                    if isinstance(resp_data, dict) and resp_data.get("type") == "order":
                        statuses = resp_data.get("data", {}).get("statuses", [])
                        if statuses and isinstance(statuses[0], dict):
                            if "error" in statuses[0]:
                                err_msg = statuses[0]["error"]
                                logger.error(f"Hyperliquid ha rebutjat l'ordre per a {coin}: {err_msg}")
                                return {"status": "err", "error": err_msg, "raw": res}
                elif res.get("status") == "err":
                    logger.error(f"Hyperliquid ha retornat error: {res.get('response')}")
                    return res
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
        try:
            acc_state = await self.get_account_state()
            current_sz = 0.0
            szi = 0.0
            for p in acc_state.get("assetPositions", []):
                pos = p.get("position", {})
                if pos.get("coin") == coin.upper():
                    szi = float(pos.get("szi", 0.0))
                    current_sz = abs(szi)
                    break

            if current_sz <= 0.0:
                logger.info(f"Cap posició oberta per a {coin} a Hyperliquid per tancar.")
                return {"status": "ok", "message": f"Cap posició oberta per a {coin}"}

            close_sz = self.round_size(coin, size if size is not None else current_sz)
            is_buy = (szi < 0.0) # Si estem Short (szi < 0), comprem per tancar. Si Long, venem.

            all_mids = await asyncio.to_thread(self.info.all_mids)
            mid_px = float(all_mids.get(coin.upper(), 0.0))
            if mid_px <= 0.0:
                return {"status": "err", "error": f"No s'ha pogut obtenir preu de mercat per a {coin}"}

            agg_px = mid_px * (1.015 if is_buy else 0.985)
            logger.info(f"Executant market_close a Hyperliquid per a {coin}: {'BUY' if is_buy else 'SELL'} {close_sz} @ {agg_px:.6f}")
            return await self.place_order(
                coin=coin,
                is_buy=is_buy,
                size=close_sz,
                price=agg_px,
                ioc=True,
            )
        except Exception as e:
            logger.error(f"Error en market_close Hyperliquid per a {coin}: {e}")
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
