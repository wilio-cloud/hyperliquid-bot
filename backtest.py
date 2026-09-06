"""Backtest històric d'Hyperliquid per a Spread Market Making i Reversió a la Mitjana (0% Fees)."""

import argparse
import json
import logging
import math
import ssl
import time
import urllib.request
from typing import Dict, List, Optional
import certifi
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

class AdvancedBacktester:
    def __init__(
        self,
        coins: List[str] = ["BTC", "ETH", "SOL"],
        initial_balance: float = 10000.0,
        position_size_usd: float = 500.0,
        mode: str = "maker",              # 'maker', 'meanrev', 'combined'
        maker_fee_rate: float = 0.0,      # 0.0% (Zero Maker Fee!)
        taker_fee_rate: float = 0.00035,  # 0.035% Taker (només si Stop Loss d'emergència)
        mean_rev_tp: float = 0.0030,      # +0.30%
        mean_rev_sl: float = 0.0025,      # -0.25%
        time_limit_min: int = 15,         # 15 minuts de límit
    ):
        self.coins = coins
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.position_size_usd = position_size_usd
        self.mode = mode
        self.maker_fee = maker_fee_rate
        self.taker_fee = taker_fee_rate
        self.mean_rev_tp = mean_rev_tp
        self.mean_rev_sl = mean_rev_sl
        self.time_limit_min = time_limit_min
        
        self.trades = []
        self.total_fees = 0.0

    def fetch_24h_candles(self, coin: str) -> List[dict]:
        ctx = ssl.create_default_context(cafile=certifi.where())
        end_time = int(time.time() * 1000)
        start_time = end_time - (24 * 3600 * 1000)
        
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": "1m",
                "startTime": start_time,
                "endTime": end_time,
            }
        }
        
        req = urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        
        with urllib.request.urlopen(req, context=ctx) as resp:
            data = json.loads(resp.read().decode())
            return [
                {
                    "t": c["t"],
                    "o": float(c["o"]),
                    "h": float(c["h"]),
                    "l": float(c["l"]),
                    "c": float(c["c"]),
                    "v": float(c["v"]),
                    "n": int(c.get("n", 1))
                }
                for c in data
            ]

    def run(self):
        console.print(f"[bold cyan]═══ INICIANT BACKTEST D'HYPERLIQUID (24 HORES / 1.440 MINUTS) ═══[/bold cyan]")
        console.print(f"Estratègia: [bold green]{self.mode.upper()}[/bold green] | Maker Fee: [bold magenta]{self.maker_fee*100:.3f}%[/bold magenta]\n")
        
        all_candles: Dict[str, List[dict]] = {}
        for coin in self.coins:
            candles = self.fetch_24h_candles(coin)
            all_candles[coin] = candles
            console.print(f" • [yellow]{coin}[/yellow]: {len(candles)} espelmes d'1 minut descarregades.")

        console.print(f"\n[bold green]Processant simulació de mercat...[/bold green]")
        for coin, candles in all_candles.items():
            if self.mode == "maker":
                self._backtest_spread_maker(coin, candles)
            elif self.mode == "meanrev":
                self._backtest_mean_reversion(coin, candles)
            elif self.mode == "combined":
                self._backtest_spread_maker(coin, candles)
                self._backtest_mean_reversion(coin, candles)

        self._print_summary()

    def _backtest_spread_maker(self, coin: str, candles: List[dict]):
        """Simula Market Making pur: Col·locar Bid i Ask i capturar l'spread quan el mercat oscil·la."""
        # Estima el spread mitjà de l'actiu a Hyperliquid
        spread_pct = 0.00008 if coin == "BTC" else (0.00015 if coin == "ETH" else 0.00035)
        
        for i in range(20, len(candles)):
            c = candles[i]
            prev = candles[i-1]
            
            candle_range_pct = (c["h"] - c["l"]) / c["o"] if c["o"] > 0 else 0
            candle_body_pct = abs(c["c"] - c["o"]) / c["o"] if c["o"] > 0 else 0

            # Condició òptima de Market Making:
            # Mercat oscil·lant (High-Low prou ampli per creuar Bid/Ask, però sense breakout direccional violent)
            is_ranging = candle_range_pct >= (spread_pct * 1.5) and (candle_body_pct < candle_range_pct * 0.65)
            is_breakout = candle_body_pct >= candle_range_pct * 0.85 and candle_range_pct > 0.0015

            if is_ranging:
                # Èxit de Spread Capture: Hem comprat al Bid i venut a l'Ask (dues ordres Maker = 0% fee)
                profit_gross = self.position_size_usd * spread_pct
                entry_fee = self.position_size_usd * self.maker_fee
                exit_fee = self.position_size_usd * self.maker_fee
                total_fee = entry_fee + exit_fee
                net_pnl = profit_gross - total_fee
                
                self.balance += net_pnl
                self.total_fees += total_fee
                self.trades.append({
                    "coin": coin,
                    "strategy": "Spread_Maker",
                    "reason": "SPREAD_CAPTURED",
                    "gross_pnl": profit_gross,
                    "net_pnl": net_pnl,
                    "fee": total_fee,
                    "win": True,
                })

            elif is_breakout:
                # Adverse Selection: Si el mercat fa un breakout brusc contra la nostra posició
                # Sortida de seguretat amb pèrdua petita d'1 tick
                loss_gross = - (self.position_size_usd * spread_pct * 2.0)
                entry_fee = self.position_size_usd * self.maker_fee
                exit_fee = self.position_size_usd * self.taker_fee  # Sortida a mercat
                total_fee = entry_fee + exit_fee
                net_pnl = loss_gross - total_fee
                
                self.balance += net_pnl
                self.total_fees += total_fee
                self.trades.append({
                    "coin": coin,
                    "strategy": "Spread_Maker",
                    "reason": "ADVERSE_SELECTION",
                    "gross_pnl": loss_gross,
                    "net_pnl": net_pnl,
                    "fee": total_fee,
                    "win": False,
                })

    def _backtest_mean_reversion(self, coin: str, candles: List[dict]):
        """Simula Reversió a la Mitjana amb filtre de tendència (EMA 50) i 0% Maker Fees."""
        if len(candles) < 60:
            return

        in_pos = False
        pos_entry, pos_side, pos_entry_i, pos_tp, pos_sl = 0.0, "BUY", 0, 0.0, 0.0

        for i in range(50, len(candles)):
            c = candles[i]

            if in_pos:
                duration = i - pos_entry_i
                closed = False
                exit_price = 0.0
                exit_reason = ""
                is_maker = True

                if pos_side == "BUY":
                    if c["h"] >= pos_tp:
                        exit_price, exit_reason, is_maker, closed = pos_tp, "TAKE_PROFIT", True, True
                    elif c["l"] <= pos_sl:
                        exit_price, exit_reason, is_maker, closed = pos_sl, "STOP_LOSS", False, True
                    elif duration >= self.time_limit_min:
                        exit_price, exit_reason, is_maker, closed = c["c"], "TIME_LIMIT", True, True
                else:
                    if c["l"] <= pos_tp:
                        exit_price, exit_reason, is_maker, closed = pos_tp, "TAKE_PROFIT", True, True
                    elif c["h"] >= pos_sl:
                        exit_price, exit_reason, is_maker, closed = pos_sl, "STOP_LOSS", False, True
                    elif duration >= self.time_limit_min:
                        exit_price, exit_reason, is_maker, closed = c["c"], "TIME_LIMIT", True, True

                if closed:
                    size_coins = self.position_size_usd / pos_entry
                    entry_fee = self.position_size_usd * self.maker_fee
                    exit_fee = (size_coins * exit_price) * (self.maker_fee if is_maker else self.taker_fee)
                    total_fee = entry_fee + exit_fee

                    gross = (exit_price - pos_entry) * size_coins if pos_side == "BUY" else (pos_entry - exit_price) * size_coins
                    net = gross - total_fee

                    self.balance += net
                    self.total_fees += total_fee
                    self.trades.append({
                        "coin": coin,
                        "strategy": "Micro_MeanRev",
                        "reason": exit_reason,
                        "gross_pnl": gross,
                        "net_pnl": net,
                        "fee": total_fee,
                        "win": net > 0,
                    })
                    in_pos = False
                    continue

            if not in_pos:
                # Mitjana dels últims 30 períodes
                window = candles[i-30:i]
                vwap = sum(w["c"] * w["v"] for w in window) / sum(w["v"] for w in window)
                dev = (c["c"] - vwap) / vwap

                # Filtre de tendència (EMA 50)
                ema50 = sum(w["c"] for w in candles[i-50:i]) / 50.0

                # Compra en sobre-venda només si el preu està sobre l'EMA50 (tendència alcista de fons)
                if dev <= -0.0025 and c["c"] >= ema50 * 0.998:
                    in_pos, pos_entry, pos_side, pos_entry_i = True, c["c"], "BUY", i
                    pos_tp = pos_entry * (1.0 + self.mean_rev_tp)
                    pos_sl = pos_entry * (1.0 - self.mean_rev_sl)

                # Venda en sobre-compra només si el preu està sota l'EMA50 (tendència baixista de fons)
                elif dev >= 0.0025 and c["c"] <= ema50 * 1.002:
                    in_pos, pos_entry, pos_side, pos_entry_i = True, c["c"], "SELL", i
                    pos_tp = pos_entry * (1.0 - self.mean_rev_tp)
                    pos_sl = pos_entry * (1.0 + self.mean_rev_sl)

    def _print_summary(self):
        total_trades = len(self.trades)
        if total_trades == 0:
            console.print("[red]Cap trade realitzat.[/red]")
            return

        wins = [t for t in self.trades if t["win"]]
        losses = [t for t in self.trades if not t["win"]]
        winrate = (len(wins) / total_trades) * 100.0

        gross_profit = sum(t["net_pnl"] for t in wins)
        gross_loss = abs(sum(t["net_pnl"] for t in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 999.0
        net_pnl = self.balance - self.initial_balance
        pnl_color = "green" if net_pnl >= 0 else "red"

        resum = Table(title=f"RESULTATS BACKTEST 24H: {self.mode.upper()} (MAKER FEE: {self.maker_fee*100:.2f}%)")
        resum.add_column("Mètrica", style="bold cyan")
        resum.add_column("Valor", justify="right", style="bold white")

        resum.add_row("Balanç Inicial", f"{self.initial_balance:,.2f} $")
        resum.add_row("Balanç Final", f"{self.balance:,.2f} $")
        resum.add_row("PnL Net Total", f"[{pnl_color}]{net_pnl:+,.2f} $[/{pnl_color}]")
        resum.add_row("Total Minioperacions", str(total_trades))
        resum.add_row("Guanyades / Perdudes", f"{len(wins)} / {len(losses)}")
        resum.add_row("Taxa d'Èxit (Winrate)", f"{winrate:.1f} %")
        resum.add_row("Profit Factor", f"{profit_factor:.2f}")
        resum.add_row("Comissions Pagades", f"{self.total_fees:.2f} $")
        console.print(resum)

        # Desglossament per Motiu
        reasons_table = Table(title="Desglossament per Motiu d'Execució")
        reasons_table.add_column("Motiu", style="yellow")
        reasons_table.add_column("Quantitat", justify="right")
        reasons_table.add_column("PnL ($)", justify="right")

        reasons = {}
        for t in self.trades:
            r = t["reason"]
            if r not in reasons:
                reasons[r] = {"count": 0, "pnl": 0.0}
            reasons[r]["count"] += 1
            reasons[r]["pnl"] += t["net_pnl"]

        for r, data in reasons.items():
            color = "green" if data["pnl"] >= 0 else "red"
            reasons_table.add_row(r, str(data["count"]), f"[{color}]{data['pnl']:+,.2f} $[/{color}]")
        console.print(reasons_table)

def main():
    parser = argparse.ArgumentParser(description="Backtest Avançat Hyperliquid")
    parser.add_argument("--mode", type=str, default="maker", choices=["maker", "meanrev", "combined"])
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL"])
    parser.add_argument("--maker-fee", type=float, default=0.0, help="Comissió Maker (default 0.0%%)")
    args = parser.parse_args()

    engine = AdvancedBacktester(
        coins=args.coins,
        mode=args.mode,
        maker_fee_rate=args.maker_fee,
    )
    engine.run()

if __name__ == "__main__":
    main()
