# Hyperliquid High-Frequency Scalping Bot (Paper Trading & Multi-Estratègia)

Un bot de trading algorítmic asíncron en Python dissenyat per aprofitar micro-moviments ràpids a **Hyperliquid**, amb un enfocament prioritari en minimitzar comissions mitjançant ordres **Post-Only (Maker)**.

---

## Característiques Principals

* **Comissions 0% / Maker Prioritàries**: Envia ordres límit amb el flag `post_only=True` per evitar creuar el spread i gaudir de comissions maker mínimes (0,01% o 0% amb rebates).
* **Simulador de Paper Trading Realista**: A diferència d'altres simuladors que assumeixen execucions instantànies, aquest modela la cua real del llibre d'ordres (`queue_ahead`) i només omple l'ordre si els trades reals del mercat absorbeixen el volum precedent.
* **Connexió Directa via WebSockets**: Rep dades de mercat del llibre d'ordres L2 i trades en temps real des dels servidors de Hyperliquid sense retard de polling HTTP.
* **Arquitectura Multi-Estratègia**: Permet comparar diferents lògiques de decisió en temps real:
  1. **Order Book Imbalance (OBI)**: Detecta asimetries en la profunditat del book (compres vs vendes) anticipant micro-salts.
  2. **Volume Burst Momentum**: Detecta ràfegues sobtades de volum en finestres de pocs segons.
  3. **Micro Mean-Reversion**: Detecta sobrecompres o sobrevendes extremes respecte al micro-VWAP.
* **Gestor de Riscos Estricte**: Inclou Take Profit, Stop Loss d'emergència, Circuit Breaker de pèrdua màxima diària i sortida per temps màxim de posició (*time-exit*).
* **Tauler Visual en Terminal (Rich)**: Mètriques en directe, PnL net, winrate, relació Maker/Taker i estat del book.

---

## Requisits i Instal·lació

```bash
cd /Users/guillemriusviladomiu/.gemini/antigravity/scratch/hyperliquid-trading-bot
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Com Executar el Bot

### 1. Mode Visual amb Tauler en Directe (Recomanat)
Mostra el llibre d'ordres en viu, posicions actives, mètriques d'èxit i guanys nets:
```bash
PYTHONPATH=. .venv/bin/python main.py --coins BTC ETH SOL
```

### 2. Mode Headless (Logs a la consola)
Ideal per executar en segon pla o per analitzar registres detallats:
```bash
PYTHONPATH=. .venv/bin/python main.py --coins BTC ETH SOL --headless
```

### 3. Executar un Backtest Històric de 24 Hores (1.440 minuts)
Descarrega automàticament les espelmes d'1 minut de les darreres 24h d'Hyperliquid i simula l'operativa:
```bash
PYTHONPATH=. .venv/bin/python backtest.py --coins BTC ETH SOL
```
Pots calibrar els paràmetres (ex: més temps límit o objectiu de TP/SL):
```bash
PYTHONPATH=. .venv/bin/python backtest.py --coins BTC ETH SOL --tp 0.0012 --sl 0.0015 --time-limit 10
```

### 4. Gravar i Reutilitzar Dades Reals Tick-a-Tick (El patró or de Scalping)
Grava tot el llibre L2 i els trades reals a disc comprimit:
```bash
PYTHONPATH=. .venv/bin/python recorder.py --coins BTC ETH SOL --out gravacio.jsonl.gz
```
I reprodueix-ho a 100x de velocitat per testejar OBI amb 100% de precisió:
```bash
PYTHONPATH=. .venv/bin/python replay.py --file gravacio.jsonl.gz
```

### 5. Executar la Suite de Tests Unitaris
```bash
PYTHONPATH=. .venv/bin/python tests/test_bot.py
```

---

## Configuració (`config/settings.py`)

Pots ajustar els paràmetres a `config/settings.py`:
* `coins`: Llista de parells a monitoritzar (ex: `["BTC", "ETH", "SOL"]`).
* `position_size_usd`: Mida de cada operació en dòlars (ex: `500.0`).
* `default_take_profit_pct`: Objectiu de guany per operació (ex: `0.0018` = +0.18%).
* `default_stop_loss_pct`: Límit de pèrdua (ex: `0.0020` = -0.20%).
* `max_daily_loss_usd`: Circuit breaker que atura el bot si s'acumulen pèrdues.
* `maker_fee_rate`: Taxa de comissió Maker (ex: `0.0001` = 0.01%).
