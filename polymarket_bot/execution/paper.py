"""
PAPER TRADING — Realistična fill simulacija.
FOK: 15% zavrnitev + spread drift check.
GTC: fill SAMO ko ask pade na naš limit.
"""

from __future__ import annotations

import asyncio
import random
import time

from config import EXEC_CONFIG, RiskConfig
from state import FillResult, Signal, State
from utils.helpers import log


async def paper_fok_fill(
    signal: Signal, state: State, config: RiskConfig
) -> FillResult | None:
    """15% zavrnitev + spread drift check. Konzervativna ocena."""
    submit_ns = time.time_ns()
    await asyncio.sleep(EXEC_CONFIG.simulated_latency_ms / 1000)
    fill_ns = submit_ns + int(EXEC_CONFIG.simulated_latency_ms * 1_000_000)
    if signal.is_close:
        return FillResult(
            order_id=f"PAPER_FOK_{fill_ns}",
            price=signal.price,
            size_usdc=signal.size_usdc,
            fill_ns=fill_ns,
        )
    m = state.quotes.get(signal.slug)
    if m is None:
        return None
    current_ask = (
        m.yes_ask if signal.side == "YES" else 1.0 - m.yes_bid
    )
    if abs(current_ask - signal.price) > 0.03:
        log(
            "INFO",
            "execution",
            f"[PAPER] FOK rejected — drift "
            f"{abs(current_ask - signal.price):.3f}",
        )
        return None
    reject_rate = max(
        0.05, config.paper_fok_reject_rate - signal.edge_estimate * 0.5
    )
    if random.random() < reject_rate:
        log(
            "INFO",
            "execution",
            f"[PAPER] FOK rejected (rate={reject_rate:.0%})",
        )
        return None
    fill_ns = submit_ns + int(EXEC_CONFIG.simulated_latency_ms * 1_000_000)
    return FillResult(
        order_id=f"PAPER_FOK_{fill_ns}",
        price=signal.price,
        size_usdc=signal.size_usdc,
        fill_ns=fill_ns,
    )


async def paper_gtc_fill(
    signal: Signal,
    state: State,
    config: RiskConfig,
    timeout_s: float = 20.0,
) -> FillResult | None:
    """Fill ko ask pade v ±1c od našega limita (30% fill verjetnost na tick)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        await asyncio.sleep(config.paper_gtc_fill_check_interval_s)
        m = state.quotes.get(signal.slug)
        if m is None:
            continue
        current_ask = (
            m.yes_ask if signal.side == "YES" else 1.0 - m.yes_bid
        )
        if current_ask <= signal.price + 0.01:
            if random.random() < 0.30:
                log(
                    "INFO",
                    "execution",
                    f"[PAPER] GTC filled @ {signal.price:.3f} "
                    f"(ask={current_ask:.3f})",
                )
                return FillResult(
                    order_id=f"PAPER_GTC_{int(time.time_ns())}",
                    price=signal.price,
                    size_usdc=signal.size_usdc,
                    fill_ns=time.time_ns(),
                )
    log(
        "INFO",
        "execution",
        f"[PAPER] GTC timeout @ {signal.price:.3f}",
    )
    return None
