"""Motor d'execució d'arbitratge real (Hyperliquid + Aevo) amb protecció Anti-Unhedged."""

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional
from core.arbitrage_models import ArbitrageDirection, ArbitrageLeg, ArbitragePosition, ArbitrageSignal
from core.arbitrage_paper_exchange import ArbitragePaperExchange
from core.hyperliquid_live_client import HyperliquidLiveClient
from core.aevo_live_client import AevoLiveClient
from core.models import OrderBookL2, OrderSide

logger = logging.getLogger("ArbitrageLiveExchange")

class ArbitrageLiveExchange(ArbitragePaperExchange):
    def __init__(
        self,
        hl_client: HyperliquidLiveClient,
        venue2_client: Optional[Any] = None,
        aevo_client: Optional[Any] = None,
        venue2_name: str = "AEVO",
        initial_hl_balance: float = 500.0,
        initial_bn_balance: float = 500.0,
        leverage: float = 3.0,
        maker_first: bool = False,
        on_open_cb: Optional[Callable[[ArbitragePosition], None]] = None,
        on_close_cb: Optional[Callable[[ArbitragePosition, str, float], None]] = None,
        state_file: str = "live_state.json",
    ):
        client2 = venue2_client or aevo_client
        if client2 is None:
            raise ValueError("Cal especificar venue2_client o aevo_client per a ArbitrageLiveExchange.")

        v2_name = venue2_name.upper()
        if v2_name == "DYDX":
            bn_maker_fee = 0.00010
            bn_taker_fee = 0.00050
        elif v2_name == "OKX":
            bn_maker_fee = 0.00020
            bn_taker_fee = 0.00050
        else:
            bn_maker_fee = 0.00030
            bn_taker_fee = 0.00050

        super().__init__(
            initial_hl_balance=initial_hl_balance,
            initial_bn_balance=initial_bn_balance,
            venue2_name=v2_name,
            leverage=leverage,
            hl_maker_fee=0.00015,
            hl_taker_fee=0.00045,
            bn_maker_fee=bn_maker_fee,
            bn_taker_fee=bn_taker_fee,
            maker_first=maker_first,
            on_open_cb=on_open_cb,
            on_close_cb=on_close_cb,
            state_file=state_file,
        )
        self.hl_client = hl_client
        self.venue2_client = client2
        self.aevo_client = client2
        self._pending_opens: set = set()
        self._pending_closes: set = set()
        self._open_broker_coins: set = set()
        self._configured_leverage_coins: set = set()
        self._last_hl_books: Dict[str, OrderBookL2] = {}
        self._last_bn_books: Dict[str, OrderBookL2] = {}

    def on_hl_book(self, book: OrderBookL2):
        self._last_hl_books[book.coin] = book
        super().on_hl_book(book)

    def on_bn_book(self, book: OrderBookL2):
        self._last_bn_books[book.coin] = book
        super().on_bn_book(book)

    async def reconcile_active_positions(self):
        """Sincronitza l'estat intern amb les posicions reals obertes a Hyperliquid i Aevo."""
        try:
            hl_state_task = asyncio.create_task(self.hl_client.get_account_state())
            aevo_pos_task = asyncio.create_task(self.aevo_client.get_positions())
            hl_state, aevo_positions = await asyncio.gather(hl_state_task, aevo_pos_task, return_exceptions=True)

            # C2 FIX: Si qualsevol API falla, avortar reconciliació per evitar liquidació massiva
            if isinstance(hl_state, Exception):
                logger.warning(f"⚠️ [RECONCILE] API Hyperliquid ha fallat ({hl_state}). Avortant reconciliació per seguretat.")
                return
            if isinstance(aevo_positions, Exception):
                logger.warning(f"⚠️ [RECONCILE] API {self.venue2_name} ha fallat ({aevo_positions}). Avortant reconciliació per seguretat.")
                return

            hl_positions_map = {}
            if isinstance(hl_state, dict):
                for p in hl_state.get("assetPositions", []):
                    pos = p.get("position", {})
                    szi = float(pos.get("szi", 0.0))
                    c = pos.get("coin", "").upper()
                    if c == "KPEPE":
                        c = "PEPE"
                    if szi != 0.0 and c:
                        hl_positions_map[c] = pos

            aevo_positions_map = {}
            if isinstance(aevo_positions, list):
                for p in aevo_positions:
                    amt = float(p.get("amount", 0.0))
                    c = p.get("asset", "").upper()
                    if amt != 0.0 and c:
                        aevo_positions_map[c] = p

            self._open_broker_coins = set(hl_positions_map.keys()).union(set(aevo_positions_map.keys()))

            # Si ni HL ni Aevo tenen cap posició oberta, netegem
            if not self._open_broker_coins and self.active_positions:
                logger.info("Reconciliació: cap posició oberta als brokers. Netejant active_positions internes.")
                self.active_positions.clear()
                self.save_state()
                return

            # Reconciliem cada moneda que tingui posicions obertes als brokers
            existing_active_coins = {p.coin.upper(): pair_id for pair_id, p in self.active_positions.items()}

            # C3 FIX: Obtenir coins amb tancament pendent per evitar double-close
            pending_close_coins = set()
            for pair_id in self._pending_closes:
                pos = self.active_positions.get(pair_id)
                if pos:
                    pending_close_coins.add(pos.coin.upper())

            for coin in list(self._open_broker_coins):
                if coin in self._pending_opens:
                    logger.debug(f"Reconciliació: {coin} té una ordre d'obertura en curs. Ometent temporalment.")
                    continue
                if coin in pending_close_coins:
                    logger.debug(f"Reconciliació: {coin} té un tancament en curs. Ometent temporalment.")
                    continue

                hl_pos = hl_positions_map.get(coin)
                aevo_pos = aevo_positions_map.get(coin)

                # CAS 1: Pota òrfena a Hyperliquid sense cobertura a Venue2 (Anti-Unhedged Guard)
                if hl_pos and not aevo_pos:
                    szi = float(hl_pos.get("szi", 0.0))
                    logger.warning(
                        f"🚨 [ORPHAN GUARD] Detectada posició òrfena a Hyperliquid per a {coin} "
                        f"(szi={szi}) sense cobertura a {self.venue2_name}. Tancant a mercat per protegir capital..."
                    )
                    try:
                        await self.hl_client.market_close(coin=coin, size=abs(szi))
                    except Exception as ohe:
                        logger.error(f"Error tancant posició òrfena Hyperliquid {coin}: {ohe}")
                    if coin in existing_active_coins:
                        self.active_positions.pop(existing_active_coins[coin], None)

                # CAS 2: Pota òrfena a Venue2 sense cobertura a Hyperliquid
                elif aevo_pos and not hl_pos:
                    amt = float(aevo_pos.get("amount", 0.0))
                    logger.warning(
                        f"🚨 [ORPHAN GUARD] Detectada posició òrfena a {self.venue2_name} per a {coin} "
                        f"(amt={amt}) sense cobertura a Hyperliquid. Tancant a mercat per protegir capital..."
                    )
                    try:
                        await self.venue2_client.market_close(coin=coin, size=amt)
                    except Exception as ove:
                        logger.error(f"Error tancant posició òrfena {self.venue2_name} {coin}: {ove}")
                    if coin in existing_active_coins:
                        self.active_positions.pop(existing_active_coins[coin], None)

                # CAS 3: Posicions a ambdós brokers -> Verificar Delta Neutrality i Rebalancejar
                elif hl_pos and aevo_pos:
                    szi = float(hl_pos.get("szi", 0.0))
                    hl_entry_px = float(hl_pos.get("entryPx", 0.0))
                    aevo_amount = float(aevo_pos.get("amount", 0.0))
                    aevo_side = aevo_pos.get("side", "").lower()
                    aevo_entry_px = float(aevo_pos.get("avg_entry_price", 0.0))

                    hl_sz = abs(szi)
                    bn_sz = abs(aevo_amount)
                    diff = hl_sz - bn_sz

                    # Comprovar desequilibri de mida (ex: NEAR 24 a HL vs 12 a OKX)
                    if abs(diff) > 0.001:
                        logger.warning(
                            f"⚖️ [DELTA REBALANCER] Desequilibri detectat per a {coin}: "
                            f"HL={hl_sz}, {self.venue2_name}={bn_sz} (Diferència: {diff:+.4f})"
                        )
                        if diff > 0.001:
                            excess_hl = diff
                            logger.info(f"⚖️ Tancant excés de {excess_hl:.4f} {coin} a Hyperliquid...")
                            try:
                                await self.hl_client.market_close(coin=coin, size=excess_hl)
                                hl_sz = bn_sz
                            except Exception as re_e:
                                logger.error(f"Error rebalancejant excés Hyperliquid {coin}: {re_e}")
                        elif diff < -0.001:
                            excess_bn = abs(diff)
                            logger.info(f"⚖️ Tancant excés de {excess_bn:.4f} {coin} a {self.venue2_name}...")
                            try:
                                await self.venue2_client.market_close(coin=coin, size=excess_bn)
                                bn_sz = hl_sz
                            except Exception as re_e:
                                logger.error(f"Error rebalancejant excés {self.venue2_name} {coin}: {re_e}")

                    matched_sz = min(hl_sz, bn_sz)

                    if szi < 0 and aevo_side == "buy":
                        direction = ArbitrageDirection.SELL_HL_BUY_BN
                        hl_side = OrderSide.SELL
                        bn_side = OrderSide.BUY
                    else:
                        direction = ArbitrageDirection.BUY_HL_SELL_BN
                        hl_side = OrderSide.BUY
                        bn_side = OrderSide.SELL

                    # Actualitzar o crear ArbitragePosition local
                    if coin in existing_active_coins:
                        pair_id = existing_active_coins[coin]
                        pos_obj = self.active_positions[pair_id]
                        pos_obj.leg_hl.size = matched_sz
                        pos_obj.leg_bn.size = matched_sz
                        pos_obj.leg_hl.size_usd = matched_sz * pos_obj.leg_hl.entry_price
                        pos_obj.leg_bn.size_usd = matched_sz * pos_obj.leg_bn.entry_price
                    else:
                        pair_id = f"arb_{coin}_reconciled_{int(time.time())}"
                        mid = (hl_entry_px + aevo_entry_px) / 2.0 if (hl_entry_px + aevo_entry_px) > 0 else 1.0
                        entry_spread = abs(hl_entry_px - aevo_entry_px) / mid * 100.0

                        leg_hl = ArbitrageLeg(
                            venue="HYPERLIQUID",
                            coin=coin,
                            side=hl_side,
                            entry_price=hl_entry_px,
                            size=matched_sz,
                            size_usd=matched_sz * hl_entry_px,
                            current_price=hl_entry_px,
                            fee_rate=self.hl_taker_fee,
                            fees_paid=matched_sz * hl_entry_px * self.hl_taker_fee,
                        )
                        leg_bn = ArbitrageLeg(
                            venue=self.venue2_name,
                            coin=coin,
                            side=bn_side,
                            entry_price=aevo_entry_px,
                            size=matched_sz,
                            size_usd=matched_sz * aevo_entry_px,
                            current_price=aevo_entry_px,
                            fee_rate=self.bn_taker_fee,
                            fees_paid=matched_sz * aevo_entry_px * self.bn_taker_fee,
                        )
                        raw_pos = aevo_pos.get("raw", {}) if isinstance(aevo_pos, dict) else {}
                        pos_c_time = raw_pos.get("cTime") or aevo_pos.get("cTime")
                        try:
                            parsed_entry_time = float(pos_c_time) / 1000.0 if pos_c_time else time.time()
                        except Exception:
                            parsed_entry_time = time.time()

                        pos_obj = ArbitragePosition(
                            pair_id=pair_id,
                            coin=coin,
                            direction=direction,
                            leg_hl=leg_hl,
                            leg_bn=leg_bn,
                            entry_spread_pct=max(entry_spread, 0.180),
                            entry_time=parsed_entry_time,
                        )
                        self.active_positions[pair_id] = pos_obj
                        logger.info(f"✅ Reconciliada posició activa existent per a {coin} ({pair_id}) amb mida 1:1 {matched_sz}")

            # Si una posició local ja està tancada als brokers, l'eliminem
            for coin, pair_id in list(existing_active_coins.items()):
                if coin not in self._open_broker_coins:
                    logger.info(f"Netejant posició {coin} d'active_positions perquè ja no existeix als brokers.")
                    self.active_positions.pop(pair_id, None)

            self.save_state()
        except Exception as e:
            logger.debug(f"Error en reconcile_active_positions: {e}")

    def has_open_position(self, coin: str) -> bool:
        """Comprova si hi ha posició oberta tant localment com directament als comptes d'Hyperliquid o Aevo."""
        if coin.upper() in self._open_broker_coins:
            return True
        return super().has_open_position(coin)

    async def sync_real_balances(self):
        """Sincronitza els saldos reals disponibles a la blockchain d'Hyperliquid i Aevo."""
        try:
            hl_bal_task = asyncio.create_task(self.hl_client.get_balance())
            aevo_bal_task = asyncio.create_task(self.aevo_client.get_balance())
            hl_bal, aevo_bal = await asyncio.gather(hl_bal_task, aevo_bal_task, return_exceptions=True)

            if isinstance(hl_bal, (int, float)) and hl_bal > 0:
                self.hl_balance_usd = float(hl_bal)
            if isinstance(aevo_bal, (int, float)) and aevo_bal > 0:
                self.bn_balance_usd = float(aevo_bal)

            # Obtenir marge lliure disponible per a verificació prèvia a l'obertura
            if hasattr(self.aevo_client, "get_available_balance"):
                try:
                    avail = await self.aevo_client.get_available_balance()
                    self.bn_avail_usd = float(avail)
                except Exception:
                    self.bn_avail_usd = self.bn_balance_usd
            else:
                self.bn_avail_usd = self.bn_balance_usd

            # Només inicialitzem el balanç de referència UN COP (cold boot).
            # Evitem el reset continu que destruïa la baseline d'equity.
            if not self.closed_positions and not getattr(self, "_initial_balance_set", False):
                self.initial_total_balance = self.total_balance_usd + getattr(self, "total_fees_paid", 0.0)
                self._initial_balance_set = True

            logger.info(
                f"Saldos reals sincronitzats: Hyperliquid = {self.hl_balance_usd:.2f}$ | "
                f"{self.venue2_name} = {self.bn_balance_usd:.2f}$ (Lliure: {getattr(self, 'bn_avail_usd', self.bn_balance_usd):.2f}$) | Total = {self.total_balance_usd:.2f}$"
            )
            await self.reconcile_active_positions()
            if self.hl_client.referral_code:
                await self.hl_client.apply_referral_code()
            self.save_state()
        except Exception as e:
            logger.error(f"Error sincronitzant saldos reals: {e}")

    async def configure_all_leverage(self, leverage: Optional[int] = None, coins: Optional[List[str]] = None) -> Dict[str, Any]:
        """Configura el palanquejament desitjat (ex: 2x) i Cross Margin a Hyperliquid i Venue2 per a tots els mercats."""
        lev = int(leverage or self.leverage or 3)
        self.leverage = float(lev)
        results = {"status": "ok", "leverage": lev, "hl": {}, "aevo": {}}
        target_coins = coins or [
            "ETH", "BTC", "SOL", "SUI", "NEAR", "LINK", "AVAX",
            "ARB", "OP", "APT", "SEI", "TIA", "RENDER", "INJ",
            "ENA", "DOGE", "WIF", "AAVE", "UNI", "HYPE", "PUMP", "PEPE"
        ]
        logger.info(f"⚡ [LEVERAGE] Aplicant {lev}x Cross Margin a Hyperliquid i {self.venue2_name} per a {len(target_coins)} monedes...")

        for c in target_coins:
            try:
                hl_res = await self.hl_client.set_leverage(c, leverage=lev, is_cross=True)
            except Exception as e:
                hl_res = {"status": "err", "error": str(e)}
            try:
                aevo_res = await self.aevo_client.set_leverage(c, leverage=lev)
            except Exception as e:
                aevo_res = {"status": "err", "error": str(e)}

            results["hl"][c] = hl_res
            results["aevo"][c] = aevo_res
            self._configured_leverage_coins.add(c)
            await asyncio.sleep(0.05)

        logger.info(f"⚡ [LEVERAGE] Configuració completada ({lev}x) per a {len(target_coins)} monedes.")
        return results

    def open_arbitrage_position(
        self,
        signal: ArbitrageSignal,
        size_usd: float = 250.0,
        is_maker: bool = False,
        maker_first: Optional[bool] = None,
    ) -> Optional[ArbitragePosition]:
        """Inicia l'execució atòmica d'obertura en segon pla."""
        if getattr(self, "trading_paused", False):
            logger.debug(f"Trading pausat per l'usuari. Omissió d'obertura per a {signal.coin}.")
            return None

        if self.has_open_position(signal.coin) or signal.coin in self._pending_opens:
            logger.debug(f"Ja hi ha posició o execució en curs per {signal.coin}.")
            return None

        # Comprovació de capital disponible
        required_margin = size_usd / self.leverage
        bn_avail = getattr(self, "bn_avail_usd", self.bn_balance_usd)
        if self.hl_balance_usd < required_margin or bn_avail < required_margin:
            logger.warning(
                f"Capital insuficient per obrir {signal.coin}: requerit {required_margin:.1f}$, "
                f"HL={self.hl_balance_usd:.1f}$, {self.venue2_name}={bn_avail:.1f}$"
            )
            return None

        effective_maker_first = self.maker_first if maker_first is None else maker_first
        self._pending_opens.add(signal.coin)
        asyncio.create_task(self._execute_live_open(signal, size_usd, is_maker, effective_maker_first))
        return None

    async def _execute_live_open(
        self,
        signal: ArbitrageSignal,
        size_usd: float,
        is_maker: bool,
        effective_maker_first: bool = False,
    ):
        coin = signal.coin
        pair_id = f"ARB_{coin}_LIVE_{int(time.time() * 1000)}"
        try:
            # L4 FIX: Protecció contra preus zero/negatius (glitch ticks)
            if signal.hl_price <= 0 or signal.bn_price <= 0:
                logger.warning(f"⚠️ [GUARD] Preus invàlids per {coin}: HL={signal.hl_price}, OKX={signal.bn_price}. Avortant obertura.")
                return
            # Assegurar palanquejament 2x abans d'obrir si no s'ha configurat encara
            if coin not in self._configured_leverage_coins:
                try:
                    await self.hl_client.set_leverage(coin, leverage=int(self.leverage), is_cross=True)
                    await self.aevo_client.set_leverage(coin, leverage=int(self.leverage))
                    self._configured_leverage_coins.add(coin)
                except Exception as le:
                    logger.debug(f"Error ajustant palanquejament previ per {coin}: {le}")

            # 1. Determinació de costats
            if signal.direction == ArbitrageDirection.SELL_HL_BUY_BN:
                hl_is_buy = False
                aevo_is_buy = True
            else:
                hl_is_buy = True
                aevo_is_buy = False

            hl_sz = self.hl_client.round_size(coin, size_usd / signal.hl_price)
            aevo_sz = self.aevo_client.round_size(coin, size_usd / signal.bn_price)

            if self.venue2_name == "OKX":
                # A OKX operem en múltiples discrets de contractes (ctVal).
                # Alineem la mida de Hyperliquid amb la dels contractes OKX per garantir neutralitat delta perfecta.
                hl_sz = self.hl_client.round_size(coin, aevo_sz)

            if hl_sz <= 0 or aevo_sz <= 0:
                logger.warning(
                    f"⚠️ [MIDA INVALIDA] Mida calculada no vàlida per a {coin}: "
                    f"HL={hl_sz}, {self.venue2_name}={aevo_sz}. Cancel·lant ordre."
                )
                return

            # Protecció estricta de límit màxim de capital per ordre de prova
            actual_order_usd = max(hl_sz * signal.hl_price, aevo_sz * signal.bn_price)
            max_allowed_usd = max(size_usd * 1.35, 40.0)
            if actual_order_usd > max_allowed_usd:
                logger.warning(
                    f"⚠️ [MIDA EXCESSIVA] L'ordre mínima de {coin} requeriria {actual_order_usd:.1f}$, "
                    f"que supera el límit configurat ({size_usd:.1f}$, límit segur {max_allowed_usd:.1f}$). "
                    f"Cancel·lant entrada per protegir el capital."
                )
                return

            logger.info(
                f"🚀 [EXECUCIÓ REAL ENVIANT] {coin} {signal.direction.value} | "
                f"HL: {'BUY' if hl_is_buy else 'SELL'} {hl_sz} @ {signal.hl_price} | "
                f"{self.venue2_name}: {'BUY' if aevo_is_buy else 'SELL'} {aevo_sz} @ {signal.bn_price} | "
                f"Mode={'MAKER_FIRST' if effective_maker_first else ('MAKER' if is_maker else 'TAKER_IOC')}"
            )

            # Pre-flight check: assegurar que el spread d'execució actual del llibre encara supera el llindar mínim
            hl_book = self._last_hl_books.get(coin)
            bn_book = self._last_bn_books.get(coin)
            if hl_book and bn_book and hl_book.best_bid and hl_book.best_ask and bn_book.best_bid and bn_book.best_ask:
                mid = (hl_book.mid_price + bn_book.mid_price) / 2.0
                if mid > 0:
                    if signal.direction == ArbitrageDirection.SELL_HL_BUY_BN:
                        curr_exec_spread = ((hl_book.best_bid - bn_book.best_ask) / mid) * 100.0
                    else:
                        curr_exec_spread = ((bn_book.best_bid - hl_book.best_ask) / mid) * 100.0
                    
                    # Per a carry trades, el spread de preus és secundari (entrem per funding APR).
                    # Només requerim que la base no sigui massa adversa (>= -0.05%).
                    is_carry = getattr(signal, "strategy_type", "SPREAD_SCALP") == "FUNDING_CARRY"
                    if is_carry:
                        min_allowed = -0.050
                    else:
                        min_allowed = 0.080 if effective_maker_first else 0.200
                    if curr_exec_spread < min_allowed:
                        logger.warning(
                            f"⚠️ [PRE-FLIGHT REBUTJAT] Spread per a {coin} s'ha reduït a {curr_exec_spread:.3f}% "
                            f"(mínim requerit {min_allowed:.3f}%). Avortant entrada per protegir capital."
                        )
                        return

            # Pre-flight check de venue2: Assegurar que el segon exchange té credencials vàlides abans d'obrir a HL
            if hasattr(self.aevo_client, "is_ready_to_trade"):
                try:
                    res = self.aevo_client.is_ready_to_trade()
                    if isinstance(res, (tuple, list)) and len(res) == 2:
                        ready, reason = res
                        if not ready:
                            logger.warning(
                                f"🛡️ [BLOCAT PER SEGURETAT] {self.venue2_name} no està llest per operar: {reason}. "
                                f"Avortant entrada a Hyperliquid per protegir capital."
                            )
                            return
                except Exception as te:
                    logger.debug(f"Error comprovant is_ready_to_trade: {te}")

            # 2. PAS A: Enviament de la pota primària (Hyperliquid Alo si Maker-First, o IOC si Taker)
            hl_res = None
            fb_res = None
            use_post_only = is_maker or effective_maker_first

            # Determinació del preu d'execució:
            # - En mode Maker (Post-Only): Comprem al BID i venem a l'ASK per descansar al llibre com a Maker (sense creuar).
            #   Afegim 1 tick de marge per evitar rebuig "would have immediately matched".
            # - En mode Taker (IOC): Comprem a l'ASK i venem al BID per omplir immediatament.
            hl_book = self._last_hl_books.get(coin)
            if use_post_only and hl_book and hl_book.best_bid and hl_book.best_ask:
                # Per evitar "Post only order would have immediately matched":
                # SELL: posar 1 tick per sobre del best_ask actual (no creuem el bid)
                # BUY: posar 1 tick per sota del best_bid actual (no creuem l'ask)
                tick_size = (hl_book.best_ask - hl_book.best_bid) * 0.01  # ~1% del spread com a tick buffer
                tick_size = max(tick_size, hl_book.best_bid * 0.00001)    # mínim 0.001%
                if hl_is_buy:
                    hl_order_price = hl_book.best_bid - tick_size
                else:
                    hl_order_price = hl_book.best_ask + tick_size
            else:
                hl_order_price = signal.hl_price

            logger.info(
                f"Enviant pota primària a Hyperliquid per a {coin} "
                f"({'BUY' if hl_is_buy else 'SELL'} @ {hl_order_price}, PostOnly={use_post_only})..."
            )
            try:
                hl_res = await self.hl_client.place_order(
                    coin=coin,
                    is_buy=hl_is_buy,
                    size=hl_sz,
                    price=hl_order_price,
                    post_only=use_post_only,
                    ioc=not use_post_only,
                )
            except Exception as e:
                hl_res = {"status": "err", "error": str(e)}

            hl_ok = False
            hl_fill_type = "TAKER"
            if isinstance(hl_res, dict):
                # 1. Si no és PostOnly i status és "ok" -> fill immediat com a Taker
                # 2. Si té el camp "fill" explícit -> fill immediat
                if hl_res.get("status") == "ok" and (not use_post_only or "fill" in hl_res):
                    hl_ok = True
                    hl_fill_type = "MAKER" if use_post_only else "TAKER"
                elif hl_res.get("status") == "resting" or (use_post_only and hl_res.get("status") == "ok"):
                    # L'ordre Maker està al llibre. Per carry, esperem fins a 15s; per scalp, 3s.
                    is_carry_trade = getattr(signal, "strategy_type", "SPREAD_SCALP") == "FUNDING_CARRY"
                    max_wait_iters = 30 if is_carry_trade else 6  # 30×0.5s=15s, 6×0.5s=3s
                    wait_label = "15s" if is_carry_trade else "3s"
                    oid = hl_res.get("oid")
                    logger.info(f"Ordre Maker {coin} descansant al llibre (OID {oid}). Esperant execució passiva ({wait_label} max)...")
                    for _ in range(max_wait_iters):
                        await asyncio.sleep(0.5)
                        try:
                            st = await self.hl_client.get_account_state()
                            for p in st.get("assetPositions", []):
                                pos = p.get("position", {})
                                p_coin = pos.get("coin", "").upper()
                                if (p_coin == coin or (coin == "PEPE" and p_coin == "KPEPE")) and abs(float(pos.get("szi", 0.0))) > 0:
                                    hl_ok = True
                                    hl_fill_type = "MAKER"
                                    logger.info(f"✅ Ordre Maker {coin} omplerta passivament al llibre! (0.015% Maker fee)")
                                    break
                        except Exception:
                            pass
                        if hl_ok:
                            break

                    if not hl_ok:
                        if oid:
                            logger.info(f"Timeout ordre Maker {coin}. Cancel·lant OID {oid} a cost 0$ gas...")
                            c_res = await self.hl_client.cancel_order(coin, oid)
                            logger.info(f"Cancel·lació OID {oid} executada: {c_res}")
                        try:
                            await self.hl_client.cancel_all_orders(coin=coin)
                        except Exception as ce:
                            logger.debug(f"Neteja d'ordres pendents per {coin}: {ce}")

                        # Comprovació de cursa: si s'ha omplert just abans de la cancel·lació
                        try:
                            st = await self.hl_client.get_account_state()
                            for p in st.get("assetPositions", []):
                                pos = p.get("position", {})
                                p_coin = pos.get("coin", "").upper()
                                if (p_coin == coin or (coin == "PEPE" and p_coin == "KPEPE")) and abs(float(pos.get("szi", 0.0))) > 0:
                                    hl_ok = True
                                    hl_fill_type = "MAKER"
                                    logger.info(f"🎯 Ordre Maker {coin} omplerta just a la cursa de cancel·lació! Procedint amb cobertura.")
                                    break
                        except Exception as se:
                            logger.debug(f"Error verificant estat post-cancel·lació per {coin}: {se}")

                        # Taker Fallback ELIMINAT: era la causa principal de pèrdues.
                        # Quan l'ordre Maker no s'omple en el timeout, cancel·lem a cost $0.
                        # No executem MAI com a Taker agressiu — la fricció (0.26%+) supera
                        # qualsevol spread observable entre exchanges.
                        if not hl_ok:
                            logger.info(f"⏹️ [MAKER NO OMPLERT] {coin}: ordre Maker no omplerta dins del timeout. Cancel·lat a cost $0.")

            # Si Hyperliquid no s'omple o falla, avortem immediatament sense tocar Aevo (0 risc, 0 exposició)
            if not hl_ok:
                logger.warning(
                    f"⚠️ [EXECUCIÓ AVORTADA] Pota Hyperliquid no omplerta per {coin} ({hl_res}). "
                    f"Cancel·lant sense obrir a {self.venue2_name} (0 exposició direccional, 0 comissió pagada)."
                )
                return

            # 3. PAS B: Hyperliquid omplert! Enviament immediat de cobertura a venue2
            logger.info(f"Pota Hyperliquid omplerta ({hl_fill_type}) per a {coin}. Enviant cobertura immediata a {self.venue2_name}...")
            latest_bn = self._last_bn_books.get(coin)
            if latest_bn and latest_bn.best_bid and latest_bn.best_ask:
                base_hedge_px = latest_bn.best_ask if aevo_is_buy else latest_bn.best_bid
            else:
                base_hedge_px = signal.bn_price
            hedge_px = base_hedge_px * 1.003 if aevo_is_buy else base_hedge_px * 0.997

            try:
                extra_kwargs = {}
                import inspect
                if "ioc" in inspect.signature(self.aevo_client.place_order).parameters:
                    extra_kwargs["ioc"] = True

                aevo_res = await self.aevo_client.place_order(
                    coin=coin,
                    is_buy=aevo_is_buy,
                    size=aevo_sz,
                    price=hedge_px,
                    post_only=False,
                    reduce_only=False,
                    **extra_kwargs,
                )
            except Exception as e:
                aevo_res = {"status": "err", "error": str(e)}

            aevo_ok = isinstance(aevo_res, dict) and aevo_res.get("status") == "ok"

            # 4. CAS 1: Ambdues han tingut èxit -> Posició delta-neutral assegurada!
            if aevo_ok:
                # Extreure preus reals d'execució d'entrada retornats per les APIs
                actual_hl_entry_px = signal.hl_price
                effective_hl_res = fb_res if hl_fill_type == "TAKER_FALLBACK" else hl_res
                # Fix: HL client retorna "fill" (no "filled"). Acceptem ambdues claus per robustesa.
                fill_data = None
                if isinstance(effective_hl_res, dict):
                    fill_data = effective_hl_res.get("fill") or effective_hl_res.get("filled")
                if fill_data and isinstance(fill_data, dict):
                    try:
                        actual_hl_entry_px = float(fill_data.get("avgPx", signal.hl_price))
                        logger.info(f"📊 Preu real d'entrada HL per {coin}: {actual_hl_entry_px} (vs senyal: {signal.hl_price})")
                    except (ValueError, TypeError):
                        pass

                actual_bn_entry_px = signal.bn_price
                if isinstance(aevo_res, dict) and "data" in aevo_res:
                    try:
                        actual_bn_entry_px = float(aevo_res["data"].get("avg_price", aevo_res["data"].get("price", signal.bn_price)))
                    except Exception:
                        pass

                # Càlcul del spread real executat d'entrada
                entry_mid = (actual_hl_entry_px + actual_bn_entry_px) / 2.0
                if hl_is_buy:
                    actual_entry_spread = ((actual_bn_entry_px - actual_hl_entry_px) / entry_mid * 100.0) if entry_mid > 0 else signal.spread_pct
                else:
                    actual_entry_spread = ((actual_hl_entry_px - actual_bn_entry_px) / entry_mid * 100.0) if entry_mid > 0 else signal.spread_pct

                hl_fee_rate = self.hl_maker_fee if hl_fill_type == "MAKER" else self.hl_taker_fee
                bn_fee_rate = self.bn_maker_fee if is_maker else self.bn_taker_fee

                actual_hl_usd = hl_sz * actual_hl_entry_px
                actual_bn_usd = aevo_sz * actual_bn_entry_px

                hl_entry_fee = actual_hl_usd * hl_fee_rate
                bn_entry_fee = actual_bn_usd * bn_fee_rate
                self.total_fees_paid += (hl_entry_fee + bn_entry_fee)

                leg_hl = ArbitrageLeg(
                    venue="HYPERLIQUID",
                    coin=coin,
                    side=OrderSide.BUY if hl_is_buy else OrderSide.SELL,
                    entry_price=actual_hl_entry_px,
                    size=hl_sz,
                    size_usd=actual_hl_usd,
                    current_price=actual_hl_entry_px,
                    fee_rate=hl_fee_rate,
                    fees_paid=hl_entry_fee,
                )
                leg_bn = ArbitrageLeg(
                    venue=self.venue2_name,
                    coin=coin,
                    side=OrderSide.BUY if aevo_is_buy else OrderSide.SELL,
                    entry_price=actual_bn_entry_px,
                    size=aevo_sz,
                    size_usd=actual_bn_usd,
                    current_price=actual_bn_entry_px,
                    fee_rate=bn_fee_rate,
                    fees_paid=bn_entry_fee,
                )

                position = ArbitragePosition(
                    pair_id=pair_id,
                    coin=coin,
                    direction=signal.direction,
                    leg_hl=leg_hl,
                    leg_bn=leg_bn,
                    entry_spread_pct=actual_entry_spread,
                    entry_time=time.time(),
                    current_spread_pct=actual_entry_spread,
                )
                position.update_pnl()
                self.active_positions[pair_id] = position

                logger.info(
                    f"✅ [ARB REAL OBERT AMB ÈXIT] {coin} {signal.direction.value} | "
                    f"Spread Real: {actual_entry_spread:+.3f}% (HL: {actual_hl_entry_px:.5g}, {self.venue2_name}: {actual_bn_entry_px:.5g}) | Mida: {actual_hl_usd:.1f}$ x 2"
                )
                self.record_equity_point("OPEN")
                self.save_state()

                if self.on_open_cb:
                    self.on_open_cb(position)

            # 5. CAS 2: ROLLBACK DE SEGURETAT (Anti-Unhedged Guard)
            else:
                logger.error(
                    f"🚨 [ALERTA DE SEGURETAT] Pota de {self.venue2_name} fallada ({aevo_res}) després d'omplir Hyperliquid per {coin}. "
                    f"Fent ROLLBACK IMMEDIAT a Hyperliquid per eliminar exposició direccional..."
                )
                await self.hl_client.market_close(coin=coin, size=hl_sz)
                logger.info(f"🛡️ Rollback Hyperliquid completat per {coin}. Compte pla i protegit.")
                await self.reconcile_active_positions()

        except Exception as e:
            logger.error(f"Error crític en _execute_live_open per {coin}: {e}")
        finally:
            self._pending_opens.discard(coin)

    def close_arbitrage_position(
        self,
        pair_id: str,
        hl_exit_price: float,
        bn_exit_price: float,
        reason: str = "SPREAD_CONVERGED",
        is_maker: bool = False,
    ) -> Optional[ArbitragePosition]:
        """Inicia el tancament atòmic de la posició en segon pla."""
        pos = self.active_positions.get(pair_id)
        if not pos or pos.is_closed or pair_id in self._pending_closes:
            return None

        self._pending_closes.add(pair_id)
        asyncio.create_task(
            self._execute_live_close(pos, hl_exit_price, bn_exit_price, reason, is_maker)
        )
        return pos

    async def _execute_live_close(
        self,
        pos: ArbitragePosition,
        hl_exit_price: float,
        bn_exit_price: float,
        reason: str,
        is_maker: bool,
    ):
        pair_id = pos.pair_id
        coin = pos.coin
        try:
            logger.info(
                f"🔄 [TANCAMENT REAL ENVIANT] {coin} ({reason}) | "
                f"HL px: {hl_exit_price:.4f} | {self.venue2_name} px: {bn_exit_price:.4f}"
            )

            hl_is_buy_to_close = (pos.leg_hl.side == OrderSide.SELL)
            aevo_is_buy_to_close = (pos.leg_bn.side == OrderSide.SELL)

            # Tancament seqüencial: Primer tanquem Hyperliquid (Sempre Taker IOC per evitar ordres resting penjades)
            try:
                # Buffer de seguretat de 0.25% per assegurar execució immediata IOC al millor preu disponible sense rebuig
                agg_hl_exit_px = hl_exit_price * (1.0025 if hl_is_buy_to_close else 0.9975)
                hl_res = await self.hl_client.place_order(
                    coin=coin,
                    is_buy=hl_is_buy_to_close,
                    size=pos.leg_hl.size,
                    price=agg_hl_exit_px,
                    post_only=False,
                    ioc=True,
                )
            except Exception as e:
                hl_res = {"status": "err", "error": str(e)}

            hl_ok = isinstance(hl_res, dict) and hl_res.get("status") == "ok"
            if not hl_ok:
                logger.warning(
                    f"⚠️ Pota de tancament HL per {coin} no omplerta ({hl_res}). "
                    f"Es reintentarà en el proper cicle."
                )
                return

            # Hyperliquid tancat! Ara tanquem venue2 amb reduce_only
            latest_bn = self._last_bn_books.get(coin)
            if latest_bn and latest_bn.best_bid and latest_bn.best_ask:
                base_close_px = latest_bn.best_ask if aevo_is_buy_to_close else latest_bn.best_bid
            else:
                base_close_px = bn_exit_price
            close_px = base_close_px * 1.003 if aevo_is_buy_to_close else base_close_px * 0.997

            try:
                extra_kwargs = {}
                import inspect
                if "ioc" in inspect.signature(self.aevo_client.place_order).parameters:
                    extra_kwargs["ioc"] = True

                aevo_res = await self.aevo_client.place_order(
                    coin=coin,
                    is_buy=aevo_is_buy_to_close,
                    size=pos.leg_bn.size,
                    price=close_px,
                    post_only=False,
                    reduce_only=True,
                    **extra_kwargs,
                )
            except Exception as e:
                aevo_res = {"status": "err", "error": str(e)}

            aevo_ok = isinstance(aevo_res, dict) and aevo_res.get("status") == "ok"
            if not aevo_ok:
                logger.error(f"🚨 Error tancant {self.venue2_name} ({aevo_res}). Forçant market_close a {self.venue2_name}...")
                await self.aevo_client.market_close(coin=coin, size=pos.leg_bn.size)

            # Extreure preus reals d'execució retornats per les APIs
            actual_hl_exit_px = hl_exit_price
            # Fix: HL client retorna "fill" (no "filled"). Acceptem ambdues claus per robustesa.
            fill_data = None
            if isinstance(hl_res, dict):
                fill_data = hl_res.get("fill") or hl_res.get("filled")
            if fill_data and isinstance(fill_data, dict):
                try:
                    actual_hl_exit_px = float(fill_data.get("avgPx", hl_exit_price))
                    logger.info(f"📊 Preu real de sortida HL per {coin}: {actual_hl_exit_px} (vs teòric: {hl_exit_price})")
                except (ValueError, TypeError):
                    pass

            actual_bn_exit_px = bn_exit_price
            if isinstance(aevo_res, dict) and "data" in aevo_res:
                try:
                    actual_bn_exit_px = float(aevo_res["data"].get("avg_price", aevo_res["data"].get("price", bn_exit_price)))
                except Exception:
                    pass

            # Com que Hyperliquid es tanca sempre per IOC (Taker), apliquem hl_taker_fee
            hl_fee_rate = self.hl_taker_fee
            bn_fee_rate = self.bn_maker_fee if is_maker else self.bn_taker_fee

            pos.leg_hl.close(actual_hl_exit_px, exit_fee_rate=hl_fee_rate)
            pos.leg_bn.close(actual_bn_exit_px, exit_fee_rate=bn_fee_rate)

            # Càlcul de comissions de sortida per al total de l'exchange
            hl_exit_fee = pos.leg_hl.size * actual_hl_exit_px * hl_fee_rate
            bn_exit_fee = pos.leg_bn.size * actual_bn_exit_px * bn_fee_rate
            self.total_fees_paid += (hl_exit_fee + bn_exit_fee)

            pos.is_closed = True
            pos.exit_time = time.time()
            pos.exit_reason = reason
            pos.update_pnl()

            if pair_id in self.active_positions:
                del self.active_positions[pair_id]
            self.closed_positions.append(pos)
            # H4 FIX: Limitar llista per evitar memory leak en bot 24/7
            if len(self.closed_positions) > 200:
                self.closed_positions = self.closed_positions[-200:]

            logger.info(
                f"🎉 [ARB REAL TANCAT] {coin} {reason} | PnL Net Realitzat: {pos.realized_pnl:+.3f}$ "
                f"(Funding: {pos.accumulated_funding:+.4f}$, Fees Totals: {pos.total_fees:.4f}$)"
            )
            self.record_equity_point(f"CLOSE_{reason}")
            self.save_state()

            if self.on_close_cb:
                self.on_close_cb(pos, reason, pos.realized_pnl)

            # Sincronitza saldos reals de la blockchain després de tancar
            asyncio.create_task(self.sync_real_balances())

        except Exception as e:
            logger.error(f"Error crític tancant posició real {coin}: {e}")
        finally:
            self._pending_closes.discard(pair_id)

    async def close_all_live_positions(self) -> Dict[str, Any]:
        """Tanca a mercat TOTS els contractes oberts a Hyperliquid i Aevo per deixar els comptes 100% plans."""
        self.trading_paused = True
        v2_key = f"{self.venue2_name.lower()}_closed"
        results = {"hl_closed": [], "aevo_closed": [], v2_key: []}
        try:
            # 0. Cancel·lar qualsevol ordre oberta a Hyperliquid i Venue2 per alliberar marge
            try:
                await self.hl_client.cancel_all_orders()
            except Exception as he:
                logger.debug(f"Error cancel·lant ordres pendents Hyperliquid: {he}")

            try:
                await self.venue2_client.cancel_all_orders()
            except Exception as ce:
                logger.debug(f"Error cancel·lant ordres pendents {self.venue2_name}: {ce}")

            # 1. Tancar Hyperliquid
            hl_state = await self.hl_client.get_account_state()
            if isinstance(hl_state, dict):
                for p in hl_state.get("assetPositions", []):
                    pos = p.get("position", {})
                    coin = pos.get("coin")
                    szi = float(pos.get("szi", 0.0))
                    if szi != 0.0 and coin:
                        logger.info(f"Tancant posició restant a Hyperliquid: {coin} (szi={szi})")
                        res = await self.hl_client.market_close(coin=coin, size=abs(szi))
                        results["hl_closed"].append({"coin": coin, "size": abs(szi), "res": res})

            # 2. Tancar Venue 2 (Aevo / dYdX)
            v2_positions = await self.venue2_client.get_positions()
            if isinstance(v2_positions, list):
                for p in v2_positions:
                    coin = p.get("asset")
                    amount = float(p.get("amount", 0.0))
                    if amount > 0.0 and coin:
                        logger.info(f"Tancant posició restant a {self.venue2_name}: {coin} (amount={amount})")
                        res = await self.venue2_client.market_close(coin=coin, size=amount)
                        closed_item = {"coin": coin, "size": amount, "res": res}
                        results["aevo_closed"].append(closed_item)
                        if v2_key != "aevo_closed":
                            results[v2_key].append(closed_item)

            self.active_positions.clear()
            self._open_broker_coins.clear()
            self.save_state()
            await self.sync_real_balances()
            results["status"] = "ok"
        except Exception as e:
            logger.error(f"Error en close_all_live_positions: {e}")
            results["status"] = "err"
            results["error"] = str(e)
        return results

    @property
    def net_pnl(self) -> float:
        """En mode live, les API dels brokers (HL accountValue, OKX equity)
        ja inclouen l'unrealized PnL dins l'equity del compte.
        No cal sumar total_unrealized_pnl de nou (evitem double-counting)."""
        return self.total_balance_usd - self.initial_total_balance

    @property
    def metrics(self) -> dict:
        m = super().metrics
        m["execution_mode"] = "live"
        return m
