"""
BINANCE SBE STREAM
BTC/ETH bestBidAsk + btcusdt depth10@100ms za OBI filter.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from pathlib import Path

import sbe
import websockets

import refs
from config import CONFIG
from state import State
from strategy.signal import on_new_tick
from utils.helpers import current_window_ts, get_slug, log

WS_URL = (
    "wss://stream-sbe.binance.com:9443/stream"
    "?streams=btcusdt@bestBidAsk"
    "/ethusdt@bestBidAsk"
    "/btcusdt@depth10@100ms"
)

SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "stream_1_0.xml"
_schema: sbe.Schema | None = None


def _get_schema() -> sbe.Schema:
    global _schema
    if _schema is None:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            _schema = sbe.Schema.parse(f)
    return _schema


def extract_symbol(raw: bytes, block_length: int, header_size: int = 8) -> str:
    sym_offset = header_size + block_length
    if sym_offset >= len(raw):
        return "unknown"
    sym_len = raw[sym_offset]
    end = sym_offset + 1 + sym_len
    if end > len(raw):
        return "unknown"
    return bytes(raw[sym_offset + 1 : end]).decode("ascii", errors="replace").lower()


def parse_depth_update(decoded: object, symbol: str, state: State) -> None:
    try:
        val = decoded.value
        bids: list[tuple[float, float]] = []
        asks: list[tuple[float, float]] = []
        for i in range(10):
            bp_key = f"bidPrice_{i}"
            bq_key = f"bidQty_{i}"
            ap_key = f"askPrice_{i}"
            aq_key = f"askQty_{i}"
            exp = int(val.get("priceExponent", -2))
            if bp_key in val and bq_key in val:
                bp = int(val[bp_key]) * (10**exp)
                bq = int(val[bq_key]) * (10**exp)
                if bp > 0:
                    bids.append((bp, bq))
            if ap_key in val and aq_key in val:
                ap = int(val[ap_key]) * (10**exp)
                aq = int(val[aq_key]) * (10**exp)
                if ap > 0:
                    asks.append((ap, aq))
        if bids:
            state.binance_bids[symbol] = bids
        if asks:
            state.binance_asks[symbol] = asks
    except Exception:
        pass


async def binance_stream(state: State) -> None:
    import os
    schema = _get_schema()
    api_key = os.getenv("BINANCE_API_KEY", "")
    extra_headers = {"X-MBX-APIKEY": api_key} if api_key else {}
    async with websockets.connect(WS_URL, max_size=2**20, additional_headers=extra_headers) as ws:
        state.open_connections.append(ws)
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    decoded = schema.decode(memoryview(raw))
                    mn = decoded.message_name
                    block_length = decoded.header["blockLength"]
                    if mn == "BestBidAskStreamEvent":
                        val = decoded.value
                        exp = int(val.get("priceExponent", -2))
                        bid = int(val["bidPrice"]) * (10**exp)
                        ask = int(val["askPrice"]) * (10**exp)
                        mid = (bid + ask) / 2.0
                        symbol = extract_symbol(raw, block_length)
                        if symbol in ("btcusdt", "ethusdt"):
                            if symbol not in state.price_buffer:
                                state.price_buffer[symbol] = deque(
                                    maxlen=CONFIG.price_buffer_maxlen
                                )
                            now_ns = time.time_ns() + state.ntp_offset_ns
                            state.price_buffer[symbol].append((now_ns, mid))
                            state.last_binance_tick_ns[symbol] = now_ns
                            if symbol == "btcusdt":
                                active_slug = get_slug(current_window_ts())
                                if active_slug in state.markets:
                                    on_new_tick(
                                        active_slug,
                                        state,
                                        now_ns,
                                        refs.execution_ref,
                                        refs.risk_ref,
                                    )
                    elif mn == "DepthUpdateEvent":
                        symbol = extract_symbol(raw, block_length)
                        if symbol == "btcusdt":
                            parse_depth_update(decoded, symbol, state)
        finally:
            if ws in state.open_connections:
                state.open_connections.remove(ws)


async def binance_stream_with_reconnect(state: State) -> None:
    backoff_s = 1
    while True:
        try:
            await binance_stream(state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(
                "WARN",
                "binance",
                f"WS padel: {e} — reconnect cez {backoff_s}s",
            )
            state.last_binance_tick_ns.clear()
            await asyncio.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, CONFIG.binance_reconnect_max_s)
        else:
            backoff_s = 1
