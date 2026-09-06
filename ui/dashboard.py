"""Panell de control interactiu en terminal amb Rich."""

import time
from typing import Dict, List
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core.models import OrderBookL2, Position
from core.paper_exchange import PaperExchange

console = Console()

def generate_dashboard(
    exchange: PaperExchange,
    books: Dict[str, OrderBookL2],
    start_time: float,
    active_strategies: List[str],
) -> Group:
    elapsed = int(time.time() - start_time)
    mins, secs = divmod(elapsed, 60)
    hours, mins = divmod(mins, 60)
    time_str = f"{hours:02d}:{mins:02d}:{secs:02d}"

    metrics = exchange.metrics
    pnl = metrics["net_pnl"]
    pnl_color = "green" if pnl >= 0 else "red"
    winrate_color = "green" if metrics["winrate_pct"] >= 55 else ("yellow" if metrics["winrate_pct"] > 45 else "red")

    # 1. Header amb Mètriques Clau
    header_text = Text()
    header_text.append(" HYPERLIQUID SCALPING BOT ", style="bold white on blue")
    header_text.append(f"  [PAPER TRADING - FEED REAL MAINNET]  Temps actiu: {time_str}\n\n", style="bold cyan")
    
    header_text.append(f"Balanç: ", style="bold")
    header_text.append(f"{metrics['balance']:,.2f}$   ", style="bold white")
    header_text.append(f"PnL Net: ", style="bold")
    header_text.append(f"{pnl:+,.2f}$   ", style=f"bold {pnl_color}")
    header_text.append(f"Trades: ", style="bold")
    header_text.append(f"{metrics['total_trades']} (W: {metrics['wins']} / L: {metrics['losses']})   ", style="white")
    header_text.append(f"Winrate: ", style="bold")
    header_text.append(f"{metrics['winrate_pct']:.1f}%   ", style=f"bold {winrate_color}")
    header_text.append(f"Profit Factor: ", style="bold")
    header_text.append(f"{metrics['profit_factor']:.2f}   ", style="bold white")
    header_text.append(f"Comissions Pagades: ", style="bold")
    header_text.append(f"{metrics['total_fees']:.4f}$ ", style="bold magenta")
    header_text.append(f"(Maker: {metrics['maker_ratio_pct']:.0f}%)", style="cyan")

    header_panel = Panel(header_text, border_style="blue", title="Resum Executiu")

    # 2. Taula de Mercat en Viu
    market_table = Table(title="Feed de Mercat Hyperliquid (L2 Order Book)", expand=True)
    market_table.add_column("Moneda", style="bold yellow", justify="center")
    market_table.add_column("Millor Bid", justify="right", style="green")
    market_table.add_column("Millor Ask", justify="right", style="red")
    market_table.add_column("Mid Price", justify="right", style="bold white")
    market_table.add_column("Spread %", justify="right")
    market_table.add_column("Top 5 Bid Vol ($)", justify="right")
    market_table.add_column("Top 5 Ask Vol ($)", justify="right")
    market_table.add_column("Imbalance Ratio", justify="center", style="bold")

    for coin, book in books.items():
        if book.mid_price is None:
            continue
        
        bid_vol = sum(lvl.price * lvl.size for lvl in book.bids[:5])
        ask_vol = sum(lvl.price * lvl.size for lvl in book.asks[:5])
        
        if ask_vol > 0:
            ratio = bid_vol / ask_vol
            imb_str = f"{ratio:.2f}x Bid" if ratio >= 1.0 else f"{(ask_vol/bid_vol):.2f}x Ask"
            imb_style = "bold green" if ratio >= 1.8 else ("bold red" if (ask_vol/bid_vol) >= 1.8 else "dim")
        else:
            imb_str = "N/A"
            imb_style = "dim"

        spread_style = "green" if book.spread_pct < 0.02 else ("yellow" if book.spread_pct < 0.05 else "red")

        market_table.add_row(
            coin,
            f"{book.best_bid:,.2f}" if book.best_bid else "-",
            f"{book.best_ask:,.2f}" if book.best_ask else "-",
            f"{book.mid_price:,.2f}",
            f"[{spread_style}]{book.spread_pct:.3f}%[/{spread_style}]",
            f"{bid_vol:,.0f}$",
            f"{ask_vol:,.0f}$",
            f"[{imb_style}]{imb_str}[/{imb_style}]",
        )

    # 3. Taula de Posicions Actives
    pos_table = Table(title="Posicions Obertes en Directe", expand=True)
    pos_table.add_column("ID", style="dim", justify="center")
    pos_table.add_column("Moneda", style="bold yellow")
    pos_table.add_column("Costat", justify="center")
    pos_table.add_column("Mida ($)", justify="right")
    pos_table.add_column("Entrada", justify="right")
    pos_table.add_column("Preu Actual", justify="right")
    pos_table.add_column("Take Profit", justify="right", style="green")
    pos_table.add_column("Stop Loss", justify="right", style="red")
    pos_table.add_column("PnL No Realitzat", justify="right", style="bold")
    pos_table.add_column("Estratègia", style="cyan")

    if not exchange.positions:
        pos_table.add_row("-", "Sense posicions actives", "-", "-", "-", "-", "-", "-", "-", "-")
    else:
        for pos in exchange.positions.values():
            side_color = "green" if pos.side.value == "BUY" else "red"
            pnl_style = "green" if pos.unrealized_pnl >= 0 else "red"
            pos_table.add_row(
                pos.position_id,
                pos.coin,
                f"[{side_color}]{pos.side.value}[/{side_color}]",
                f"{pos.size_usd:.1f}$",
                f"{pos.entry_price:,.2f}",
                f"{pos.current_price:,.2f}",
                f"{pos.take_profit:,.2f}",
                f"{pos.stop_loss:,.2f}",
                f"[{pnl_style}]{pos.unrealized_pnl:+,.3f}$ ({pos.unrealized_pnl_pct:+.2f}%)[/{pnl_style}]",
                pos.strategy_name,
            )

    # 4. Historial Darreres Posicions Tancades
    closed_table = Table(title="Darreres 5 Minioperacions Tancades", expand=True)
    closed_table.add_column("Moneda", style="yellow")
    closed_table.add_column("Costat", justify="center")
    closed_table.add_column("Entrada", justify="right")
    closed_table.add_column("Sortida", justify="right")
    closed_table.add_column("Motiu Sortida", justify="center")
    closed_table.add_column("Net PnL ($)", justify="right", style="bold")
    closed_table.add_column("Fees ($)", justify="right", style="dim")
    closed_table.add_column("Estratègia", style="cyan")

    recent_closed = exchange.closed_positions[-5:]
    if not recent_closed:
        closed_table.add_row("-", "-", "-", "-", "Cap operació tancada encara", "-", "-", "-")
    else:
        for p in reversed(recent_closed):
            side_color = "green" if p.side.value == "BUY" else "red"
            pnl_style = "green" if p.realized_pnl > 0 else "red"
            reason_style = "bold green" if p.exit_reason == "TAKE_PROFIT" else "bold red"
            closed_table.add_row(
                p.coin,
                f"[{side_color}]{p.side.value}[/{side_color}]",
                f"{p.entry_price:,.2f}",
                f"{p.exit_price:,.2f}" if p.exit_price else "-",
                f"[{reason_style}]{p.exit_reason}[/{reason_style}]",
                f"[{pnl_style}]{p.realized_pnl:+,.3f}$[/{pnl_style}]",
                f"{p.fees_paid:.4f}$",
                p.strategy_name,
            )

    return Group(header_panel, market_table, pos_table, closed_table)
