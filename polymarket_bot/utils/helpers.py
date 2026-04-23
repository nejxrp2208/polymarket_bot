"""
UTILS — Pomožne funkcije.
get_slug, current_window_ts, seconds_until_rollover, get_ntp_offset_ns, log
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone


def get_slug(ts: int) -> str:
    return f"btc-updown-5m-{ts}"


def current_window_ts() -> int:
    return (int(time.time()) // 300) * 300


def seconds_until_rollover() -> float:
    now = time.time()
    return (int(now) // 300) * 300 + 300 - now


def log(level: str, component: str, msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    print(
        f"[{ts}] [{level:8s}] [{component:15s}] {msg}",
        file=sys.stderr,
    )


def get_ntp_offset_ns() -> int:
    """Kliči SAMO v main()."""
    try:
        import ntplib

        resp = ntplib.NTPClient().request("time.cloudflare.com", version=3)
        offset_ns = int(resp.offset * 1e9)
        log("INFO", "ntp", f"offset={resp.offset * 1000:+.1f}ms")
        return offset_ns
    except Exception as exc:
        log("WARN", "ntp", f"sync neuspesen: {exc}")
        return 0
