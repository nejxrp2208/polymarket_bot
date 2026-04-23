"""
REFS — Globalne mutable reference.
Nastavi jih main() po kreiranju objektov.
Feeds in strategy jih berejo.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from execution.layer import ExecutionLayer
    from risk.manager import RiskManager
    from state import State

execution_ref: ExecutionLayer | None = None
risk_ref: RiskManager | None = None
trade_log: list[dict] = []
global_state: State | None = None
