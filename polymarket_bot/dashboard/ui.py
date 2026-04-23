"""
DASHBOARD — TERMINAL UI
2 stolpca: levo (balance/stats), desno zgoraj (markets), desno spodaj (log).
Knjižnica: rich | Osvežuje se vsako 1 sekundo.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from state import State
from utils.helpers import current_window_ts, get_slug

console = Console()


def _fmt_pnl(pnl: float) -> Text:
    color = "green" if pnl >= 0 else "red"
    return Text(f"{pnl:+.2f}", style=color)


# ── Levi panel ───────────────────────────────────────────────


def build_left_panel(state: State) -> Panel:
    total = state.paper_wins + state.paper_losses
    wr = state.paper_wins / max(1, total) * 100
    dd = 0.0
    if state.peak_balance > 0:
        dd = (
            (state.peak_balance - state.usdc_balance)
            / state.peak_balance
            * 100
        )

    bar_len = 20
    filled = int((dd / 20.0) * bar_len)
    bar_char = "█" * filled + "░" * (bar_len - filled)
    bar_color = "red" if dd >= 15 else "yellow" if dd >= 10 else "green"

    ks_text = (
        "[red]PAUSIRANO[/red]"
        if state.trading_paused
        else "[green]aktiven[/green]"
    )

    btc = state.price_buffer.get("btcusdt")
    eth = state.price_buffer.get("ethusdt")
    btc_price = btc[-1][1] if btc else 0.0
    eth_price = eth[-1][1] if eth else 0.0

    def pct_change(sym: str) -> str:
        b = state.price_buffer.get(sym)
        if not b or len(b) < 2:
            return "+0.00%"
        cutoff = time.time_ns() - 300_000_000_000
        old = next(
            (p for ts, p in reversed(list(b)) if ts <= cutoff),
            b[0][1],
        )
        pct = (b[-1][1] - old) / old * 100 if old > 0 else 0
        color = "green" if pct >= 0 else "red"
        return f"[{color}]{pct:+.2f}%[/{color}]"

    lines = [
        "[bold cyan]polymarket bot · v1.0[/bold cyan]",
        f"[dim]{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC[/dim]",
        "",
        "[dim]balance[/dim]",
        f"[bold white]${state.usdc_balance:,.2f}[/bold white]",
        "[dim]USDC · polygon[/dim]",
        "",
        "[dim]session pnl[/dim]",
        f"[{'green' if state.pnl_total >= 0 else 'red'}]{state.pnl_total:+.2f}[/]",
        f"[{'green' if state.pnl_total >= 0 else 'red'}]{state.pnl_total / max(1, state.peak_balance) * 100:+.2f}%[/]",
        "",
        "[dim]trades[/dim]",
        f"total  [white]{total}[/white]   [green]win {state.paper_wins}[/green]   [red]loss {state.paper_losses}[/red]",
        "",
        "[dim]win rate[/dim]",
        f"[{'green' if wr >= 55 else 'yellow' if wr >= 50 else 'red'}]{wr:.1f}%[/]",
        "",
        f"[dim]drawdown[/dim]   {ks_text}",
        f"[{bar_color}]{dd:.1f}%[/{bar_color}][dim]  max 20%[/dim]",
        f"[{bar_color}]{bar_char}[/{bar_color}]",
        "",
        f"[dim]btc[/dim]             [dim]eth[/dim]",
        f"[white]{btc_price:,.0f}[/white]        [white]{eth_price:,.0f}[/white]",
        f"{pct_change('btcusdt')}        {pct_change('ethusdt')}",
    ]

    return Panel("\n".join(lines), border_style="dim", padding=(0, 1))


# ── Srednji panel ────────────────────────────────────────────


def build_middle_panel(state: State) -> Panel:
    lines = ["[bold]active markets · btc up/down 5m[/bold]", ""]

    now_ts = current_window_ts()
    found_any = False
    for delta in [0, -300]:
        slug = get_slug(now_ts + delta)
        m = state.markets.get(slug)
        if not m:
            continue
        found_any = True
        q = state.quotes.get(slug)
        price_to_beat = state.window_open_price.get(slug, 0.0)
        secs_left = max(0, (now_ts + delta + 300) - time.time())
        status = (
            f"expires in {int(secs_left // 60)}:{int(secs_left % 60):02d}"
            if secs_left > 0
            else "[dim]resolving...[/dim]"
        )

        lines.append(f"[bold]{slug}[/bold]")
        if price_to_beat > 0:
            lines.append(f"price to beat  [yellow]${price_to_beat:,.2f}[/yellow]")
        else:
            lines.append("price to beat  [dim]načitavam...[/dim]")

        if q:
            up_prob = q.yes_bid
            down_prob = 1.0 - q.yes_ask
            spread = q.yes_ask - q.yes_bid
            lines.append(
                f"UP  (yes) [green]{up_prob:.2f}[/green]   "
                f"DOWN (no) [red]{down_prob:.2f}[/red]   "
                f"spread [dim]{spread:.2f}[/dim]"
            )
        else:
            lines.append("[dim]quotes: čakam na WS...[/dim]")

        lines.append(status)
        lines.append("")

    if not found_any:
        lines.append("[dim]ni aktivnih marketov[/dim]")

    lines.append("[bold]active trades[/bold]")
    lines.append(
        "[dim]market          side   entry    size    unreal pnl[/dim]"
    )

    positions = list(state.open_positions.values())
    if positions:
        for pos in positions:
            q2 = state.quotes.get(pos.slug)
            mid = 0.0
            if q2:
                mid = (
                    (q2.yes_bid + q2.yes_ask) / 2.0
                    if pos.side == "YES"
                    else 1.0 - (q2.yes_bid + q2.yes_ask) / 2.0
                )
            unreal = mid - pos.entry_price
            unreal_str = (
                f"[green]+${unreal:.2f}[/green]"
                if unreal >= 0
                else f"[red]-${abs(unreal):.2f}[/red]"
            )
            side_color = "green" if pos.side == "YES" else "red"
            lines.append(
                f"…{pos.slug[-10:]}  "
                f"[{side_color}]{pos.side}[/{side_color}]  "
                f"{pos.entry_price:.3f}   "
                f"${pos.size_usdc:.2f}   "
                f"{unreal_str}"
            )
    else:
        lines.append("[dim]no open positions[/dim]")

    return Panel("\n".join(lines), border_style="dim", padding=(0, 1))


# ── Desni panel ──────────────────────────────────────────────


def build_right_panel(state: State, trade_log: list) -> Panel:
    lines = ["[bold]last trades log[/bold]", ""]
    for entry in trade_log[-5:]:
        ts = entry.get("ts", "")
        side = entry.get("side", "")
        outcome = entry.get("outcome", "")
        pnl = entry.get("pnl", 0.0)
        ent_price = entry.get("entry", 0.0)
        s_color = "green" if side == "YES" else "red"
        o_color = "green" if outcome == "WIN" else "red"
        p_str = (
            f"[green]+${pnl:.2f}[/green]"
            if pnl >= 0
            else f"[red]-${abs(pnl):.2f}[/red]"
        )
        lines.append(
            f"[dim]{ts}[/dim]  "
            f"[{s_color}]{side}[/{s_color}]  "
            f"[{o_color}]{outcome}[/{o_color}]  "
            f"{p_str}  "
            f"[dim]entry {ent_price:.3f}[/dim]"
        )
    return Panel("\n".join(lines), border_style="dim", padding=(0, 1))


# ── Async dashboard task ────────────────────────────────────


def _build_layout(state: State, trade_log: list) -> Layout:
    layout = Layout()
    layout.split_row(
        Layout(name="left", ratio=1),
        Layout(name="right", ratio=1),
    )
    layout["right"].split_column(
        Layout(name="markets", ratio=2),
        Layout(name="trades", ratio=1),
    )
    layout["left"].update(build_left_panel(state))
    layout["markets"].update(build_middle_panel(state))
    layout["trades"].update(build_right_panel(state, trade_log))
    return layout


async def dashboard_task(state: State, trade_log: list) -> None:
    with Live(console=console, refresh_per_second=4, screen=True) as live:
        while True:
            try:
                live.update(_build_layout(state, trade_log))
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                from utils.helpers import log
                log("ERROR", "dashboard", str(e))
                await asyncio.sleep(1.0)
