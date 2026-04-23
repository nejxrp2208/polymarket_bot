"""
SIZING — Pomožne funkcije za Kelly position sizing.
Glavna logika je v on_new_tick (strategy/signal.py) in
RiskManager.compute_risk_sized_amount (risk/manager.py).
"""

from __future__ import annotations

from config import STRATEGY_CONFIG


def compute_kelly_fraction(
    fair_prob: float,
    entry_price: float,
    fee_per_unit: float,
) -> float:
    """
    Izračunaj quarter Kelly fraction.
    fair_prob: BSM verjetnost za izid
    entry_price: cena tokena (taker ali maker)
    fee_per_unit: fee na enoto (0 za maker)
    Vrne: fraction bankrolla za trade
    """
    if entry_price <= 0 or entry_price >= 1:
        return 0.0
    net_odds = (1.0 - entry_price) / entry_price
    effective_b = net_odds - fee_per_unit / entry_price
    if effective_b <= 0:
        return 0.0
    full_kelly = (
        fair_prob * effective_b - (1 - fair_prob)
    ) / effective_b
    return max(0.0, full_kelly * STRATEGY_CONFIG.kelly_fraction)
