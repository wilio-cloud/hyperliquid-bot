"""Motor de Paper Trading realista per a Hyperliquid."""

import logging
import time
import uuid
from typing import Callable, Dict, List, Optional
from config.settings import config
from core.models import (
    BookLevel,
    Order,
    OrderBookL2,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Trade,
)

logger = logging.getLogger("PaperExchange")

class SimulatedOrderQueue:
    """Modela la cua del llibre d'ordres per no assumir fills instantanis irreals."""
    def __init__(self, order: Order, queue_ahead: float):
        self.order = order
        self.queue_ahead = queue_ahead  # Volum que hi havia abans que nosaltres

class PaperExchange:
    def __init__(
        self,
        initial_balance: float = config.initial_balance_usd,
        on_fill_cb: Optional[Callable] = None,
        on_close_cb: Optional[Callable] = None,
    ):
        self.balance_usd: float = initial_balance
        self.initial_balance: float = initial_balance
        self.on_fill_cb = on_fill_cb
        self.on_close_cb = on_close_cb
        
        # Ordres pendents (en cua límit)
        self.open_orders: Dict[str, SimulatedOrderQueue] = {}
        
        # Posicions actives: coin -> Position
        self.positions: Dict[str, Position] = {}
        
        # Historial de posicions tancades
        self.closed_positions: List[Position] = []
        
        # Estadístiques
        self.total_fees_paid: float = 0.0
        self.total_maker_orders: int = 0
        self.total_taker_orders: int = 0

    def get_position(self, coin: str) -> Optional[Position]:
        return self.positions.get(coin)

    def has_open_orders(self, coin: str) -> bool:
        """Indica si ja hi ha alguna ordre pendent per a aquesta moneda."""
        return any(q.order.coin == coin for q in self.open_orders.values())

    def clean_stale_orders(self, max_age_seconds: float = 30.0):
        """Cancel·la ordres límit velles que han quedat enrere respecte al preu."""
        now = time.time()
        stale_ids = [oid for oid, q in self.open_orders.items() if (now - q.order.created_at) > max_age_seconds]
        for oid in stale_ids:
            self.cancel_order(oid)

    def place_order(
        self,
        coin: str,
        side: OrderSide,
        price: float,
        size_usd: float,
        order_type: OrderType = OrderType.LIMIT,
        post_only: bool = True,
        strategy_name: str = "",
        current_book: Optional[OrderBookL2] = None,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
    ) -> Optional[Order]:
        """Envia una ordre simulada."""
        if price <= 0 or size_usd <= 0:
            return None

        # Guard: No permetre obrir si ja hi ha una posició oberta o una ordre pendent per aquesta moneda
        if coin in self.positions:
            logger.debug(f"[REJECTED] Ja hi ha una posició oberta per {coin}")
            return None

        if self.has_open_orders(coin):
            logger.debug(f"[REJECTED] Ja hi ha una ordre pendent a la cua per {coin}")
            return None

        size_coins = size_usd / price

        order = Order(
            order_id=str(uuid.uuid4())[:8],
            coin=coin,
            side=side,
            order_type=order_type,
            price=price,
            size=size_coins,
            post_only=post_only,
            status=OrderStatus.OPEN,
            created_at=time.time(),
            strategy_name=strategy_name,
            take_profit=take_profit,
            stop_loss=stop_loss,
        )

        if order_type == OrderType.LIMIT:
            # Validació Post-Only
            if post_only and current_book:
                if side == OrderSide.BUY and current_book.best_ask and price >= current_book.best_ask:
                    # En creuar el spread, una ordre post-only és rebutjada per l'exchange
                    order.status = OrderStatus.REJECTED
                    logger.debug(f"[REJECTED] Ordre Post-Only {coin} BUY {price} >= BestAsk {current_book.best_ask}")
                    return order
                elif side == OrderSide.SELL and current_book.best_bid and price <= current_book.best_bid:
                    order.status = OrderStatus.REJECTED
                    logger.debug(f"[REJECTED] Ordre Post-Only {coin} SELL {price} <= BestBid {current_book.best_bid}")
                    return order

            # Càlcul del volum que tenim per davant a la cua
            queue_ahead = 0.0
            if current_book:
                levels = current_book.bids if side == OrderSide.BUY else current_book.asks
                for lvl in levels:
                    if (side == OrderSide.BUY and lvl.price >= price) or (side == OrderSide.SELL and lvl.price <= price):
                        queue_ahead += lvl.size

            self.open_orders[order.order_id] = SimulatedOrderQueue(order, queue_ahead)
            logger.info(
                f"[OPEN ORDER] {order.side.value} {order.coin} @ {order.price:.2f} "
                f"({size_usd:.1f}$) [Cua per davant: {queue_ahead:.4f}] - {strategy_name}"
            )
            return order

        elif order_type == OrderType.MARKET:
            # Ordre Market: Execució immediata (Taker)
            exec_price = price
            if current_book:
                exec_price = current_book.best_ask if side == OrderSide.BUY else current_book.best_bid or price
            self._fill_order(order, exec_price=exec_price, is_maker=False)
            return order

        return None

    def cancel_order(self, order_id: str):
        """Cancel·la una ordre oberta."""
        if order_id in self.open_orders:
            order_q = self.open_orders.pop(order_id)
            order_q.order.status = OrderStatus.CANCELLED
            logger.debug(f"[CANCEL] Ordre {order_id} cancel·lada.")

    def cancel_all_for_coin(self, coin: str):
        to_cancel = [oid for oid, q in self.open_orders.items() if q.order.coin == coin]
        for oid in to_cancel:
            self.cancel_order(oid)

    def on_trade(self, trade: Trade):
        """Consumeix els trades de mercat per avançar la cua d'ordres pendents."""
        filled_ids = []
        for order_id, queue_item in list(self.open_orders.items()):
            order = queue_item.order
            if order.coin != trade.coin:
                continue

            # Per una compra límit: els trades de venda a mercat (side == SELL) van menjant la cua
            if order.side == OrderSide.BUY:
                if trade.price <= order.price:
                    queue_item.queue_ahead -= trade.size
                    if queue_item.queue_ahead <= 0:
                        self._fill_order(order, exec_price=order.price, is_maker=True)
                        filled_ids.append(order_id)
            # Per una venda límit: els trades de compra a mercat (side == BUY) van menjant la cua
            elif order.side == OrderSide.SELL:
                if trade.price >= order.price:
                    queue_item.queue_ahead -= trade.size
                    if queue_item.queue_ahead <= 0:
                        self._fill_order(order, exec_price=order.price, is_maker=True)
                        filled_ids.append(order_id)

        for oid in filled_ids:
            if oid in self.open_orders:
                del self.open_orders[oid]

    def on_book_update(self, book: OrderBookL2):
        """Actualitza el preu actual de les posicions i avalua Take Profit / Stop Loss."""
        self.clean_stale_orders()
        mid = book.mid_price
        if not mid or book.coin not in self.positions:
            return

        pos = self.positions[book.coin]
        pos.current_price = mid

        # Càlcul PnL no realitzat
        if pos.side == OrderSide.BUY:
            pnl_pct = (mid - pos.entry_price) / pos.entry_price
        else:
            pnl_pct = (pos.entry_price - mid) / pos.entry_price

        pos.unrealized_pnl_pct = pnl_pct * 100.0
        pos.unrealized_pnl = pos.size_usd * pnl_pct

        # Comprovació de Take Profit (amb ordre Maker límit idealment)
        if (pos.side == OrderSide.BUY and mid >= pos.take_profit) or \
           (pos.side == OrderSide.SELL and mid <= pos.take_profit):
            self.close_position(
                coin=pos.coin,
                exit_price=pos.take_profit,
                exit_reason="TAKE_PROFIT",
                is_maker=True,
            )

        # Comprovació de Stop Loss (ordre a mercat Taker d'emergència)
        elif (pos.side == OrderSide.BUY and mid <= pos.stop_loss) or \
             (pos.side == OrderSide.SELL and mid >= pos.stop_loss):
            self.close_position(
                coin=pos.coin,
                exit_price=mid,
                exit_reason="STOP_LOSS",
                is_maker=False,  # Emergència: taker fee + possible slippage
            )

    def _fill_order(self, order: Order, exec_price: float, is_maker: bool):
        """Executa l'ordre i obre la posició."""
        if order.coin in self.positions:
            logger.warning(f"[FILL IGNORED] Posició ja existent per {order.coin}. Fill duplicat descartat.")
            return

        order.status = OrderStatus.FILLED
        order.filled_at = time.time()
        order_value = order.size * exec_price
        
        fee_rate = config.maker_fee_rate if is_maker else config.taker_fee_rate
        fee = order_value * fee_rate
        order.fee_paid = fee
        self.total_fees_paid += fee

        if is_maker:
            self.total_maker_orders += 1
        else:
            self.total_taker_orders += 1

        # Càlcul de Take Profit i Stop Loss calibrat respecte al preu d'execució real
        tp_pct = config.default_take_profit_pct
        sl_pct = config.default_stop_loss_pct
        tp_price = exec_price * (1.0 + tp_pct) if order.side == OrderSide.BUY else exec_price * (1.0 - tp_pct)
        sl_price = exec_price * (1.0 - sl_pct) if order.side == OrderSide.BUY else exec_price * (1.0 + sl_pct)

        pos = Position(
            position_id=str(uuid.uuid4())[:8],
            coin=order.coin,
            side=order.side,
            entry_price=exec_price,
            size=order.size,
            size_usd=order_value,
            take_profit=tp_price,
            stop_loss=sl_price,
            strategy_name=order.strategy_name,
            current_price=exec_price,
            fees_paid=fee,
        )
        self.positions[order.coin] = pos

        # Cancel·la immediatament qualsevol altra ordre pendent per a aquesta moneda
        self.cancel_all_for_coin(order.coin)

        logger.info(
            f"[FILLED ENTRY] {order.coin} {order.side.value} @ {exec_price:.2f} "
            f"(TP: {tp_price:.2f}, SL: {sl_price:.2f}, Fee: {fee:.4f}$) [{order.strategy_name}]"
        )
        if self.on_fill_cb:
            self.on_fill_cb(pos, order, is_maker)

    def close_position(
        self,
        coin: str,
        exit_price: float,
        exit_reason: str,
        is_maker: bool = True,
    ):
        """Tanca una posició oberta i calcula el PnL net definitiu."""
        if coin not in self.positions:
            return

        pos = self.positions.pop(coin)
        pos.is_closed = True
        pos.exit_price = exit_price
        pos.exit_time = time.time()
        pos.exit_reason = exit_reason

        exit_value = pos.size * exit_price
        fee_rate = config.maker_fee_rate if is_maker else config.taker_fee_rate
        exit_fee = exit_value * fee_rate
        pos.fees_paid += exit_fee
        self.total_fees_paid += exit_fee

        if is_maker:
            self.total_maker_orders += 1
        else:
            self.total_taker_orders += 1

        # Càlcul PnL
        if pos.side == OrderSide.BUY:
            gross_pnl = (exit_price - pos.entry_price) * pos.size
        else:
            gross_pnl = (pos.entry_price - exit_price) * pos.size

        net_pnl = gross_pnl - pos.fees_paid
        pos.realized_pnl = net_pnl
        self.balance_usd += net_pnl
        self.closed_positions.append(pos)

        # Cancel·la qualsevol ordre romanent del mateix actiu
        self.cancel_all_for_coin(coin)

        color = "GREEN" if net_pnl > 0 else "RED"
        logger.info(
            f"[CLOSED {exit_reason}] {coin} @ {exit_price:.2f} | "
            f"Net PnL: {net_pnl:+.3f}$ (Gross: {gross_pnl:+.3f}$, Fees: {pos.fees_paid:.4f}$) "
            f"[{color}] | Balance: {self.balance_usd:.2f}$"
        )
        if self.on_close_cb:
            self.on_close_cb(pos, exit_reason, net_pnl)

    @property
    def metrics(self) -> dict:
        total_trades = len(self.closed_positions)
        if total_trades == 0:
            return {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "winrate_pct": 0.0,
                "net_pnl": 0.0,
                "profit_factor": 0.0,
                "balance": self.balance_usd,
                "total_fees": self.total_fees_paid,
                "maker_ratio_pct": 0.0,
            }

        wins = [p for p in self.closed_positions if p.realized_pnl > 0]
        losses = [p for p in self.closed_positions if p.realized_pnl <= 0]
        
        gross_profits = sum(p.realized_pnl for p in wins)
        gross_losses = abs(sum(p.realized_pnl for p in losses))
        
        winrate = (len(wins) / total_trades) * 100.0
        profit_factor = (gross_profits / gross_losses) if gross_losses > 0 else (999.0 if gross_profits > 0 else 0.0)
        net_pnl = self.balance_usd - self.initial_balance
        total_orders = self.total_maker_orders + self.total_taker_orders
        maker_ratio = (self.total_maker_orders / total_orders * 100.0) if total_orders > 0 else 0.0

        return {
            "total_trades": total_trades,
            "wins": len(wins),
            "losses": len(losses),
            "winrate_pct": winrate,
            "net_pnl": net_pnl,
            "profit_factor": profit_factor,
            "balance": self.balance_usd,
            "total_fees": self.total_fees_paid,
            "maker_ratio_pct": maker_ratio,
        }
