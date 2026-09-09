"""Client d'execució en viu per a OKX V5 REST API (Derivats Perpètus USDT-M)."""

import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import logging
import os
import ssl
from typing import Any, Dict, List, Optional
import aiohttp
import certifi

logger = logging.getLogger("OkxLive")

def get_ssl_context() -> ssl.SSLContext:
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

class OkxLiveClient:
    """
    Client REST autenticat per a OKX V5 API.
    Gestiona balanços, palanquejament 2x creuat, i enviament d'ordres (Maker, Taker, IOC).
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
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        passphrase: Optional[str] = None,
        is_demo: Optional[bool] = None,
        base_url: Optional[str] = None,
    ):
        self.api_key = (api_key or os.environ.get("OKX_API_KEY", "")).strip().strip('"').strip("'")
        self.api_secret = (api_secret or os.environ.get("OKX_API_SECRET", "")).strip().strip('"').strip("'")
        self.passphrase = (passphrase or os.environ.get("OKX_PASSPHRASE", "")).strip().strip('"').strip("'")
        self.is_demo = (
            is_demo
            if is_demo is not None
            else (os.environ.get("OKX_IS_DEMO", "false").lower() in ("1", "true", "yes"))
        )
        default_base_url = "https://eea.okx.com" if os.environ.get("OKX_REGION", "eea").lower() == "eea" else "https://www.okx.com"
        self.base_url = (base_url or os.environ.get("OKX_REST_URL", default_base_url)).strip().rstrip("/")
        self._ssl_context = get_ssl_context()
        self.contract_specs: Dict[str, dict] = {}
        self.symbol_map: Dict[str, str] = {}
        self.reverse_map: Dict[str, str] = {}
        self._specs_initialized = False
        self.pos_mode: str = "net_mode"
        self.acct_lv: str = "2"
        self.account_config: dict = {}

    def is_ready_to_trade(self) -> bool:
        """Comprova si les credencials d'OKX estan degudament configurades."""
        return bool(self.api_key and self.api_secret and self.passphrase)

    def _sign(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        """Calcula la signatura HMAC-SHA256 codificada en base64 segons l'estàndard d'OKX V5."""
        message = f"{timestamp}{method.upper()}{request_path}{body}"
        mac = hmac.new(self.api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _get_headers(self, method: str, request_path: str, body: str = "", override_demo: Optional[bool] = None) -> dict:
        """Genera els headers d'autenticació per a una petició REST d'OKX V5."""
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        sign = self._sign(ts, method, request_path, body)
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        demo_flag = self.is_demo if override_demo is None else override_demo
        if demo_flag:
            headers["x-simulated-trading"] = "1"
        return headers

    async def _request(self, method: str, path: str, params: Optional[dict] = None, data: Optional[dict] = None) -> dict:
        """Executa una crida HTTP asíncrona signada contra OKX V5 amb fallback automàtic per a EEA/Europa."""
        url = f"{self.base_url}{path}"
        query_str = ""
        if params:
            import urllib.parse
            query_str = "?" + urllib.parse.urlencode(params)
            url += query_str

        request_path = path + query_str
        body_str = json.dumps(data) if data else ""
        headers = self._get_headers(method, request_path, body_str)

        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self._ssl_context)) as session:
                async with session.request(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    data=body_str if body_str else None,
                    timeout=aiohttp.ClientTimeout(total=8.0),
                ) as resp:
                    res_json = await resp.json()

                    # Fallback 1: Si retorna 50119 ("API key doesn't exist"), provar domini europeu eea.okx.com
                    if res_json.get("code") == "50119":
                        for alt_domain in ["https://eea.okx.com", "https://my.okx.com"]:
                            if alt_domain == self.base_url:
                                continue
                            alt_url = f"{alt_domain}{path}{query_str}"
                            try:
                                async with session.request(
                                    method=method.upper(),
                                    url=alt_url,
                                    headers=headers,
                                    data=body_str if body_str else None,
                                    timeout=aiohttp.ClientTimeout(total=8.0),
                                ) as alt_resp:
                                    alt_json = await alt_resp.json()
                                    if alt_json.get("code") == "0":
                                        logger.info(f"Connexió OKX exitosa amb el domini regional: {alt_domain}")
                                        self.base_url = alt_domain
                                        return alt_json
                            except Exception:
                                pass

                    return res_json
        except Exception as e:
            logger.error(f"Error HTTP en petició OKX ({method} {path}): {e}")
            return {"code": "-1", "msg": str(e), "data": []}

    def get_inst_id(self, coin: str) -> str:
        """Retorna l'identificador d'instrument oficial d'OKX (X-Perp per a EEA o USDT-SWAP per a Global)."""
        c = coin.upper()
        if c in self.symbol_map:
            return self.symbol_map[c]
        if c in self.contract_specs and "instId" in self.contract_specs[c]:
            return self.contract_specs[c]["instId"]
        return f"{c}-USDT-SWAP"

    async def init_contract_specs(self):
        """Descarrega les especificacions de cada contracte (ctVal, tickSz) i la configuració del compte des d'OKX."""
        if self._specs_initialized:
            return

        # 1. Carregar configuració del compte (posMode, acctLv) si tenim credencials
        if not self.account_config and self.is_ready_to_trade():
            try:
                cfg_res = await self._request("GET", "/api/v5/account/config")
                if cfg_res.get("code") == "0" and cfg_res.get("data"):
                    self.account_config = cfg_res["data"][0]
                    self.pos_mode = self.account_config.get("posMode", "net_mode")
                    self.acct_lv = self.account_config.get("acctLv", "2")
                    logger.info(f"Configuració OKX carregada: posMode={self.pos_mode}, acctLv={self.acct_lv}")
            except Exception as ce:
                logger.debug(f"Error consultant account/config OKX: {ce}")

        is_eea = ("eea.okx.com" in self.base_url) or (os.environ.get("OKX_REGION", "eea").lower() == "eea")

        # 2. Descarregar instruments FUTURES (per als contractes USD_UM_XPERP a Europa / EEA)
        if is_eea:
            try:
                res_fut = await self._request("GET", "/api/v5/public/instruments", params={"instType": "FUTURES"})
                if res_fut.get("code") == "0":
                    for item in res_fut.get("data", []):
                        inst_id = item.get("instId", "")
                        if "XPERP" in inst_id and item.get("state") != "suspend":
                            parts = inst_id.split("-")
                            coin = parts[0].upper()
                            if coin not in self.symbol_map:
                                self.symbol_map[coin] = inst_id
                                self.reverse_map[inst_id] = coin
                                self.contract_specs[coin] = {
                                    "instId": inst_id,
                                    "ctVal": float(item.get("ctVal", self.DEFAULT_XPERP_CT_VAL.get(coin, 1.0))),
                                    "minSz": float(item.get("minSz", 1.0)),
                                    "lotSz": float(item.get("lotSz", 1.0)),
                                    "tickSz": float(item.get("tickSz", 0.01)),
                                }
                    logger.info(f"Metadades de contractes OKX X-Perp REST carregades ({len(self.contract_specs)} monedes).")
            except Exception as fe:
                logger.debug(f"Error consultant instruments FUTURES OKX: {fe}")

        # 3. Descarregar instruments SWAP (per a comptes globals o monedes sense X-Perp)
        try:
            res = await self._request("GET", "/api/v5/public/instruments", params={"instType": "SWAP"})
            if res.get("code") == "0":
                for item in res.get("data", []):
                    inst_id = item.get("instId", "")
                    # inst_id: SOL-USDT-SWAP -> coin: SOL
                    parts = inst_id.split("-")
                    if len(parts) >= 3 and parts[1] == "USDT":
                        coin = parts[0].upper()
                        if coin not in self.contract_specs:
                            self.symbol_map[coin] = inst_id
                            self.reverse_map[inst_id] = coin
                            self.contract_specs[coin] = {
                                "instId": inst_id,
                                "ctVal": float(item.get("ctVal", self.DEFAULT_CT_VAL.get(coin, 1.0))),
                                "minSz": float(item.get("minSz", 1.0)),
                                "lotSz": float(item.get("lotSz", 1.0)),
                                "tickSz": float(item.get("tickSz", 0.01)),
                            }
                self._specs_initialized = True
                logger.info(f"Metadades totals de contractes OKX REST carregades ({len(self.contract_specs)} monedes).")
        except Exception as e:
            logger.debug(f"Error inicialitzant metadades de contractes OKX: {e}")

    def get_contract_val(self, coin: str) -> float:
        """Retorna el valor de 1 contracte en unitats de la moneda base."""
        c = coin.upper()
        if c in self.contract_specs:
            return self.contract_specs[c]["ctVal"]
        is_eea = ("eea.okx.com" in self.base_url) or (os.environ.get("OKX_REGION", "eea").lower() == "eea")
        if is_eea and c in self.DEFAULT_XPERP_CT_VAL:
            return self.DEFAULT_XPERP_CT_VAL[c]
        return self.DEFAULT_CT_VAL.get(c, 1.0)

    def to_contract_size(self, coin: str, size: float) -> int:
        """
        Converteix la mida en unitats de moneda base al nombre enter de contractes d'OKX.
        Si la mida és inferior a mig contracte, retorna 0 per evitar sobredimensionar ordres (ex: BTC/ETH en proves micro).
        """
        ct_val = self.get_contract_val(coin)
        return int(round(size / ct_val))

    def round_size(self, coin: str, theoretical_size: float) -> float:
        """
        Arrodoneix la mida en unitats de moneda a un múltiple exacte d'un contracte d'OKX.
        Retorna la mida exacta en moneda base que representaran aquests contractes.
        """
        contracts = self.to_contract_size(coin, theoretical_size)
        ct_val = self.get_contract_val(coin)
        return round(contracts * ct_val, 6)

    def round_price(self, coin: str, price: float) -> float:
        """Arrodoneix el preu segons el tickSz de l'instrument d'OKX evitant problemes de notació científica."""
        c = coin.upper()
        tick_sz = 0.01
        if c in self.contract_specs:
            tick_sz = self.contract_specs[c].get("tickSz", 0.01)
        try:
            d_str = f"{float(tick_sz):.8f}".rstrip("0")
            decimals = len(d_str.split(".")[1]) if "." in d_str else 2
        except Exception:
            decimals = 4
        return round(price, decimals)

    async def get_balance(self) -> float:
        """Retorna el patrimoni total (Total Equity) en USDT o USDC a OKX (float) per a ús directe a l'exchange."""
        bal_data = await self.get_account_balance()
        total = float(bal_data.get("total", 0.0))
        if total > 0:
            return total
        return float(bal_data.get("available", 0.0))

    async def get_available_balance(self) -> float:
        """Retorna el marge lliure disponible en USDT o USDC a OKX (sense comptar marge retingut)."""
        bal_data = await self.get_account_balance()
        return float(bal_data.get("available", 0.0))

    async def get_account_balance(self, ccy: Optional[str] = None) -> Dict[str, Any]:
        """Consulta el balanç complet del compte unificat de trading a OKX (Trading Account)."""
        params = {"ccy": ccy} if ccy else None
        res = await self._request("GET", "/api/v5/account/balance", params=params)
        avail_bal = 0.0
        total_equity = 0.0
        currencies = {}
        if res.get("code") == "0" and res.get("data"):
            details = res["data"][0].get("details", [])
            for d in details:
                c = d.get("ccy")
                avail = float(d.get("availBal", d.get("availEq", 0.0)))
                eq = float(d.get("eq", avail))
                currencies[c] = {"available": avail, "total": eq}
                if c in ("USDT", "USDC"):
                    avail_bal += avail
                    total_equity += eq
        return {
            "available": avail_bal,
            "total": total_equity,
            "currencies": currencies,
            "code": res.get("code"),
            "msg": res.get("msg"),
            "raw": res.get("data"),
        }

    async def get_funding_balance(self, ccy: Optional[str] = None) -> Dict[str, Any]:
        """Consulta el balanç del compte de finançament (Funding Account / Dipòsits) a OKX."""
        params = {"ccy": ccy} if ccy else None
        res = await self._request("GET", "/api/v5/asset/balances", params=params)
        currencies = {}
        total_usd = 0.0
        if res.get("code") == "0" and res.get("data"):
            for d in res.get("data", []):
                c = d.get("ccy")
                avail = float(d.get("availBal", 0.0))
                bal = float(d.get("bal", avail))
                currencies[c] = {"available": avail, "balance": bal}
                if c in ("USDT", "USDC"):
                    total_usd += bal
        return {
            "total_usd": total_usd,
            "currencies": currencies,
            "code": res.get("code"),
            "msg": res.get("msg"),
            "raw": res.get("data"),
        }

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Consulta les posicions perpètues obertes a OKX (tant SWAP com X-Perp)."""
        await self.init_contract_specs()
        # Sense instType per consultar totes les posicions reals obertes
        res = await self._request("GET", "/api/v5/account/positions")
        positions = []
        if res.get("code") == "0" and res.get("data"):
            for item in res["data"]:
                inst_id = item.get("instId", "")
                parts = inst_id.split("-")
                coin = self.reverse_map.get(inst_id) or (parts[0].upper() if len(parts) >= 1 else "")
                pos_contracts = float(item.get("pos", 0.0))
                if pos_contracts == 0:
                    continue

                ct_val = self.get_contract_val(coin)
                base_amount = abs(pos_contracts) * ct_val

                pos_side = item.get("posSide", "net")
                if pos_side == "net":
                    side = "buy" if pos_contracts > 0 else "sell"
                else:
                    side = "buy" if pos_side == "long" else "sell"

                positions.append({
                    "asset": coin,
                    "coin": coin,
                    "amount": base_amount,
                    "contracts": abs(pos_contracts),
                    "side": side,
                    "avg_entry_price": float(item.get("avgPx", 0.0)),
                    "unrealized_pnl": float(item.get("upl", 0.0)),
                    "instId": inst_id,
                    "raw": item,
                })
        return positions

    async def set_leverage(self, coin: str, leverage: int = 2) -> dict:
        """Configura el palanquejament creuat (Cross Margin) a OKX per a la moneda."""
        await self.init_contract_specs()
        inst_id = self.get_inst_id(coin)
        payload = {
            "instId": inst_id,
            "lever": str(leverage),
            "mgnMode": "cross",
        }
        if self.pos_mode == "long_short_mode":
            payload["posSide"] = "long"
            res_l = await self._request("POST", "/api/v5/account/set-leverage", data=payload)
            payload["posSide"] = "short"
            res_s = await self._request("POST", "/api/v5/account/set-leverage", data=payload)
            return res_l if res_l.get("code") == "0" else res_s
        else:
            payload["posSide"] = "net"
            res = await self._request("POST", "/api/v5/account/set-leverage", data=payload)
            if res.get("code") != "0":
                payload.pop("posSide", None)
                res = await self._request("POST", "/api/v5/account/set-leverage", data=payload)
            logger.debug(f"Palanquejament OKX per {coin} ({inst_id}) a {leverage}x: {res.get('msg', 'OK')}")
            return res

    async def place_order(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        price: float,
        post_only: bool = False,
        ioc: bool = False,
        reduce_only: bool = False,
    ) -> dict:
        """Envia una ordre límit, post-only o IOC a OKX."""
        await self.init_contract_specs()
        inst_id = self.get_inst_id(coin)
        side = "buy" if is_buy else "sell"

        if post_only:
            ord_type = "post_only"
        elif ioc:
            ord_type = "ioc"
        else:
            ord_type = "limit"

        ct_val = self.get_contract_val(coin)
        contracts = max(1, int(round(size / ct_val)))
        rounded_px = self.round_price(coin, price)

        # Determinar posSide segons la configuració del compte
        if self.pos_mode == "long_short_mode":
            if reduce_only:
                pos_side = "long" if not is_buy else "short"
            else:
                pos_side = "long" if is_buy else "short"
        else:
            pos_side = "net"

        payload = {
            "instId": inst_id,
            "tdMode": "cross",
            "side": side,
            "ordType": ord_type,
            "sz": str(contracts),
            "px": str(rounded_px),
            "posSide": pos_side,
        }
        if reduce_only:
            payload["reduceOnly"] = True

        logger.info(
            f"📤 Enviant ordre OKX V5: {coin} ({inst_id}) {side.upper()} {contracts} cts (~{contracts*ct_val} {coin}) @ {rounded_px} "
            f"(Type={ord_type}, ReduceOnly={reduce_only})..."
        )

        res = await self._request("POST", "/api/v5/trade/order", data=payload)

        if res.get("code") == "0" and res.get("data"):
            order_data = res["data"][0]
            ord_id = order_data.get("ordId", "")
            s_code = order_data.get("sCode", "0")
            s_msg = order_data.get("sMsg", "")
            if s_code == "0":
                logger.info(f"✅ Ordre OKX acceptada! OrdId: {ord_id}")
                fill_sz = contracts * ct_val
                avg_px = rounded_px

                if ioc:
                    await asyncio.sleep(0.12)
                    try:
                        chk = await self._request("GET", "/api/v5/trade/order", params={"instId": inst_id, "ordId": ord_id})
                        if chk.get("code") == "0" and chk.get("data"):
                            ord_info = chk["data"][0]
                            acc_fill = float(ord_info.get("accFillSz", 0.0))
                            ord_state = ord_info.get("state", "")
                            if acc_fill <= 0 and ord_state == "canceled":
                                logger.warning(f"❌ Ordre IOC OKX no s'ha omplert al preu {rounded_px} (state={ord_state}, accFillSz=0).")
                                return {"status": "err", "error": "ioc_unfilled", "ordId": ord_id}
                            if acc_fill > 0:
                                fill_sz = acc_fill * ct_val
                                avg_px = float(ord_info.get("avgPx", rounded_px))
                                logger.info(f"🎯 Ordre IOC OKX omplerta amb èxit! {acc_fill} cts @ {avg_px}")
                    except Exception as ie:
                        logger.debug(f"Error verificant estat IOC OKX: {ie}")

                return {
                    "status": "ok",
                    "ordId": ord_id,
                    "data": {
                        "price": avg_px,
                        "avg_price": avg_px,
                        "size": fill_sz,
                        "ordId": ord_id,
                        "instId": inst_id,
                    },
                }
            else:
                logger.error(f"❌ Error intern ordre OKX (sCode {s_code}): {s_msg}")
                return {"status": "err", "code": s_code, "error": s_msg}
        else:
            code = res.get("code", "-1")
            msg = res.get("msg", "Unknown error")
            logger.error(f"❌ Error petició ordre OKX (Code {code}): {msg}")
            return {"status": "err", "code": code, "error": msg}

    async def market_close(self, coin: str, size: Optional[float] = None) -> dict:
        """Tanca immediatament una posició a OKX (sencera o parcialment per rebalanceig)."""
        await self.init_contract_specs()
        inst_id = self.get_inst_id(coin)
        pos_side = "net"
        positions = await self.get_positions()
        pos = next((p for p in positions if p.get("coin") == coin.upper()), None)
        if not pos:
            return {"status": "ok", "msg": "No position to close"}

        current_side = pos.get("side", "")
        current_amount = float(pos.get("amount", 0.0))

        # Si no s'especifica mida o és >= posició sencera, usem close-position d'OKX
        if size is None or size >= current_amount * 0.99:
            if self.pos_mode == "long_short_mode":
                pos_side = "long" if current_side == "buy" else "short"
            payload = {
                "instId": inst_id,
                "mgnMode": "cross",
                "posSide": pos_side,
            }
            logger.info(f"🚨 Tancant posició sencera a OKX: {coin} ({inst_id}, posSide={pos_side})...")
            res = await self._request("POST", "/api/v5/trade/close-position", data=payload)
            if res.get("code") == "0":
                return {"status": "ok", "data": res.get("data", [])}
            return {"status": "err", "code": res.get("code"), "error": res.get("msg")}
        else:
            # Tancament parcial per Delta Rebalancer: ordre contrària agressiva IOC amb reduce_only=True
            close_is_buy = (current_side == "sell")
            last_price = float(pos.get("avg_entry_price", 0.0))
            aggr_px = last_price * 1.05 if close_is_buy else last_price * 0.95
            logger.info(f"⚖️ Tancant parcialment a OKX: {coin} {size} (de {current_amount}) amb IOC reduce_only...")
            return await self.place_order(
                coin=coin,
                is_buy=close_is_buy,
                size=size,
                price=aggr_px,
                post_only=False,
                ioc=True,
                reduce_only=True,
            )

    async def cancel_all_orders(self, coin: Optional[str] = None) -> dict:
        """Cancel·la totes les ordres pendents per a un instrument o globalment."""
        if coin:
            await self.init_contract_specs()
            inst_id = self.get_inst_id(coin)
            # Consulta ordres pendents
            pending = await self._request("GET", "/api/v5/trade/orders-pending", params={"instId": inst_id})
            if pending.get("code") == "0" and pending.get("data"):
                cancel_list = [{"instId": inst_id, "ordId": o["ordId"]} for o in pending["data"]]
                if cancel_list:
                    return await self._request("POST", "/api/v5/trade/cancel-batch-orders", data=cancel_list)
        return {"status": "ok"}
