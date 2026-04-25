"""
POLYMARKET CLOB STREAM
WS subscribe na aktivni market, BBA events, rollover detection.
"""

from __future__ import annotations

import asyncio
import json
import time

import requests
import websockets

import refs
from config import CONFIG
from state import Quote, State
from logging_.db import log_bba
from utils.helpers import (
    current_window_ts,
    get_slug,
    log,
    seconds_until_rollover,
)

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


async def fetch_token_ids(
    slug: str, retries: int = 15, interval: int = 1
) -> list[str] | None:
    loop = asyncio.get_running_loop()
    for attempt in range(retries):
        try:
            r = await loop.run_in_executor(
                None,
                lambda: requests.get(
                    f"{GAMMA_URL}?slug={slug}", timeout=5
                ),
            )
            data = json.loads(r.text, parse_float=str)
            if data:
                raw = data[0].get("clobTokenIds")
                if isinstance(raw, str):
                    raw = json.loads(raw)
                if raw and len(raw) >= 2:
                    return [str(raw[0]), str(raw[1])]
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(interval)
    return None


async def polymarket_stream(state: State) -> None:
    initial_window_ts = current_window_ts()
    active_slug = get_slug(initial_window_ts)
    m = state.markets.get(active_slug)
    if not m:
        return

    stop_event = asyncio.Event()
    async with websockets.connect(CLOB_WS_URL) as ws:
        state.open_connections.append(ws)
        try:
            # Subscribe — lowercase "market" + custom_feature_enabled required
            sub = {
                "assets_ids": [m.yes_id, m.no_id],
                "type": "market",
                "custom_feature_enabled": True,
            }
            await ws.send(json.dumps(sub))

            # Ping task — string PING (ne websocket-level ping)
            async def _ping(w, stop):
                while not stop.is_set():
                    try:
                        await asyncio.sleep(10)
                        await w.send("PING")
                    except Exception:
                        break

            ping_task = asyncio.create_task(_ping(ws, stop_event))

            try:
                while not stop_event.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=15.0)
                    except asyncio.TimeoutError:
                        log("WARN", "polymarket", "WS recv timeout 15s — reconnect")
                        return
                    if msg == "PONG":
                        continue
                    events = json.loads(msg)
                    if not isinstance(events, list):
                        events = [events]
                    for ev in events:
                        et = ev.get("event_type", ev.get("type", ""))
                        asset_id = ev.get("asset_id", "")
                        slug = state.token_to_slug.get(asset_id)

                        if et == "book" and slug:
                            # Initial book snapshot — vzemi best bid/ask
                            bids = sorted(
                                ev.get("bids", []),
                                key=lambda x: float(x["price"]),
                                reverse=True,
                            )
                            asks = sorted(
                                ev.get("asks", []),
                                key=lambda x: float(x["price"]),
                            )
                            if bids and asks:
                                bid = float(bids[0]["price"])
                                ask = float(asks[0]["price"])
                                existing = state.quotes.get(slug)
                                # Posodobi samo YES token (prvi)
                                if asset_id == m.yes_id or existing is None:
                                    state.quotes[slug] = Quote(
                                        yes_bid=bid,
                                        yes_ask=ask,
                                        timestamp_ns=time.time_ns(),
                                    )

                        elif et == "best_bid_ask" and slug:
                            bid = float(ev.get("best_bid", 0))
                            ask = float(ev.get("best_ask", 1))
                            if asset_id == m.yes_id:
                                state.quotes[slug] = Quote(
                                    yes_bid=bid,
                                    yes_ask=ask,
                                    timestamp_ns=time.time_ns(),
                                )
                                mid = (bid + ask) / 2.0
                                if slug not in state.polymarket_mid_buffer:
                                    from collections import deque
                                    state.polymarket_mid_buffer[slug] = deque(maxlen=600)
                                state.polymarket_mid_buffer[slug].append((time.time_ns(), mid))
                                from core.exit_monitor import check_sl_now
                                asyncio.create_task(
                                    check_sl_now(slug, mid, state),
                                    name=f"sl_check_{slug}_{time.time_ns()}",
                                )
                                from strategy.signal import on_new_tick
                                now_ns = time.time_ns()
                                on_new_tick(slug, state, now_ns, refs.execution_ref, refs.risk_ref)
                            state.last_polymarket_tick_ns[slug] = time.time_ns()
                            side = "YES" if asset_id == m.yes_id else "NO"
                            asyncio.create_task(
                                log_bba(
                                    slug,
                                    side,
                                    ev.get("best_bid", "0"),
                                    ev.get("best_ask", "1"),
                                )
                            )

                        # Rollover detection — primerjaj window_ts ne sekunde
                        if current_window_ts() != initial_window_ts:
                            stop_event.set()
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
        finally:
            if ws in state.open_connections:
                state.open_connections.remove(ws)


async def polymarket_stream_with_reconnect(state: State) -> None:
    backoff_s = 1
    while True:
        try:
            await polymarket_stream(state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(
                "WARN",
                "polymarket",
                f"WS padel: {e} — reconnect cez {backoff_s}s",
            )
            await asyncio.sleep(backoff_s)
            backoff_s = min(
                backoff_s * 2, CONFIG.polymarket_reconnect_max_s
            )
        else:
            backoff_s = 1
            await asyncio.sleep(1.0)
