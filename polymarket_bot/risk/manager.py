"""
RISK MANAGER
can_trade, compute_risk_sized_amount, on_fill, on_close, _check_drawdown
"""

from __future__ import annotations

import refs
from config import EXEC_CONFIG, RiskConfig
from state import State
from utils.helpers import log


class RiskManager:

    def __init__(self, config: RiskConfig, state: State):
        self.cfg = config
        self.state = state

    def can_trade(self) -> tuple[bool, str]:
        """Klicej na začetku on_new_tick()."""
        if self.state.trading_paused:
            return False, self.state.pause_reason
        if (
            self.state.open_positions_count
            >= self.cfg.max_open_positions
        ):
            return False, (
                f"max open: "
                f"{self.state.open_positions_count}/"
                f"{self.cfg.max_open_positions}"
            )
        if (
            self.state.usdc_balance
            < EXEC_CONFIG.min_order_size_usdc + 0.50
        ):
            return False, f"balance: {self.state.usdc_balance:.2f}"
        return True, ""

    def compute_risk_sized_amount(
        self, kelly_fraction: float
    ) -> float:
        """Quarter Kelly clamped na 5%-8% bankrolla."""
        clamped = max(
            self.cfg.min_risk_pct,
            min(self.cfg.max_risk_pct, kelly_fraction),
        )
        size = clamped * self.state.usdc_balance
        return round(
            max(
                EXEC_CONFIG.min_order_size_usdc,
                min(EXEC_CONFIG.max_order_size_usdc, size),
            ),
            2,
        )

    def on_fill(self) -> None:
        """Klicej iz execution._on_fill()."""
        self.state.open_positions_count += 1
        if self.state.usdc_balance > self.state.peak_balance:
            self.state.peak_balance = self.state.usdc_balance

    def on_close(self, pnl_usdc: float) -> None:
        """Klicej ob zaprtju pozicije."""
        self.state.open_positions_count = max(
            0, self.state.open_positions_count - 1
        )
        self.state.pnl_total += pnl_usdc
        self.state.pnl_today += pnl_usdc
        if pnl_usdc > 0:
            self.state.paper_wins += 1
        else:
            self.state.paper_losses += 1
        if self.state.usdc_balance > self.state.peak_balance:
            self.state.peak_balance = self.state.usdc_balance

        # Posodobi trade log za dashboard
        for e in reversed(refs.trade_log):
            if e.get("outcome") == "PENDING":
                e["outcome"] = "WIN" if pnl_usdc > 0 else "LOSS"
                e["pnl"] = pnl_usdc
                break

    def _check_drawdown(self) -> None:
        if self.state.peak_balance <= 0:
            return
        dd = (
            self.state.peak_balance - self.state.usdc_balance
        ) / self.state.peak_balance
        if (
            dd >= self.cfg.max_drawdown_pct
            and not self.state.trading_paused
        ):
            self.state.trading_paused = True
            self.state.pause_reason = (
                f"20% DD: peak={self.state.peak_balance:.2f} "
                f"current={self.state.usdc_balance:.2f} "
                f"dd={dd * 100:.1f}%"
            )
            log(
                "CRITICAL",
                "risk",
                f"TRADING PAUSIRANO — {self.state.pause_reason}",
            )
        elif (
            self.state.trading_paused
            and self.state.pause_reason.startswith("20% DD")
            and dd < self.cfg.max_drawdown_pct * 0.5
        ):
            self.state.trading_paused = False
            self.state.pause_reason = ""
            log(
                "INFO",
                "risk",
                f"TRADING ODBLOKIRAN — dd={dd * 100:.1f}% (pod 10%)",
            )
