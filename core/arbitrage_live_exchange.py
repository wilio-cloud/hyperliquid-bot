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
        leverage: float = 2.0,
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

            hl_positions_map = {}
            if isinstance(hl_state, dict):
                for p in hl_state.get("assetPositions", []):
                    pos = p.get("position", {})
                    szi = float(pos.get("szi", 0.0))
                    c = pos.get("coin", "").upper()
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

            for coin in self._open_broker_coins:
                if coin not in existing_active_coins:
                    hl_pos = hl_positions_map.get(coin)
                    aevo_pos = aevo_positions_map.get(coin)
                    if hl_pos and aevo_pos:
                        szi = float(hl_pos.get("szi", 0.0))
                        hl_entry_px = float(hl_pos.get("entryPx", 0.0))
                        aevo_amount = float(aevo_pos.get("amount", 0.0))
                        aevo_side = aevo_pos.get("side", "").lower()
                        aevo_entry_px = float(aevo_pos.get("avg_entry_price", 0.0))

                        if szi < 0 and aevo_side == "buy":
                            direction = ArbitrageDirection.SELL_HL_BUY_BN
                            hl_side = OrderSide.SELL
                            bn_side = OrderSide.BUY
                        else:
                            direction = ArbitrageDirection.BUY_HL_SELL_BN
                            hl_side = OrderSide.BUY
                            bn_side = OrderSide.SELL

                        hl_sz = abs(szi)
                        bn_sz = abs(aevo_amount)
                        pair_id = f"arb_{coin}_reconciled_{int(time.time())}"

                        mid = (hl_entry_px + aevo_entry_px) / 2.0 if (hl_entry_px + aevo_entry_px) > 0 else 1.0
                        entry_spread = abs(hl_entry_px - aevo_entry_px) / mid * 100.0

                        leg_hl = ArbitrageLeg(
                            venue="HYPERLIQUID",
                            coin=coin,
                            side=hl_side,
                            entry_price=hl_entry_px,
                            size=hl_sz,
                            size_usd=hl_sz * hl_entry_px,
                            current_price=hl_entry_px,
                            fee_rate=self.hl_taker_fee,
                            fees_paid=hl_sz * hl_entry_px * self.hl_taker_fee,
                        )
                        leg_bn = ArbitrageLeg(
                            venue=self.venue2_name,
                            coin=coin,
                            side=bn_side,
                            entry_price=aevo_entry_px,
                            size=bn_sz,
                            size_usd=bn_sz * aevo_entry_px,
                            current_price=aevo_entry_px,
                            fee_rate=self.bn_taker_fee,
                            fees_paid=bn_sz * aevo_entry_px * self.bn_taker_fee,
                        )
                        pos_obj = ArbitragePosition(
                            pair_id=pair_id,
                            coin=coin,
                            direction=direction,
                            leg_hl=leg_hl,
                            leg_bn=leg_bn,
                            entry_spread_pct=entry_spread,
                            entry_time=time.time(),
                        )
                        self.active_positions[pair_id] = pos_obj
                        logger.info(f"✅ Reconciliada posició activa existent per a {coin} a active_positions ({pair_id})")

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

            if not self.closed_positions and not self.active_positions:
                self.initial_total_balance = self.total_balance_usd

            logger.info(
                f"Saldos reals sincronitzats: Hyperliquid = {self.hl_balance_usd:.2f}$ | "
                f"Aevo = {self.bn_balance_usd:.2f}$ | Total = {self.total_balance_usd:.2f}$"
            )
            await self.reconcile_active_positions()
            self.save_state()
        except Exception as e:
            logger.error(f"Error sincronitzant saldos reals: {e}")

    async def configure_all_leverage(self, leverage: Optional[int] = None) -> Dict[str, Any]:
        """Configura el palanquejament desitjat (ex: 2x) i Cross Margin a Hyperliquid i Aevo per a tots els mercats."""
        lev = int(leverage or self.leverage or 2)
        results = {"status": "ok", "leverage": lev, "hl": {}, "aevo": {}}
        coins = ["SOL", "HYPE", "NEAR", "PUMP", "SUI", "ZEC"]
        logger.info(f"⚡ [LEVERAGE] Aplicant {lev}x Cross Margin a Hyperliquid i Aevo per a {coins}...")

        async def _configure_coin(c):
            try:
                hl_res = await self.hl_client.set_leverage(c, leverage=lev, is_cross=True)
            except Exception as e:
                hl_res = {"status": "err", "error": str(e)}
            try:
                aevo_res = await self.aevo_client.set_leverage(c, leverage=lev)
            except Exception as e:
                aevo_res = {"status": "err", "error": str(e)}
            return c, hl_res, aevo_res

        outcomes = await asyncio.gather(*[_configure_coin(c) for c in coins], return_exceptions=True)
        for out in outcomes:
            if isinstance(out, tuple) and len(out) == 3:
                c, hl_res, aevo_res = out
                results["hl"][c] = hl_res
                results["aevo"][c] = aevo_res

        self._configured_leverage_coins.update(coins)
        logger.info(f"⚡ [LEVERAGE] Resultats de configuració ({lev}x): {results}")
        return results

    def open_arbitrage_position(
        self,
        signal: ArbitrageSignal,
        size_usd: float = 250.0,
        is_maker: bool = False,
        maker_first: Optional[bool] = None,
    ) -> Optional[ArbitragePosition]:
        """Inicia l'execució atòmica d'obertura en segon pla."""
        if self.has_open_position(signal.coin) or signal.coin in self._pending_opens:
            logger.debug(f"Ja hi ha posició o execució en curs per {signal.coin}.")
            return None

        # Comprovació de capital disponible
        required_margin = size_usd / self.leverage
        if self.hl_balance_usd < required_margin or self.bn_balance_usd < required_margin:
            logger.warning(
                f"Capital insuficient per obrir {signal.coin}: requerit {required_margin:.1f}$, "
                f"HL={self.hl_balance_usd:.1f}$, Aevo={self.bn_balance_usd:.1f}$"
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

            logger.info(
                f"🚀 [EXECUCIÓ REAL ENVIANT] {coin} {signal.direction.value} | "
                f"HL: {'BUY' if hl_is_buy else 'SELL'} {hl_sz} @ {signal.hl_price} | "
                f"Aevo: {'BUY' if aevo_is_buy else 'SELL'} {aevo_sz} @ {signal.bn_price} | "
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
                    
                    # Amb Maker-First el hurdle rate pot ser de 0.09% en comptes de 0.20%
                    min_allowed = 0.080 if effective_maker_first else 0.200
                    if curr_exec_spread < min_allowed:
                        logger.warning(
                            f"⚠️ [PRE-FLIGHT REBUTJAT] Spread per a {coin} s'ha reduït a {curr_exec_spread:.3f}% "
                            f"(mínim requerit {min_allowed:.3f}%). Avortant entrada per protegir capital."
                        )
                        return

            # 2. PAS A: Enviament de la pota primària (Hyperliquid Alo si Maker-First, o IOC si Taker)
            use_post_only = is_maker or effective_maker_first
            logger.info(f"Enviant pota primària a Hyperliquid per a {coin} (PostOnly={use_post_only})...")
            try:
                hl_res = await self.hl_client.place_order(
                    coin=coin,
                    is_buy=hl_is_buy,
                    size=hl_sz,
                    price=signal.hl_price,
                    post_only=use_post_only,
                    ioc=not use_post_only,
                )
            except Exception as e:
                hl_res = {"status": "err", "error": str(e)}

            hl_ok = False
            hl_fill_type = "TAKER"
            if isinstance(hl_res, dict):
                if hl_res.get("status") == "ok":
                    hl_ok = True
                    hl_fill_type = "MAKER" if use_post_only else "TAKER"
                elif hl_res.get("status") == "resting":
                    # L'ordre Maker està al llibre. Esperem fins a 3 segons que s'ompli passivament
                    oid = hl_res.get("oid")
                    logger.info(f"Ordre Maker {coin} descansant al llibre (OID {oid}). Esperant execució passiva (3s max)...")
                    for _ in range(6):
                        await asyncio.sleep(0.5)
                        try:
                            st = await self.hl_client.get_account_state()
                            for p in st.get("assetPositions", []):
                                pos = p.get("position", {})
                                if pos.get("coin", "").upper() == coin and abs(float(pos.get("szi", 0.0))) > 0:
                                    hl_ok = True
                                    hl_fill_type = "MAKER"
                                    logger.info(f"✅ Ordre Maker {coin} omplerta passivament al llibre! (0.015% Maker fee)")
                                    break
                        except Exception:
                            pass
                        if hl_ok:
                            break

                    if not hl_ok and oid:
                        logger.info(f"Timeout ordre Maker {coin}. Cancel·lant OID {oid} a cost 0$ gas...")
                        await self.hl_client.cancel_order(coin, oid)

            # Si Hyperliquid no s'omple o falla, avortem immediatament sense tocar Aevo (0 risc, 0 exposició)
            if not hl_ok:
                logger.warning(
                    f"⚠️ [EXECUCIÓ AVORTADA] Pota Hyperliquid no omplerta per {coin} ({hl_res}). "
                    f"Cancel·lant sense obrir a Aevo (0 exposició direccional, 0 comissió pagada)."
                )
                return

            # 3. PAS B: Hyperliquid omplert! Enviament immediat de cobertura a Aevo
            logger.info(f"Pota Hyperliquid omplerta ({hl_fill_type}) per a {coin}. Enviant cobertura immediata a Aevo...")
            try:
                aevo_res = await self.aevo_client.place_order(
                    coin=coin,
                    is_buy=aevo_is_buy,
                    size=aevo_sz,
                    price=signal.bn_price,
                    post_only=False,
                    reduce_only=False,
                )
            except Exception as e:
                aevo_res = {"status": "err", "error": str(e)}

            aevo_ok = isinstance(aevo_res, dict) and aevo_res.get("status") == "ok"

            # 4. CAS 1: Ambdues han tingut èxit -> Posició delta-neutral assegurada!
            if aevo_ok:
                hl_fee_rate = self.hl_maker_fee if hl_fill_type == "MAKER" else self.hl_taker_fee
                bn_fee_rate = self.bn_maker_fee if is_maker else self.bn_taker_fee

                hl_entry_fee = size_usd * hl_fee_rate
                bn_entry_fee = size_usd * bn_fee_rate
                self.total_fees_paid += (hl_entry_fee + bn_entry_fee)

                leg_hl = ArbitrageLeg(
                    venue="HYPERLIQUID",
                    coin=coin,
                    side=OrderSide.BUY if hl_is_buy else OrderSide.SELL,
                    entry_price=signal.hl_price,
                    size=hl_sz,
                    size_usd=size_usd,
                    current_price=signal.hl_price,
                    fee_rate=hl_fee_rate,
                    fees_paid=hl_entry_fee,
                )
                leg_bn = ArbitrageLeg(
                    venue=self.venue2_name,
                    coin=coin,
                    side=OrderSide.BUY if aevo_is_buy else OrderSide.SELL,
                    entry_price=signal.bn_price,
                    size=aevo_sz,
                    size_usd=size_usd,
                    current_price=signal.bn_price,
                    fee_rate=bn_fee_rate,
                    fees_paid=bn_entry_fee,
                )

                position = ArbitragePosition(
                    pair_id=pair_id,
                    coin=coin,
                    direction=signal.direction,
                    leg_hl=leg_hl,
                    leg_bn=leg_bn,
                    entry_spread_pct=signal.spread_pct,
                    entry_time=time.time(),
                    current_spread_pct=signal.spread_pct,
                )
                position.update_pnl()
                self.active_positions[pair_id] = position

                logger.info(
                    f"✅ [ARB REAL OBERT AMB ÈXIT] {coin} {signal.direction.value} | "
                    f"Spread: {signal.spread_pct:+.3f}% | Mida: {size_usd:.1f}$ x 2"
                )
                self.record_equity_point("OPEN")
                self.save_state()

                if self.on_open_cb:
                    self.on_open_cb(position)

            # 5. CAS 2: ROLLBACK DE SEGURETAT (Anti-Unhedged Guard)
            else:
                logger.error(
                    f"🚨 [ALERTA DE SEGURETAT] Pota d'Aevo fallada ({aevo_res}) després d'omplir Hyperliquid per {coin}. "
                    f"Fent ROLLBACK IMMEDIAT a Hyperliquid per eliminar exposició direccional..."
                )
                await self.hl_client.market_close(coin=coin, size=hl_sz)
                logger.info(f"🛡️ Rollback Hyperliquid completat per {coin}. Compte pla i protegit.")

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
                f"HL px: {hl_exit_price:.4f} | Aevo px: {bn_exit_price:.4f}"
            )

            hl_is_buy_to_close = (pos.leg_hl.side == OrderSide.SELL)
            aevo_is_buy_to_close = (pos.leg_bn.side == OrderSide.SELL)

            # Tancament seqüencial: Primer tanquem Hyperliquid (Sempre Taker IOC per evitar ordres resting penjades)
            try:
                hl_res = await self.hl_client.place_order(
                    coin=coin,
                    is_buy=hl_is_buy_to_close,
                    size=pos.leg_hl.size,
                    price=hl_exit_price,
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

            # Hyperliquid tancat! Ara tanquem Aevo amb reduce_only
            try:
                aevo_res = await self.aevo_client.place_order(
                    coin=coin,
                    is_buy=aevo_is_buy_to_close,
                    size=pos.leg_bn.size,
                    price=bn_exit_price,
                    post_only=False,
                    reduce_only=True,
                )
            except Exception as e:
                aevo_res = {"status": "err", "error": str(e)}

            aevo_ok = isinstance(aevo_res, dict) and aevo_res.get("status") == "ok"
            if not aevo_ok:
                logger.error(f"🚨 Error tancant Aevo ({aevo_res}). Forçant market_close a Aevo...")
                await self.aevo_client.market_close(coin=coin, size=pos.leg_bn.size)

            # Extreure preus reals d'execució retornats per les APIs
            actual_hl_exit_px = hl_exit_price
            if isinstance(hl_res, dict) and "filled" in hl_res:
                try:
                    actual_hl_exit_px = float(hl_res["filled"].get("avgPx", hl_exit_price))
                except Exception:
                    pass

            actual_bn_exit_px = bn_exit_price
            if isinstance(aevo_res, dict) and "data" in aevo_res:
                try:
                    actual_bn_exit_px = float(aevo_res["data"].get("avg_price", bn_exit_price))
                except Exception:
                    pass

            # Com que Hyperliquid es tanca sempre per IOC (Taker), apliquem hl_taker_fee
            hl_fee_rate = self.hl_taker_fee
            bn_fee_rate = self.bn_maker_fee if is_maker else self.bn_taker_fee

            pos.leg_hl.close(actual_hl_exit_px, exit_fee_rate=hl_fee_rate)
            pos.leg_bn.close(actual_bn_exit_px, exit_fee_rate=bn_fee_rate)

            # Càlcul de PnL amb preus i comissions 100% reals
            if pos.leg_hl.side == OrderSide.BUY:
                hl_gross = (actual_hl_exit_px - pos.leg_hl.entry_price) * pos.leg_hl.size
            else:
                hl_gross = (pos.leg_hl.entry_price - actual_hl_exit_px) * pos.leg_hl.size
            hl_exit_fee = pos.leg_hl.size * actual_hl_exit_px * hl_fee_rate

            if pos.leg_bn.side == OrderSide.BUY:
                bn_gross = (actual_bn_exit_px - pos.leg_bn.entry_price) * pos.leg_bn.size
            else:
                bn_gross = (pos.leg_bn.entry_price - actual_bn_exit_px) * pos.leg_bn.size
            bn_exit_fee = pos.leg_bn.size * actual_bn_exit_px * bn_fee_rate

            self.total_fees_paid += (hl_exit_fee + bn_exit_fee)
            pos.is_closed = True
            pos.exit_time = time.time()
            pos.exit_reason = reason
            pos.realized_pnl = (hl_gross - hl_exit_fee) + (bn_gross - bn_exit_fee) + pos.accumulated_funding

            if pair_id in self.active_positions:
                del self.active_positions[pair_id]
            self.closed_positions.append(pos)

            logger.info(
                f"🎉 [ARB REAL TANCAT] {coin} {reason} | PnL Net Realitzat: {pos.realized_pnl:+.3f}$"
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
    def metrics(self) -> dict:
        m = super().metrics
        m["execution_mode"] = "live"
        return m
