"""
STATE — EN CENTRALEN OBJEKT
Quote, Market, PositionState, FeeConfig, Signal, FillResult, ExecutionResult, State
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field


# ── Data types ──────────────────────────────────────────────


@dataclass
class Quote:
    yes_bid: float
    yes_ask: float
    timestamp_ns: int


@dataclass
class Market:
    slug: str
    yes_id: str
    no_id: str


@dataclass
class PositionState:
    slug: str
    side: str  # "YES" | "NO"
    entry_price: float
    size_usdc: float
    entry_ns: int
    fee_usdc: float = 0.0
    order_type: str = "FOK"
    entry_mode: str = "BSM"  # "BSM" | "PRICE_LVL" | "EXPIRY"


@dataclass
class FeeConfig:
    fee_rate_bps: int = 0
    # Crypto maker rebate = 20% (Polymarket docs, april 2026)
    maker_rebate: float = 0.20

    def fee_usdc(self, price: float, size_usdc: float) -> float:
        """
        NOVA formula april 2026 — brez eksponenta!
        fee = (rate/0.25) * size * p * (1-p)
        Peak pri p=0.50: rate * size (npr. 1.80% * size)
        """
        if self.fee_rate_bps == 0:
            return 0.0
        rate = self.fee_rate_bps / 10_000
        fee = (rate / 0.25) * size_usdc * price * (1.0 - price)
        return round(fee, 5)

    def min_edge_required(self, price: float) -> float:
        return self.fee_usdc(price, 1.0) + 0.007


# ── Execution types ─────────────────────────────────────────


@dataclass
class Signal:
    slug: str
    token_id: str
    side: str  # "YES" | "NO"
    price: float
    size_usdc: float
    signal_ns: int
    binance_ref_price: float
    binance_ref_ns: int
    edge_estimate: float
    order_type: str = "FOK"  # "FOK" ali "GTC" — per-signal!
    is_close: bool = False  # True = close signal, bypass position+edge checks
    mode: str = "BSM"  # "BSM" | "PRICE_LVL" | "EXPIRY"


@dataclass
class FillResult:
    order_id: str
    price: float
    size_usdc: float
    fill_ns: int


@dataclass
class ExecutionResult:
    success: bool
    order_id: str | None = None
    fill_price: float | None = None
    fill_size_usdc: float | None = None
    fee_paid_usdc: float | None = None
    latency_signal_to_submit_ms: float | None = None
    latency_signal_to_fill_ms: float | None = None
    latency_binance_to_fill_ms: float | None = None
    reject_reason: str | None = None


# ── Central State ───────────────────────────────────────────


@dataclass
class State:
    # ── Data feeds ──────────────────────────────────────
    price_buffer: dict[str, deque] = field(default_factory=dict)
    # (timestamp_ns, mid_price) pari, maxlen=120_000

    sigmas: dict[str, float] = field(default_factory=dict)
    sigmas_long: dict[str, float] = field(default_factory=dict)
    # dolgoročna sigma za vol shock detection (30-min EMA)

    quotes: dict[str, Quote] = field(default_factory=dict)
    chainlink_prices: dict[str, float] = field(default_factory=dict)
    polymarket_mid_buffer: dict[str, deque] = field(default_factory=dict)
    # (timestamp_ns, yes_mid) pari, maxlen=600, za vol filter

    # Binance depth10 za OBI filter
    # Vsak entry: lista [(price, qty), ...] top 10 levelov
    binance_bids: dict[str, list] = field(default_factory=dict)
    binance_asks: dict[str, list] = field(default_factory=dict)

    # ── Market mapping ──────────────────────────────────
    markets: dict[str, Market] = field(default_factory=dict)
    token_to_slug: dict[str, str] = field(default_factory=dict)
    window_open_price: dict[str, float] = field(default_factory=dict)
    # KRITIČNO: nastavi na Chainlink ceno ob window boundary!

    # ── Signal tracking ─────────────────────────────────
    last_signal_ns: dict[str, int] = field(default_factory=dict)
    trades_this_window: dict[str, int] = field(default_factory=dict)
    traded_directions: dict[str, set] = field(default_factory=dict)
    # slug → {"YES", "NO"} — kateri side smo že tradali v tem oknu
    _trend_cache: dict = field(default_factory=dict)
    # {window_s: (timestamp_ns, value)} — 500ms cache za get_trend

    # ── Position tracking ───────────────────────────────
    open_positions: dict[str, PositionState] = field(default_factory=dict)
    # key = "YES" | "NO" — obe strani lahko odprti hkrati
    pending_sides: set = field(default_factory=set)
    # strani v letu (signal sprejet, fill še čakamo)

    # ── Balance + execution ──────────────────────────────
    usdc_balance: float = 0.0
    cooldown_until_ns: int = 0
    last_order_id: str | None = None
    last_fill_price: float | None = None
    last_fee_paid_usdc: float = 0.0
    pending_rebate_usdc: float = 0.0
    rebate_period_start_ns: int = 0
    last_user_ws_tick_ns: int = 0
    fee_config: FeeConfig = field(default_factory=FeeConfig)
    pending_fills: dict[str, FillResult] = field(default_factory=dict)
    _order_locks: dict = field(default_factory=dict, repr=False)
    # per-side asyncio.Lock — ustvarjeni lazily pri prvem dostopu

    # ── Risk management ──────────────────────────────────
    peak_balance: float = 0.0
    trading_paused: bool = False
    pause_reason: str = ""
    open_positions_count: int = 0  # HKRATNIH odprtih

    # ── Performance tracking ─────────────────────────────
    pnl_total: float = 0.0  # skupni realiziran PnL
    pnl_today: float = 0.0  # reset ob midnight
    paper_wins: int = 0
    paper_losses: int = 0
    paper_pnl_history: list = field(default_factory=list)
    paper_start_ns: int = 0
    trades_this_session: int = 0

    # ── Health + system ──────────────────────────────────
    last_binance_tick_ns: dict[str, int] = field(default_factory=dict)
    last_polymarket_tick_ns: dict[str, int] = field(default_factory=dict)
    ntp_offset_ns: int = 0
    open_connections: list = field(default_factory=list)

    def snapshot(self) -> dict:
        """Serializabilen snapshot za state_snapshot.json."""
        return {
            "sigmas": self.sigmas,
            "sigmas_long": self.sigmas_long,
            "window_open_price": self.window_open_price,
            "usdc_balance": self.usdc_balance,
            "pnl_total": self.pnl_total,
            "peak_balance": self.peak_balance,
            "paper_wins": self.paper_wins,
            "paper_losses": self.paper_losses,
            "paper_pnl_history": self.paper_pnl_history,
            "timestamp": time.time(),
        }
