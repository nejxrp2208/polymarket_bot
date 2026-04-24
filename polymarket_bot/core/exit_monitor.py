"""
EXIT MONITOR
Stop-loss + market expiry + PRICE_LVL reversal za vse odprte pozicije.
_close_position: BUY nasprotne strani — NE SELL (bug #294).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from config import EXIT_CONFIG, FAST_SCALP_CONFIG, STRATEGY_CONFIG
from logging_.db import log_exit
from state import PositionState, Signal, State
from utils.helpers import log
from utils.telegram import notify_exit

if TYPE_CHECKING:
    from execution.layer import ExecutionLayer
    from risk.manager import RiskManager


async def exit_monitor_task(
    state: State,
    execution: ExecutionLayer,
    risk: RiskManager,
) -> None:
    while True:
        try:
            await asyncio.sleep(1.0)
            if not state.open_positions:
                continue

            for pos in list(state.open_positions.values()):
                await _check_position(pos, state, execution, risk)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("ERROR", "exit", str(e))


async def _check_position(
    pos: PositionState,
    state: State,
    execution: ExecutionLayer,
    risk: RiskManager,
) -> None:
    # Market expiry
    try:
        window_ts = int(pos.slug.split("-")[-1])
    except (ValueError, IndexError):
        window_ts = 0
    if window_ts > 0 and time.time() > window_ts + 300 + 5:
        open_price = state.window_open_price.get(pos.slug, 0.0)
        cl_now = state.chainlink_prices.get("btc/usd", 0.0)
        buf = state.price_buffer.get("btcusdt")
        btc_now = buf[-1][1] if buf else 0.0
        ref_now = cl_now if cl_now > 0 else btc_now
        if open_price > 0 and ref_now > 0:
            yes_won = ref_now >= open_price
            resolution = 1.0 if yes_won else 0.0
            exit_price = resolution if pos.side == "YES" else 1.0 - resolution
            log(
                "INFO", "exit",
                f"market_expiry {pos.side} | btc={ref_now:.2f} open={open_price:.2f} yes_won={yes_won}",
            )
        else:
            log("WARN", "exit", f"market_expiry: open_price=0 za {pos.slug} — skip")
            state.open_positions.pop(pos.side, None)
            return
        shares = pos.size_usdc / pos.entry_price if pos.entry_price > 0 else 0
        pnl = shares * exit_price - pos.size_usdc - pos.fee_usdc
        state.usdc_balance += shares * exit_price
        risk.on_close(pnl_usdc=pnl)
        await log_exit(
            slug=pos.slug,
            mode=execution.config.mode,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size_usdc=pos.size_usdc,
            fee_usdc=pos.fee_usdc,
            reason="market_expiry",
        )
        state.open_positions.pop(pos.side, None)
        log("INFO", "exit", f"market_expiry | side={pos.side} exit={exit_price:.2f} pnl={pnl:+.2f}")
        notify_exit(pos.side, pos.entry_price, exit_price, pnl, "market_expiry", pos.slug)
        return

    q = state.quotes.get(pos.slug)
    if q is None:
        return

    yes_mid = (q.yes_bid + q.yes_ask) / 2.0

    if pos.entry_mode == "PRICE_LVL":
        # Za PRICE_LVL pozicije: EXIT samo na reversal (ne stop_loss)
        if pos.side == "YES" and yes_mid <= 0.35:
            log("INFO", "exit", f"PRICE_LVL reversal YES→NO | mid={yes_mid:.3f}")
            closed = await _close_position(pos, state, execution, risk, "price_reversal")
            if closed and "NO" not in state.open_positions:
                await _fire_reversal_entry("NO", pos, state, execution, risk, q)
        elif pos.side == "NO" and yes_mid >= 0.65:
            log("INFO", "exit", f"PRICE_LVL reversal NO→YES | mid={yes_mid:.3f}")
            closed = await _close_position(pos, state, execution, risk, "price_reversal")
            if closed and "YES" not in state.open_positions:
                await _fire_reversal_entry("YES", pos, state, execution, risk, q)
        return

    if pos.entry_mode == "FAST_SCALP":
        await _check_fast_scalp_exit(pos, state, execution, risk, yes_mid)
        return

    # BSM / EXPIRY pozicije: normalen stop_loss
    mid = yes_mid if pos.side == "YES" else 1.0 - yes_mid
    unrealized = mid - pos.entry_price
    if unrealized <= -EXIT_CONFIG.stop_loss_cents:
        log("WARN", "exit", f"stop-loss {pos.side} | unrealized={unrealized:.3f}")
        await _close_position(pos, state, execution, risk, "stop_loss")


async def _check_fast_scalp_exit(
    pos: PositionState,
    state: State,
    execution: ExecutionLayer,
    risk: RiskManager,
    yes_mid: float,
) -> None:
    mid = yes_mid if pos.side == "YES" else 1.0 - yes_mid
    unrealized = mid - pos.entry_price
    if unrealized >= FAST_SCALP_CONFIG.take_profit_cents:
        log("INFO", "exit", f"FAST_SCALP take-profit {pos.side} | unrealized={unrealized:+.3f}")
        await _close_position(pos, state, execution, risk, "take_profit")
    elif unrealized <= -FAST_SCALP_CONFIG.stop_loss_cents:
        log("WARN", "exit", f"FAST_SCALP stop-loss {pos.side} | unrealized={unrealized:+.3f}")
        await _close_position(pos, state, execution, risk, "stop_loss")


async def _fire_reversal_entry(
    side: str,
    old_pos: PositionState,
    state: State,
    execution: ExecutionLayer,
    risk: RiskManager,
    q,
) -> None:
    m = state.markets.get(old_pos.slug)
    buf = state.price_buffer.get("btcusdt")
    if not m:
        return
    now_ns = time.time_ns()
    if side == "NO":
        token_id = m.no_id
        price = round(1.0 - q.yes_bid, 3)
        edge = round(abs((1.0 - (q.yes_bid + q.yes_ask) / 2.0) - 0.5), 3)
    else:
        token_id = m.yes_id
        price = round(q.yes_ask, 3)
        edge = round(abs((q.yes_bid + q.yes_ask) / 2.0 - 0.5), 3)

    size = risk.compute_risk_sized_amount(STRATEGY_CONFIG.kelly_fraction)
    if size <= 0:
        return

    signal = Signal(
        slug=old_pos.slug,
        token_id=token_id,
        side=side,
        price=price,
        size_usdc=size,
        signal_ns=now_ns,
        binance_ref_price=buf[-1][1] if buf else 0.0,
        binance_ref_ns=buf[-1][0] if buf else now_ns,
        edge_estimate=edge,
        order_type="FOK",
        is_close=False,
    )
    asyncio.create_task(
        execution.receive_signal(signal),
        name=f"reversal_{side}_{now_ns}",
    )


async def _close_position(
    pos: PositionState,
    state: State,
    execution: ExecutionLayer,
    risk: RiskManager,
    reason: str,
) -> bool:
    """BUY nasprotne strani — NE SELL (bug #294). Vrne True ob uspehu."""
    m = state.markets.get(pos.slug)
    q = state.quotes.get(pos.slug)
    if not m or not q:
        return False
    if pos.side == "YES":
        token_id = m.no_id
        close_price = round(1.0 - q.yes_bid, 3)
        close_side = "NO"
    else:
        token_id = m.yes_id
        close_price = round(q.yes_ask, 3)
        close_side = "YES"

    now_ns = time.time_ns()
    buf = state.price_buffer.get("btcusdt")
    signal = Signal(
        slug=pos.slug,
        token_id=token_id,
        side=close_side,
        price=close_price,
        size_usdc=pos.size_usdc,
        signal_ns=now_ns,
        binance_ref_price=buf[-1][1] if buf else 0.0,
        binance_ref_ns=buf[-1][0] if buf else now_ns,
        edge_estimate=0.0,
        order_type="FOK",
        is_close=True,
    )
    result = await execution.receive_signal(signal)
    if result.success:
        shares = pos.size_usdc / pos.entry_price if pos.entry_price > 0 else 0
        # exit vrednost pozicije = nasprotna stran cene (yes_bid za YES, 1-yes_ask za NO)
        exit_value = 1.0 - close_price
        pnl = shares * exit_value - pos.size_usdc - pos.fee_usdc
        state.usdc_balance += shares * exit_value
        risk.on_close(pnl_usdc=pnl)
        await log_exit(
            slug=pos.slug,
            mode=execution.config.mode,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_value,
            size_usdc=pos.size_usdc,
            fee_usdc=pos.fee_usdc,
            reason=reason,
        )
        state.open_positions.pop(pos.side, None)
        log("INFO", "exit", f"{reason} | {pos.side} entry={pos.entry_price:.3f} exit={exit_value:.3f} pnl={pnl:+.2f}")
        notify_exit(pos.side, pos.entry_price, exit_value, pnl, reason, pos.slug)
        return True
    return False
