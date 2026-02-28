"""
SoloMiner Terminal UI (TUI).
Curses-based interface with feature parity to the macOS GUI.
No external dependencies - uses Python stdlib curses only.

Screens:
    Dashboard  - live hashrate, status, stats, start/stop, benchmark
    Settings   - Mining, Pools, General, About sub-tabs
    Stats      - Overview, Sessions
    Logs       - Color-coded scrollable log viewer

Usage:
    python3 main.py --tui
"""

import curses
import os
import sys
import time
import threading
import struct
import hashlib

from .config import (
    MinerConfig,
    load_config,
    save_config,
    load_stats,
    save_stats,
    read_log,
    clear_log,
    append_log,
    ping_pool,
    validate_bitcoin_address,
    PoolConfig,
    DEFAULT_POOLS,
    APP_VERSION,
)
from .engine import MiningEngine

# ── Color pair IDs ──
C_NORMAL = 0
C_HEADER = 1
C_ACCENT = 2
C_GREEN = 3
C_RED = 4
C_ORANGE = 5
C_DIM = 6
C_CYAN = 7
C_STATUS_BAR = 8
C_CARD = 9
C_SELECTED = 10
C_BLUE = 11


def _init_colors():
    """Initialize color pairs for the TUI."""
    curses.start_color()
    curses.use_default_colors()
    # (pair_id, fg, bg)
    curses.init_pair(C_HEADER, curses.COLOR_WHITE, -1)
    curses.init_pair(C_ACCENT, curses.COLOR_CYAN, -1)
    curses.init_pair(C_GREEN, curses.COLOR_GREEN, -1)
    curses.init_pair(C_RED, curses.COLOR_RED, -1)
    curses.init_pair(C_ORANGE, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
    curses.init_pair(C_CYAN, curses.COLOR_CYAN, -1)
    curses.init_pair(C_STATUS_BAR, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(C_CARD, curses.COLOR_WHITE, -1)
    curses.init_pair(C_SELECTED, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_BLUE, curses.COLOR_BLUE, -1)


def _format_hashrate(hr: float) -> str:
    if hr >= 1e9:
        return f"{hr / 1e9:.2f} GH/s"
    elif hr >= 1e6:
        return f"{hr / 1e6:.2f} MH/s"
    elif hr >= 1e3:
        return f"{hr / 1e3:.2f} KH/s"
    else:
        return f"{hr:.0f} H/s"


def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m {sec}s"
    return f"{m}m {sec}s"


def _format_hashes(n: int) -> str:
    if n >= 1e12:
        return f"{n / 1e12:.2f} TH"
    elif n >= 1e9:
        return f"{n / 1e9:.2f} GH"
    elif n >= 1e6:
        return f"{n / 1e6:.2f} MH"
    elif n >= 1e3:
        return f"{n / 1e3:.1f} KH"
    return str(n)


def _format_runtime(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, _ = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def _safe_addstr(win, y, x, text, attr=0):
    """Write text to window, silently ignoring out-of-bounds errors."""
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0:
        return
    # Truncate if would exceed width
    max_len = w - x - 1
    if max_len <= 0:
        return
    try:
        win.addnstr(y, x, text, max_len, attr)
    except curses.error:
        pass


def _draw_box(win, y, x, h, w, title=""):
    """Draw a box outline with optional title."""
    rows, cols = win.getmaxyx()
    if y + h > rows or x + w > cols:
        h = min(h, rows - y)
        w = min(w, cols - x)
    if h < 2 or w < 2:
        return
    try:
        # Top border
        win.addch(y, x, curses.ACS_ULCORNER)
        win.hline(y, x + 1, curses.ACS_HLINE, w - 2)
        win.addch(y, x + w - 1, curses.ACS_URCORNER)
        # Bottom border
        win.addch(y + h - 1, x, curses.ACS_LLCORNER)
        win.hline(y + h - 1, x + 1, curses.ACS_HLINE, w - 2)
        win.addch(y + h - 1, x + w - 1, curses.ACS_LRCORNER)
        # Side borders
        for row in range(y + 1, y + h - 1):
            win.addch(row, x, curses.ACS_VLINE)
            win.addch(row, x + w - 1, curses.ACS_VLINE)
        if title:
            _safe_addstr(win, y, x + 2, f" {title} ", curses.A_BOLD)
    except curses.error:
        pass


# ═══════════════════════════════════════════════════════════════
# Main TUI Application
# ═══════════════════════════════════════════════════════════════


class SoloMinerTUI:
    """Curses-based TUI for SoloMiner with full feature parity to the GUI."""

    SCREENS = ["dashboard", "settings", "stats", "logs"]
    SETTINGS_TABS = ["Mining", "Pools", "General", "About"]
    STATS_TABS = ["Overview", "Sessions"]

    def __init__(self):
        self._config: MinerConfig = load_config()
        self._engine = MiningEngine()
        self._screen = "dashboard"
        self._settings_tab = 0  # Mining=0, Pools=1, General=2, About=3
        self._stats_tab = 0  # Overview=0, Sessions=1
        self._running = True

        # Selection cursors for interactive fields
        self._sel_idx = 0  # General selection index within a screen
        self._input_mode = False
        self._input_buffer = ""
        self._input_field = ""  # Which field is being edited

        # Pool management
        self._pool_scroll = 0

        # Log scrolling
        self._log_scroll = 0
        self._log_lines: list = []

        # Benchmark state
        self._benchmarking = False
        self._bench_result = ""

        # Ping states
        self._ping_results: dict = {}  # pool_index -> "120ms" or "Timeout" etc.

    def run(self, stdscr):
        """Main entry point - called by curses.wrapper()."""
        self._stdscr = stdscr
        _init_colors()
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(500)  # 500ms refresh for live stats

        while self._running:
            try:
                stdscr.erase()
                h, w = stdscr.getmaxyx()
                if h < 10 or w < 40:
                    _safe_addstr(stdscr, 0, 0, "Terminal too small (min 40x10)")
                    stdscr.refresh()
                    time.sleep(0.5)
                    continue

                self._draw_status_bar(stdscr, h, w)

                if self._screen == "dashboard":
                    self._draw_dashboard(stdscr, h, w)
                elif self._screen == "settings":
                    self._draw_settings(stdscr, h, w)
                elif self._screen == "stats":
                    self._draw_stats(stdscr, h, w)
                elif self._screen == "logs":
                    self._draw_logs(stdscr, h, w)

                stdscr.refresh()
                self._handle_input(stdscr)
            except curses.error:
                pass
            except KeyboardInterrupt:
                self._running = False

        # Cleanup
        if self._engine.is_running:
            self._engine.stop()

    # ── Status bar ──

    def _draw_status_bar(self, win, h, w):
        """Draw top status bar and bottom help bar."""
        # Top bar
        status = self._engine.status if self._engine.is_running else "Idle"
        hr_str = (
            _format_hashrate(self._engine.hashrate)
            if self._engine.is_running
            else "---"
        )
        top = f" SoloMiner v{APP_VERSION}  |  {status}  |  {hr_str} "
        top = top.ljust(w)
        _safe_addstr(win, 0, 0, top[:w], curses.color_pair(C_STATUS_BAR))

        # Bottom help bar
        if self._input_mode:
            hint = " [Enter] Confirm  [Esc] Cancel "
        elif self._screen == "dashboard":
            hint = " [S]ettings  [T]stats  [L]ogs  [M]ine  [B]enchmark  [Q]uit "
        elif self._screen == "settings":
            hint = " [Esc/Bksp] Back  [Tab] Next Tab  [Enter] Edit/Toggle  [Up/Down] Navigate "
        elif self._screen == "stats":
            hint = " [Esc/Bksp] Back  [Tab] Next Tab "
        elif self._screen == "logs":
            hint = " [Esc/Bksp] Back  [R]efresh  [C]lear  [Up/Down] Scroll "
        else:
            hint = " [Esc] Back  [Q]uit "
        hint = hint.ljust(w)
        _safe_addstr(win, h - 1, 0, hint[:w], curses.color_pair(C_STATUS_BAR))

    # ═══════════════════════════════════════════════════════════
    # Dashboard
    # ═══════════════════════════════════════════════════════════

    def _draw_dashboard(self, win, h, w):
        y = 2
        cx = 2  # content x offset

        # ── Title row ──
        _safe_addstr(
            win, y, cx, "SoloMiner", curses.A_BOLD | curses.color_pair(C_HEADER)
        )

        # Status indicator
        eng = self._engine
        if eng.is_running:
            status = eng.status
            if status == "Mining":
                st_color = curses.color_pair(C_GREEN)
                st_char = ">>>"
            elif status in (
                "Connecting",
                "Reconnecting",
                "Subscribing",
                "Authorizing",
                "Starting",
            ):
                st_color = curses.color_pair(C_ORANGE)
                st_char = "..."
            elif status in ("Auth Failed", "Disconnected", "Error"):
                st_color = curses.color_pair(C_RED)
                st_char = "!!!"
            else:
                st_color = curses.color_pair(C_ORANGE)
                st_char = "..."
            _safe_addstr(
                win,
                y,
                w - len(status) - 6,
                f"{st_char} {status}",
                st_color | curses.A_BOLD,
            )
        else:
            _safe_addstr(win, y, w - 8, "Idle", curses.color_pair(C_DIM))

        # ── Hashrate box ──
        y += 2
        box_w = min(w - 4, 60)
        _draw_box(win, y, cx, 5, box_w, "Hash Rate")
        hr = eng.hashrate if eng.is_running else 0.0
        hr_str = _format_hashrate(hr) if not self._benchmarking else "Benchmarking..."
        if self._bench_result and not eng.is_running and not self._benchmarking:
            hr_str = self._bench_result
        _safe_addstr(
            win,
            y + 2,
            cx + 3,
            hr_str,
            curses.A_BOLD | curses.color_pair(C_GREEN if eng.is_running else C_NORMAL),
        )
        threads_str = f"{eng.active_thread_count} thr" if eng.is_running else "--"
        _safe_addstr(
            win,
            y + 2,
            cx + box_w - len(threads_str) - 3,
            threads_str,
            curses.color_pair(C_DIM),
        )
        peak_str = (
            f"Peak: {_format_hashrate(eng.peak_hashrate)}" if eng.is_running else ""
        )
        _safe_addstr(win, y + 3, cx + 3, peak_str, curses.color_pair(C_DIM))

        # ── Stats rows ──
        y += 6
        stats_col = cx + 2
        val_col = cx + 18

        rows = [
            ("Pool", self._get_pool_display()),
            ("Network", self._config.network),
            ("Algorithm", "SHA-256d"),
            (
                "Shares",
                f"{eng.shares_accepted}/{eng.shares_accepted + eng.shares_rejected}"
                if eng.is_running
                else "0/0",
            ),
            (
                "Difficulty",
                f"{eng.difficulty:.2e}"
                if eng.is_running and eng.difficulty > 0
                else "---",
            ),
            ("Mode", self._config.performance_mode),
            (
                "Uptime",
                _format_uptime(eng.uptime_seconds) if eng.is_running else "0m 0s",
            ),
        ]
        for label, val in rows:
            if y >= h - 6:
                break
            _safe_addstr(win, y, stats_col, label, curses.color_pair(C_DIM))
            _safe_addstr(win, y, val_col, str(val), curses.A_BOLD)
            y += 1

        # ── Detail rows ──
        y += 1
        if y < h - 5:
            gpu_name = eng.miner.gpu_name if eng.miner else "---"
            best_bits = eng.miner.best_share_bits if eng.miner else 0
            jobs = eng.jobs_received if eng.is_running else 0
            details = [
                ("GPU", gpu_name),
                ("Best Share", f"{best_bits} bits"),
                ("Jobs", str(jobs)),
            ]
            for label, val in details:
                if y >= h - 4:
                    break
                _safe_addstr(win, y, stats_col, label, curses.color_pair(C_DIM))
                _safe_addstr(win, y, val_col, val, curses.color_pair(C_CYAN))
                y += 1

        # ── Action buttons hint ──
        y = h - 3
        if eng.is_running:
            _safe_addstr(
                win, y, cx, "[M] Stop Mining", curses.color_pair(C_RED) | curses.A_BOLD
            )
        else:
            _safe_addstr(
                win,
                y,
                cx,
                "[M] Start Mining",
                curses.color_pair(C_GREEN) | curses.A_BOLD,
            )
        _safe_addstr(win, y, cx + 20, "[B] Benchmark", curses.color_pair(C_ACCENT))
        _safe_addstr(win, y, cx + 38, "[S] Settings", curses.color_pair(C_ACCENT))

    def _get_pool_display(self) -> str:
        pools = self._config.pools
        if not pools:
            return "---"
        idx = self._config.active_pool_index
        if idx >= len(pools):
            idx = 0
        return pools[idx].get("name", "---")

    # ═══════════════════════════════════════════════════════════
    # Settings
    # ═══════════════════════════════════════════════════════════

    def _draw_settings(self, win, h, w):
        y = 2
        cx = 2

        # Tab bar
        _safe_addstr(
            win, y, cx, "Settings", curses.A_BOLD | curses.color_pair(C_HEADER)
        )
        y += 1
        for i, tab in enumerate(self.SETTINGS_TABS):
            attr = (
                curses.color_pair(C_SELECTED) | curses.A_BOLD
                if i == self._settings_tab
                else curses.color_pair(C_DIM)
            )
            label = f" {tab} "
            _safe_addstr(win, y, cx, label, attr)
            cx += len(label) + 1
        y += 2

        if self._settings_tab == 0:
            self._draw_settings_mining(win, y, h, w)
        elif self._settings_tab == 1:
            self._draw_settings_pools(win, y, h, w)
        elif self._settings_tab == 2:
            self._draw_settings_general(win, y, h, w)
        elif self._settings_tab == 3:
            self._draw_settings_about(win, y, h, w)

    # ── Settings > Mining ──

    def _draw_settings_mining(self, win, y, h, w):
        cx = 2
        fields = self._mining_fields()
        for i, (label, value, editable) in enumerate(fields):
            if y >= h - 2:
                break
            is_sel = (i == self._sel_idx) and not self._input_mode
            is_editing = self._input_mode and self._input_field == label

            # Label
            _safe_addstr(win, y, cx, label + ":", curses.color_pair(C_DIM))

            # Value
            val_x = cx + 20
            if is_editing:
                display = self._input_buffer + "_"
                _safe_addstr(
                    win,
                    y,
                    val_x,
                    display,
                    curses.A_UNDERLINE | curses.color_pair(C_ACCENT),
                )
            elif is_sel and editable:
                _safe_addstr(
                    win,
                    y,
                    val_x,
                    str(value),
                    curses.color_pair(C_SELECTED) | curses.A_BOLD,
                )
            else:
                attr = curses.A_BOLD if editable else curses.color_pair(C_DIM)
                _safe_addstr(win, y, val_x, str(value), attr)

            if is_sel and not self._input_mode:
                _safe_addstr(
                    win, y, cx - 1, ">", curses.color_pair(C_GREEN) | curses.A_BOLD
                )
            y += 1

        # Save hint
        y += 1
        if y < h - 2:
            _safe_addstr(
                win,
                y,
                cx,
                "[Enter] Edit  [S] Save Config  [Left/Right] Cycle",
                curses.color_pair(C_DIM),
            )

    def _mining_fields(self) -> list:
        """Return list of (label, current_value, editable) for mining settings."""
        cfg = self._config
        addr = cfg.bitcoin_address
        addr_display = addr if addr else "(none - bc1q...)"
        gpu_t = "Auto" if cfg.gpu_threads == 0 else str(cfg.gpu_threads)
        cpu_t = "Auto" if cfg.cpu_threads == 0 else str(cfg.cpu_threads)
        return [
            ("Algorithm", "SHA-256d (Bitcoin)", False),
            ("Network", cfg.network, True),
            ("Worker Name", cfg.worker_name, True),
            ("Address", addr_display, True),
            ("GPU Threads", gpu_t, True),
            ("CPU Threads", cpu_t, True),
            ("Perf. Mode", cfg.performance_mode, True),
        ]

    def _cycle_mining_field(self, direction: int):
        """Cycle the currently selected mining field value."""
        fields = self._mining_fields()
        if self._sel_idx >= len(fields):
            return
        label = fields[self._sel_idx][0]
        cfg = self._config

        if label == "Network":
            nets = ["Mainnet", "Testnet3", "Testnet4", "Signet", "Regtest"]
            idx = nets.index(cfg.network) if cfg.network in nets else 0
            idx = (idx + direction) % len(nets)
            cfg.network = nets[idx]
        elif label == "GPU Threads":
            opts = list(range(0, 5))  # 0=auto, 1-4
            idx = opts.index(cfg.gpu_threads) if cfg.gpu_threads in opts else 0
            idx = (idx + direction) % len(opts)
            cfg.gpu_threads = opts[idx]
        elif label == "CPU Threads":
            cpu_max = os.cpu_count() or 4
            opts = list(range(0, cpu_max + 1))
            idx = opts.index(cfg.cpu_threads) if cfg.cpu_threads in opts else 0
            idx = (idx + direction) % len(opts)
            cfg.cpu_threads = opts[idx]
        elif label == "Perf. Mode":
            modes = ["Auto", "Full Speed", "Eco Mode"]
            idx = (
                modes.index(cfg.performance_mode)
                if cfg.performance_mode in modes
                else 1
            )
            idx = (idx + direction) % len(modes)
            cfg.performance_mode = modes[idx]

    def _start_edit_mining_field(self):
        """Start editing the currently selected mining field."""
        fields = self._mining_fields()
        if self._sel_idx >= len(fields):
            return
        label, value, editable = fields[self._sel_idx]
        if not editable:
            return

        # For cycle-able fields, cycle on Enter
        if label in ("Network", "GPU Threads", "CPU Threads", "Perf. Mode"):
            self._cycle_mining_field(1)
            return

        # For text fields, enter input mode
        if label == "Worker Name":
            self._input_mode = True
            self._input_field = label
            self._input_buffer = self._config.worker_name
        elif label == "Address":
            self._input_mode = True
            self._input_field = label
            self._input_buffer = self._config.bitcoin_address

    def _finish_edit_mining_field(self):
        """Commit the current input buffer to the config."""
        label = self._input_field
        val = self._input_buffer.strip()
        if label == "Worker Name":
            self._config.worker_name = val
        elif label == "Address":
            self._config.bitcoin_address = val
            # Validate address and show result in log
            if val:
                valid, err = validate_bitcoin_address(val, self._config.network)
                if valid:
                    append_log(f"[TUI] Address accepted: {val[:16]}...")
                else:
                    append_log(f"[TUI] Address warning: {err}")
        self._input_mode = False
        self._input_field = ""
        self._input_buffer = ""

    def _save_mining_config(self):
        """Save mining settings to disk."""
        save_config(self._config)
        append_log(f"[TUI] Config saved: network={self._config.network}")

    # ── Settings > Pools ──

    def _draw_settings_pools(self, win, y, h, w):
        cx = 2
        pools = self._config.pools
        max_visible = h - y - 6

        _safe_addstr(win, y, cx, f"Pools ({len(pools)})", curses.A_BOLD)
        _safe_addstr(
            win,
            y,
            cx + 20,
            f"Active: #{self._config.active_pool_index}",
            curses.color_pair(C_GREEN),
        )
        y += 1

        # Column headers
        _safe_addstr(
            win,
            y,
            cx,
            "  # On Name                 Host                        ",
            curses.color_pair(C_DIM),
        )
        y += 1

        for pi in range(len(pools)):
            if y >= h - 4:
                break
            pool = pools[pi]
            is_sel = pi == self._sel_idx
            is_active = pi == self._config.active_pool_index
            enabled = pool.get("enabled", True)
            name = pool.get("name", "???")[:20]
            host = pool.get("host", "???")
            port = pool.get("port", 3333)

            marker = ">" if is_sel else " "
            en_mark = "[x]" if enabled else "[ ]"
            active_mark = " *" if is_active else "  "

            line = f"{marker}{pi:>2} {en_mark} {name:<20} {host}:{port:<5}"
            ping = self._ping_results.get(pi, "")

            attr = curses.color_pair(C_SELECTED) if is_sel else curses.A_NORMAL
            if is_active:
                attr |= curses.A_BOLD
            _safe_addstr(win, y, cx, line, attr)

            # Active marker
            badge_x = cx + len(line) + 1
            _safe_addstr(
                win,
                y,
                badge_x,
                f"BTC{active_mark}",
                curses.color_pair(C_BLUE) | curses.A_BOLD,
            )

            # Ping result
            if ping:
                ping_col = C_GREEN if "ms" in ping else C_RED
                _safe_addstr(
                    win,
                    y,
                    min(badge_x + 8, w - len(ping) - 2),
                    ping,
                    curses.color_pair(ping_col),
                )

            y += 1

        # Actions
        y += 1
        if y < h - 2:
            _safe_addstr(
                win,
                y,
                cx,
                "[Enter] Toggle  [A] Set Active  [P] Ping  [D] Delete  [N] New  [R] Reset  [S] Save",
                curses.color_pair(C_DIM),
            )

    # ── Settings > General ──

    def _draw_settings_general(self, win, y, h, w):
        cx = 2
        cfg = self._config

        fields = [
            ("Start at Login", "Yes" if cfg.start_at_login else "No", True),
            ("Restart on Stall", "Yes" if cfg.restart_on_stall else "No", True),
            ("Stall Timeout", f"{cfg.stall_timeout_minutes} min", True),
        ]

        for i, (label, value, editable) in enumerate(fields):
            if y >= h - 4:
                break
            is_sel = i == self._sel_idx
            marker = ">" if is_sel else " "
            attr = curses.color_pair(C_SELECTED) if is_sel else curses.A_NORMAL
            _safe_addstr(win, y, cx, f"{marker} {label}:", curses.color_pair(C_DIM))
            _safe_addstr(win, y, cx + 22, value, attr | curses.A_BOLD)
            y += 1

        y += 2
        if y < h - 2:
            _safe_addstr(
                win,
                y,
                cx,
                "[Enter] Toggle  [S] Save  [C] Clear Log",
                curses.color_pair(C_DIM),
            )

    def _toggle_general_field(self):
        """Toggle/cycle the currently selected general field."""
        cfg = self._config
        if self._sel_idx == 0:
            cfg.start_at_login = not cfg.start_at_login
        elif self._sel_idx == 1:
            cfg.restart_on_stall = not cfg.restart_on_stall
        elif self._sel_idx == 2:
            timeouts = [5, 10, 15, 30, 60]
            idx = (
                timeouts.index(cfg.stall_timeout_minutes)
                if cfg.stall_timeout_minutes in timeouts
                else 1
            )
            cfg.stall_timeout_minutes = timeouts[(idx + 1) % len(timeouts)]

    # ── Settings > About ──

    def _draw_settings_about(self, win, y, h, w):
        cx = 4
        _safe_addstr(
            win, y, cx, "SoloMiner", curses.A_BOLD | curses.color_pair(C_HEADER)
        )
        y += 1
        _safe_addstr(win, y, cx, f"Version {APP_VERSION}", curses.color_pair(C_DIM))
        y += 1
        _safe_addstr(win, y, cx, "by Cooper Wang", curses.color_pair(C_DIM))
        y += 2
        desc = (
            "A lightweight, native macOS menu bar application for solo Bitcoin mining."
            " Uses Apple Metal for GPU-accelerated SHA-256d hashing."
            " Connects to pools via the Stratum v1 protocol."
        )
        # Word wrap
        for line in _word_wrap(desc, w - cx - 2):
            if y >= h - 8:
                break
            _safe_addstr(win, y, cx, line, curses.color_pair(C_DIM))
            y += 1

        y += 1
        info = [
            ("Framework", "PyObjC + AppKit / curses TUI"),
            ("GPU", "Apple Metal"),
            ("Algorithm", "SHA-256d (Bitcoin)"),
            ("Protocol", "Stratum v1"),
            ("Platform", "macOS (ARM + Intel)"),
        ]
        for label, val in info:
            if y >= h - 2:
                break
            _safe_addstr(win, y, cx, f"{label}:", curses.color_pair(C_DIM))
            _safe_addstr(win, y, cx + 14, val, curses.A_BOLD)
            y += 1

    # ═══════════════════════════════════════════════════════════
    # Stats
    # ═══════════════════════════════════════════════════════════

    def _draw_stats(self, win, h, w):
        y = 2
        cx = 2

        _safe_addstr(
            win, y, cx, "Statistics", curses.A_BOLD | curses.color_pair(C_HEADER)
        )
        y += 1
        tab_x = cx
        for i, tab in enumerate(self.STATS_TABS):
            attr = (
                curses.color_pair(C_SELECTED) | curses.A_BOLD
                if i == self._stats_tab
                else curses.color_pair(C_DIM)
            )
            label = f" {tab} "
            _safe_addstr(win, y, tab_x, label, attr)
            tab_x += len(label) + 1
        y += 2

        if self._stats_tab == 0:
            self._draw_stats_overview(win, y, h, w)
        elif self._stats_tab == 1:
            self._draw_stats_sessions(win, y, h, w)

    def _draw_stats_overview(self, win, y, h, w):
        cx = 2
        stats = load_stats()

        cards = [
            ("Hashes", _format_hashes(stats.get("total_hashes", 0)), "Total computed"),
            (
                "Runtime",
                _format_runtime(stats.get("total_runtime_seconds", 0)),
                "Total mining time",
            ),
            ("Shares", str(stats.get("shares_found", 0)), "Shares found"),
            (
                "Peak",
                _format_hashrate(stats.get("peak_hashrate", 0.0)),
                "Peak hash rate",
            ),
        ]

        col_w = min((w - 6) // 2, 35)
        for i, (title, value, subtitle) in enumerate(cards):
            col = cx + (i % 2) * (col_w + 2)
            row = y + (i // 2) * 5
            if row + 4 >= h - 1:
                break
            _draw_box(win, row, col, 4, col_w, title)
            _safe_addstr(
                win,
                row + 1,
                col + 3,
                value,
                curses.A_BOLD | curses.color_pair(C_ORANGE),
            )
            _safe_addstr(win, row + 2, col + 3, subtitle, curses.color_pair(C_DIM))

    def _draw_stats_sessions(self, win, y, h, w):
        cx = 2
        stats = load_stats()
        sessions = stats.get("sessions", [])

        if not sessions:
            _safe_addstr(
                win,
                y + 2,
                cx + 4,
                "No mining sessions recorded yet.",
                curses.color_pair(C_DIM),
            )
            return

        # Header
        _safe_addstr(
            win,
            y,
            cx,
            f"{'Start Time':<20} {'Runtime':>8} {'Shares':>6} {'Peak':>10}",
            curses.A_BOLD | curses.color_pair(C_DIM),
        )
        y += 1

        # Show last 20, reversed
        visible = sessions[-20:]
        visible.reverse()
        for sess in visible:
            if y >= h - 2:
                break
            start = sess.get("start_time", "?")
            runtime = _format_runtime(sess.get("runtime_seconds", 0))
            shares = sess.get("shares", 0)
            peak = sess.get("peak_hashrate", 0)
            peak_str = f"{peak / 1e6:.1f}M" if peak > 0 else "---"
            _safe_addstr(
                win, y, cx, f"{start:<20} {runtime:>8} {shares:>6} {peak_str:>10}"
            )
            y += 1

    # ═══════════════════════════════════════════════════════════
    # Logs
    # ═══════════════════════════════════════════════════════════

    def _draw_logs(self, win, h, w):
        y = 2
        cx = 1

        _safe_addstr(
            win, y, cx + 1, "Mining Logs", curses.A_BOLD | curses.color_pair(C_HEADER)
        )
        _safe_addstr(win, y, w - 18, "[R]efresh [C]lear", curses.color_pair(C_DIM))
        y += 1

        self._refresh_log_lines()

        visible_h = h - y - 2
        total = len(self._log_lines)
        # Clamp scroll
        max_scroll = max(0, total - visible_h)
        self._log_scroll = max(0, min(self._log_scroll, max_scroll))

        start = self._log_scroll
        for i in range(visible_h):
            line_idx = start + i
            if line_idx >= total:
                break
            line = self._log_lines[line_idx]
            # Color code
            attr = curses.color_pair(C_DIM)
            line_upper = line.upper()
            if "SHARE FOUND" in line.upper():
                attr = curses.color_pair(C_GREEN) | curses.A_BOLD
            elif (
                "ERROR" in line_upper
                or "REJECTED" in line_upper
                or "FAILED" in line_upper
            ):
                attr = curses.color_pair(C_RED)
            elif "ACCEPTED" in line or "Authorized" in line:
                attr = curses.color_pair(C_GREEN)
            elif "[STRATUM" in line:
                attr = curses.color_pair(C_BLUE)
            elif "[ENGINE" in line:
                attr = curses.color_pair(C_CYAN)

            display = line[: w - cx - 1]
            _safe_addstr(win, y + i, cx, display, attr)

        # Scroll indicator
        if total > visible_h:
            pct = int(100 * (start + visible_h) / total) if total > 0 else 100
            _safe_addstr(
                win, h - 2, w - 12, f"{pct:>3}% ({total})", curses.color_pair(C_DIM)
            )

    def _refresh_log_lines(self):
        text = read_log()
        self._log_lines = text.splitlines() if text else ["(no log entries)"]

    # ═══════════════════════════════════════════════════════════
    # Input handling
    # ═══════════════════════════════════════════════════════════

    def _handle_input(self, win):
        try:
            ch = win.getch()
        except curses.error:
            return

        if ch == -1:
            return

        # ── Input mode (editing a text field) ──
        if self._input_mode:
            if ch == 27:  # Esc
                self._input_mode = False
                self._input_field = ""
                self._input_buffer = ""
            elif ch in (10, 13):  # Enter
                self._finish_edit_mining_field()
            elif ch in (127, curses.KEY_BACKSPACE, 8):
                self._input_buffer = self._input_buffer[:-1]
            elif 32 <= ch <= 126:
                self._input_buffer += chr(ch)
            return

        # ── Global keys ──
        if ch == ord("q") or ch == ord("Q"):
            if self._screen == "dashboard":
                self._running = False
                return
            else:
                self._screen = "dashboard"
                self._sel_idx = 0
                return

        if ch == 27 or ch == curses.KEY_BACKSPACE or ch == 127 or ch == 8:
            if self._screen != "dashboard":
                self._screen = "dashboard"
                self._sel_idx = 0
                return

        # ── Screen-specific keys ──
        if self._screen == "dashboard":
            self._handle_dashboard_input(ch)
        elif self._screen == "settings":
            self._handle_settings_input(ch)
        elif self._screen == "stats":
            self._handle_stats_input(ch)
        elif self._screen == "logs":
            self._handle_logs_input(ch)

    def _handle_dashboard_input(self, ch):
        c = chr(ch) if 32 <= ch <= 126 else ""
        if c in ("s", "S"):
            self._screen = "settings"
            self._sel_idx = 0
        elif c in ("t", "T"):
            self._screen = "stats"
            self._sel_idx = 0
        elif c in ("l", "L"):
            self._screen = "logs"
            self._log_scroll = max(0, len(self._log_lines) - 20)
            self._refresh_log_lines()
        elif c in ("m", "M"):
            self._toggle_mining()
        elif c in ("b", "B"):
            self._run_benchmark()

    def _handle_settings_input(self, ch):
        c = chr(ch) if 32 <= ch <= 126 else ""

        # Tab switching
        if ch == 9:  # Tab
            if self._settings_tab < 3:
                self._settings_tab += 1
            else:
                self._settings_tab = 0
            self._sel_idx = 0
            return
        # Shift-Tab (often 353)
        if ch == 353:
            if self._settings_tab > 0:
                self._settings_tab -= 1
            else:
                self._settings_tab = 3
            self._sel_idx = 0
            return

        # Navigation
        if ch == curses.KEY_UP:
            self._sel_idx = max(0, self._sel_idx - 1)
            return
        if ch == curses.KEY_DOWN:
            self._sel_idx += 1
            return

        # Left/Right for cycling
        if ch == curses.KEY_LEFT and self._settings_tab == 0:
            self._cycle_mining_field(-1)
            return
        if ch == curses.KEY_RIGHT and self._settings_tab == 0:
            self._cycle_mining_field(1)
            return

        # Enter
        if ch in (10, 13):
            if self._settings_tab == 0:
                self._start_edit_mining_field()
            elif self._settings_tab == 1:
                self._pool_toggle_enabled()
            elif self._settings_tab == 2:
                self._toggle_general_field()
            return

        # Save
        if c in ("s", "S"):
            if self._settings_tab in (0, 2):
                self._save_mining_config()
            elif self._settings_tab == 1:
                save_config(self._config)
                append_log("[TUI] Pool configuration saved")
            return

        # Pool-specific keys
        if self._settings_tab == 1:
            if c in ("a", "A"):
                self._pool_set_active()
            elif c in ("p", "P"):
                self._pool_ping()
            elif c in ("d", "D"):
                self._pool_delete()
            elif c in ("n", "N"):
                self._pool_add_interactive()
            elif c in ("r", "R"):
                self._pool_reset()

        # General-specific keys
        if self._settings_tab == 2:
            if c in ("c", "C"):
                clear_log()
                append_log("[TUI] Log cleared")

    def _handle_stats_input(self, ch):
        if ch == 9:  # Tab
            self._stats_tab = (self._stats_tab + 1) % len(self.STATS_TABS)

    def _handle_logs_input(self, ch):
        c = chr(ch) if 32 <= ch <= 126 else ""
        if ch == curses.KEY_UP:
            self._log_scroll = max(0, self._log_scroll - 1)
        elif ch == curses.KEY_DOWN:
            self._log_scroll += 1
        elif ch == curses.KEY_PPAGE:  # Page Up
            self._log_scroll = max(0, self._log_scroll - 20)
        elif ch == curses.KEY_NPAGE:  # Page Down
            self._log_scroll += 20
        elif c in ("r", "R"):
            self._refresh_log_lines()
            self._log_scroll = max(0, len(self._log_lines) - 20)
        elif c in ("c", "C"):
            clear_log()
            self._log_lines = ["(log cleared)"]
            self._log_scroll = 0

    # ═══════════════════════════════════════════════════════════
    # Actions
    # ═══════════════════════════════════════════════════════════

    def _toggle_mining(self):
        if self._engine.is_running:
            self._engine.stop()
            append_log("[TUI] Mining stopped")
        else:
            self._config = load_config()
            address = self._config.bitcoin_address
            if not address:
                append_log(
                    "[TUI] ERROR: No Bitcoin address. Configure in Settings > Mining."
                )
                return

            # Validate address format
            valid, err = validate_bitcoin_address(address, self._config.network)
            if not valid:
                append_log(f"[TUI] ERROR: Invalid address: {err}")
                return

            pools = self._config.pools
            if not pools:
                append_log("[TUI] ERROR: No pools configured.")
                return

            idx = self._config.active_pool_index
            if idx >= len(pools):
                idx = 0
            pool = pools[idx]
            host = pool.get("host", "public-pool.io")
            port = pool.get("port", 3333)

            self._engine.set_thread_config(
                self._config.gpu_threads, self._config.cpu_threads
            )
            self._engine.set_performance_mode(self._config.performance_mode)
            self._engine.start(
                host, port, address, self._config.worker_name, self._config.network
            )
            append_log(f"[TUI] Mining started -> {host}:{port} (Bitcoin / SHA-256d)")

    def _run_benchmark(self):
        if self._benchmarking or self._engine.is_running:
            return
        self._benchmarking = True
        self._bench_result = ""

        def _bench():
            try:
                from .metal_miner import MetalMiner

                miner = MetalMiner()
                header = b"\x00" * 80
                target = (1 << 256) - 1
                total_hashes = 0
                batch = (1 << 22) if miner.use_gpu else (1 << 18)
                iters = 10
                start = time.time()
                for _ in range(iters):
                    miner.mine_range_gpu(
                        header, target, 0, batch
                    ) if miner.use_gpu else miner.mine_range_cpu(
                        header, target, 0, batch
                    )
                    total_hashes += batch
                elapsed = time.time() - start
                rate = total_hashes / elapsed if elapsed > 0 else 0
                self._bench_result = f"{_format_hashrate(rate)} ({miner.gpu_name})"
                append_log(f"[TUI] Benchmark: {self._bench_result}")
            except Exception as e:
                self._bench_result = f"Error: {e}"
                append_log(f"[TUI] Benchmark error: {e}")
            finally:
                self._benchmarking = False

        t = threading.Thread(target=_bench, daemon=True)
        t.start()

    # ── Pool actions ──

    def _pool_toggle_enabled(self):
        pools = self._config.pools
        if 0 <= self._sel_idx < len(pools):
            pools[self._sel_idx]["enabled"] = not pools[self._sel_idx].get(
                "enabled", True
            )

    def _pool_set_active(self):
        pools = self._config.pools
        if 0 <= self._sel_idx < len(pools):
            self._config.active_pool_index = self._sel_idx

    def _pool_delete(self):
        pools = self._config.pools
        if 0 <= self._sel_idx < len(pools) and len(pools) > 1:
            pools.pop(self._sel_idx)
            if self._config.active_pool_index >= len(pools):
                self._config.active_pool_index = 0
            if self._sel_idx >= len(pools):
                self._sel_idx = len(pools) - 1
            save_config(self._config)

    def _pool_ping(self):
        pools = self._config.pools
        if 0 <= self._sel_idx < len(pools):
            pi = self._sel_idx
            pool = pools[pi]
            host = pool.get("host", "")
            port = pool.get("port", 3333)
            self._ping_results[pi] = "..."

            def _do_ping():
                online, latency, err = ping_pool(host, port)
                if online:
                    self._ping_results[pi] = f"{latency}ms"
                else:
                    self._ping_results[pi] = err or "Offline"

            t = threading.Thread(target=_do_ping, daemon=True)
            t.start()

    def _pool_reset(self):
        from dataclasses import asdict

        self._config.pools = [asdict(p) for p in DEFAULT_POOLS]
        self._config.active_pool_index = 0
        self._sel_idx = 0
        self._ping_results.clear()
        save_config(self._config)
        append_log("[TUI] Pools reset to defaults")

    def _pool_add_interactive(self):
        """Add a new pool using inline input. Uses a simple curses prompt."""
        name = self._prompt("Pool name: ")
        if not name:
            return
        host = self._prompt("Host: ")
        if not host:
            return
        port_str = self._prompt("Port [3333]: ")
        try:
            port = int(port_str) if port_str else 3333
        except ValueError:
            port = 3333

        self._config.pools.append(
            {
                "name": name,
                "host": host,
                "port": port,
                "enabled": True,
            }
        )
        save_config(self._config)
        append_log(f"[TUI] Added pool: {name} ({host}:{port})")

    def _prompt(self, prompt_text: str) -> str:
        """Simple blocking text prompt at the bottom of the screen."""
        win = self._stdscr
        h, w = win.getmaxyx()
        curses.curs_set(1)
        win.nodelay(False)

        buf = ""
        while True:
            # Draw prompt
            _safe_addstr(win, h - 2, 0, " " * (w - 1))
            _safe_addstr(
                win, h - 2, 1, prompt_text + buf + "_", curses.color_pair(C_ACCENT)
            )
            win.refresh()

            ch = win.getch()
            if ch in (10, 13):  # Enter
                break
            elif ch == 27:  # Esc
                buf = ""
                break
            elif ch in (127, curses.KEY_BACKSPACE, 8):
                buf = buf[:-1]
            elif 32 <= ch <= 126:
                buf += chr(ch)

        curses.curs_set(0)
        win.nodelay(True)
        return buf.strip()


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


def _word_wrap(text: str, width: int) -> list:
    """Simple word-wrap for a string."""
    words = text.split()
    lines = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = current + " " + word if current else word
    if current:
        lines.append(current)
    return lines


def run_tui():
    """Entry point for the TUI. Called from main.py."""
    tui = SoloMinerTUI()
    curses.wrapper(tui.run)
