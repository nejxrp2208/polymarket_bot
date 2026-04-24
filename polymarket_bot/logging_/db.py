"""
LOGGING LAYER — SQLITE WAL
init_db, log_bba, log_signal, log_fill, log_exit, flush_all, session_stats
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import sqlite3
import time
from pathlib import Path

from utils.helpers import log

LOG_DIR = Path("logs")
DB_PATH = LOG_DIR / "polymarket.db"

_db_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="DBWriter"
)

_bba_buffer: list = []
_signal_buffer: list = []
_btc_buffer: list = []
BATCH_SIZE = 50


# ── DB init ─────────────────────────────────────────────────


def init_db() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute(
        """CREATE TABLE IF NOT EXISTS bba (
        ts_ms INTEGER, slug TEXT, side TEXT,
        bid REAL, ask REAL, mid REAL, spread REAL)"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS signals (
        ts_ms INTEGER, slug TEXT, direction TEXT,
        taker_price REAL, exec_edge REAL, fair_yes REAL,
        window_remaining_s REAL, total_score REAL,
        sigma REAL, btc_price REAL, sent_to_exec INTEGER,
        mode_label TEXT)"""
    )
    try:
        cur.execute("ALTER TABLE signals ADD COLUMN mode_label TEXT")
    except Exception:
        pass
    cur.execute(
        """CREATE TABLE IF NOT EXISTS fills (
        ts_ms INTEGER, slug TEXT, mode TEXT, side TEXT,
        fill_price REAL, size_usdc REAL, fee_usdc REAL,
        order_id TEXT, lat_submit_ms REAL,
        lat_fill_ms REAL, lat_total_ms REAL)"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS exits (
        ts_ms INTEGER, slug TEXT, mode TEXT, side TEXT,
        entry_price REAL, exit_price REAL, size_usdc REAL,
        fee_usdc REAL, pnl_usdc REAL, reason TEXT)"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS btc_prices (
        ts_ms INTEGER, price REAL, sigma REAL)"""
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_bba_slug_ts ON bba(slug,ts_ms)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sig_slug_ts ON signals(slug,ts_ms)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sig_mode ON signals(mode_label)"
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_fill_ts ON fills(ts_ms)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exit_slug ON exits(slug)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_btc_ts ON btc_prices(ts_ms)")
    con.commit()
    con.close()


# ── Sync writer (runs in thread) ────────────────────────────


def _db_write_sync(table: str, rows: list) -> None:
    if not rows:
        return
    try:
        ph = ",".join(["?"] * len(rows[0]))
        con = sqlite3.connect(DB_PATH, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        con.executemany(f"INSERT INTO {table} VALUES ({ph})", rows)
        con.commit()
        con.close()
    except Exception as e:
        with open(LOG_DIR / "errors.log", "a") as f:
            f.write(f"{int(time.time() * 1000)} {table}: {e}\n")


# ── Async flush ─────────────────────────────────────────────


async def _flush_async(buf: list, table: str) -> None:
    if not buf:
        return
    rows, buf[:] = buf[:], []
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_db_executor, _db_write_sync, table, rows)


# ── Public log functions ────────────────────────────────────


async def log_bba(
    slug: str, side: str, bid: str, ask: str
) -> None:
    try:
        b = float(bid)
        a = float(ask)
        _bba_buffer.append(
            (
                int(time.time() * 1000),
                slug,
                side,
                b,
                a,
                round((b + a) / 2, 5),
                round(a - b, 5),
            )
        )
        if len(_bba_buffer) >= BATCH_SIZE:
            await _flush_async(_bba_buffer, "bba")
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def log_signal(
    slug: str,
    signal_dict: dict,
    scored: object,
    sigma: float,
    btc_price: float,
    sent: bool,
    mode_label: str = "BSM",
) -> None:
    try:
        score = getattr(scored, "total_score", 0.0) if scored else 0.0
        _signal_buffer.append(
            (
                int(time.time() * 1000),
                slug,
                signal_dict.get("direction"),
                signal_dict.get("taker_price"),
                signal_dict.get("exec_edge"),
                signal_dict.get("fair"),
                signal_dict.get("window_remaining_s"),
                score,
                sigma,
                btc_price,
                int(sent),
                mode_label,
            )
        )
        if len(_signal_buffer) >= BATCH_SIZE:
            await _flush_async(_signal_buffer, "signals")
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def log_fill(
    slug: str,
    mode: str,
    side: str,
    fill_price: float,
    size_usdc: float,
    fee_usdc: float,
    order_id: str,
    lat_submit_ms: float,
    lat_fill_ms: float,
    lat_total_ms: float,
) -> None:
    try:
        rows = [
            (
                int(time.time() * 1000),
                slug,
                mode,
                side,
                fill_price,
                size_usdc,
                fee_usdc,
                order_id,
                lat_submit_ms,
                lat_fill_ms,
                lat_total_ms,
            )
        ]
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_db_executor, _db_write_sync, "fills", rows)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def log_exit(
    slug: str,
    mode: str,
    side: str,
    entry_price: float,
    exit_price: float,
    size_usdc: float,
    fee_usdc: float,
    reason: str,
) -> None:
    try:
        shares = size_usdc / entry_price if entry_price > 0 else 0
        pnl = round(shares * exit_price - size_usdc - fee_usdc, 5)
        rows = [
            (
                int(time.time() * 1000),
                slug,
                mode,
                side,
                entry_price,
                exit_price,
                size_usdc,
                fee_usdc,
                pnl,
                reason,
            )
        ]
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_db_executor, _db_write_sync, "exits", rows)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


# ── Flush helpers ───────────────────────────────────────────


async def log_btc_price(price: float, sigma: float) -> None:
    try:
        _btc_buffer.append((int(time.time() * 1000), price, sigma))
        if len(_btc_buffer) >= BATCH_SIZE:
            await _flush_async(_btc_buffer, "btc_prices")
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def flush_all() -> None:
    await _flush_async(_bba_buffer, "bba")
    await _flush_async(_signal_buffer, "signals")
    await _flush_async(_btc_buffer, "btc_prices")


def flush_all_sync() -> None:
    """Windows KeyboardInterrupt fallback."""
    for buf, tbl in [(_bba_buffer, "bba"), (_signal_buffer, "signals"), (_btc_buffer, "btc_prices")]:
        if buf:
            rows, buf[:] = buf[:], []
            _db_write_sync(tbl, rows)


# ── Session stats ───────────────────────────────────────────


def session_stats() -> None:
    try:
        con = sqlite3.connect(str(DB_PATH))
        bba_n = con.execute("SELECT COUNT(*) FROM bba").fetchone()[0]
        sig_n = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        snt_n = con.execute(
            "SELECT COUNT(*) FROM signals WHERE sent_to_exec=1"
        ).fetchone()[0]
        fil_n = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        ext_n = con.execute("SELECT COUNT(*) FROM exits").fetchone()[0]
        con.close()
        mb = os.path.getsize(DB_PATH) / 1024 / 1024
        print(f"\n{'=' * 45}")
        print("LOGGING SUMMARY")
        print(f"BBA tiki:    {bba_n:>10,}")
        print(f"Signali:     {sig_n:>10,}  (poslani: {snt_n})")
        print(f"Fill-i:      {fil_n:>10,}")
        print(f"Exit-i:      {ext_n:>10,}")
        print(f"DB:          {mb:>9.1f} MB")
        print(f"{'=' * 45}")
    except Exception as e:
        log("WARN", "logger", f"session_stats: {e}")
