"""
ANALYSIS — SQL queriji za post-session analizo.
Uporabi standalone ali v Jupyter notebook.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path("logs") / "polymarket.db"


def get_connection() -> sqlite3.Connection:
    return sqlite3.connect(str(DB_PATH))


def trade_summary() -> dict:
    """Skupen pregled tradov."""
    con = get_connection()
    cur = con.cursor()

    fills = cur.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    exits = cur.execute("SELECT COUNT(*) FROM exits").fetchone()[0]

    wins = cur.execute(
        "SELECT COUNT(*) FROM exits WHERE pnl_usdc > 0"
    ).fetchone()[0]
    losses = cur.execute(
        "SELECT COUNT(*) FROM exits WHERE pnl_usdc <= 0"
    ).fetchone()[0]

    total_pnl = cur.execute(
        "SELECT COALESCE(SUM(pnl_usdc), 0) FROM exits"
    ).fetchone()[0]

    avg_win = cur.execute(
        "SELECT COALESCE(AVG(pnl_usdc), 0) FROM exits WHERE pnl_usdc > 0"
    ).fetchone()[0]
    avg_loss = cur.execute(
        "SELECT COALESCE(AVG(pnl_usdc), 0) FROM exits WHERE pnl_usdc <= 0"
    ).fetchone()[0]

    avg_latency = cur.execute(
        "SELECT COALESCE(AVG(lat_total_ms), 0) FROM fills"
    ).fetchone()[0]

    con.close()

    win_rate = wins / max(1, wins + losses) * 100

    return {
        "fills": fills,
        "exits": exits,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 1),
        "total_pnl_usdc": round(total_pnl, 4),
        "avg_win_usdc": round(avg_win, 4),
        "avg_loss_usdc": round(avg_loss, 4),
        "avg_latency_ms": round(avg_latency, 1),
    }


def pnl_by_hour() -> list[tuple]:
    """PnL po urah (za grafikon)."""
    con = get_connection()
    rows = con.execute(
        """
        SELECT
            ts_ms / 3600000 * 3600000 AS hour_ms,
            SUM(pnl_usdc) AS pnl,
            COUNT(*) AS trades
        FROM exits
        GROUP BY hour_ms
        ORDER BY hour_ms
        """
    ).fetchall()
    con.close()
    return rows


def signals_vs_fills() -> dict:
    """Razmerje signali → fill-i (conversion rate)."""
    con = get_connection()
    total_signals = con.execute(
        "SELECT COUNT(*) FROM signals WHERE sent_to_exec = 1"
    ).fetchone()[0]
    total_fills = con.execute(
        "SELECT COUNT(*) FROM fills"
    ).fetchone()[0]
    con.close()
    rate = total_fills / max(1, total_signals) * 100
    return {
        "signals_sent": total_signals,
        "fills": total_fills,
        "conversion_rate_pct": round(rate, 1),
    }


def print_summary() -> None:
    """Izpiše full summary v terminal."""
    s = trade_summary()
    sv = signals_vs_fills()

    print("\n" + "=" * 50)
    print("POLYMARKET BOT — SESSION ANALYSIS")
    print("=" * 50)
    print(f"  Signals sent:    {sv['signals_sent']}")
    print(f"  Fills:           {s['fills']}")
    print(f"  Conversion:      {sv['conversion_rate_pct']}%")
    print(f"  Exits:           {s['exits']}")
    print(f"  Wins:            {s['wins']}")
    print(f"  Losses:          {s['losses']}")
    print(f"  Win rate:        {s['win_rate_pct']}%")
    print(f"  Total PnL:       ${s['total_pnl_usdc']:+.4f}")
    print(f"  Avg win:         ${s['avg_win_usdc']:+.4f}")
    print(f"  Avg loss:        ${s['avg_loss_usdc']:+.4f}")
    print(f"  Avg latency:     {s['avg_latency_ms']:.1f}ms")
    print("=" * 50)


if __name__ == "__main__":
    print_summary()
