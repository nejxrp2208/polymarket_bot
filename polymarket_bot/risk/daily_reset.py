"""
DAILY RESET + LIVE GATE VALIDACIJA
daily_reset_task: midnight UTC reset pnl_today
validate_paper_performance: 5 pogojev pred live modom
"""

from __future__ import annotations

import asyncio
import statistics
import time

from config import RiskConfig
from risk.manager import RiskManager
from state import State
from utils.helpers import log


async def daily_reset_task(
    state: State, risk: RiskManager
) -> None:
    while True:
        try:
            now = time.time()
            next_mid = (int(now // 86400) + 1) * 86400
            await asyncio.sleep(next_mid - now)
            if state.pnl_today != 0:
                state.paper_pnl_history.append(state.pnl_today)
            log(
                "INFO",
                "risk",
                f"DAILY RESET | pnl={state.pnl_today:+.3f}",
            )
            state.pnl_today = 0.0
            if state.trading_paused:
                log(
                    "WARN",
                    "risk",
                    "Trading ostaja pausirano — rocni restart",
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("ERROR", "risk", str(e))


def validate_paper_performance(
    state: State, config: RiskConfig
) -> tuple[bool, list[str]]:
    """Poklicej PRED spremembo mode='live'."""
    issues: list[str] = []
    total = state.paper_wins + state.paper_losses
    if total < config.live_gate_min_trades:
        issues.append(
            f"Premalo tradov: {total} < {config.live_gate_min_trades}"
        )
    if total > 0:
        wr = state.paper_wins / total
        if wr < config.live_gate_min_win_rate:
            issues.append(
                f"Win rate: {wr * 100:.1f}% < "
                f"{config.live_gate_min_win_rate * 100:.0f}%"
            )
    if state.peak_balance > 0:
        dd = (
            state.peak_balance - state.usdc_balance
        ) / state.peak_balance
        if dd > config.live_gate_max_dd:
            issues.append(
                f"DD: {dd * 100:.1f}% > "
                f"{config.live_gate_max_dd * 100:.0f}%"
            )
    if state.paper_start_ns > 0:
        eh = (time.time_ns() - state.paper_start_ns) / 3.6e12
        if eh < config.live_gate_hours:
            issues.append(
                f"Cas: {eh:.1f}h < {config.live_gate_hours}h"
            )
    if len(state.paper_pnl_history) >= 2:
        mean = statistics.mean(state.paper_pnl_history)
        std = statistics.stdev(state.paper_pnl_history)
        sharpe = mean / std if std > 0 else 0.0
        if sharpe < config.live_gate_min_sharpe:
            issues.append(
                f"Sharpe: {sharpe:.2f} < {config.live_gate_min_sharpe}"
            )
    passed = len(issues) == 0
    status = (
        "USPESEN" if passed else f"NI USPEL ({len(issues)} težav)"
    )
    log(
        "INFO" if passed else "WARN",
        "risk",
        f"LIVE GATE {status}",
    )
    for i in issues:
        log("WARN", "risk", f"  - {i}")
    return passed, issues
