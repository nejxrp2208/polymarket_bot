"""
STRATEGY LAYER — BINANCE LAG ARBITRAGE

FILOZOFIJA:
  Binance premakne BTC → Polymarket zamudi 30-90s → vstopimo v lag oknu.
  window_open_price = Chainlink (resolution reference)
  current_price     = Binance (8s pred Chainlinkom = naš edge)
  z = (binance_current - chainlink_open) / (sigma * sqrt(t))

DVA NAČINA VSTOPA:
  GTC MAKER (gap 0.12-0.18): limit @ ask-2c, fee=0%, +20% rebate
  FOK TAKER (gap > 0.18):    instant fill, fee=1.80% pri p=0.50
"""

from __future__ import annotations

import asyncio
import math
import statistics
import time
from typing import TYPE_CHECKING

from scipy.special import ndtr as norm_cdf

from config import CONFIG, FAST_SCALP_CONFIG, STRATEGY_CONFIG, ZONE_FLIP_CONFIG
from logging_.db import log_signal
from state import Signal, State
from utils.helpers import log, seconds_until_rollover

if TYPE_CHECKING:
    from execution.layer import ExecutionLayer
    from risk.manager import RiskManager


# ── Pomožne signal funkcije ─────────────────────────────────


def vol_shock_detected(state: State) -> bool:
    if not STRATEGY_CONFIG.vol_shock_enabled:
        return False
    recent = state.sigmas.get("btcusdt", 0)
    long = state.sigmas_long.get("btcusdt", 0)
    if long <= 0:
        return False
    if recent / long > STRATEGY_CONFIG.vol_shock_ratio:
        log(
            "WARN",
            "strategy",
            f"vol shock: ratio={recent / long:.1f}x → pause",
        )
        return True
    return False


def get_trend(state: State, window_s: int) -> float:
    now_ns = time.time_ns()
    cached = state._trend_cache.get(window_s)
    if cached and now_ns - cached[0] < 500_000_000:
        return cached[1]
    buf = state.price_buffer.get("btcusdt")
    if not buf or len(buf) < 2:
        return 0.0
    cutoff_ns = now_ns - window_s * 1_000_000_000
    oldest = None
    for i in range(len(buf) - 1, -1, -1):
        ts, p = buf[i]
        if ts <= cutoff_ns:
            oldest = p
            break
    if oldest is None or oldest <= 0:
        return 0.0
    val = (buf[-1][1] - oldest) / oldest
    state._trend_cache[window_s] = (now_ns, val)
    return val


def get_10min_trend(state: State) -> float:
    return get_trend(state, STRATEGY_CONFIG.trend_window_s)


def momentum_filter_passes(direction: str, state: State) -> bool:
    """Blokira vstop če 60s momentum kaže jasno nasprotno smer."""
    trend_60s = get_trend(state, 60)
    threshold = 0.0005  # 0.05% v 60s = jasen momentum
    if direction == "BUY_YES" and trend_60s < -threshold:
        log("DEBUG", "strategy", f"momentum filter: BUY_YES zavrnjen (60s trend={trend_60s*100:.3f}%)")
        return False
    if direction == "BUY_NO" and trend_60s > threshold:
        log("DEBUG", "strategy", f"momentum filter: BUY_NO zavrnjen (60s trend={trend_60s*100:.3f}%)")
        return False
    return True


def trend_filter_passes(direction: str, state: State) -> bool:
    if not STRATEGY_CONFIG.trend_filter_enabled:
        return True
    trend = get_10min_trend(state)
    t = STRATEGY_CONFIG.trend_strong_threshold
    if direction == "BUY_YES" and trend < -t:
        log(
            "DEBUG",
            "strategy",
            f"trend filter: BUY_YES zavrnjen ({trend * 100:.2f}%)",
        )
        return False
    if direction == "BUY_NO" and trend > t:
        log(
            "DEBUG",
            "strategy",
            f"trend filter: BUY_NO zavrnjen ({trend * 100:.2f}%)",
        )
        return False
    return True


def compute_obi(
    bids: list, asks: list, levels: int = 10
) -> float:
    bv = sum(p * q for p, q in bids[:levels])
    av = sum(p * q for p, q in asks[:levels])
    tot = bv + av
    return bv / tot if tot > 0 else 0.5


def polymarket_vol_filter_passes(slug: str, state: State) -> bool:
    buf = state.polymarket_mid_buffer.get(slug)
    if not buf or len(buf) < 10:
        return True
    vol = statistics.stdev(mid for _, mid in buf)
    if vol < STRATEGY_CONFIG.min_poly_vol:
        log("DEBUG", "strategy", f"poly vol filter: zavrnjen (vol={vol:.3f})")
        return False
    return True


def obi_filter_passes(direction: str, state: State) -> bool:
    if not STRATEGY_CONFIG.obi_filter_enabled:
        return True
    bids = state.binance_bids.get("btcusdt")
    asks = state.binance_asks.get("btcusdt")
    if not bids or not asks:
        return True  # ni podatkov → ne kaznujemo
    obi = compute_obi(bids, asks, STRATEGY_CONFIG.obi_levels)
    if (
        direction == "BUY_YES"
        and obi < STRATEGY_CONFIG.obi_min_buy_yes
    ):
        log(
            "DEBUG",
            "strategy",
            f"OBI: BUY_YES zavrnjen (obi={obi:.2f})",
        )
        return False
    if (
        direction == "BUY_NO"
        and obi > STRATEGY_CONFIG.obi_max_buy_no
    ):
        log(
            "DEBUG",
            "strategy",
            f"OBI: BUY_NO zavrnjen (obi={obi:.2f})",
        )
        return False
    return True


# ── Core Signal ──────────────────────────────────────────────


def compute_strategy_signal(
    slug: str, state: State, now_ns: int
) -> dict | None:
    cfg = STRATEGY_CONFIG

    # 1. Direction dedup — ne ponavljaj iste smeri v istem oknu
    direction_check_yes = state.traded_directions.get(slug, set())
    # (preverimo po izračunu fair_yes, spodaj)
    # 2. Časovni filter
    window_remaining_s = seconds_until_rollover()
    if not (
        cfg.entry_window_min_s
        <= window_remaining_s
        <= cfg.entry_window_max_s
    ):
        return None
    # 3. Vol shock
    if vol_shock_detected(state):
        return None
    # 4. Cene
    cl_open = state.window_open_price.get(slug)
    if not cl_open or cl_open <= 0:
        return None
    buf = state.price_buffer.get("btcusdt")
    if not buf:
        return None
    bn_now = buf[-1][1]
    sigma = state.sigmas.get("btcusdt", CONFIG.sigma_default) or CONFIG.sigma_default
    m = state.quotes.get(slug)
    if not m:
        return None
    # 5. Stale + market filter
    if (now_ns - m.timestamp_ns) / 1e9 > CONFIG.stale_polymarket_s:
        return None
    if m.yes_ask - m.yes_bid > cfg.max_spread:
        return None
    if m.yes_bid < cfg.min_yes_bid or m.yes_ask > cfg.max_yes_ask:
        return None
    # 6. BSM fair value z blagim Bayesian blendom (90% BSM, 10% market mid)
    price_change = (bn_now - cl_open) / cl_open
    z = price_change / (sigma * math.sqrt(window_remaining_s))
    bsm_fair = norm_cdf(z)
    yes_mid = (m.yes_bid + m.yes_ask) / 2.0
    fair_yes = 0.90 * bsm_fair + 0.10 * yes_mid
    if fair_yes >= 0.5:
        direction = "BUY_YES"
        taker_price = m.yes_ask
        gap = fair_yes - taker_price
    else:
        direction = "BUY_NO"
        taker_price = 1.0 - m.yes_bid
        gap = (1.0 - fair_yes) - taker_price
    # 6b. Direction dedup
    side = "YES" if direction == "BUY_YES" else "NO"
    if side in state.traded_directions.get(slug, set()):
        return None
    # 7. Gap filter (exec_edge >= min_gap)
    if gap < cfg.min_gap:
        return None
    if gap < state.fee_config.min_edge_required(taker_price):
        return None
    # 8. Trend filter
    if not trend_filter_passes(direction, state):
        return None
    # 8b. Momentum filter (60s)
    if not momentum_filter_passes(direction, state):
        return None
    # 9. OBI filter
    if not obi_filter_passes(direction, state):
        return None
    # 9b. Polymarket vol filter (stdev YES mid 60s >= 0.12)
    if not polymarket_vol_filter_passes(slug, state):
        return None
    # 10. Order type + entry price
    if cfg.maker_mode_enabled and cfg.min_gap <= gap <= cfg.maker_gap_max:
        order_type = "GTC"
        entry_price = round(
            max(0.01, min(0.99, taker_price - cfg.maker_offset)), 3
        )
        mode_label = "MAKER"
    else:
        order_type = "FOK"
        entry_price = taker_price
        mode_label = "TAKER"
    # 11. Time weight (logging)
    tw = 1.0 - (
        (window_remaining_s - cfg.entry_window_min_s)
        / (cfg.entry_window_max_s - cfg.entry_window_min_s)
    )
    tw = max(0.0, min(1.0, tw))
    bids = state.binance_bids.get("btcusdt", [])
    asks = state.binance_asks.get("btcusdt", [])
    obi = compute_obi(bids, asks) if bids and asks else 0.5

    log(
        "INFO",
        "strategy",
        f"[{mode_label}] {direction} @ {entry_price:.3f} "
        f"gap={gap:.3f} fair={fair_yes:.3f} "
        f"Δbtc={price_change * 100:+.3f}% "
        f"obi={obi:.2f} window={window_remaining_s:.0f}s",
    )

    return {
        "slug": slug,
        "direction": direction,
        "taker_price": entry_price,
        "exec_edge": gap,
        "fair": fair_yes,
        "yes_bid": m.yes_bid,
        "yes_ask": m.yes_ask,
        "window_remaining_s": window_remaining_s,
        "order_type": order_type,
        "mode_label": mode_label,
        "time_weight": tw,
        "obi": obi,
        "binance_delta_pct": price_change * 100,
        "chainlink_open": cl_open,
        "binance_current": bn_now,
    }


# ── PRICE LEVEL SIGNAL — zadnjih 150s ───────────────────────


def compute_price_level_signal(
    slug: str, state: State, now_ns: int
) -> dict | None:
    """Disabled — PRICE_LVL strategija povzroča preveč lažnih vhodov."""
    return None
    window_remaining_s = seconds_until_rollover()
    if window_remaining_s > 150:
        return None
    m = state.quotes.get(slug)
    if not m:
        return None
    if (now_ns - m.timestamp_ns) / 1e9 > CONFIG.stale_polymarket_s:
        return None
    mid = (m.yes_bid + m.yes_ask) / 2.0
    if 0.65 <= mid <= 0.75:
        direction = "BUY_YES"
        side = "YES"
        taker_price = m.yes_ask
    elif 0.25 <= mid <= 0.35:
        direction = "BUY_NO"
        side = "NO"
        taker_price = 1.0 - m.yes_bid
    else:
        return None
    # Direction dedup
    if side in state.traded_directions.get(slug, set()):
        return None
    edge = abs(mid - 0.5) - 0.15  # koliko je nad pragom 0.65/0.35
    if edge <= 0:
        return None
    buf = state.price_buffer.get("btcusdt")
    bn_now = buf[-1][1] if buf else 0.0
    log(
        "INFO",
        "strategy",
        f"[PRICE_LVL] {direction} @ {taker_price:.3f} "
        f"mid={mid:.3f} window={window_remaining_s:.0f}s",
    )
    return {
        "slug": slug,
        "direction": direction,
        "taker_price": taker_price,
        "exec_edge": edge,
        "fair": mid,
        "yes_bid": m.yes_bid,
        "yes_ask": m.yes_ask,
        "window_remaining_s": window_remaining_s,
        "order_type": "FOK",
        "mode_label": "PRICE_LVL",
        "binance_current": bn_now,
    }


# ── EXPIRY SIGNAL — zadnjih 130s ────────────────────────────


def compute_expiry_signal(
    slug: str, state: State, now_ns: int
) -> dict | None:
    window_remaining_s = seconds_until_rollover()
    if window_remaining_s > 130:
        return None

    cl_open = state.window_open_price.get(slug)
    if not cl_open or cl_open <= 0:
        return None
    buf = state.price_buffer.get("btcusdt")
    if not buf:
        return None
    bn_now = buf[-1][1]
    sigma = state.sigmas.get("btcusdt", CONFIG.sigma_default) or CONFIG.sigma_default
    m = state.quotes.get(slug)
    if not m:
        return None
    if (now_ns - m.timestamp_ns) / 1e9 > CONFIG.stale_polymarket_s:
        return None

    price_change = (bn_now - cl_open) / cl_open
    z = price_change / (sigma * math.sqrt(max(window_remaining_s, 1)))
    fair_yes = norm_cdf(z)

    if fair_yes >= 0.70:
        direction = "BUY_YES"
        taker_price = m.yes_ask
        fair_prob = fair_yes
        if m.yes_ask < 0.30 or m.yes_ask > 0.70:
            return None
    elif fair_yes <= 0.30:
        direction = "BUY_NO"
        taker_price = 1.0 - m.yes_bid
        fair_prob = 1.0 - fair_yes
        if taker_price < 0.30 or taker_price > 0.70:
            return None
    else:
        return None

    log(
        "INFO",
        "strategy",
        f"[EXPIRY] {direction} @ {taker_price:.3f} "
        f"fair={fair_yes:.3f} Δbtc={price_change * 100:+.3f}% "
        f"window={window_remaining_s:.0f}s",
    )

    return {
        "slug": slug,
        "direction": direction,
        "taker_price": taker_price,
        "exec_edge": abs(fair_yes - 0.5),
        "fair": fair_yes,
        "fair_prob": fair_prob,
        "yes_bid": m.yes_bid,
        "yes_ask": m.yes_ask,
        "window_remaining_s": window_remaining_s,
        "order_type": "FOK",
        "mode_label": "EXPIRY",
        "binance_current": bn_now,
        "chainlink_open": cl_open,
    }


# ── FAST SCALP SIGNAL — kratkoročni momentum ────────────────


def compute_fast_scalp_signal(
    slug: str, state: State, now_ns: int
) -> dict | None:
    cfg = FAST_SCALP_CONFIG
    if not cfg.enabled:
        return None
    window_remaining_s = seconds_until_rollover()
    if not (cfg.entry_window_min_s <= window_remaining_s <= cfg.entry_window_max_s):
        return None
    m = state.quotes.get(slug)
    if not m:
        return None
    if (now_ns - m.timestamp_ns) / 1e9 > CONFIG.stale_polymarket_s:
        return None
    if m.yes_ask - m.yes_bid > cfg.max_spread:
        return None
    yes_mid = (m.yes_bid + m.yes_ask) / 2.0
    if not (cfg.min_yes_mid <= yes_mid <= cfg.max_yes_mid):
        return None
    # Momentum: BTC premik v zadnjih momentum_window_s sekundah
    momentum = get_trend(state, cfg.momentum_window_s)
    if abs(momentum) < cfg.min_momentum_pct / 100.0:
        return None
    buf = state.price_buffer.get("btcusdt")
    if not buf:
        return None
    bn_now = buf[-1][1]
    if momentum > 0:
        direction = "BUY_YES"
        side = "YES"
        taker_price = round(m.yes_ask, 3)
        edge = yes_mid - 0.5
    else:
        direction = "BUY_NO"
        side = "NO"
        taker_price = round(1.0 - m.yes_bid, 3)
        edge = 0.5 - yes_mid
    if edge <= 0:
        return None
    # Ne vstopi v že aktivno smer
    if side in state.open_positions or side in state.pending_sides:
        return None
    log(
        "INFO",
        "strategy",
        f"[FAST_SCALP] {direction} @ {taker_price:.3f} "
        f"momentum={momentum*100:+.3f}% mid={yes_mid:.3f} window={window_remaining_s:.0f}s",
    )
    return {
        "slug": slug,
        "direction": direction,
        "taker_price": taker_price,
        "exec_edge": round(edge, 3),
        "fair": yes_mid,
        "yes_bid": m.yes_bid,
        "yes_ask": m.yes_ask,
        "window_remaining_s": window_remaining_s,
        "order_type": "FOK",
        "mode_label": "FAST_SCALP",
        "binance_current": bn_now,
    }


# ── ZONE FLIP SIGNAL — BSM cona 0.67-0.70 zadnjih 120s ─────


def compute_zone_flip_signal(
    slug: str, state: State, now_ns: int
) -> dict | None:
    cfg = ZONE_FLIP_CONFIG
    if not cfg.enabled:
        return None

    window_remaining_s = seconds_until_rollover()
    if window_remaining_s > cfg.entry_window_max_s:
        return None

    # Že v zone_flip poziciji za ta slug?
    for pos in state.zone_flip_positions.values():
        if pos.slug == slug:
            return None

    # Reversal že narejen — ne vstopamo znova iz normalnega signala
    if state.zone_flip_reversed.get(slug, False):
        return None

    buf = state.price_buffer.get("btcusdt")
    if not buf or len(buf) < 2:
        return None
    bn_now = buf[-1][1]

    cl_open = state.window_open_price.get(slug)
    if not cl_open or cl_open <= 0:
        return None

    sigma = state.sigmas.get("btcusdt", CONFIG.sigma_default) or CONFIG.sigma_default

    q = state.quotes.get(slug)
    if q is None:
        return None
    if (now_ns - q.timestamp_ns) / 1e9 > CONFIG.stale_polymarket_s:
        return None
    if q.yes_ask - q.yes_bid > cfg.max_spread:
        return None

    price_change = (bn_now - cl_open) / cl_open
    z = price_change / (sigma * math.sqrt(max(window_remaining_s, 1)))
    bsm_fair = norm_cdf(z)
    yes_mid = (q.yes_bid + q.yes_ask) / 2.0
    fair_yes = 0.90 * bsm_fair + 0.10 * yes_mid

    in_yes_zone = cfg.entry_zone_low <= fair_yes <= cfg.entry_zone_high
    # NO cona: fair_yes med 0.30-0.33 (NO token fair = 1 - fair_yes = 0.67-0.70)
    in_no_zone = (1.0 - cfg.entry_zone_high) <= fair_yes <= (1.0 - cfg.entry_zone_low)

    if not in_yes_zone and not in_no_zone:
        return None

    if in_yes_zone:
        direction = "BUY_YES"
        side = "YES"
        taker_price = round(q.yes_ask, 3)
        fair_token = fair_yes
    else:
        direction = "BUY_NO"
        side = "NO"
        taker_price = round(1.0 - q.yes_bid, 3)
        fair_token = 1.0 - fair_yes

    # Ne vstopamo če je Polymarket že reagiral
    if taker_price > fair_token + 0.03:
        return None

    log(
        "INFO", "strategy",
        f"[ZONE_FLIP] {direction} | fair={fair_yes:.3f} entry={taker_price:.3f} "
        f"yes_mid={yes_mid:.3f} window={window_remaining_s:.0f}s",
    )

    return {
        "slug": slug,
        "direction": direction,
        "side": side,
        "taker_price": taker_price,
        "exec_edge": round(abs(fair_token - taker_price), 3),
        "fair": fair_yes,
        "yes_bid": q.yes_bid,
        "yes_ask": q.yes_ask,
        "window_remaining_s": window_remaining_s,
        "order_type": "FOK",
        "mode_label": "ZONE_FLIP",
        "binance_current": bn_now,
        "chainlink_open": cl_open,
    }


# ── ON NEW TICK — SKUPNI ENTRY POINT ────────────────────────


def on_new_tick(
    slug: str,
    state: State,
    now_ns: int,
    execution: ExecutionLayer | None,
    risk: RiskManager | None,
) -> None:
    """Klicej iz binance_stream() in polymarket_stream()."""
    if execution is None or risk is None:
        return

    # Risk check PRVO
    can, reason = risk.can_trade()
    if not can:
        return

    buf = state.price_buffer.get("btcusdt")

    # FAST_SCALP signal — dedup je v compute_fast_scalp_signal + execution prechecks
    fs = compute_fast_scalp_signal(slug, state, now_ns)
    if fs is not None:
        m_fs = state.markets.get(slug)
        if m_fs:
            fs_size = risk.compute_risk_sized_amount(FAST_SCALP_CONFIG.kelly_fraction)
            if fs_size > 0:
                fs_side = "YES" if fs["direction"] == "BUY_YES" else "NO"
                asyncio.create_task(
                    execution.receive_signal(Signal(
                        slug=slug,
                        token_id=(m_fs.yes_id if fs["direction"] == "BUY_YES" else m_fs.no_id),
                        side=fs_side,
                        price=fs["taker_price"],
                        size_usdc=fs_size,
                        signal_ns=now_ns,
                        binance_ref_price=fs["binance_current"],
                        binance_ref_ns=buf[-1][0] if buf else now_ns,
                        edge_estimate=fs["exec_edge"],
                        order_type="FOK",
                        mode="FAST_SCALP",
                    )),
                    name=f"fastscalp_{slug}_{now_ns}",
                )
                asyncio.create_task(log_signal(
                    slug=slug, signal_dict=fs, scored=None,
                    sigma=state.sigmas.get("btcusdt", 0.0),
                    btc_price=fs["binance_current"],
                    sent=True, mode_label="FAST_SCALP",
                ))

    # ZONE_FLIP signal
    zf = compute_zone_flip_signal(slug, state, now_ns)
    if zf is not None:
        m_zf = state.markets.get(slug)
        if m_zf:
            zf_size = risk.compute_risk_sized_amount(ZONE_FLIP_CONFIG.kelly_fraction)
            if zf_size > 0:
                asyncio.create_task(
                    execution.receive_signal(Signal(
                        slug=slug,
                        token_id=(m_zf.yes_id if zf["direction"] == "BUY_YES" else m_zf.no_id),
                        side=zf["side"],
                        price=zf["taker_price"],
                        size_usdc=zf_size,
                        signal_ns=now_ns,
                        binance_ref_price=zf["binance_current"],
                        binance_ref_ns=buf[-1][0] if buf else now_ns,
                        edge_estimate=zf["exec_edge"],
                        order_type="FOK",
                        mode="ZONE_FLIP",
                    )),
                    name=f"zoneflip_{slug}_{now_ns}",
                )
                asyncio.create_task(log_signal(
                    slug=slug, signal_dict=zf, scored=None,
                    sigma=state.sigmas.get("btcusdt", 0.0),
                    btc_price=zf["binance_current"],
                    sent=True, mode_label="ZONE_FLIP",
                ))

    # EXPIRY mutex — samo če ni odprtih pozicij ali pending strani
    if not state.open_positions and not state.pending_sides:
        expiry = compute_expiry_signal(slug, state, now_ns)
        if expiry is not None:
            m_exp = state.markets.get(slug)
            if m_exp:
                exp_size = risk.compute_risk_sized_amount(STRATEGY_CONFIG.kelly_fraction)
                if exp_size > 0:
                    exp_side = "YES" if expiry["direction"] == "BUY_YES" else "NO"
                    asyncio.create_task(
                        execution.receive_signal(Signal(
                            slug=slug,
                            token_id=(m_exp.yes_id if expiry["direction"] == "BUY_YES" else m_exp.no_id),
                            side=exp_side,
                            price=expiry["taker_price"],
                            size_usdc=exp_size,
                            signal_ns=now_ns,
                            binance_ref_price=expiry["binance_current"],
                            binance_ref_ns=buf[-1][0] if buf else now_ns,
                            edge_estimate=expiry["exec_edge"],
                            order_type="FOK",
                            mode="EXPIRY",
                        )),
                        name=f"expiry_{slug}_{now_ns}",
                    )
                    asyncio.create_task(log_signal(
                        slug=slug, signal_dict=expiry, scored=None,
                        sigma=state.sigmas.get("btcusdt", 0.0),
                        btc_price=expiry["binance_current"],
                        sent=True, mode_label="EXPIRY",
                    ))

    # BSM signal
    raw = compute_strategy_signal(slug, state, now_ns)
    if raw is None:
        return

    direction = raw["direction"]
    fair_prob = raw["fair"] if direction == "BUY_YES" else 1.0 - raw["fair"]
    taker = raw["taker_price"]
    order_type = raw["order_type"]
    fee_pu = 0.0 if order_type == "GTC" else state.fee_config.fee_usdc(taker, 1.0)
    net_odds = (1.0 - taker) / taker if taker > 0 else 0
    effective_b = net_odds - fee_pu / taker if taker > 0 and net_odds > 0 else 0
    if effective_b <= 0:
        return
    full_kelly = (fair_prob * effective_b - (1 - fair_prob)) / effective_b
    quarter_k = max(0.0, full_kelly * STRATEGY_CONFIG.kelly_fraction)

    size = risk.compute_risk_sized_amount(quarter_k)
    if size <= 0:
        return

    m = state.markets.get(slug)
    if not m:
        return

    bsm_side = "YES" if direction == "BUY_YES" else "NO"
    asyncio.create_task(log_signal(
        slug=slug, signal_dict=raw, scored=None,
        sigma=state.sigmas.get("btcusdt", 0.0),
        btc_price=raw["binance_current"],
        sent=True, mode_label=raw.get("mode_label", "BSM"),
    ))
    asyncio.create_task(
        execution.receive_signal(Signal(
            slug=slug,
            token_id=(m.yes_id if direction == "BUY_YES" else m.no_id),
            side=bsm_side,
            price=taker,
            size_usdc=size,
            signal_ns=now_ns,
            binance_ref_price=raw["binance_current"],
            binance_ref_ns=buf[-1][0] if buf else now_ns,
            edge_estimate=raw["exec_edge"],
            order_type=order_type,
        )),
        name=f"exec_{slug}_{now_ns}",
    )
