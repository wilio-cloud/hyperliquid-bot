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

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        passphrase: Optional[str] = None,
        is_demo: Optional[bool] = None,
        base_url: Optional[str] = None,
    ):
        self.api_key = api_key or os.environ.get("OKX_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("OKX_API_SECRET", "")
        self.passphrase = passphrase or os.environ.get("OKX_PASSPHRASE", "")
        self.is_demo = (
            is_demo
            if is_demo is not None
            else (os.environ.get("OKX_IS_DEMO", "false").lower() in ("1", "true", "yes"))
        )
        self.base_url = base_url or os.environ.get("OKX_REST_URL", "https://www.okx.com")
        self._ssl_context = get_ssl_context()
        self.contract_specs: Dict[str, dict] = {}
        self._specs_initialized = False

    def is_ready_to_trade(self) -> bool:
        """Comprova si les credencials d'OKX estan degudament configurades."""
        return bool(self.api_key and self.api_secret and self.passphrase)

    def _sign(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        """Calcula la signatura HMAC-SHA256 codificada en base64 segons l'estàndard d'OKX V5."""
        message = f"{timestamp}{method.upper()}{request_path}{body}"
        mac = hmac.new(self.api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _get_headers(self, method: str, request_path: str, body: str = "") -> dict:
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
        if self.is_demo:
            headers["x-simulated-trading"] = "1"
        return headers

    async def _request(self, method: str, path: str, params: Optional[dict] = None, data: Optional[dict] = None) -> dict:
        """Executa una crida HTTP asíncrona signada contra OKX V5."""
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
                    return res_json
        except Exception as e:
            logger.error(f"Error HTTP en petició OKX ({method} {path}): {e}")
            return {"code": "-1", "msg": str(e), "data": []}

    async def init_contract_specs(self):
        """Descarrega les especificacions de cada contracte (ctVal, tickSz) des d'OKX."""
        if self._specs_initialized:
            return
        try:
            res = await self._request("GET", "/api/v5/public/instruments", params={"instType": "SWAP"})
            if res.get("code") == "0":
                for item in res.get("data", []):
                    inst_id = item.get("instId", "")
                    # inst_id: SOL-USDT-SWAP -> coin: SOL
                    parts = inst_id.split("-")
                    if len(parts) >= 3 and parts[1] == "USDT":
                        coin = parts[0].upper()
                        self.contract_specs[coin] = {
                            "ctVal": float(item.get("ctVal", self.DEFAULT_CT_VAL.get(coin, 1.0))),
                            "minSz": float(item.get("minSz", 1.0)),
                            "lotSz": float(item.get("lotSz", 1.0)),
                            "tickSz": float(item.get("tickSz", 0.01)),
                        }
                self._specs_initialized = True
                logger.info(f"Metadades de contractes OKX REST carregades ({len(self.contract_specs)} monedes).")
        except Exception as e:
            logger.debug(f"Error inicialitzant metadades de contractes OKX: {e}")

    def get_contract_val(self, coin: str) -> float:
        """Retorna el valor de 1 contracte en unitats de la moneda base."""
        c = coin.upper()
        if c in self.contract_specs:
            return self.contract_specs[c]["ctVal"]
        return self.DEFAULT_CT_VAL.get(c, 1.0)

    def to_contract_size(self, coin: str, size: float) -> int:
        """Converteix la mida en unitats de moneda base al nombre enter de contractes d'OKX."""
        ct_val = self.get_contract_val(coin)
        return max(1, int(round(size / ct_val)))

    def round_size(self, coin: str, theoretical_size: float) -> float:
        """
        Arrodoneix la mida en unitats de moneda a un múltiple exacte d'un contracte d'OKX.
        Retorna la mida exacta en moneda base que representaran aquests contractes.
        """
        contracts = self.to_contract_size(coin, theoretical_size)
        ct_val = self.get_contract_val(coin)
        return round(contracts * ct_val, 6)

    def round_price(self, coin: str, price: float) -> float:
        """Arrodoneix el preu segons el tickSz de l'instrument d'OKX."""
        c = coin.upper()
        tick_sz = 0.01
        if c in self.contract_specs:
            tick_sz = self.contract_specs[c].get("tickSz", 0.01)
        decimals = len(str(tick_sz).split(".")[1]) if "." in str(tick_sz) else 2
        return round(price, decimals)

    async def get_balance(self) -> float:
        """Retorna el saldo disponible en USDT o USDC a OKX (float) per a ús directe a l'exchange."""
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
        return {"available": avail_bal, "total": total_equity, "currencies": currencies, "raw": res.get("data")}

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
        return {"total_usd": total_usd, "currencies": currencies, "raw": res.get("data")}

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Consulta les posicions perpètues obertes a OKX."""
        res = await self._request("GET", "/api/v5/account/positions", params={"instType": "SWAP"})
        positions = []
        if res.get("code") == "0" and res.get("data"):
            for item in res["data"]:
                inst_id = item.get("instId", "")
                parts = inst_id.split("-")
                coin = parts[0].upper() if len(parts) >= 1 else ""
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
                    "raw": item,
                })
        return positions

    async def set_leverage(self, coin: str, leverage: int = 2) -> dict:
        """Configura el palanquejament creuat (Cross Margin) a OKX per a la moneda."""
        inst_id = f"{coin.upper()}-USDT-SWAP"
        payload = {
            "instId": inst_id,
            "lever": str(leverage),
            "mgnMode": "cross",
        }
        res = await self._request("POST", "/api/v5/account/set-leverage", data=payload)
        logger.debug(f"Palanquejament OKX per {coin} a {leverage}x: {res.get('msg', OK)}")
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
        inst_id = f"{coin.upper()}-USDT-SWAP"
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

        payload = {
            "instId": inst_id,
            "tdMode": "cross",
            "side": side,
            "ordType": ord_type,
            "sz": str(contracts),
            "px": str(rounded_px),
            "posSide": "net",
        }
        if reduce_only:
            payload["reduceOnly"] = True

        logger.info(
            f"📤 Enviant ordre OKX V5: {coin} {side.upper()} {contracts} cts (~{contracts*ct_val} {coin}) @ {rounded_px} "
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
                return {
                    "status": "ok",
                    "ordId": ord_id,
                    "data": {
                        "price": rounded_px,
                        "avg_price": rounded_px,
                        "size": contracts * ct_val,
                        "ordId": ord_id,
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

    async def market_close(self, coin: str, size: float) -> dict:
        """Tanca immediatament una posició oberta a OKX mitjançant l'endpoint close-position."""
        inst_id = f"{coin.upper()}-USDT-SWAP"
        payload = {
            "instId": inst_id,
            "mgnMode": "cross",
            "posSide": "net",
        }
        logger.info(f"🚨 Tancant posició restant a OKX: {coin}...")
        res = await self._request("POST", "/api/v5/trade/close-position", data=payload)
        if res.get("code") == "0":
            return {"status": "ok", "data": res.get("data", [])}
        return {"status": "err", "code": res.get("code"), "error": res.get("msg")}

    async def cancel_all_orders(self, coin: Optional[str] = None) -> dict:
        """Cancel·la totes les ordres pendents per a un instrument o globalment."""
        if coin:
            inst_id = f"{coin.upper()}-USDT-SWAP"
            # Consulta ordres pendents
            pending = await self._request("GET", "/api/v5/trade/orders-pending", params={"instId": inst_id})
            if pending.get("code") == "0" and pending.get("data"):
                cancel_list = [{"instId": inst_id, "ordId": o["ordId"]} for o in pending["data"]]
                if cancel_list:
                    return await self._request("POST", "/api/v5/trade/cancel-batch-orders", data=cancel_list)
        return {"status": "ok"}
