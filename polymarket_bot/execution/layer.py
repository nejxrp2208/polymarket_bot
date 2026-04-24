"""
EXECUTION LAYER
SDK: py-clob-client==0.34.6 | signature_type=0 (EOA)
Aktivni bugi april 2026: #301 (min size), #294 (sell side fee),
#297 (proxy wallet), #292 (WS freeze)
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timezone

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

import refs
from config import CONFIG, EXEC_CONFIG, ExecutionConfig, RISK_CONFIG, STRATEGY_CONFIG
from utils.telegram import notify_fill
from execution.paper import paper_fok_fill, paper_gtc_fill
from logging_.db import log_fill
from state import (
    ExecutionResult,
    FillResult,
    PositionState,
    Signal,
    State,
)
from utils.helpers import log, seconds_until_rollover


# ── CLOB client builder ─────────────────────────────────────


def build_clob_client() -> ClobClient:
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=os.getenv("POLY_PRIVATE_KEY"),
        chain_id=137,
        signature_type=0,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


async def fetch_usdc_balance(clob: ClobClient) -> float:
    try:
        from py_clob_client.clob_types import (
            AssetType,
            BalanceAllowanceParams,
        )

        loop = asyncio.get_running_loop()
        bal = await loop.run_in_executor(
            None,
            lambda: clob.get_balance_allowance(
                params=BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL
                )
            ),
        )
        return float(bal.get("balance", 0)) / 1_000_000
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log("WARN", "execution", f"balance fetch: {e}")
        return 0.0


async def wait_for_fill(
    order_id: str, timeout_sec: float, state: State
) -> FillResult | None:
    deadline_ns = time.time_ns() + int(timeout_sec * 1e9)
    while time.time_ns() < deadline_ns:
        await asyncio.sleep(0.01)
        fill = state.pending_fills.pop(order_id, None)
        if fill is not None:
            return fill
        if state.last_user_ws_tick_ns > 0:
            age_s = (time.time_ns() - state.last_user_ws_tick_ns) / 1e9
            if age_s > 5.0:
                log(
                    "WARN",
                    "execution",
                    f"WS user freeze {age_s:.1f}s",
                )
    return None


# ── ExecutionLayer class ─────────────────────────────────────


class ExecutionLayer:
    def __init__(
        self, clob: ClobClient, config: ExecutionConfig, state: State
    ):
        self.clob = clob
        self.config = config
        self.state = state

    async def receive_signal(
        self, signal: Signal
    ) -> ExecutionResult:
        lock = self.state._order_locks.setdefault(
            signal.side, asyncio.Lock()
        )
        if lock.locked():
            return ExecutionResult(
                success=False, reject_reason="lock_busy"
            )
        async with lock:
            result = self._run_prechecks(signal)
            if result is not None:
                return result
            self.state.pending_sides.add(signal.side)
            order_id = None
            submit_ns = 0
            try:
                order_id, submit_ns = await self._submit_order(signal)
            except asyncio.CancelledError:
                self.state.pending_sides.discard(signal.side)
                raise
            except Exception as e:
                log("ERROR", "execution", f"submit: {e}")
            finally:
                if order_id is None:
                    self.state.pending_sides.discard(signal.side)
            if order_id is None:
                return ExecutionResult(
                    success=False, reject_reason="submit_failed"
                )

        fill = None
        try:
            fill = await self._wait_for_fill(
                order_id, signal, submit_ns
            )
        except asyncio.CancelledError:
            self.state.pending_sides.discard(signal.side)
            raise
        finally:
            if fill is None:
                self.state.pending_sides.discard(signal.side)

        if fill is None:
            await self._on_timeout(order_id, signal)
            return ExecutionResult(
                success=False, reject_reason="timeout"
            )

        return await self._on_fill(fill, signal, submit_ns)

    def _run_prechecks(
        self, signal: Signal
    ) -> ExecutionResult | None:
        now_ns = time.time_ns()
        if now_ns < self.state.cooldown_until_ns:
            return ExecutionResult(
                success=False, reject_reason="cooldown"
            )
        age_ms = (now_ns - signal.signal_ns) / 1_000_000
        if age_ms > self.config.stale_signal_ms:
            return ExecutionResult(
                success=False, reject_reason="stale"
            )
        if seconds_until_rollover() < CONFIG.no_trade_window_s:
            return ExecutionResult(
                success=False, reject_reason="expiry_too_close"
            )
        if not signal.is_close and (
            signal.side in self.state.open_positions
            or signal.side in self.state.pending_sides
        ):
            return ExecutionResult(
                success=False, reject_reason="position_open"
            )
        if self.state.usdc_balance < signal.size_usdc + 0.10:
            return ExecutionResult(
                success=False, reject_reason="balance"
            )
        if not (
            self.config.min_order_size_usdc
            <= signal.size_usdc
            <= self.config.max_order_size_usdc
        ):
            return ExecutionResult(
                success=False, reject_reason="size"
            )
        # edge_estimate=0 je bypass za exit signale
        if signal.edge_estimate > 0:
            req = self.state.fee_config.min_edge_required(signal.price)
            if signal.edge_estimate <= req:
                return ExecutionResult(
                    success=False, reject_reason="no_edge"
                )
        return None

    async def _submit_order(
        self, signal: Signal
    ) -> tuple[str | None, int]:
        if self.config.mode == "paper":
            await asyncio.sleep(
                self.config.simulated_latency_ms / 1000
            )
            oid = f"PAPER_{uuid.uuid4().hex[:8]}"
            submit_ns = time.time_ns()
            log(
                "INFO",
                "execution",
                f"[PAPER] {signal.side} {signal.size_usdc:.2f}USDC "
                f"@ {signal.price:.3f} "
                f"type={signal.order_type} "
                f"slug={signal.slug}",
            )
            return oid, submit_ns
        # LIVE mode
        try:
            fee_resp = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self.clob.get_fee_rate(signal.token_id),
            )
            fee_bps = int(fee_resp.get("fee_rate_bps", 0))
            args = OrderArgs(
                token_id=signal.token_id,
                price=signal.price,
                size=signal.size_usdc,
                side=BUY,
                fee_rate_bps=fee_bps,
            )
            signed = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self.clob.create_order(args)
            )
            # FIX: signal.order_type, ne self.config.order_type
            resp = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self.clob.post_order(
                    signed, OrderType[signal.order_type]
                ),
            )
            submit_ns = time.time_ns()
            oid = resp.get("orderID") or resp.get("order_id")
            return oid, submit_ns
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("ERROR", "execution", f"submit: {e}")
            return None, 0

    async def _wait_for_fill(
        self, order_id: str, signal: Signal, submit_ns: int
    ) -> FillResult | None:
        if self.config.mode == "paper":
            if signal.order_type == "GTC":
                return await paper_gtc_fill(
                    signal,
                    self.state,
                    RISK_CONFIG,
                    timeout_s=STRATEGY_CONFIG.maker_fill_timeout_s,
                )
            else:
                return await paper_fok_fill(
                    signal, self.state, RISK_CONFIG
                )
        return await wait_for_fill(
            order_id, self.config.fill_timeout_sec, self.state
        )

    async def _on_fill(
        self, fill: FillResult, signal: Signal, submit_ns: int
    ) -> ExecutionResult:
        now_ns = time.time_ns()
        # Maker = 0 fee, taker = normalni fee
        if signal.order_type == "GTC":
            fee_usdc = 0.0
            # 20% rebate od taker-jevega feea
            counterparty_fee = self.state.fee_config.fee_usdc(
                fill.price, fill.size_usdc
            )
            self.state.pending_rebate_usdc += (
                counterparty_fee * self.state.fee_config.maker_rebate
            )
        else:
            fee_usdc = self.state.fee_config.fee_usdc(
                fill.price, fill.size_usdc
            )

        lat_submit = (submit_ns - signal.signal_ns) / 1_000_000
        lat_fill = (fill.fill_ns - signal.signal_ns) / 1_000_000
        lat_total = (fill.fill_ns - signal.binance_ref_ns) / 1_000_000

        self.state.usdc_balance -= fill.size_usdc + fee_usdc
        self.state.last_fee_paid_usdc = fee_usdc
        self.state.last_fill_price = fill.price
        self.state.last_order_id = fill.order_id
        self.state.pending_sides.discard(signal.side)

        if not signal.is_close:
            self.state.trades_this_session += 1
            self.state.cooldown_until_ns = now_ns + int(
                self.config.cooldown_sec * 1e9
            )
            self.state.last_signal_ns[signal.slug] = now_ns

        # Risk + position tracking (close signali ne štejejo kot nova pozicija)
        if not signal.is_close:
            refs.risk_ref.on_fill()
            self.state.open_positions[signal.side] = PositionState(
                slug=signal.slug,
                side=signal.side,
                entry_price=fill.price,
                size_usdc=fill.size_usdc,
                entry_ns=fill.fill_ns,
                fee_usdc=fee_usdc,
                order_type=signal.order_type,
                entry_mode=signal.mode,
            )
            self.state.trades_this_window[signal.slug] = (
                self.state.trades_this_window.get(signal.slug, 0) + 1
            )
            if signal.slug not in self.state.traded_directions:
                self.state.traded_directions[signal.slug] = set()
            self.state.traded_directions[signal.slug].add(signal.side)
            refs.trade_log.append(
                {
                    "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                    "side": signal.side,
                    "outcome": "PENDING",
                    "pnl": 0.0,
                    "entry": fill.price,
                    "order_id": fill.order_id,
                }
            )
            notify_fill(signal.side, fill.price, fill.size_usdc, signal.slug, self.config.mode)

        await log_fill(
            slug=signal.slug,
            mode=self.config.mode,
            side=signal.side,
            fill_price=fill.price,
            size_usdc=fill.size_usdc,
            fee_usdc=fee_usdc,
            order_id=fill.order_id,
            lat_submit_ms=lat_submit,
            lat_fill_ms=lat_fill,
            lat_total_ms=lat_total,
        )
        log(
            "INFO",
            "execution",
            f"[{self.config.mode.upper()}] FILL "
            f"{signal.side} @ {fill.price:.3f} "
            f"size={fill.size_usdc:.2f} "
            f"fee={fee_usdc:.4f} "
            f"type={signal.order_type} "
            f"T={lat_total:.0f}ms",
        )

        return ExecutionResult(
            success=True,
            order_id=fill.order_id,
            fill_price=fill.price,
            fill_size_usdc=fill.size_usdc,
            fee_paid_usdc=fee_usdc,
            latency_signal_to_submit_ms=lat_submit,
            latency_signal_to_fill_ms=lat_fill,
            latency_binance_to_fill_ms=lat_total,
        )

    async def _on_timeout(
        self, order_id: str, signal: Signal
    ) -> None:
        self.state.pending_sides.discard(signal.side)
        if (
            self.config.mode == "live"
            and signal.order_type == "GTC"
        ):
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self.clob.cancel_order(order_id)
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log("WARN", "execution", f"cancel: {e}")
