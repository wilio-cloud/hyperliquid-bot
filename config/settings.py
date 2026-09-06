"""Configuració general per al bot d'Hyperliquid."""

from typing import List
from pydantic import BaseModel, Field

class BotConfig(BaseModel):
    # Parells a monitoritzar i operar (Centrat en BTC per màxima liquiditat i mínim soroll)
    coins: List[str] = Field(default_factory=lambda: ["BTC"])
    
    # Endpoints de Hyperliquid
    mainnet_ws_url: str = "wss://api.hyperliquid.xyz/ws"
    testnet_ws_url: str = "wss://api.hyperliquid-testnet.xyz/ws"
    use_testnet: bool = False  # Per feed de dades, Mainnet té llibre d'ordres real i actiu
    
    # Mode Paper Trading (Simulació en memòria amb dades en viu)
    paper_trading: bool = True
    initial_balance_usd: float = 10000.0
    
    # Comissions de Hyperliquid (Maker molt baix o 0%, Taker estàndard)
    maker_fee_rate: float = 0.00010  # 0.01% (pot ser 0% o negatiu amb rebates)
    taker_fee_rate: float = 0.00035  # 0.035%
    
    # Gestió de posició per minioperació (1.000$ per trade = 10% del compte)
    position_size_usd: float = 1000.0  # Mida de cada ordre en dòlars
    max_open_positions: int = 2        # Màxim de posicions simultànies globals
    max_positions_per_coin: int = 1
    
    # Objectius de Scalping per defecte (+0.080% TP, -0.180% SL per BTC)
    default_take_profit_pct: float = 0.00080  # +0.080%
    default_stop_loss_pct: float = 0.00180    # -0.180%
    
    # Circuit Breaker (Protecció de capital)
    max_daily_loss_usd: float = 200.0  # Si perdem 200$, el bot atura totes les noves entrades
    max_consecutive_losses: int = 4    # Pausa de seguretat si encadena 4 pèrdues
    cooldown_seconds: int = 30         # Temps d'espera després de tancar posició

    # Paràmetres específics d'estratègia
    obi_imbalance_ratio: float = 2.5   # Ràtio Bid/Ask mínim per disparar senyal OBI
    obi_depth_levels: int = 5          # Quants nivells del book inspeccionar
    
    volume_burst_multiplier: float = 3.0 # Volum en ràfega vs mitjana recent
    volume_burst_window_sec: float = 5.0 # Finestra de segons per mesurar ràfega
    
    mean_rev_dev_pct: float = 0.0015   # Desviació de VWAP per buscar micro-retorn (0.15%)
    mean_rev_window: int = 60          # Segons per calcular micro-VWAP

config = BotConfig()
