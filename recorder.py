"""Gravador de dades en temps real (L2 Book & Trades) per a backtests exactes de tick."""

import argparse
import asyncio
import gzip
import json
import logging
import signal
import time
from typing import List
import certifi
import ssl
import websockets

logger = logging.getLogger("Recorder")

async def record_stream(coins: List[str], filename: str, duration_sec: int = 0):
    url = "wss://api.hyperliquid.xyz/ws"
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    start_time = time.time()
    msg_count = 0

    print(f"[RECORDER] Gravant llibre L2 i trades per a {coins} a '{filename}'...")
    print("Prem Ctrl+C quan vulguis aturar la gravació.")

    with gzip.open(filename, "at", encoding="utf-8") as f:
        async with websockets.connect(url, ssl=ssl_context) as ws:
            for coin in coins:
                await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}}))
                await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}))

            while True:
                try:
                    msg = await ws.recv()
                    f.write(msg + "\n")
                    msg_count += 1

                    if msg_count % 500 == 0:
                        elapsed = int(time.time() - start_time)
                        print(f" • Gravats {msg_count:,} esdeveniments ({elapsed}s transcorreguts)...")

                    if duration_sec > 0 and (time.time() - start_time) >= duration_sec:
                        break
                except asyncio.CancelledError:
                    break

    print(f"\n[RECORDER FINALITZAT] Total de {msg_count:,} missatges desats a {filename}.")

def main():
    parser = argparse.ArgumentParser(description="Hyperliquid Market Data Recorder")
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL"])
    parser.add_argument("--out", type=str, default="data_recording.jsonl.gz")
    parser.add_argument("--duration", type=int, default=0, help="Durada en segons (0 = indefinit)")
    args = parser.parse_args()

    try:
        asyncio.run(record_stream(args.coins, args.out, args.duration))
    except KeyboardInterrupt:
        print("\nGravació aturada manualment.")

if __name__ == "__main__":
    main()
