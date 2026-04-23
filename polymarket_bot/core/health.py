"""
HEALTH + LAG MONITOR
health_monitor_task: stale feedi, sigma anomalije.
lag_monitor_task: event loop blokade > 50ms.
"""

from __future__ import annotations

import asyncio
import time

from config import CONFIG
from state import State
from utils.helpers import log


def _binance_tick_rate(state: State, sym: str, window_s: int = 10) -> float:
    """Ticks/sec za zadnjih window_s sekund."""
    buf = state.price_buffer.get(sym)
    if not buf or len(buf) < 2:
        return 0.0
    cutoff_ns = time.time_ns() - window_s * 1_000_000_000
    count = sum(1 for ts, _ in buf if ts >= cutoff_ns)
    return count / window_s


async def health_monitor_task(state: State) -> None:
    _feed_log_counter = 0
    while True:
        try:
            await asyncio.sleep(10)
            _feed_log_counter += 1
            now_ns = time.time_ns()

            # Stale checks
            for sym, last_ns in state.last_binance_tick_ns.items():
                if (now_ns - last_ns) / 1e9 > CONFIG.stale_binance_s:
                    log("WARN", "health", f"Binance {sym} stale")
            for slug, last_ns in state.last_polymarket_tick_ns.items():
                if (now_ns - last_ns) / 1e9 > CONFIG.stale_polymarket_s:
                    log("WARN", "health", f"Polymarket {slug} stale")
            for sym, sigma in state.sigmas.items():
                if sigma < CONFIG.sigma_min or sigma > CONFIG.sigma_max:
                    log("ERROR", "health", f"sigma OOB: {sym}={sigma:.6f}")

            # Feed speed — vsake 30s
            if _feed_log_counter % 3 == 0:
                btc_rate = _binance_tick_rate(state, "btcusdt")
                eth_rate = _binance_tick_rate(state, "ethusdt")
                parts = [f"binance btc={btc_rate:.1f}/s eth={eth_rate:.1f}/s"]
                for slug, last_ns in state.last_polymarket_tick_ns.items():
                    age_ms = (now_ns - last_ns) / 1_000_000
                    parts.append(f"poly {slug[-10:]} last={age_ms:.0f}ms ago")
                log("INFO", "health", " | ".join(parts))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("ERROR", "health", str(e))


async def lag_monitor_task() -> None:
    while True:
        t1 = asyncio.get_event_loop().time()
        await asyncio.sleep(1.0)
        t2 = asyncio.get_event_loop().time()
        drift_ms = (t2 - t1 - 1.0) * 1000
        if drift_ms > 50:
            log(
                "WARN",
                "lag",
                f"event loop drift={drift_ms:.1f}ms!",
            )
