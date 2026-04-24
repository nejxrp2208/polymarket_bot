"""
TELEGRAM NOTIFIKACIJE
Pošilja sporočila za: start, fill, exit (PnL), dnevni summary.
Fire-and-forget — napake ne crashajo bota.
Uporablja requests v threadu (brez aiohttp session problemov).
"""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import datetime, timezone


def _get_creds() -> tuple[str, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return token, chat_id


def _send_sync(text: str) -> None:
    """Sync send v ločenem threadu — ne blokira event loopa."""
    token, chat_id = _get_creds()
    if not token or not chat_id:
        print(f"[TELEGRAM] NAPAKA: TOKEN={bool(token)} CHAT_ID={bool(chat_id)}")
        return
    try:
        import requests
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
        print(f"[TELEGRAM] HTTP {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        print(f"[TELEGRAM] Exception: {e}")


def notify(text: str) -> None:
    """Fire-and-forget — klicej kjerkoli brez await."""
    threading.Thread(target=_send_sync, args=(text,), daemon=True).start()


def notify_start(mode: str, balance: float) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    notify(
        f"🚀 <b>BOT ZAGNAN [{mode.upper()}]</b>\n"
        f"Balance: <b>${balance:.2f} USDC</b>\n"
        f"⏰ {ts} UTC"
    )


def notify_stop(balance: float, pnl: float, wins: int, losses: int) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    total = wins + losses
    wr = wins / max(1, total) * 100
    notify(
        f"🛑 <b>BOT USTAVLJEN</b>\n"
        f"Balance: <b>${balance:.2f} USDC</b>\n"
        f"PnL: <b>{'+'if pnl>=0 else ''}{pnl:.2f} USDC</b>\n"
        f"Trades: {total}  ✅{wins}  ❌{losses}  WR={wr:.0f}%\n"
        f"⏰ {ts} UTC"
    )


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
