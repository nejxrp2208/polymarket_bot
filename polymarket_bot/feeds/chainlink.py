"""
CHAINLINK RTDS STREAM
btc/usd in eth/usd realne cene za resolution reference.
"""

from __future__ import annotations

import asyncio
import json

import websockets

from config import CONFIG
from state import State
from utils.helpers import log

RTDS_URL = "wss://ws-live-data.polymarket.com"


async def chainlink_worker(sym: str, state: State) -> None:
    async with websockets.connect(RTDS_URL) as ws:
        state.open_connections.append(ws)
        try:
            sub = {
                "action": "subscribe",
                "subscriptions": [
                    {
                        "topic": "crypto_prices_chainlink",
                        "type": "*",
                        "filters": json.dumps({"symbol": sym}),
                    }
                ],
            }
            await ws.send(json.dumps(sub))
            async for msg in ws:
                data = json.loads(msg)
                if (
                    data.get("topic") == "crypto_prices_chainlink"
                    and "payload" in data
                ):
                    val = data["payload"].get("value")
                    if val is not None:
                        state.chainlink_prices[sym] = float(val)
        finally:
            if ws in state.open_connections:
                state.open_connections.remove(ws)


async def chainlink_worker_with_reconnect(
    sym: str, state: State
) -> None:
    backoff_s = 1
    while True:
        try:
            await chainlink_worker(sym, state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(
                "WARN",
                "chainlink",
                f"{sym} padel: {e} — reconnect cez {backoff_s}s",
            )
            await asyncio.sleep(backoff_s)
            backoff_s = min(
                backoff_s * 2, CONFIG.chainlink_reconnect_max_s
            )
        else:
            backoff_s = 1
