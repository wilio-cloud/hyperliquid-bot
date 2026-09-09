"""Client d'execució en real per a Hyperliquid basat en API Agents."""

import asyncio
import logging
from typing import Any, Dict, List, Optional
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

logger = logging.getLogger("HyperliquidLiveClient")

# Precisió de decimals per moneda a Hyperliquid
COIN_SZ_DECIMALS = {
    "BTC": 5,
    "ETH": 4,
    "SOL": 2,
    "HYPE": 2,
    "NEAR": 1,
    "SUI": 1,
    "DOGE": 0,
    "PUMP": 0,
    "AVAX": 2,
    "LINK": 1,
    "ARB": 1,
    "OP": 1,
    "TIA": 1,
    "INJ": 1,
    "APT": 2,
    "SEI": 0,
    "RENDER": 1,
    "ENA": 0,
    "WIF": 0,
    "AAVE": 2,
    "UNI": 1,
    "ZEC": 2,
    "kPEPE": 0,
    "PEPE": 0,
}

class HyperliquidLiveClient:
    def __init__(
        self,
        wallet_address: str,
        agent_private_key: str,
        testnet: bool = False,
        referral_code: Optional[str] = None,
    ):
        self.wallet_address = wallet_address.lower()
        self.agent_private_key = agent_private_key
        self.testnet = testnet
        self.referral_code = referral_code
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
        self.coin_sz_decimals: Dict[str, int] = dict(COIN_SZ_DECIMALS)
        self._load_meta_sz_decimals()
        logger.info(
            f"HyperliquidLiveClient inicialitzat per a wallet {self.wallet_address[:8]}... "
            f"mitjançant Agent {self.agent_account.address[:8]}... (Testnet={self.testnet})"
        )

    def _load_meta_sz_decimals(self):
        """Carrega dinàmicament els decimals de mida de cada actiu des de la metadata d'Hyperliquid."""
        try:
            meta = self.info.meta()
            for item in meta.get("universe", []):
                name = item.get("name")
                sz_dec = item.get("szDecimals")
                if name and sz_dec is not None:
                    self.coin_sz_decimals[name.upper()] = int(sz_dec)
            logger.info(f"Carregats dinàmicament szDecimals per a {len(self.coin_sz_decimals)} actius d'Hyperliquid.")
        except Exception as e:
            logger.warning(f"No s'han pogut carregar szDecimals dinàmics d'Hyperliquid (s'usaran valors per defecte): {e}")

    async def get_referral_state(self) -> Dict[str, Any]:
        """Consulta l'estat de referits del compte a Hyperliquid."""
        try:
            return await asyncio.to_thread(self.info.query_referral_state, self.wallet_address)
        except Exception as e:
            logger.debug(f"No s'ha pogut consultar referral_state a Hyperliquid: {e}")
            return {}

    async def apply_referral_code(self, code: Optional[str] = None) -> Dict[str, Any]:
        """
        Aplica un codi de referit a Hyperliquid per obtenir el descompte del -4% en comissions.
        Només s'aplica si el compte no té cap referent registrat prèviament.
        """
        target_code = (code or self.referral_code or "").strip()
        if not target_code:
            return {"status": "skipped", "reason": "empty_code"}

        try:
            state = await self.get_referral_state()
            referred_by = state.get("referredBy")
            if referred_by:
                existing_code = referred_by.get("code") if isinstance(referred_by, dict) else str(referred_by)
                logger.info(f"El compte ja té un codi de referit actiu a Hyperliquid: '{existing_code}' (-4% vigent).")
                return {"status": "ok", "already_set": True, "code": existing_code}

            logger.info(f"Registrant codi de referit '{target_code}' a Hyperliquid per al descompte del -4%...")
            res = await asyncio.to_thread(self.exchange.set_referrer, target_code)
            logger.info(f"Codi de referit aplicat correctament a Hyperliquid: {res}")
            return res if isinstance(res, dict) else {"status": "ok", "raw": res}
        except Exception as e:
            logger.warning(f"No s'ha pogut aplicar el codi de referit '{target_code}' a Hyperliquid: {e}")
            return {"status": "err", "error": str(e)}

    def round_size(self, coin: str, size: float) -> float:
        """Ajusta la mida al nombre màxim de decimals admès per Hyperliquid."""
        sz_map = getattr(self, "coin_sz_decimals", COIN_SZ_DECIMALS)
        decimals = sz_map.get(coin.upper(), COIN_SZ_DECIMALS.get(coin.upper(), 2))
        if decimals == 0:
            return float(int(size))
        return round(size, decimals)

    def round_price(self, coin: str, price: float) -> float:
        """
        Arrodoneix el preu segons les especificacions estrictes d'Hyperliquid:
        - Màxim 5 xifres significatives
        - Màxim 6 - szDecimals decimals
        """
        if price <= 0:
            return price
        import math
        sz_map = getattr(self, "coin_sz_decimals", COIN_SZ_DECIMALS)
        sz_decimals = sz_map.get(coin.upper(), 2)
        max_decimals = max(0, 6 - sz_decimals)
        digits = 5
        decimals = max(0, digits - int(math.floor(math.log10(abs(price)))) - 1)
        decimals = min(decimals, max_decimals)
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

            # En comptes Unified, spot_usdc ja conté la totalitat del capital (inclòs el marge 'hold').
            # Només si spot_usdc és 0 utilitzem account_value o withdrawable del clearinghouse de perps.
            if spot_usdc > 0:
                total_bal = spot_usdc
            else:
                total_bal = max(account_value, withdrawable)

            logger.info(
                f"Balanç Hyperliquid obtingut: Perps={account_value:.2f}$, "
                f"SpotUSDC={spot_usdc:.2f}$, Withdrawable={withdrawable:.2f}$ -> Total={total_bal:.2f}$"
            )
            return total_bal
        except Exception as e:
            logger.error(f"Error consultant balanç a Hyperliquid: {e}")
            return 0.0

    async def set_leverage(self, coin: str, leverage: int = 2, is_cross: bool = True) -> Dict[str, Any]:
        """Ajusta el palanquejament per a un actiu a Hyperliquid."""
        hl_coin = "kPEPE" if coin.upper() == "PEPE" else coin.upper()
        def _execute():
            return self.exchange.update_leverage(
                leverage=int(leverage),
                name=hl_coin,
                is_cross=is_cross,
            )

        try:
            res = await asyncio.to_thread(_execute)
            logger.info(f"Palanquejament Hyperliquid ajustat per a {coin} ({leverage}x, Cross={is_cross}): {res}")
            return res if isinstance(res, dict) else {"status": "ok", "raw": res}
        except Exception as e:
            logger.error(f"Error ajustant palanquejament Hyperliquid per a {coin}: {e}")
            return {"status": "err", "error": str(e)}

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
        hl_coin = "kPEPE" if coin.upper() == "PEPE" else coin.upper()
        mult = 1000.0 if coin.upper() == "PEPE" else 1.0

        target_size = size / mult
        target_price = price * mult

        rounded_sz = self.round_size(hl_coin, target_size)
        if rounded_sz <= 0:
            return {"status": "err", "error": f"Mida invàlida per a {coin}: {size}"}

        # Collar de seguretat de 0.25% per a ordres IOC per assegurar fill immediat al millor preu
        if ioc:
            collar_px = target_price * (1.0025 if is_buy else 0.9975)
            rounded_px = self.round_price(hl_coin, collar_px)
        else:
            rounded_px = self.round_price(hl_coin, target_price)

        tif = "Alo" if post_only else ("Ioc" if ioc else "Gtc")
        order_type = {"limit": {"tif": tif}}

        def _execute():
            return self.exchange.order(
                name=hl_coin,
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
                            st0 = statuses[0]
                            if "error" in st0:
                                err_msg = st0["error"]
                                logger.error(f"Hyperliquid ha rebutjat l'ordre per a {coin}: {err_msg}")
                                return {"status": "err", "error": err_msg, "raw": res}
                            elif "resting" in st0:
                                logger.warning(f"Ordre Hyperliquid pendent al llibre (resting, no omplerta) per a {coin}: {st0}")
                                return {"status": "resting", "oid": st0.get("resting", {}).get("oid"), "raw": res}
                            elif "filled" in st0:
                                fill_info = st0.get("filled", {})
                                logger.info(f"Hyperliquid ordre {coin} omplerta: {fill_info}")
                                return {"status": "ok", "fill": fill_info, "raw": res}
                elif res.get("status") == "err":
                    logger.error(f"Hyperliquid ha retornat error: {res.get('response')}")
                    return res
            return {"status": "ok", "raw": res}
        except Exception as e:
            logger.error(f"Excepció enviant ordre Hyperliquid per a {coin}: {e}")
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
            hl_coin = "kPEPE" if coin.upper() == "PEPE" else coin.upper()
            mult = 1000.0 if coin.upper() == "PEPE" else 1.0

            acc_state = await self.get_account_state()
            current_sz = 0.0
            szi = 0.0
            for p in acc_state.get("assetPositions", []):
                pos = p.get("position", {})
                if pos.get("coin") == hl_coin:
                    szi = float(pos.get("szi", 0.0))
                    current_sz = abs(szi) * mult
                    break

            if current_sz <= 0.0:
                logger.info(f"Cap posició oberta per a {coin} a Hyperliquid per tancar.")
                return {"status": "ok", "message": f"Cap posició oberta per a {coin}"}

            close_sz = size if size is not None else current_sz
            is_buy = (szi < 0.0) # Si estem Short (szi < 0), comprem per tancar. Si Long, venem.

            all_mids = await asyncio.to_thread(self.info.all_mids)
            mid_px = float(all_mids.get(hl_coin, 0.0)) / mult
            if mid_px <= 0.0:
                return {"status": "err", "error": f"No s'ha pogut obtenir preu de mercat per a {coin}"}

            agg_px = mid_px * (1.015 if is_buy else 0.985)
            logger.info(f"Executant market_close a Hyperliquid per a {coin}: {'BUY' if is_buy else 'SELL'} {close_sz} @ {agg_px:.8f}")
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
        hl_coin = "kPEPE" if coin.upper() == "PEPE" else coin.upper()
        def _execute():
            return self.exchange.cancel(hl_coin, oid)
        try:
            res = await asyncio.to_thread(_execute)
            logger.info(f"Cancel·lació d'ordre Hyperliquid {coin} OID {oid}: {res}")
            return res
        except Exception as e:
            logger.error(f"Error cancel_order Hyperliquid: {e}")
            return {"status": "err", "error": str(e)}

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        """Consulta totes les ordres pendents a Hyperliquid."""
        try:
            return await asyncio.to_thread(self.info.frontend_open_orders, self.wallet_address)
        except Exception as e:
            logger.error(f"Error consultant ordres obertes Hyperliquid: {e}")
            return []

    async def cancel_all_orders(self, coin: Optional[str] = None) -> List[Dict[str, Any]]:
        """Cancel·la totes les ordres pendents a Hyperliquid."""
        def _execute():
            orders = self.info.frontend_open_orders(self.wallet_address)
            res = []
            for o in orders:
                c = o.get("coin")
                oid = o.get("oid")
                if coin is None or (c and c.upper() == coin.upper()):
                    try:
                        r = self.exchange.cancel(name=c, oid=oid)
                        res.append(r)
                    except Exception as ce:
                        logger.error(f"Error cancel·lant ordre Hyperliquid {oid} ({c}): {ce}")
            return res

        try:
            return await asyncio.to_thread(_execute)
        except Exception as e:
            logger.error(f"Error cancel·lant ordres Hyperliquid: {e}")
            return []
