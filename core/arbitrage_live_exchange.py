"""Motor d'execució d'arbitratge real (Hyperliquid + Aevo) amb protecció Anti-Unhedged."""

import asyncio
import logging
import time
from typing import Callable, Dict, List, Optional
from core.arbitrage_models import ArbitrageDirection, ArbitrageLeg, ArbitragePosition, ArbitrageSignal
from core.arbitrage_paper_exchange import ArbitragePaperExchange
from core.hyperliquid_live_client import HyperliquidLiveClient
from core.aevo_live_client import AevoLiveClient
from core.models import OrderSide

logger = logging.getLogger("ArbitrageLiveExchange")

class ArbitrageLiveExchange(ArbitragePaperExchange):
    def __init__(
        self,
        hl_client: HyperliquidLiveClient,
        aevo_client: AevoLiveClient,
        initial_hl_balance: float = 500.0,
        initial_bn_balance: float = 500.0,
        leverage: float = 2.0,
        on_open_cb: Optional[Callable[[ArbitragePosition], None]] = None,
        on_close_cb: Optional[Callable[[ArbitragePosition, str, float], None]] = None,
        state_file: str = "live_state.json",
    ):
        super().__init__(
            initial_hl_balance=initial_hl_balance,
            initial_bn_balance=initial_bn_balance,
            venue2_name="AEVO",
            leverage=leverage,
            on_open_cb=on_open_cb,
            on_close_cb=on_close_cb,
            state_file=state_file,
        )
        self.hl_client = hl_client
        self.aevo_client = aevo_client
        self._pending_opens: set = set()
        self._pending_closes: set = set()

    async def reconcile_active_positions(self):
        """Si tant a Hyperliquid com a Aevo no hi ha cap posició oberta a nivell de compte, sincronitza l'estat intern."""
        try:
            hl_state_task = asyncio.create_task(self.hl_client.get_account_state())
            aevo_state_task = asyncio.create_task(self.aevo_client.get_account_state())
            hl_state, aevo_state = await asyncio.gather(hl_state_task, aevo_state_task, return_exceptions=True)

            hl_has_pos = False
            if isinstance(hl_state, dict):
                for p in hl_state.get("assetPositions", []):
                    if float(p.get("position", {}).get("szi", 0.0)) != 0.0:
                        hl_has_pos = True
                        break

            aevo_has_pos = False
            if isinstance(aevo_state, dict):
                for p in aevo_state.get("positions", []):
                    if float(p.get("amount", 0.0)) != 0.0:
                        aevo_has_pos = True
                        break

            if not hl_has_pos and not aevo_has_pos and self.active_positions:
                logger.info("Reconciliació de posicions: ni Hyperliquid ni Aevo tenen posicions obertes. Netejant active_positions internes.")
                self.active_positions.clear()
                self.save_state()
        except Exception as e:
            logger.debug(f"Error en reconcile_active_positions: {e}")

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

    def open_arbitrage_position(
        self,
        signal: ArbitrageSignal,
        size_usd: float = 250.0,
        is_maker: bool = False,
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

        self._pending_opens.add(signal.coin)
        asyncio.create_task(self._execute_live_open(signal, size_usd, is_maker))
        return None

    async def _execute_live_open(
        self,
        signal: ArbitrageSignal,
        size_usd: float,
        is_maker: bool,
    ):
        coin = signal.coin
        pair_id = f"ARB_{coin}_LIVE_{int(time.time() * 1000)}"
        try:
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
                f"Aevo: {'BUY' if aevo_is_buy else 'SELL'} {aevo_sz} @ {signal.bn_price}"
            )

            # 2. Enviament simultani de les dues potes en paral·lel
            hl_task = asyncio.create_task(
                self.hl_client.place_order(
                    coin=coin,
                    is_buy=hl_is_buy,
                    size=hl_sz,
                    price=signal.hl_price,
                    post_only=is_maker,
                    ioc=not is_maker,
                )
            )
            aevo_task = asyncio.create_task(
                self.aevo_client.place_order(
                    coin=coin,
                    is_buy=aevo_is_buy,
                    size=aevo_sz,
                    price=signal.bn_price,
                    post_only=is_maker,
                )
            )

            hl_res, aevo_res = await asyncio.gather(hl_task, aevo_task, return_exceptions=True)

            # 3. Comprovació d'èxit de les dues potes
            hl_ok = isinstance(hl_res, dict) and hl_res.get("status") == "ok"
            aevo_ok = isinstance(aevo_res, dict) and aevo_res.get("status") == "ok"

            # 4. CAS 1: Ambdues han tingut èxit -> Posició delta-neutral assegurada!
            if hl_ok and aevo_ok:
                hl_fee_rate = self.hl_maker_fee if is_maker else self.hl_taker_fee
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
                    venue="AEVO",
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
            elif hl_ok and not aevo_ok:
                logger.error(
                    f"🚨 [ALERTA DE SEGURETAT] Pota d'Aevo fallada ({aevo_res}) per {coin}. "
                    f"Fent ROLLBACK IMMEDIAT a Hyperliquid per eliminar exposició direccional..."
                )
                await self.hl_client.market_close(coin=coin, size=hl_sz)
                logger.info(f"🛡️ Rollback Hyperliquid completat per {coin}. Compte pla i protegit.")

            elif aevo_ok and not hl_ok:
                logger.error(
                    f"🚨 [ALERTA DE SEGURETAT] Pota d'Hyperliquid fallada ({hl_res}) per {coin}. "
                    f"Fent ROLLBACK IMMEDIAT a Aevo per eliminar exposició direccional..."
                )
                await self.aevo_client.market_close(coin=coin, size=aevo_sz)
                logger.info(f"🛡️ Rollback Aevo completat per {coin}. Compte pla i protegit.")

            else:
                logger.warning(f"❌ Ambdues potes han fallat per {coin}: HL={hl_res}, Aevo={aevo_res}")

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

            # Enviament en paral·lel dels tancaments
            # Per tancar HL: venem si estàvem Buy, comprem si estàvem Sell
            hl_is_buy_to_close = (pos.leg_hl.side == OrderSide.SELL)
            aevo_is_buy_to_close = (pos.leg_bn.side == OrderSide.SELL)

            hl_task = asyncio.create_task(
                self.hl_client.place_order(
                    coin=coin,
                    is_buy=hl_is_buy_to_close,
                    size=pos.leg_hl.size,
                    price=hl_exit_price,
                    post_only=is_maker,
                    ioc=not is_maker,
                )
            )
            aevo_task = asyncio.create_task(
                self.aevo_client.place_order(
                    coin=coin,
                    is_buy=aevo_is_buy_to_close,
                    size=pos.leg_bn.size,
                    price=bn_exit_price,
                    post_only=is_maker,
                    reduce_only=True,
                )
            )

            hl_res, aevo_res = await asyncio.gather(hl_task, aevo_task, return_exceptions=True)

            # Actualització del registre local
            hl_fee_rate = self.hl_maker_fee if is_maker else self.hl_taker_fee
            bn_fee_rate = self.bn_maker_fee if is_maker else self.bn_taker_fee

            pos.leg_hl.close(hl_exit_price, exit_fee_rate=hl_fee_rate)
            pos.leg_bn.close(bn_exit_price, exit_fee_rate=bn_fee_rate)

            # Càlcul de PnL
            if pos.leg_hl.side == OrderSide.BUY:
                hl_gross = (hl_exit_price - pos.leg_hl.entry_price) * pos.leg_hl.size
            else:
                hl_gross = (pos.leg_hl.entry_price - hl_exit_price) * pos.leg_hl.size
            hl_exit_fee = pos.leg_hl.size * hl_exit_price * hl_fee_rate

            if pos.leg_bn.side == OrderSide.BUY:
                bn_gross = (bn_exit_price - pos.leg_bn.entry_price) * pos.leg_bn.size
            else:
                bn_gross = (pos.leg_bn.entry_price - bn_exit_price) * pos.leg_bn.size
            bn_exit_fee = pos.leg_bn.size * bn_exit_price * bn_fee_rate

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
