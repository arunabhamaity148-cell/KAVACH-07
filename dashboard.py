"""
KAVACH-07 — Rich Terminal Dashboard
Real-time monitoring via Rich Live. Updated from the main loop.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import pytz
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

IST = pytz.timezone("Asia/Kolkata")


class Dashboard:
    """Rich-based terminal dashboard for KAVACH-07.

    Usage:
        dash = Dashboard(config)
        dash.start()           # enter Live context
        dash.update(...)       # call from main loop
        dash.stop()            # exit Live context
    """

    def __init__(self, config: dict) -> None:
        self._cfg     = config
        self._console = Console()
        self._live: Optional[Live] = None

        # State updated each tick
        self._signals:     List[Dict]  = []
        self._strat_perf:  List[Dict]  = []
        self._open_trades: List[Dict]  = []
        self._daily_pnl:   float       = 0.0
        self._total_pnl:   float       = 0.0
        self._api_health:  Dict        = {}
        self._regime:      str         = "UNDEFINED"
        self._bot_status:  str         = "STARTING"
        self._market_data: Dict        = {}
        self._tick:        int         = 0

    # ─────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._live = Live(
            self._build_layout(),
            console=self._console,
            refresh_per_second=1,
            screen=True,
        )
        self._live.start()

    def stop(self) -> None:
        if self._live:
            self._live.stop()

    def update(
        self,
        signals: Optional[List[Dict]] = None,
        strat_perf: Optional[List[Dict]] = None,
        open_trades: Optional[List[Dict]] = None,
        daily_pnl: float = 0.0,
        total_pnl: float = 0.0,
        api_health: Optional[Dict] = None,
        regime: str = "UNDEFINED",
        bot_status: str = "RUNNING",
        market_data: Optional[Dict] = None,
    ) -> None:
        """Refresh all dashboard state and redraw."""
        if signals     is not None: self._signals     = signals
        if strat_perf  is not None: self._strat_perf  = strat_perf
        if open_trades is not None: self._open_trades = open_trades
        if api_health  is not None: self._api_health  = api_health
        if market_data is not None: self._market_data = market_data
        self._daily_pnl  = daily_pnl
        self._total_pnl  = total_pnl
        self._regime     = regime
        self._bot_status = bot_status
        self._tick      += 1

        if self._live:
            self._live.update(self._build_layout())

    # ─────────────────────────────────────────────────────────────────────
    # Layout construction
    # ─────────────────────────────────────────────────────────────────────

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header",   size=3),
            Layout(name="middle",   ratio=3),
            Layout(name="bottom",   ratio=2),
            Layout(name="footer",   size=1),
        )
        layout["middle"].split_row(
            Layout(name="signals",  ratio=2),
            Layout(name="right",    ratio=1),
        )
        layout["right"].split_column(
            Layout(name="regime",   size=6),
            Layout(name="pnl",      size=8),
            Layout(name="health",   size=6),
        )
        layout["bottom"].split_row(
            Layout(name="strategies", ratio=2),
            Layout(name="prices",     ratio=1),
        )

        layout["header"].update(self._panel_header())
        layout["signals"].update(self._panel_signals())
        layout["regime"].update(self._panel_regime())
        layout["pnl"].update(self._panel_pnl())
        layout["health"].update(self._panel_health())
        layout["strategies"].update(self._panel_strategies())
        layout["prices"].update(self._panel_prices())
        layout["footer"].update(self._footer())

        return layout

    # ─────────────────────────────────────────────────────────────────────
    # Individual panels
    # ─────────────────────────────────────────────────────────────────────

    def _panel_header(self) -> Panel:
        now_ist  = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        status_color = {
            "RUNNING": "green", "PAUSED": "yellow",
            "STARTING": "cyan", "ERROR": "red",
        }.get(self._bot_status, "white")
        text = Text()
        text.append("🛡 KAVACH-07", style="bold cyan")
        text.append("  │  ", style="dim")
        text.append(self._bot_status, style=f"bold {status_color}")
        text.append(f"  │  Tick #{self._tick}  │  {now_ist}", style="dim")
        return Panel(text, style="cyan")

    def _panel_signals(self) -> Panel:
        table = Table(
            title="⚡ Active MetaSignals",
            show_header=True,
            header_style="bold magenta",
            expand=True,
        )
        table.add_column("Symbol",     width=10)
        table.add_column("Side",       width=7)
        table.add_column("Conf",       width=7)
        table.add_column("Entry",      width=12)
        table.add_column("SL",         width=12)
        table.add_column("TP",         width=12)
        table.add_column("Regime",     width=10)
        table.add_column("Strategies", width=30)

        for s in self._signals[-10:]:
            side  = s.get("side", "?")
            side_style = "green" if side == "LONG" else ("red" if side == "SHORT" else "dim")
            table.add_row(
                s.get("symbol", ""),
                Text(side, style=f"bold {side_style}"),
                f"{s.get('confidence', 0):.1f}%",
                f"{s.get('entry', 0):.6g}",
                f"{s.get('stop_loss', 0):.6g}",
                f"{s.get('take_profit', 0):.6g}",
                s.get("regime", "?"),
                ", ".join((s.get("strategies_fired") or [])[:3]),
            )
        return Panel(table, style="bright_black")

    def _panel_regime(self) -> Panel:
        regime = self._regime
        color_map = {
            "TRENDING": "green", "RANGING": "yellow",
            "VOLATILE": "red",   "UNDEFINED": "dim",
        }
        color = color_map.get(regime, "white")
        text  = Text(f"  {regime}  ", style=f"bold {color} on black", justify="center")
        return Panel(text, title="🌡 Market Regime", style=color)

    def _panel_pnl(self) -> Panel:
        daily_color = "green" if self._daily_pnl >= 0 else "red"
        total_color = "green" if self._total_pnl >= 0 else "red"
        text = Text()
        text.append("Daily PnL:   ", style="dim")
        text.append(f"{self._daily_pnl:+.4f} USDT\n", style=f"bold {daily_color}")
        text.append("Total PnL:   ", style="dim")
        text.append(f"{self._total_pnl:+.4f} USDT\n", style=f"bold {total_color}")
        text.append("Open Trades: ", style="dim")
        text.append(f"{len(self._open_trades)}", style="cyan")
        return Panel(text, title="💰 P&L Tracker", style="bright_black")

    def _panel_health(self) -> Panel:
        text = Text()
        health_items = {
            "Binance WS":   self._api_health.get("binance_ws",   False),
            "Binance REST": self._api_health.get("binance_rest", False),
            "Hyperliquid":  self._api_health.get("hyperliquid",  False),
            "External":     self._api_health.get("external",     False),
        }
        for name, ok in health_items.items():
            status = "●" if ok else "○"
            color  = "green" if ok else "red"
            text.append(f"{status} {name}\n", style=color)
        return Panel(text, title="🔌 API Health", style="bright_black")

    def _panel_strategies(self) -> Panel:
        table = Table(
            title="📊 Strategy Performance",
            show_header=True,
            header_style="bold blue",
            expand=True,
        )
        table.add_column("Strategy",      width=25)
        table.add_column("Symbol",        width=10)
        table.add_column("Win Rate",      width=9)
        table.add_column("Profit Factor", width=13)
        table.add_column("Signals",       width=9)

        for p in (self._strat_perf or [])[:12]:
            wr     = p.get("win_rate", 0.0) or 0.0
            pf     = p.get("profit_factor", 0.0) or 0.0
            wr_col = "green" if wr >= 55 else ("yellow" if wr >= 45 else "red")
            table.add_row(
                p.get("strategy_name", "")[:24],
                p.get("symbol", ""),
                Text(f"{wr:.1f}%", style=wr_col),
                f"{pf:.2f}",
                str(p.get("total_signals", 0)),
            )
        return Panel(table, style="bright_black")

    def _panel_prices(self) -> Panel:
        table = Table(
            title="💹 Live Prices",
            show_header=True,
            header_style="bold yellow",
            expand=True,
        )
        table.add_column("Symbol",  width=12)
        table.add_column("Price",   width=12)
        table.add_column("OI(M)",   width=8)
        table.add_column("FR",      width=9)

        for sym, md in list(self._market_data.items())[:10]:
            fr     = md.funding_rate if hasattr(md, "funding_rate") else 0.0
            fr_col = "red" if fr > 0.0003 else ("green" if fr < -0.0003 else "white")
            table.add_row(
                sym,
                f"{md.price:.5g}" if hasattr(md, "price") else "—",
                f"{md.open_interest/1e6:.2f}" if hasattr(md, "open_interest") else "—",
                Text(f"{fr*100:.4f}%", style=fr_col),
            )
        return Panel(table, style="bright_black")

    def _footer(self) -> Panel:
        cmds = "/pause  /resume  /status  /trades  /signals"
        return Panel(
            Text(f"Telegram commands: {cmds}", justify="center", style="dim"),
            style="dim", height=1,
        )
