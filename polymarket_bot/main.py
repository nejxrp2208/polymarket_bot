#!/usr/bin/env python3
"""
POLYMARKET BTC BOT — MAIN ENTRY POINT
10 asyncio taskov + dashboard = 11 skupaj v TaskGroup.
Graceful shutdown + state snapshot.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import gc
import json
import os
import platform
import signal as _signal
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import refs
from config import (
    EXEC_CONFIG,
    RISK_CONFIG,
    ExecutionConfig,
)
from core.discovery import (
    discovery_task,
    init_current_market,
    wait_for_first_binance_tick,
)
from core.exit_monitor import exit_monitor_task
from utils.telegram import notify_start, notify_stop
from core.health import health_monitor_task, lag_monitor_task
from core.vol_calibrator import vol_calibrator_task
from dashboard.ui import dashboard_task
from execution.layer import (
    ExecutionLayer,
    build_clob_client,
    fetch_usdc_balance,
)
from feeds.binance import binance_stream_with_reconnect
from feeds.chainlink import chainlink_worker_with_reconnect
from feeds.polymarket import polymarket_stream_with_reconnect
from logging_.db import (
    flush_all,
    flush_all_sync,
    init_db,
    log_btc_price,
    session_stats,
)
from risk.daily_reset import daily_reset_task
from risk.manager import RiskManager
from state import State
from utils.helpers import (
    current_window_ts,
    get_ntp_offset_ns,
    get_slug,
    log,
)

_IS_WINDOWS = platform.system() == "Windows"


# ── Periodic tasks ───────────────────────────────────────────


async def ntp_resync_task(state: State) -> None:
    while True:
        await asyncio.sleep(300)
        try:
            loop = asyncio.get_running_loop()
            offset = await loop.run_in_executor(None, get_ntp_offset_ns)
            state.ntp_offset_ns = offset
            log("INFO", "ntp", f"resync offset={offset / 1e6:+.1f}ms")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("WARN", "ntp", f"resync napaka: {e}")


async def btc_price_logger_task(state: State) -> None:
    while True:
        try:
            await asyncio.sleep(1.0)
            buf = state.price_buffer.get("btcusdt")
            if buf:
                price = buf[-1][1]
                sigma = state.sigmas.get("btcusdt", 0.0)
                await log_btc_price(price, sigma)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


async def snapshot_task(state: State) -> None:
    while True:
        await asyncio.sleep(300)
        try:
            snap = state.snapshot()
            with open("state_snapshot.json", "w") as f:
                json.dump(snap, f)
            log("INFO", "system", "snapshot shranjen")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("WARN", "system", f"snapshot napaka: {e}")


# ── Graceful shutdown ────────────────────────────────────────


async def shutdown(state: State) -> None:
    log("INFO", "system", "shutdown...")
    await flush_all()
    session_stats()
    snap = state.snapshot()
    with open("state_snapshot.json", "w") as f:
        json.dump(snap, f)
    for ws in state.open_connections:
        try:
            await ws.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
    notify_stop(state.usdc_balance, state.pnl_today, state.paper_wins, state.paper_losses)
    import time; time.sleep(1)  # počakaj da thread pošlje
    log("INFO", "system", "clean shutdown")


def load_state_snapshot(state: State) -> None:
    p = Path("state_snapshot.json")
    if not p.exists():
        return
    try:
        with open(p) as f:
            snap = json.load(f)
        state.sigmas = snap.get("sigmas", {})
        state.sigmas_long = snap.get("sigmas_long", {})
        state.window_open_price = snap.get("window_open_price", {})
        state.usdc_balance = snap.get("usdc_balance", 0.0)
        state.pnl_total = snap.get("pnl_total", 0.0)
        state.peak_balance = snap.get("peak_balance", 0.0)
        state.paper_wins = snap.get("paper_wins", 0)
        state.paper_losses = snap.get("paper_losses", 0)
        state.paper_pnl_history = snap.get("paper_pnl_history", [])
        age_s = time.time() - snap.get("timestamp", 0)
        log(
            "INFO",
            "system",
            f"snapshot nalozhen (star {age_s / 60:.1f}min)",
        )
        if age_s > 3600:
            state.sigmas = {}
            state.sigmas_long = {}
            log(
                "WARN",
                "system",
                f"snapshot > 60min — sigma resetirana, čakam na vol_calibrator",
            )
        elif age_s > 1800:
            log(
                "WARN",
                "system",
                "snapshot > 30min — sigma bo nezanesljiva",
            )
    except Exception as e:
        log("WARN", "system", f"snapshot napaka: {e}")


# ── Main ─────────────────────────────────────────────────────


async def main() -> None:
    gc.set_threshold(50_000, 500, 50)
    if _IS_WINDOWS:
        asyncio.set_event_loop_policy(
            asyncio.WindowsSelectorEventLoopPolicy()
        )
        try:
            import psutil

            psutil.Process(os.getpid()).nice(
                psutil.HIGH_PRIORITY_CLASS
            )
        except Exception as e:
            log("WARN", "system", f"psutil priority: {e}")

    state = State()
    refs.global_state = state
    state.ntp_offset_ns = get_ntp_offset_ns()

    init_db()
    load_state_snapshot(state)

    # Inicializacija balance
    exec_cfg = ExecutionConfig(mode="paper")
    clob = build_clob_client()

    if exec_cfg.mode == "paper":
        state.usdc_balance = RISK_CONFIG.paper_initial_balance
        state.peak_balance = RISK_CONFIG.paper_initial_balance
        state.paper_start_ns = time.time_ns()
        log(
            "INFO",
            "risk",
            f"[PAPER] Start: "
            f"{RISK_CONFIG.paper_initial_balance} USDC",
        )
    else:
        state.usdc_balance = await fetch_usdc_balance(clob)
        state.peak_balance = state.usdc_balance

    clob_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=10, thread_name_prefix="ClobWorker"
    )
    asyncio.get_running_loop().set_default_executor(clob_executor)

    execution = ExecutionLayer(clob, exec_cfg, state)
    risk = RiskManager(RISK_CONFIG, state)

    # Nastavi globalne reference za stream callbacke
    refs.execution_ref = execution
    refs.risk_ref = risk

    # Fee rate fetch ob inicializaciji
    try:
        active = get_slug(current_window_ts())
        if active in state.markets:
            m = state.markets[active]
            fee_resp = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: clob.get_fee_rate(m.yes_id),
            )
            state.fee_config.fee_rate_bps = int(
                fee_resp.get("fee_rate_bps", 0)
            )
    except Exception as e:
        log("WARN", "main", f"fee rate fetch: {e}")

    # Warmup — počakaj na prvi Binance tik
    warmup = asyncio.create_task(
        binance_stream_with_reconnect(state),
        name="binance_warmup",
    )
    ok = await wait_for_first_binance_tick(state, timeout_s=5.0)
    if not ok:
        log("WARN", "system", "Binance warmup timeout — nadaljujem")
    warmup.cancel()
    try:
        await warmup
    except asyncio.CancelledError:
        pass

    await init_current_market(state)
    notify_start(exec_cfg.mode, state.usdc_balance)

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (_signal.SIGINT, _signal.SIGTERM):
            loop.add_signal_handler(
                sig,
                lambda: asyncio.create_task(shutdown(state)),
            )

    # ── TaskGroup: 11 taskov ─────────────────────────────
    async with asyncio.TaskGroup() as tg:
        tg.create_task(
            binance_stream_with_reconnect(state),
            name="binance",
        )
        tg.create_task(
            polymarket_stream_with_reconnect(state),
            name="polymarket",
        )
        tg.create_task(
            chainlink_worker_with_reconnect("btc/usd", state),
            name="cl_btc",
        )
        tg.create_task(
            chainlink_worker_with_reconnect("eth/usd", state),
            name="cl_eth",
        )
        tg.create_task(
            vol_calibrator_task(state),
            name="vol_cal",
        )
        tg.create_task(
            discovery_task(state),
            name="discovery",
        )
        tg.create_task(
            exit_monitor_task(state, execution, risk),
            name="exit_monitor",
        )
        tg.create_task(
            health_monitor_task(state),
            name="health",
        )
        tg.create_task(
            daily_reset_task(state, risk),
            name="daily_reset",
        )
        tg.create_task(
            lag_monitor_task(),
            name="lag",
        )
        tg.create_task(
            dashboard_task(state, refs.trade_log),
            name="dashboard",
        )
        tg.create_task(
            ntp_resync_task(state),
            name="ntp_resync",
        )
        tg.create_task(
            snapshot_task(state),
            name="snapshot",
        )
        tg.create_task(
            btc_price_logger_task(state),
            name="btc_logger",
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        if refs.global_state is not None:
            flush_all_sync()
            session_stats()
            try:
                snap = refs.global_state.snapshot()
                with open("state_snapshot.json", "w") as f:
                    json.dump(snap, f)
            except Exception:
                pass
        print("[SHUTDOWN] Ustavljeno (Ctrl+C)")
