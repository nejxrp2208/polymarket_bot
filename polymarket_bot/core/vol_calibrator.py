"""
VOL CALIBRATOR — KRATKOROČNA + DOLGOROČNA SIGMA
Vsako 60s rekalibriraj kratkoročno sigma (5-min okno).
Vsako 30 min posodobi dolgoročno sigma (za vol shock detection).
"""

from __future__ import annotations

import asyncio
import math
import time

from config import CONFIG, STRATEGY_CONFIG
from state import State
from utils.helpers import log


async def vol_calibrator_task(state: State) -> None:
    last_long_cal = 0.0
    while True:
        try:
            await asyncio.sleep(60)
            now_ns = time.time_ns()
            cutoff_ns = (
                now_ns - CONFIG.calibration_window_s * 1_000_000_000
            )

            for symbol, buf in state.price_buffer.items():
                recent: list[tuple[int, float]] = []
                for i in range(len(buf) - 1, -1, -1):
                    ts, p = buf[i]
                    if ts <= cutoff_ns or len(recent) >= CONFIG.vol_sample_max:
                        break
                    recent.append((ts, p))
                recent.reverse()
                if len(recent) < CONFIG.min_samples:
                    continue

                lr: list[float] = []
                for i in range(1, len(recent)):
                    ts_p, p_p = recent[i - 1]
                    ts_c, p_c = recent[i]
                    dt_s = (ts_c - ts_p) / 1e9
                    if dt_s <= 0 or p_p <= 0:
                        continue
                    lr.append(
                        math.log(p_c / p_p) / math.sqrt(dt_s)
                    )

                if len(lr) < CONFIG.min_samples:
                    continue
                n = len(lr)
                mean = sum(lr) / n
                var = sum((r - mean) ** 2 for r in lr) / n
                real = math.sqrt(var)
                old = state.sigmas.get(symbol, CONFIG.sigma_default)
                new = (
                    CONFIG.sigma_ema_alpha * real
                    + (1 - CONFIG.sigma_ema_alpha) * old
                )
                state.sigmas[symbol] = max(
                    CONFIG.sigma_min, min(new, CONFIG.sigma_max)
                )

            # Dolgoročna sigma — vsako 30 minut
            now = time.time()
            if (
                now - last_long_cal
                >= STRATEGY_CONFIG.long_sigma_window_s
            ):
                last_long_cal = now
                for symbol, sigma in state.sigmas.items():
                    old = state.sigmas_long.get(symbol, sigma)
                    state.sigmas_long[symbol] = (
                        0.05 * sigma + 0.95 * old
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("ERROR", "vol_cal", str(e))
