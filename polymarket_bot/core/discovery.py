"""
DISCOVERY TASK
Rollover detection, novi marketi, Chainlink window_open_price.
KRITIČEN FIX: window_open_price = Chainlink, NE Binance!
"""

from __future__ import annotations

import asyncio
import time

from feeds.polymarket import fetch_token_ids
from state import Market, State
from utils.helpers import (
    current_window_ts,
    get_slug,
    log,
    seconds_until_rollover,
)


async def discovery_task(state: State) -> None:
    while True:
        try:
            secs = seconds_until_rollover()
            await asyncio.sleep(secs)
            ts = current_window_ts()
            slug = get_slug(ts)
            log("INFO", "discovery", f"rollover — iscem: {slug}")

            token_ids = await fetch_token_ids(slug)
            if not token_ids:
                log(
                    "ERROR",
                    "discovery",
                    f"market ni najden: {slug}",
                )
                continue

            yes_id, no_id = token_ids[0], token_ids[1]
            state.markets[slug] = Market(
                slug=slug, yes_id=yes_id, no_id=no_id
            )
            state.token_to_slug[yes_id] = slug
            state.token_to_slug[no_id] = slug

            # KRITIČEN FIX: window_open_price = Chainlink, NE Binance
            # Polymarket resolva: Chainlink_end >= Chainlink_open
            cl = state.chainlink_prices.get("btc/usd")
            bn = (
                state.price_buffer["btcusdt"][-1][1]
                if state.price_buffer.get("btcusdt")
                else None
            )
            if cl:
                state.window_open_price[slug] = float(cl)
                log(
                    "INFO",
                    "discovery",
                    f"window_open_price={float(cl):.2f} (Chainlink)",
                )
            elif bn:
                state.window_open_price[slug] = bn
                log(
                    "WARN",
                    "discovery",
                    f"window_open_price={bn:.2f} (Binance fallback!)",
                )
            else:
                log(
                    "ERROR",
                    "discovery",
                    "window_open_price: ni cene!",
                )

            # Reset per-window dedup (NE čistimo position dictov — exit_monitor
            # jih zapre sam; čiščenje bi povzročilo PENDING trade brez zaprtja)
            state.trades_this_window.clear()
            state.traded_directions.clear()
            state.pending_directions.clear()
            state.zone_flip_reversed.clear()
            state.fast_scalp_pending.clear()
            state.zone_flip_pending.clear()
            state.extreme_zone_pending.clear()
            state.zone_flip_entered.clear()
            state.extreme_zone_entered.clear()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("ERROR", "discovery", str(e))


async def wait_for_first_binance_tick(
    state: State, timeout_s: float = 5.0
) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if state.price_buffer.get("btcusdt"):
            return True
        await asyncio.sleep(0.05)
    return False


async def init_current_market(state: State) -> None:
    ts = current_window_ts()
    slug = get_slug(ts)
    log("INFO", "discovery", f"init: iscem {slug}")
    token_ids = await fetch_token_ids(slug)
    if not token_ids:
        log("ERROR", "discovery", f"init failed: {slug}")
        return
    yes_id, no_id = token_ids[0], token_ids[1]
    state.markets[slug] = Market(
        slug=slug, yes_id=yes_id, no_id=no_id
    )
    state.token_to_slug[yes_id] = slug
    state.token_to_slug[no_id] = slug
    cl = state.chainlink_prices.get("btc/usd")
    bn = (
        state.price_buffer["btcusdt"][-1][1]
        if state.price_buffer.get("btcusdt")
        else None
    )
    state.window_open_price[slug] = (
        float(cl) if cl else (bn or 0.0)
    )
    log(
        "INFO",
        "discovery",
        f"init uspesen: {slug} "
        f"open_price={state.window_open_price[slug]:.2f}",
    )
