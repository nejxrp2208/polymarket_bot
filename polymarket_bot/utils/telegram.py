"""
TELEGRAM NOTIFIKACIJE
Pošilja sporočila za: fill, exit (PnL), dnevni summary.
Fire-and-forget — napake ne crashajo bota.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import aiohttp

_TOKEN: str = ""
_CHAT_ID: str = ""
_session: aiohttp.ClientSession | None = None


def _init() -> bool:
    global _TOKEN, _CHAT_ID
    if _TOKEN:
        return True
    _TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    _CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return bool(_TOKEN and _CHAT_ID)


async def _send(text: str) -> None:
    if not _init():
        return
    global _session
    try:
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession()
        url = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
        await _session.post(
            url,
            json={"chat_id": _CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=aiohttp.ClientTimeout(total=5),
        )
    except Exception:
        pass


def notify(text: str) -> None:
    """Fire-and-forget — klicej kjerkoli brez await."""
    try:
        asyncio.create_task(_send(text))
    except RuntimeError:
        pass


def notify_fill(side: str, price: float, size_usdc: float, slug: str, mode: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    emoji = "🟢" if side == "YES" else "🔴"
    notify(
        f"{emoji} <b>FILL [{mode}]</b>\n"
        f"Side: <b>{side}</b> @ {price:.3f}\n"
        f"Size: ${size_usdc:.2f}\n"
        f"Market: {slug[-20:]}\n"
        f"⏰ {ts} UTC"
    )


def notify_exit(side: str, entry: float, exit_price: float, pnl: float, reason: str, slug: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    emoji = "✅" if pnl > 0 else "❌"
    notify(
        f"{emoji} <b>EXIT [{reason}]</b>\n"
        f"Side: <b>{side}</b>  entry={entry:.3f} → exit={exit_price:.3f}\n"
        f"PnL: <b>{'+'if pnl>=0 else ''}{pnl:.2f} USDC</b>\n"
        f"Market: {slug[-20:]}\n"
        f"⏰ {ts} UTC"
    )


def notify_daily(balance: float, pnl: float, wins: int, losses: int) -> None:
    total = wins + losses
    wr = wins / max(1, total) * 100
    notify(
        f"📊 <b>DNEVNI UPDATE</b>\n"
        f"Balance: <b>${balance:.2f}</b>\n"
        f"PnL danes: <b>{'+'if pnl>=0 else ''}{pnl:.2f} USDC</b>\n"
        f"Trades: {total}  ✅{wins}  ❌{losses}  WR={wr:.0f}%"
    )
