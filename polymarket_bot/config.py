"""
CONFIG — VSE KONSTANTE NA ENEM MESTU
Polymarket BTC Bot · april 2026
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class Config:
    calibration_window_s: int = 300
    vol_sample_max: int = 2000
    min_samples: int = 30
    sigma_ema_alpha: float = 0.1
    sigma_min: float = 0.00005
    sigma_max: float = 0.01
    sigma_default: float = 0.0004
    min_edge: float = 0.025
    min_profit: float = 0.007
    cooldown_s: int = 30
    no_trade_window_s: int = 30
    stale_binance_s: int = 5
    stale_polymarket_s: int = 10
    binance_reconnect_max_s: int = 60
    polymarket_reconnect_max_s: int = 60
    chainlink_reconnect_max_s: int = 60
    price_buffer_maxlen: int = 120_000


CONFIG = Config()


@dataclass
class ExecutionConfig:
    mode: Literal["paper", "live"] = "paper"
    # order_type je zdaj per-signal (Signal.order_type) — ta je fallback
    min_order_size_usdc: float = 5.0
    max_order_size_usdc: float = 50.0
    fill_timeout_sec: float = 8.0
    cooldown_sec: float = 30.0
    stale_signal_ms: float = 200.0
    simulated_latency_ms: float = 28.0  # Ljubljana → London


EXEC_CONFIG = ExecutionConfig()


@dataclass
class StrategyConfig:
    entry_window_max_s: int = 150   # 50% elapsed = 150s remaining
    entry_window_min_s: int = 45    # 85% elapsed = 45s remaining
    min_gap: float = 0.15           # 0.15 za data collection (prej 0.20)
    maker_mode_enabled: bool = True
    maker_gap_max: float = 0.30     # maker za gap 0.20-0.30
    maker_offset: float = 0.02
    maker_fill_timeout_s: float = 20.0
    vol_shock_enabled: bool = True
    vol_shock_ratio: float = 2.5
    long_sigma_window_s: int = 1800
    trend_filter_enabled: bool = True
    trend_window_s: int = 600
    trend_strong_threshold: float = 0.001
    obi_filter_enabled: bool = True
    obi_levels: int = 10
    obi_min_buy_yes: float = 0.40
    obi_max_buy_no: float = 0.60
    max_trades_per_window: int = 1
    sizing_mode: str = "kelly"
    kelly_fraction: float = 0.50    # half-Kelly
    kelly_max_fraction: float = 0.50
    fixed_size_usdc: float = 10.0
    min_yes_bid: float = 0.05
    max_yes_ask: float = 0.95
    max_spread: float = 0.15
    min_poly_vol: float = 0.12      # stdev YES mid zadnjih 60s


STRATEGY_CONFIG = StrategyConfig()


@dataclass
class RiskConfig:
    max_open_positions: int = 10  # HKRATNIH, ne dnevnih!
    max_drawdown_pct: float = 1.00
    min_risk_pct: float = 0.03
    max_risk_pct: float = 0.05
    paper_initial_balance: float = 200.0
    paper_fok_reject_rate: float = 0.15
    paper_gtc_fill_check_interval_s: float = 2.0
    live_gate_min_trades: int = 50
    live_gate_min_win_rate: float = 0.55
    live_gate_min_sharpe: float = 0.8
    live_gate_max_dd: float = 0.15
    live_gate_hours: int = 48


RISK_CONFIG = RiskConfig()


@dataclass
class ExitConfig:
    stop_loss_cents: float = 0.20
    hold_to_expiry: bool = True
    early_exit_enabled: bool = False


EXIT_CONFIG = ExitConfig()
