"""
KAVACH-07 — Terminal Dashboard
Real-time monitoring UI built with the Rich library.
Displays bot status, active signals, PnL tracker, and API health.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import pytz
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

class Dashboard:
    """
    Rich-based terminal interface for KAVACH-07.
    Designed for 1 OCPU / 1GB RAM environments (low overhead).
    """

    def __init__(self, config: dict):
        self._cfg = config
        self._console = Console()
        self._ist = pytz.timezone(config["bot"]["timezone"])
        
        # UI State
        self._bot_status = "INITIALIZING"
        self._regime = "UNDEFINED"
        self._daily_pnl = 0.0
        self._total_pnl = 0.0
        self._open_trades_count = 0
        self._signals: List[Dict[str, Any]] = []
        self._api_status: Dict[str, bool] = {"Binance": False, "Hyperliquid": False, "News": False}
        self._start_time = time.time()

    def update_state(self, **kwargs) -> None:
        """Updates internal dashboard state variables."""
        if "bot_status" in kwargs: self._bot_status = kwargs["bot_status"]
        if "regime" in kwargs: self._regime = kwargs["regime"]
        if "daily_pnl" in kwargs: self._daily_pnl = kwargs["daily_pnl"]
        if "total_pnl" in kwargs: self._total_pnl = kwargs["total_pnl"]
        if "open_trades_count" in kwargs: self._open_trades_count = kwargs["open_trades_count"]
        if "signals" in kwargs: self._signals = kwargs["signals"][-5:] # Keep last 5
        if "api_status" in kwargs: self._api_status.update(kwargs["api_status"])

    def _make_layout(self) -> Layout:
        """Defines the terminal screen split logic."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=3)
        )
        layout["main"].split_row(
            Layout(name="left", ratio=2),
            Layout(name="right", ratio=1)
        )
        layout["right"].split_column(
            Layout(name="status", ratio=1),
            Layout(name="pnl", ratio=1)
        )
        return layout

    def _get_header(self) -> Panel:
        """Top bar panel."""
        now = datetime.now(self._ist).strftime("%Y-%m-%d %H:%M:%S IST")
        uptime = int(time.time() - self._start_time)
        grid = Table.grid(expand=True)
        grid.add_column(justify="left", ratio=1)
        grid.add_column(justify="center", ratio=1)
        grid.add_column(justify="right", ratio=1)
        grid.add_row(
            Text("🛡️ KAVACH-07 v7.0.0", style="bold cyan"),
            Text(now, style="white"),
            Text(f"Uptime: {uptime}s", style="dim")
        )
        return Panel(grid, style="blue")

    def _get_signals_table(self) -> Panel:
        """Main signal monitor."""
        table = Table(expand=True, box=None)
        table.add_column("Time", style="dim")
        table.add_column("Symbol", style="bold")
        table.add_column("Side")
        table.add_column("Conf", justify="right")
        table.add_column("Entry", justify="right")
        table.add_column("Regime")

        for s in self._signals:
            side_color = "green" if s["side"] == "LONG" else "red"
            ts = datetime.fromtimestamp(s["timestamp"], self._ist).strftime("%H:%M")
            table.add_row(
                ts,
                s["symbol"],
                Text(s["side"], style=side_color),
                f"{s['confidence']}%",
                f"{s['entry']:.4g}",
                s["regime"]
            )
        return Panel(table, title="[bold magenta]LIVE SIGNALS[/]", border_style="magenta")

    def _get_status_panel(self) -> Panel:
        """Regime and Engine health info."""
        regime_color = {"TRENDING": "green", "RANGING": "yellow", "VOLATILE": "red"}.get(self._regime, "white")
        health_text = Text()
        for engine, ok in self._api_status.items():
            status = "●" if ok else "○"
            color = "green" if ok else "red"
            health_text.append(f"{engine} ", style="white")
            health_text.append(f"{status}  ", style=color)

        grid = Table.grid(expand=True)
        grid.add_row(Text("BOT:", style="dim"), Text(self._bot_status, style="bold yellow"))
        grid.add_row(Text("REGIME:", style="dim"), Text(self._regime, style=f"bold {regime_color}"))
        grid.add_row(Text("HEALTH:", style="dim"), health_text)
        
        return Panel(grid, title="[bold yellow]SYSTEM[/]", border_style="yellow")

    def _get_pnl_panel(self) -> Panel:
        """PnL and account summary."""
        daily_color = "green" if self._daily_pnl >= 0 else "red"
        total_color = "green" if self._total_pnl >= 0 else "red"
        
        grid = Table.grid(expand=True)
        grid.add_row(Text("DAILY:", style="dim"), Text(f"${self._daily_pnl:+.2f}", style=f"bold {daily_color}"))
        grid.add_row(Text("TOTAL:", style="dim"), Text(f"${self._total_pnl:+.2f}", style=f"bold {total_color}"))
        grid.add_row(Text("OPEN:", style="dim"), Text(str(self._open_trades_count), style="bold cyan"))
        
        return Panel(grid, title="[bold green]P&L TRACKER[/]", border_style="green")

    def _get_footer(self) -> Panel:
        """Bottom informational bar."""
        return Panel(
            Text("Bot restricted to 09:00 - 00:00 IST | Risk: 3.0% / Trade", justify="center", style="dim italic"),
            border_style="dim"
        )

    def generate_screen(self) -> Layout:
        """Assembles the layout into a complete UI screen."""
        layout = self._make_layout()
        layout["header"].update(self._get_header())
        layout["left"].update(self._get_signals_table())
        layout["status"].update(self._get_status_panel())
        layout["pnl"].update(self._get_pnl_panel())
        layout["footer"].update(self._get_footer())
        return layout

    def start(self) -> Live:
        """Returns a Rich Live context for real-time rendering."""
        return Live(self.generate_screen(), console=self._console, refresh_per_second=2, screen=True)