"""
SoloMiner macOS Menu Bar Application UI.
Built with PyObjC / AppKit for a fully native macOS experience.

Single-popover architecture: all views (Dashboard, Settings, Stats, Logs)
render inside one popover with navigation. No separate windows, no dock icon,
no activation policy switching.

Fixed-size popover prevents horizontal teleporting on navigation.
"""

import objc
import os
import math
import time
import threading
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*ObjCPointer.*")
from Foundation import (
    NSObject,
    NSTimer,
    NSRunLoop,
    NSRunLoopCommonModes,
    NSMakeRect,
    NSMakeSize,
    NSAttributedString,
    NSDictionary,
    NSMutableAttributedString,
)
from AppKit import (
    NSApp,
    NSStatusBar,
    NSVariableStatusItemLength,
    NSPopover,
    NSViewController,
    NSView,
    NSTextField,
    NSButton,
    NSSegmentedControl,
    NSTextAlignmentLeft,
    NSTextAlignmentRight,
    NSTextAlignmentCenter,
    NSScrollView,
    NSTextView,
    NSSwitchButton,
    NSPopUpButton,
    NSMenu,
    NSMenuItem,
    NSLineBreakByTruncatingTail,
    NSFocusRingTypeNone,
    NSRoundedBezelStyle,
    NSFont,
    NSColor,
    NSFontWeightBold,
    NSFontWeightRegular,
    NSForegroundColorAttributeName,
)

try:
    import Quartz

    QUARTZ_AVAILABLE = True
except ImportError:
    QUARTZ_AVAILABLE = False

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
    PoolConfig,
    DEFAULT_POOLS,
    ALGORITHMS,
    APP_VERSION,
    COINS,
    COIN_REGISTRY,
    coin_to_algorithm,
    coin_to_ticker,
    coin_address_hint,
)
from .engine import MiningEngine


# ─────────────────────────────────────────────
# Color palette
# ─────────────────────────────────────────────
def rgba(r, g, b, a=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(
        r / 255, g / 255, b / 255, a
    )


BG_DARK = rgba(30, 30, 30)
BG_CARD = rgba(255, 255, 255, 0.06)
BG_CARD_HIGHLIGHT = rgba(255, 255, 255, 0.10)
TEXT_PRIMARY = NSColor.whiteColor()
TEXT_SECONDARY = rgba(160, 160, 165)
ACCENT_BLUE = rgba(0, 122, 255)
ACCENT_GREEN = rgba(48, 209, 88)
ACCENT_RED = rgba(255, 69, 58)
ACCENT_ORANGE = rgba(255, 159, 10)
BORDER_COLOR = rgba(255, 255, 255, 0.06)

# Animation colors
PULSE_GREEN = rgba(48, 209, 88, 0.8)
PULSE_ORANGE = rgba(255, 159, 10, 0.8)
GLOW_BLUE = rgba(0, 122, 255, 0.3)
SHIMMER_COLOR = rgba(255, 255, 255, 0.04)

# Log colors
LOG_RED = rgba(255, 69, 58)
LOG_GREEN = rgba(48, 209, 88)
LOG_BLUE = rgba(50, 150, 255)
LOG_CYAN = rgba(100, 210, 255)
LOG_BRIGHT_GREEN = rgba(80, 255, 100)
LOG_DEFAULT = rgba(180, 180, 185)

# ── Fixed popover dimensions ──
# Single fixed size prevents horizontal teleporting on navigation.
POPOVER_WIDTH = 380
POPOVER_HEIGHT = 600


def _cgcolor(nscolor):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return nscolor.CGColor()


def _set_bg(layer, nscolor):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        layer.setBackgroundColor_(_cgcolor(nscolor))


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def make_label(text, size=13, color=None, bold=False, alignment=NSTextAlignmentLeft):
    label = NSTextField.labelWithString_(text)
    weight = NSFontWeightBold if bold else NSFontWeightRegular
    label.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
    label.setTextColor_(color or TEXT_PRIMARY)
    label.setAlignment_(alignment)
    label.setTranslatesAutoresizingMaskIntoConstraints_(False)
    label.setLineBreakMode_(NSLineBreakByTruncatingTail)
    return label


def _make_inline_card(x, y, w, h):
    card = NSView.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    card.setWantsLayer_(True)
    _set_bg(card.layer(), BG_CARD)
    card.layer().setCornerRadius_(16)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        card.layer().setBorderColor_(_cgcolor(rgba(255, 255, 255, 0.12)))
    card.layer().setBorderWidth_(0.5)
    card.layer().setShadowOffset_((0, -1))
    card.layer().setShadowRadius_(4)
    card.layer().setShadowOpacity_(0.15)
    return card


def make_separator_at(y, width, inset=15):
    sep = NSView.alloc().initWithFrame_(NSMakeRect(inset, y, width - inset * 2, 1))
    sep.setWantsLayer_(True)
    _set_bg(sep.layer(), BORDER_COLOR)
    return sep


def make_blue_button(title, frame):
    btn = NSButton.alloc().initWithFrame_(frame)
    btn.setTitle_(title)
    btn.setBezelStyle_(NSRoundedBezelStyle)
    btn.setWantsLayer_(True)
    if hasattr(btn, "setBezelColor_"):
        btn.setBezelColor_(ACCENT_BLUE)
    return btn


# ─────────────────────────────────────────────
# Core Animation helpers (ambient effects)
# ─────────────────────────────────────────────
def _add_pulse_animation(layer, color_from, color_to, duration=2.0, key="pulse"):
    """Add a repeating opacity pulse to a layer using Core Animation."""
    if not QUARTZ_AVAILABLE:
        return
    try:
        anim = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
        anim.setFromValue_(1.0)
        anim.setToValue_(0.3)
        anim.setDuration_(duration)
        anim.setAutoreverses_(True)
        anim.setRepeatCount_(float("inf"))
        anim.setTimingFunction_(
            Quartz.CAMediaTimingFunction.functionWithName_(
                Quartz.kCAMediaTimingFunctionEaseInEaseOut
            )
        )
        layer.addAnimation_forKey_(anim, key)
    except Exception:
        pass


def _add_glow_animation(layer, duration=3.0, key="glow"):
    """Add a soft shadow glow pulse."""
    if not QUARTZ_AVAILABLE:
        return
    try:
        anim = Quartz.CABasicAnimation.animationWithKeyPath_("shadowOpacity")
        anim.setFromValue_(0.0)
        anim.setToValue_(0.5)
        anim.setDuration_(duration)
        anim.setAutoreverses_(True)
        anim.setRepeatCount_(float("inf"))
        anim.setTimingFunction_(
            Quartz.CAMediaTimingFunction.functionWithName_(
                Quartz.kCAMediaTimingFunctionEaseInEaseOut
            )
        )
        layer.addAnimation_forKey_(anim, key)
    except Exception:
        pass


def _add_shimmer_animation(layer, width, duration=4.0, key="shimmer"):
    """Add a horizontal shimmer sweep across a card layer."""
    if not QUARTZ_AVAILABLE:
        return
    try:
        anim = Quartz.CABasicAnimation.animationWithKeyPath_("position.x")
        anim.setFromValue_(-width * 0.3)
        anim.setToValue_(width * 1.3)
        anim.setDuration_(duration)
        anim.setRepeatCount_(float("inf"))
        anim.setTimingFunction_(
            Quartz.CAMediaTimingFunction.functionWithName_(
                Quartz.kCAMediaTimingFunctionEaseInEaseOut
            )
        )
        layer.addAnimation_forKey_(anim, key)
    except Exception:
        pass


def _remove_animation(layer, key="pulse"):
    try:
        layer.removeAnimationForKey_(key)
    except Exception:
        pass


# ─────────────────────────────────────────────
# Color-coded log attributed string builder
# ─────────────────────────────────────────────
def _build_log_attributed_string(log_text):
    mono_font = NSFont.monospacedSystemFontOfSize_weight_(11, NSFontWeightRegular)
    mono_bold = NSFont.monospacedSystemFontOfSize_weight_(11, NSFontWeightBold)

    result = NSMutableAttributedString.alloc().init()

    if not log_text:
        attrs = NSDictionary.dictionaryWithObjects_forKeys_(
            [LOG_DEFAULT, mono_font],
            [NSForegroundColorAttributeName, "NSFont"],
        )
        placeholder = NSAttributedString.alloc().initWithString_attributes_(
            "No log entries yet.", attrs
        )
        result.appendAttributedString_(placeholder)
        return result

    lines = log_text.split("\n")
    for i, line in enumerate(lines):
        if i > 0:
            newline_attrs = NSDictionary.dictionaryWithObjects_forKeys_(
                [LOG_DEFAULT, mono_font],
                [NSForegroundColorAttributeName, "NSFont"],
            )
            nl = NSAttributedString.alloc().initWithString_attributes_(
                "\n", newline_attrs
            )
            result.appendAttributedString_(nl)

        if not line:
            continue

        line_upper = line.upper()

        if "SHARE FOUND" in line_upper:
            color = LOG_BRIGHT_GREEN
            font = mono_bold
        elif (
            "ERROR" in line_upper or "REJECTED" in line_upper or "FAILED" in line_upper
        ):
            color = LOG_RED
            font = mono_font
        elif (
            "ACCEPTED" in line_upper
            or "SUCCESSFUL" in line_upper
            or "Authorized" in line
        ):
            color = LOG_GREEN
            font = mono_font
        elif "[STRATUM" in line:
            color = LOG_BLUE
            font = mono_font
        elif "[ENGINE" in line:
            color = LOG_CYAN
            font = mono_font
        else:
            color = LOG_DEFAULT
            font = mono_font

        attrs = NSDictionary.dictionaryWithObjects_forKeys_(
            [color, font],
            [NSForegroundColorAttributeName, "NSFont"],
        )
        attributed_line = NSAttributedString.alloc().initWithString_attributes_(
            line, attrs
        )
        result.appendAttributedString_(attributed_line)

    return result


# ─────────────────────────────────────────────
# PopoverViewController - single VC with navigation
# ─────────────────────────────────────────────
class PopoverViewController(NSViewController):
    def init(self):
        self = objc.super(PopoverViewController, self).init()
        if self is None:
            return None
        self._engine = None
        self._config = None
        self._update_timer = None

        # Navigation state
        self._current_screen = "dashboard"
        self._nav_bar = None
        self._nav_title = None
        self._nav_back_btn = None
        self._content_container = None

        # Cached embedded views
        self._dashboard_view = None
        self._settings_view = None
        self._stats_view = None
        self._logs_view = None

        # Settings sub-state
        self._settings_tab_views = None
        self._settings_tab_seg = None

        # Logs refresh timer
        self._logs_refresh_timer = None

        # Animation state
        self._status_dot = None
        self._hashrate_card = None
        self._is_mining_animated = False

        return self

    def setEngine_(self, engine):
        self._engine = engine

    def setConfig_(self, config):
        self._config = config

    # ── View loading ──
    def loadView(self):
        w, h = POPOVER_WIDTH, POPOVER_HEIGHT
        self.setView_(NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h)))
        self.view().setWantsLayer_(True)
        root = self.view()

        # ── Navigation bar (hidden on dashboard) ──
        nav_h = 36
        self._nav_bar = NSView.alloc().initWithFrame_(
            NSMakeRect(0, h - nav_h, w, nav_h)
        )
        self._nav_bar.setWantsLayer_(True)
        root.addSubview_(self._nav_bar)

        self._nav_back_btn = NSButton.alloc().initWithFrame_(NSMakeRect(8, 4, 60, 28))
        self._nav_back_btn.setTitle_("Back")
        self._nav_back_btn.setBezelStyle_(NSRoundedBezelStyle)
        self._nav_back_btn.setTarget_(self)
        self._nav_back_btn.setAction_(
            objc.selector(self.navigateBack_, signature=b"v@:@")
        )
        self._nav_bar.addSubview_(self._nav_back_btn)

        self._nav_title = make_label(
            "", size=14, bold=True, alignment=NSTextAlignmentCenter
        )
        self._nav_title.setFrame_(NSMakeRect(70, 6, w - 140, 22))
        self._nav_title.setTranslatesAutoresizingMaskIntoConstraints_(True)
        self._nav_bar.addSubview_(self._nav_title)

        self._nav_bar.setHidden_(True)

        # ── Content container: positioned below nav bar, never resized ──
        # Nav bar is 36px at top. Content is always h-36 tall, pinned at y=0.
        # Dashboard views are built at full h but the top 36px is hidden
        # behind the (hidden) nav bar - this avoids ANY frame changes during nav.
        nav_h = 36
        content_h = h - nav_h
        self._content_container = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, w, content_h)
        )
        root.addSubview_(self._content_container)

        # Build and show dashboard (built at content_h, dashboard scrolls within)
        self._dashboard_view = self._build_dashboard(w, content_h)
        self._content_container.addSubview_(self._dashboard_view)

        self._apply_config()

        self._view_loaded = True
        self.startUpdateTimer()

    # ── Navigation (fixed-size, no popover resize, no frame changes) ──
    def _navigate_to(self, screen_name):
        """Navigate between screens. No frame changes to prevent teleporting."""
        for subview in list(self._content_container.subviews()):
            subview.removeFromSuperview()

        # Stop logs timer if leaving logs
        if self._current_screen == "logs" and self._logs_refresh_timer:
            self._logs_refresh_timer.invalidate()
            self._logs_refresh_timer = None

        self._current_screen = screen_name
        w, h = POPOVER_WIDTH, POPOVER_HEIGHT
        nav_h = 36
        content_h = h - nav_h

        if screen_name == "dashboard":
            self._nav_bar.setHidden_(True)
            # Dashboard uses same content_h as other views
            self._dashboard_view = self._build_dashboard(w, content_h)
            self._content_container.addSubview_(self._dashboard_view)
            self._apply_config()
        else:
            self._nav_bar.setHidden_(False)

            titles = {
                "settings": "Settings",
                "stats": "Statistics",
                "logs": "Mining Logs",
            }
            self._nav_title.setStringValue_(titles.get(screen_name, ""))

            if screen_name == "settings":
                self._config = load_config()
                self._settings_view = self._build_settings(w, content_h)
                self._content_container.addSubview_(self._settings_view)
            elif screen_name == "stats":
                self._stats_view = self._build_stats(w, content_h)
                self._content_container.addSubview_(self._stats_view)
            elif screen_name == "logs":
                self._logs_view = self._build_logs(w, content_h)
                self._content_container.addSubview_(self._logs_view)
                self._logs_refresh_timer = (
                    NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                        2.0,
                        self,
                        objc.selector(self._autoRefreshLogs_, signature=b"v@:@"),
                        None,
                        True,
                    )
                )
                NSRunLoop.currentRunLoop().addTimer_forMode_(
                    self._logs_refresh_timer, NSRunLoopCommonModes
                )

    @objc.typedSelector(b"v@:@")
    def navigateBack_(self, sender):
        self._navigate_to("dashboard")

    @objc.typedSelector(b"v@:@")
    def _autoRefreshLogs_(self, timer):
        if self._current_screen != "logs":
            timer.invalidate()
            return
        if hasattr(self, "_logs_text_view") and self._logs_text_view:
            log_text = read_log() or ""
            attr_str = _build_log_attributed_string(log_text)
            ts = self._logs_text_view.textStorage()
            ts.setAttributedString_(attr_str)
            length = self._logs_text_view.string().length()
            self._logs_text_view.scrollRangeToVisible_((length, 0))

    # ── Dashboard builder ──
    def _build_dashboard(self, width, height):
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        y = height - 10

        # ── Header: SoloMiner title + animated status dot + status text ──
        y -= 30
        title = make_label("SoloMiner", size=16, bold=True)
        title.setFrame_(NSMakeRect(15, y, 150, 22))
        title.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(title)

        # Status dot (animated pulse when mining)
        dot_size = 8
        self._status_dot = NSView.alloc().initWithFrame_(
            NSMakeRect(width - 140, y + 7, dot_size, dot_size)
        )
        self._status_dot.setWantsLayer_(True)
        _set_bg(self._status_dot.layer(), TEXT_SECONDARY)
        self._status_dot.layer().setCornerRadius_(dot_size / 2)
        view.addSubview_(self._status_dot)

        self._auth_label = make_label("Idle", size=11, color=TEXT_SECONDARY)
        self._auth_label.setFrame_(NSMakeRect(width - 128, y + 2, 115, 18))
        self._auth_label.setAlignment_(NSTextAlignmentRight)
        self._auth_label.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(self._auth_label)

        # ── Separator ──
        y -= 12
        view.addSubview_(make_separator_at(y, width))

        # ── Hashrate hero card (with glow animation when mining) ──
        y -= 8
        hr_card_h = 52
        y -= hr_card_h
        self._hashrate_card = _make_inline_card(15, y, width - 30, hr_card_h)
        self._hashrate_card.layer().setCornerRadius_(14)
        view.addSubview_(self._hashrate_card)

        hr_label = make_label("Hash Rate", size=10, color=TEXT_SECONDARY)
        hr_label.setFrame_(NSMakeRect(14, 30, 80, 16))
        hr_label.setTranslatesAutoresizingMaskIntoConstraints_(True)
        self._hashrate_card.addSubview_(hr_label)

        self._hashrate_val = make_label("0.00 MH/s", size=20, bold=True)
        self._hashrate_val.setFrame_(NSMakeRect(14, 4, width - 60, 28))
        self._hashrate_val.setTranslatesAutoresizingMaskIntoConstraints_(True)
        self._hashrate_card.addSubview_(self._hashrate_val)

        # Thread count badge (right side of hashrate card)
        self._threads_badge = make_label("--", size=9, color=TEXT_SECONDARY)
        self._threads_badge.setFrame_(NSMakeRect(width - 90, 32, 45, 14))
        self._threads_badge.setAlignment_(NSTextAlignmentRight)
        self._threads_badge.setTranslatesAutoresizingMaskIntoConstraints_(True)
        self._hashrate_card.addSubview_(self._threads_badge)

        # ── Separator ──
        y -= 8
        view.addSubview_(make_separator_at(y, width))

        # ── Stats rows ──
        row_height = 20
        stats_info = [
            ("Pool", "_pool_val", "---"),
            ("Network", "_network_val", "Mainnet"),
            ("Coin", "_coin_val", "Bitcoin"),
            ("Algorithm", "_algo_val", "SHA-256d"),
            ("Shares", "_shares_val", "0/0"),
            ("Difficulty", "_diff_val", "---"),
            ("Mode", "_mode_val", "Full Speed"),
            ("Uptime", "_uptime_val", "0m 0s"),
        ]
        y -= 4
        for label_text, attr_name, default_val in stats_info:
            y -= row_height
            lbl = make_label(label_text, size=11, color=TEXT_SECONDARY)
            lbl.setFrame_(NSMakeRect(20, y, 90, 18))
            lbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
            view.addSubview_(lbl)

            val = make_label(default_val, size=11, bold=True)
            val.setFrame_(NSMakeRect(115, y, width - 135, 18))
            val.setTranslatesAutoresizingMaskIntoConstraints_(True)
            view.addSubview_(val)
            setattr(self, attr_name, val)

        # ── Separator ──
        y -= 8
        view.addSubview_(make_separator_at(y, width))

        # ── Detail info card: GPU / Best Share / Peak / Jobs ──
        y -= 4
        card_h = 88
        y -= card_h
        detail_card = _make_inline_card(15, y, width - 30, card_h)
        view.addSubview_(detail_card)

        extra_rows = [
            ("GPU", "_gpu_val", "---"),
            ("Best Share", "_best_share_val", "0 bits"),
            ("Peak Rate", "_peak_val", "0.00 MH/s"),
            ("Jobs", "_jobs_val", "0"),
        ]
        ey = card_h - 4
        for label_text, attr_name, default_val in extra_rows:
            ey -= 19
            lbl = make_label(label_text, size=10, color=TEXT_SECONDARY)
            lbl.setFrame_(NSMakeRect(12, ey, 80, 16))
            lbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
            detail_card.addSubview_(lbl)

            val = make_label(default_val, size=10, bold=True)
            val.setFrame_(NSMakeRect(95, ey, width - 135, 16))
            val.setTranslatesAutoresizingMaskIntoConstraints_(True)
            detail_card.addSubview_(val)
            setattr(self, attr_name, val)

        # ── Separator ──
        y -= 8
        view.addSubview_(make_separator_at(y, width))

        # ── Performance Mode ──
        y -= 20
        perf_label = make_label("Performance Mode", size=10, color=TEXT_SECONDARY)
        perf_label.setFrame_(NSMakeRect(15, y, 150, 16))
        perf_label.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(perf_label)

        y -= 30
        self._perf_seg = NSSegmentedControl.alloc().initWithFrame_(
            NSMakeRect(15, y, width - 30, 26)
        )
        self._perf_seg.setSegmentCount_(3)
        self._perf_seg.setLabel_forSegment_("Auto", 0)
        self._perf_seg.setLabel_forSegment_("Full Speed", 1)
        self._perf_seg.setLabel_forSegment_("Eco Mode", 2)
        self._perf_seg.setSelectedSegment_(1)
        self._perf_seg.setTarget_(self)
        self._perf_seg.setAction_(
            objc.selector(self.perfModeChanged_, signature=b"v@:@")
        )
        view.addSubview_(self._perf_seg)

        # ── Separator ──
        y -= 10
        view.addSubview_(make_separator_at(y, width))

        # ── Start/Stop + Settings + Stats ──
        y -= 34
        self._start_stop_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(15, y, 80, 28)
        )
        self._start_stop_btn.setTitle_("Start")
        self._start_stop_btn.setBezelStyle_(NSRoundedBezelStyle)
        self._start_stop_btn.setTarget_(self)
        self._start_stop_btn.setAction_(
            objc.selector(self.toggleMining_, signature=b"v@:@")
        )
        self._start_stop_btn.setWantsLayer_(True)
        if hasattr(self._start_stop_btn, "setBezelColor_"):
            self._start_stop_btn.setBezelColor_(ACCENT_GREEN)
        view.addSubview_(self._start_stop_btn)

        settings_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(width - 90, y, 75, 28)
        )
        settings_btn.setBezelStyle_(NSRoundedBezelStyle)
        settings_btn.setTitle_("Settings")
        settings_btn.setTarget_(self)
        settings_btn.setAction_(objc.selector(self.openSettings_, signature=b"v@:@"))
        view.addSubview_(settings_btn)

        stats_btn = NSButton.alloc().initWithFrame_(NSMakeRect(width - 155, y, 58, 28))
        stats_btn.setBezelStyle_(NSRoundedBezelStyle)
        stats_btn.setTitle_("Stats")
        stats_btn.setTarget_(self)
        stats_btn.setAction_(objc.selector(self.openStats_, signature=b"v@:@"))
        view.addSubview_(stats_btn)

        # ── Separator ──
        y -= 8
        view.addSubview_(make_separator_at(y, width))

        # ── Benchmark + Logs ──
        y -= 30
        bench_btn = NSButton.alloc().initWithFrame_(NSMakeRect(15, y, 100, 24))
        bench_btn.setTitle_("Benchmark")
        bench_btn.setBezelStyle_(NSRoundedBezelStyle)
        bench_btn.setTarget_(self)
        bench_btn.setAction_(objc.selector(self.runBenchmark_, signature=b"v@:@"))
        view.addSubview_(bench_btn)

        logs_btn = NSButton.alloc().initWithFrame_(NSMakeRect(width - 80, y, 65, 24))
        logs_btn.setTitle_("Logs")
        logs_btn.setBezelStyle_(NSRoundedBezelStyle)
        logs_btn.setTarget_(self)
        logs_btn.setAction_(objc.selector(self.openLogs_, signature=b"v@:@"))
        view.addSubview_(logs_btn)

        # ── Separator ──
        y -= 8
        view.addSubview_(make_separator_at(y, width))

        # ── Quit ──
        y -= 28
        quit_btn = NSButton.alloc().initWithFrame_(NSMakeRect(15, y, 110, 24))
        quit_btn.setTitle_("Quit SoloMiner")
        quit_btn.setBezelStyle_(NSRoundedBezelStyle)
        quit_btn.setTarget_(self)
        quit_btn.setAction_(objc.selector(self.quitApp_, signature=b"v@:@"))
        view.addSubview_(quit_btn)

        return view

    def _apply_config(self):
        if self._config:
            mode_map = {"Auto": 0, "Full Speed": 1, "Eco Mode": 2}
            idx = mode_map.get(self._config.performance_mode, 1)
            if hasattr(self, "_perf_seg") and self._perf_seg:
                self._perf_seg.setSelectedSegment_(idx)
            if hasattr(self, "_mode_val") and self._mode_val:
                self._mode_val.setStringValue_(self._config.performance_mode)
            if hasattr(self, "_network_val") and self._network_val:
                self._network_val.setStringValue_(self._config.network)
            if hasattr(self, "_coin_val") and self._coin_val:
                ticker = coin_to_ticker(self._config.coin)
                self._coin_val.setStringValue_(f"{self._config.coin} ({ticker})")
            if hasattr(self, "_algo_val") and self._algo_val:
                self._algo_val.setStringValue_(self._config.active_algorithm)
            if self._config.pools and hasattr(self, "_pool_val") and self._pool_val:
                active = self._config.pools[self._config.active_pool_index]
                self._pool_val.setStringValue_(active.get("name", "---"))

    # ── Animations ──
    def _start_mining_animations(self):
        """Start ambient animations when mining begins."""
        if self._is_mining_animated:
            return
        self._is_mining_animated = True

        # Pulse the status dot
        if self._status_dot:
            _set_bg(self._status_dot.layer(), ACCENT_GREEN)
            _add_pulse_animation(
                self._status_dot.layer(), ACCENT_GREEN, PULSE_GREEN, 1.5
            )

        # Glow on hashrate card
        if self._hashrate_card:
            layer = self._hashrate_card.layer()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                layer.setShadowColor_(_cgcolor(ACCENT_BLUE))
            layer.setShadowRadius_(8)
            _add_glow_animation(layer, duration=2.5)

    def _stop_mining_animations(self):
        """Stop ambient animations."""
        if not self._is_mining_animated:
            return
        self._is_mining_animated = False

        if self._status_dot:
            _remove_animation(self._status_dot.layer(), "pulse")
            _set_bg(self._status_dot.layer(), TEXT_SECONDARY)

        if self._hashrate_card:
            layer = self._hashrate_card.layer()
            _remove_animation(layer, "glow")
            layer.setShadowOpacity_(0.15)
            layer.setShadowRadius_(4)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                layer.setShadowColor_(_cgcolor(NSColor.blackColor()))

    # ── Timer ──
    def startUpdateTimer(self):
        if self._update_timer is not None:
            return
        self._update_timer = (
            NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0,
                self,
                objc.selector(self.updateStats_, signature=b"v@:@"),
                None,
                True,
            )
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(
            self._update_timer, NSRunLoopCommonModes
        )

    @objc.typedSelector(b"v@:@")
    def updateStats_(self, timer):
        if not getattr(self, "_view_loaded", False):
            return
        if not hasattr(self, "_hashrate_val") or self._hashrate_val is None:
            return

        if self._engine and self._engine.is_running:
            hr = self._engine.hashrate
            if hr >= 1e9:
                hr_str = f"{hr / 1e9:.2f} GH/s"
            elif hr >= 1e6:
                hr_str = f"{hr / 1e6:.2f} MH/s"
            elif hr >= 1e3:
                hr_str = f"{hr / 1e3:.2f} KH/s"
            else:
                hr_str = f"{hr:.0f} H/s"
            self._hashrate_val.setStringValue_(hr_str)

            # Thread badge
            if hasattr(self, "_threads_badge") and self._threads_badge:
                tc = self._engine.active_thread_count
                self._threads_badge.setStringValue_(f"{tc} thr" if tc > 0 else "--")

            # Uptime
            secs = int(self._engine.uptime_seconds)
            hours = secs // 3600
            mins = (secs % 3600) // 60
            s = secs % 60
            if hours > 0:
                self._uptime_val.setStringValue_(f"{hours}h {mins}m {s}s")
            else:
                self._uptime_val.setStringValue_(f"{mins}m {s}s")

            self._shares_val.setStringValue_(
                f"{self._engine.shares_accepted}/{self._engine.shares_accepted + self._engine.shares_rejected}"
            )

            diff = self._engine.difficulty
            if diff > 0:
                self._diff_val.setStringValue_(f"{diff:.2e}")

            # Extra info
            if hasattr(self, "_gpu_val") and self._gpu_val:
                if self._engine.miner:
                    self._gpu_val.setStringValue_(self._engine.miner.gpu_name or "---")
                    self._best_share_val.setStringValue_(
                        f"{self._engine.miner.best_share_bits} bits"
                    )
            if hasattr(self, "_peak_val") and self._peak_val:
                pk = self._engine.peak_hashrate
                if pk >= 1e9:
                    self._peak_val.setStringValue_(f"{pk / 1e9:.2f} GH/s")
                elif pk >= 1e6:
                    self._peak_val.setStringValue_(f"{pk / 1e6:.2f} MH/s")
                elif pk >= 1e3:
                    self._peak_val.setStringValue_(f"{pk / 1e3:.2f} KH/s")
                else:
                    self._peak_val.setStringValue_(f"{pk:.0f} H/s")
            if hasattr(self, "_jobs_val") and self._jobs_val:
                self._jobs_val.setStringValue_(str(self._engine.jobs_received))

            # Status
            status = self._engine.status
            if status == "Mining":
                self._auth_label.setStringValue_("Mining")
                self._auth_label.setTextColor_(ACCENT_GREEN)
                _set_bg(self._status_dot.layer(), ACCENT_GREEN)
                self._start_mining_animations()
            elif status == "Authorized":
                self._auth_label.setStringValue_("Authorized")
                self._auth_label.setTextColor_(ACCENT_GREEN)
                _set_bg(self._status_dot.layer(), ACCENT_GREEN)
            elif status in (
                "Connecting",
                "Connected",
                "Subscribing",
                "Subscribed",
                "Authorizing",
                "Reconnecting",
                "Starting",
            ):
                self._auth_label.setStringValue_(status)
                self._auth_label.setTextColor_(ACCENT_ORANGE)
                _set_bg(self._status_dot.layer(), ACCENT_ORANGE)
                _add_pulse_animation(
                    self._status_dot.layer(), ACCENT_ORANGE, PULSE_ORANGE, 1.0, "pulse"
                )
            elif status in (
                "Auth Failed",
                "Subscribe Failed",
                "DNS Failed",
                "Timeout",
                "Refused",
                "Error",
            ):
                self._auth_label.setStringValue_(status)
                self._auth_label.setTextColor_(ACCENT_RED)
                _set_bg(self._status_dot.layer(), ACCENT_RED)
                self._stop_mining_animations()
            elif status == "Disconnected":
                self._auth_label.setStringValue_("Disconnected")
                self._auth_label.setTextColor_(ACCENT_RED)
                _set_bg(self._status_dot.layer(), ACCENT_RED)
                self._stop_mining_animations()
            else:
                self._auth_label.setStringValue_(status)
                self._auth_label.setTextColor_(ACCENT_ORANGE)

            # Menu bar title
            try:
                app_delegate = NSApp.delegate()
                if app_delegate and hasattr(app_delegate, "_status_item"):
                    btn = app_delegate._status_item.button()
                    if btn:
                        btn.setTitle_(f"SoloMiner {hr_str}")
            except Exception:
                pass
        else:
            self._hashrate_val.setStringValue_("0.00 MH/s")
            self._uptime_val.setStringValue_("0m 0s")
            self._auth_label.setStringValue_("Idle")
            self._auth_label.setTextColor_(TEXT_SECONDARY)
            if hasattr(self, "_threads_badge") and self._threads_badge:
                self._threads_badge.setStringValue_("--")
            if hasattr(self, "_gpu_val") and self._gpu_val:
                self._gpu_val.setStringValue_("---")
            if hasattr(self, "_best_share_val") and self._best_share_val:
                self._best_share_val.setStringValue_("0 bits")
            if hasattr(self, "_peak_val") and self._peak_val:
                self._peak_val.setStringValue_("0.00 MH/s")
            if hasattr(self, "_jobs_val") and self._jobs_val:
                self._jobs_val.setStringValue_("0")
            self._stop_mining_animations()
            try:
                app_delegate = NSApp.delegate()
                if app_delegate and hasattr(app_delegate, "_status_item"):
                    btn = app_delegate._status_item.button()
                    if btn:
                        btn.setTitle_("SoloMiner")
            except Exception:
                pass

    # ── Actions ──
    @objc.typedSelector(b"v@:@")
    def perfModeChanged_(self, sender):
        modes = {0: "Auto", 1: "Full Speed", 2: "Eco Mode"}
        mode = modes.get(sender.selectedSegment(), "Full Speed")
        if hasattr(self, "_mode_val") and self._mode_val:
            self._mode_val.setStringValue_(mode)
        if self._config:
            self._config.performance_mode = mode
            save_config(self._config)
        if self._engine:
            self._engine.set_performance_mode(mode)

    @objc.typedSelector(b"v@:@")
    def toggleMining_(self, sender):
        if self._mining_active:
            self._stop_mining()
        else:
            self._start_mining()

    @property
    def _mining_active(self):
        return self._engine and self._engine.is_running

    def _start_mining(self):
        self._config = load_config()
        self._apply_config()

        if not self._config:
            return
        # Get per-coin address
        coin = self._config.coin
        address = self._config.get_address_for_coin(coin)
        if not address:
            append_log(
                f"ERROR: No address configured for {coin}. Open Settings > Mining."
            )
            self._auth_label.setStringValue_("No Address")
            self._auth_label.setTextColor_(ACCENT_RED)
            return

        pools = self._config.pools
        if not pools:
            return
        active_idx = self._config.active_pool_index
        if active_idx >= len(pools):
            active_idx = 0
        active = pools[active_idx]
        host = active.get("host", "public-pool.io")
        port = active.get("port", 3333)

        # Apply thread config and coin/algorithm from settings
        self._engine.set_coin(self._config.coin)
        self._engine.set_thread_config(
            self._config.gpu_threads, self._config.cpu_threads
        )

        self._engine.start(
            host,
            port,
            address,
            self._config.worker_name,
            self._config.network,
        )
        self._start_stop_btn.setTitle_("Stop")
        if hasattr(self._start_stop_btn, "setBezelColor_"):
            self._start_stop_btn.setBezelColor_(ACCENT_RED)
        self._pool_val.setStringValue_(active.get("name", f"{host}:{port}"))
        self._network_val.setStringValue_(self._config.network)
        self._mode_val.setStringValue_(self._config.performance_mode)
        if hasattr(self, "_coin_val") and self._coin_val:
            ticker = coin_to_ticker(coin)
            self._coin_val.setStringValue_(f"{coin} ({ticker})")
        if hasattr(self, "_algo_val") and self._algo_val:
            self._algo_val.setStringValue_(self._config.active_algorithm)
        append_log(
            f"Mining started -> {host}:{port} ({coin} / {self._config.active_algorithm})"
        )

    def _stop_mining(self):
        if self._engine:
            self._engine.stop()
        self._start_stop_btn.setTitle_("Start")
        if hasattr(self._start_stop_btn, "setBezelColor_"):
            self._start_stop_btn.setBezelColor_(ACCENT_GREEN)
        self._hashrate_val.setStringValue_("0.00 MH/s")
        self._uptime_val.setStringValue_("0m 0s")
        self._auth_label.setStringValue_("Idle")
        self._auth_label.setTextColor_(TEXT_SECONDARY)
        self._stop_mining_animations()
        append_log("Mining stopped")

    @objc.typedSelector(b"v@:@")
    def openSettings_(self, sender):
        self._navigate_to("settings")

    @objc.typedSelector(b"v@:@")
    def openStats_(self, sender):
        self._navigate_to("stats")

    @objc.typedSelector(b"v@:@")
    def openLogs_(self, sender):
        self._navigate_to("logs")

    @objc.typedSelector(b"v@:@")
    def runBenchmark_(self, sender):
        append_log("Starting benchmark...")
        self._hashrate_val.setStringValue_("Benchmarking...")
        self._bench_result = None

        def _bench():
            from .metal_miner import MetalMiner
            import time as _time

            miner = MetalMiner()
            header = b"\x00" * 80
            target = (1 << 256) - 1

            start = _time.time()
            iterations = 10
            batch = 1 << 22 if miner.use_gpu else 1 << 18
            for _ in range(iterations):
                if miner.use_gpu:
                    miner.mine_range_gpu(header, target, 0, batch)
                else:
                    miner.mine_range_cpu(header, target, 0, batch)
            elapsed = _time.time() - start
            total = batch * iterations
            rate = total / elapsed

            if rate >= 1e9:
                hr_str = f"{rate / 1e9:.2f} GH/s"
            elif rate >= 1e6:
                hr_str = f"{rate / 1e6:.2f} MH/s"
            elif rate >= 1e3:
                hr_str = f"{rate / 1e3:.2f} KH/s"
            else:
                hr_str = f"{rate:.0f} H/s"

            self._bench_result = (hr_str, miner.gpu_name)
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                objc.selector(
                    PopoverViewController._benchDone_,
                    signature=b"v@:@",
                ),
                None,
                False,
            )

        threading.Thread(target=_bench, daemon=True).start()

    @objc.typedSelector(b"v@:@")
    def _benchDone_(self, _unused):
        if self._bench_result:
            hr_str, gpu_name = self._bench_result
            if hasattr(self, "_hashrate_val") and self._hashrate_val:
                self._hashrate_val.setStringValue_(hr_str)
            if hasattr(self, "_gpu_val") and self._gpu_val:
                self._gpu_val.setStringValue_(gpu_name or "---")
            append_log(f"Benchmark result: {hr_str} (GPU: {gpu_name})")
            self._bench_result = None

    @objc.typedSelector(b"v@:@")
    def quitApp_(self, sender):
        if self._engine and self._engine.is_running:
            self._engine.stop()
        NSApp.terminate_(None)

    # ─────────────────────────────────────────
    # Embedded Settings
    # ─────────────────────────────────────────
    def _build_settings(self, w, h):
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))

        seg_w = min(320, w - 30)
        self._settings_tab_seg = NSSegmentedControl.alloc().initWithFrame_(
            NSMakeRect((w - seg_w) / 2, h - 36, seg_w, 28)
        )
        self._settings_tab_seg.setSegmentCount_(4)
        self._settings_tab_seg.setLabel_forSegment_("General", 0)
        self._settings_tab_seg.setLabel_forSegment_("Pools", 1)
        self._settings_tab_seg.setLabel_forSegment_("Mining", 2)
        self._settings_tab_seg.setLabel_forSegment_("About", 3)
        self._settings_tab_seg.setSelectedSegment_(0)
        self._settings_tab_seg.setTarget_(self)
        self._settings_tab_seg.setAction_(
            objc.selector(self.settingsTabChanged_, signature=b"v@:@")
        )
        view.addSubview_(self._settings_tab_seg)

        content_h = h - 42
        container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, content_h))
        view.addSubview_(container)
        self._settings_tab_container = container

        general = self._build_settings_general(w, content_h)
        pools = self._build_settings_pools(w, content_h)
        mining = self._build_settings_mining(w, content_h)
        about = self._build_settings_about(w, content_h)

        self._settings_tab_views = [general, pools, mining, about]
        for v in self._settings_tab_views:
            v.setHidden_(True)
            container.addSubview_(v)
        general.setHidden_(False)

        return view

    @objc.typedSelector(b"v@:@")
    def settingsTabChanged_(self, sender):
        idx = sender.selectedSegment()
        for i, v in enumerate(self._settings_tab_views):
            v.setHidden_(i != idx)

    # ── General tab ──
    def _build_settings_general(self, w, h):
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        y = h - 10
        pad = 18

        y -= 22
        header = make_label("Startup", size=13, bold=True)
        header.setFrame_(NSMakeRect(pad, y, 200, 20))
        header.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(header)

        y -= 55
        card = _make_inline_card(pad, y, w - pad * 2, 50)
        view.addSubview_(card)

        lbl = make_label("Start at Login", size=12)
        lbl.setFrame_(NSMakeRect(12, 26, 180, 18))
        lbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
        card.addSubview_(lbl)

        self._login_toggle = NSButton.alloc().initWithFrame_(
            NSMakeRect(w - pad * 2 - 55, 26, 44, 20)
        )
        self._login_toggle.setButtonType_(NSSwitchButton)
        self._login_toggle.setTitle_("")
        self._login_toggle.setState_(1 if self._config.start_at_login else 0)
        card.addSubview_(self._login_toggle)

        status_lbl = make_label("Service ready", size=9, color=ACCENT_GREEN)
        status_lbl.setFrame_(NSMakeRect(12, 5, 180, 14))
        status_lbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
        card.addSubview_(status_lbl)

        y -= 26
        header2 = make_label("Auto-Restart", size=13, bold=True)
        header2.setFrame_(NSMakeRect(pad, y, 200, 20))
        header2.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(header2)

        y -= 85
        card2 = _make_inline_card(pad, y, w - pad * 2, 80)
        view.addSubview_(card2)

        lbl2 = make_label("Restart on stall", size=12)
        lbl2.setFrame_(NSMakeRect(12, 54, 180, 18))
        lbl2.setTranslatesAutoresizingMaskIntoConstraints_(True)
        card2.addSubview_(lbl2)

        self._restart_toggle = NSButton.alloc().initWithFrame_(
            NSMakeRect(w - pad * 2 - 55, 54, 44, 20)
        )
        self._restart_toggle.setButtonType_(NSSwitchButton)
        self._restart_toggle.setTitle_("")
        self._restart_toggle.setState_(1 if self._config.restart_on_stall else 0)
        card2.addSubview_(self._restart_toggle)

        sep = NSView.alloc().initWithFrame_(NSMakeRect(12, 47, w - pad * 2 - 24, 1))
        sep.setWantsLayer_(True)
        _set_bg(sep.layer(), BORDER_COLOR)
        card2.addSubview_(sep)

        lbl3 = make_label("Timeout", size=12)
        lbl3.setFrame_(NSMakeRect(12, 24, 80, 18))
        lbl3.setTranslatesAutoresizingMaskIntoConstraints_(True)
        card2.addSubview_(lbl3)

        self._timeout_popup = NSPopUpButton.alloc().initWithFrame_(
            NSMakeRect(w - pad * 2 - 135, 22, 125, 22)
        )
        for mins in [5, 10, 15, 30, 60]:
            self._timeout_popup.addItemWithTitle_(f"{mins} minutes")
        idx = {5: 0, 10: 1, 15: 2, 30: 3, 60: 4}.get(
            self._config.stall_timeout_minutes, 1
        )
        self._timeout_popup.selectItemAtIndex_(idx)
        card2.addSubview_(self._timeout_popup)

        hint = make_label(
            "Auto-restart mining if no activity detected",
            size=9,
            color=TEXT_SECONDARY,
        )
        hint.setFrame_(NSMakeRect(12, 4, w - pad * 2 - 24, 14))
        hint.setTranslatesAutoresizingMaskIntoConstraints_(True)
        card2.addSubview_(hint)

        y -= 26
        header3 = make_label("Activity Log", size=13, bold=True)
        header3.setFrame_(NSMakeRect(pad, y, 200, 20))
        header3.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(header3)

        y -= 58
        card3 = _make_inline_card(pad, y, w - pad * 2, 52)
        view.addSubview_(card3)

        view_log_btn = NSButton.alloc().initWithFrame_(NSMakeRect(12, 26, 140, 22))
        view_log_btn.setTitle_("View Mining Activity")
        view_log_btn.setBezelStyle_(NSRoundedBezelStyle)
        view_log_btn.setTarget_(self)
        view_log_btn.setAction_(objc.selector(self.openLogs_, signature=b"v@:@"))
        card3.addSubview_(view_log_btn)

        clear_log_btn = NSButton.alloc().initWithFrame_(NSMakeRect(12, 2, 120, 22))
        clear_log_btn.setTitle_("Clear Activity Log")
        clear_log_btn.setBezelStyle_(NSRoundedBezelStyle)
        clear_log_btn.setTarget_(self)
        clear_log_btn.setAction_(objc.selector(self.clearLogAction_, signature=b"v@:@"))
        card3.addSubview_(clear_log_btn)

        save_btn = make_blue_button(
            "Save General", NSMakeRect((w - 130) / 2, 10, 130, 28)
        )
        save_btn.setTarget_(self)
        save_btn.setAction_(objc.selector(self.saveGeneralConfig_, signature=b"v@:@"))
        save_btn.setKeyEquivalent_("\r")
        view.addSubview_(save_btn)

        return view

    @objc.typedSelector(b"v@:@")
    def clearLogAction_(self, sender):
        clear_log()

    @objc.typedSelector(b"v@:@")
    def saveGeneralConfig_(self, sender):
        if self._config:
            self._config.start_at_login = bool(self._login_toggle.state())
            self._config.restart_on_stall = bool(self._restart_toggle.state())
            timeout_items = [5, 10, 15, 30, 60]
            self._config.stall_timeout_minutes = timeout_items[
                self._timeout_popup.indexOfSelectedItem()
            ]
            save_config(self._config)
            append_log("General settings saved")

    # ── Pools tab ──
    def _build_settings_pools(self, w, h):
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        pad = 15
        y = h - 10

        y -= 20
        header = make_label("Mining Pools", size=13, bold=True)
        header.setFrame_(NSMakeRect(pad, y, 150, 20))
        header.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(header)

        hint = make_label("Tap to set active", size=9, color=TEXT_SECONDARY)
        hint.setFrame_(NSMakeRect(w - 120, y + 2, 105, 16))
        hint.setAlignment_(NSTextAlignmentRight)
        hint.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(hint)

        y -= 6
        pool_h = 52
        self._pool_cards = []
        self._pool_ping_labels = []
        pools = self._config.pools
        for i, pool in enumerate(pools):
            y -= pool_h + 3
            card = _make_inline_card(pad, y, w - pad * 2, pool_h)
            card.layer().setCornerRadius_(12)
            is_active = i == self._config.active_pool_index
            enabled = pool.get("enabled", True)
            if is_active:
                _set_bg(card.layer(), BG_CARD_HIGHLIGHT)
            view.addSubview_(card)

            cb = NSButton.alloc().initWithFrame_(NSMakeRect(8, 16, 22, 22))
            cb.setButtonType_(NSSwitchButton)
            cb.setTitle_("")
            cb.setState_(1 if enabled else 0)
            cb.setTag_(i)
            cb.setTarget_(self)
            cb.setAction_(objc.selector(self.poolToggled_, signature=b"v@:@"))
            card.addSubview_(cb)

            name = pool.get("name", "Unknown")
            host = pool.get("host", "")
            port = pool.get("port", 3333)
            pool_coin = pool.get("coin", "Bitcoin")
            pool_algo = coin_to_algorithm(pool_coin)

            name_lbl = make_label(name, size=11, bold=True)
            name_lbl.setFrame_(NSMakeRect(34, 32, w - pad * 2 - 155, 16))
            name_lbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
            card.addSubview_(name_lbl)

            detail_lbl = make_label(f"{host} :{port}", size=9, color=TEXT_SECONDARY)
            detail_lbl.setFrame_(NSMakeRect(34, 18, w - pad * 2 - 155, 14))
            detail_lbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
            card.addSubview_(detail_lbl)

            # Coin + algorithm badge
            badge_text = f"{pool_coin} ({pool_algo})"
            algo_lbl = make_label(badge_text, size=8, color=ACCENT_BLUE, bold=True)
            algo_lbl.setFrame_(NSMakeRect(34, 3, 120, 13))
            algo_lbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
            card.addSubview_(algo_lbl)

            # Ping status label
            ping_lbl = make_label("", size=8, color=TEXT_SECONDARY)
            ping_lbl.setFrame_(NSMakeRect(96, 3, 80, 13))
            ping_lbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
            card.addSubview_(ping_lbl)
            self._pool_ping_labels.append(ping_lbl)

            # Ping button
            ping_btn = NSButton.alloc().initWithFrame_(
                NSMakeRect(w - pad * 2 - 130, 16, 36, 18)
            )
            ping_btn.setBezelStyle_(NSRoundedBezelStyle)
            ping_btn.setTitle_("Ping")
            ping_btn.setTag_(i)
            ping_btn.setTarget_(self)
            ping_btn.setAction_(objc.selector(self.pingPool_, signature=b"v@:@"))
            ping_btn.setFont_(NSFont.systemFontOfSize_(8))
            card.addSubview_(ping_btn)

            active_btn = NSButton.alloc().initWithFrame_(
                NSMakeRect(w - pad * 2 - 52, 16, 42, 18)
            )
            active_btn.setBezelStyle_(NSRoundedBezelStyle)
            active_btn.setTitle_("Active" if is_active else "Set")
            active_btn.setTag_(i)
            active_btn.setTarget_(self)
            active_btn.setAction_(objc.selector(self.setActivePool_, signature=b"v@:@"))
            active_btn.setFont_(NSFont.systemFontOfSize_(9))
            card.addSubview_(active_btn)

            del_btn = NSButton.alloc().initWithFrame_(
                NSMakeRect(w - pad * 2 - 88, 16, 32, 18)
            )
            del_btn.setBezelStyle_(NSRoundedBezelStyle)
            del_btn.setTitle_("Del")
            del_btn.setTag_(i)
            del_btn.setTarget_(self)
            del_btn.setAction_(objc.selector(self.deletePool_, signature=b"v@:@"))
            del_btn.setFont_(NSFont.systemFontOfSize_(9))
            card.addSubview_(del_btn)

            self._pool_cards.append((card, cb, name_lbl, detail_lbl, active_btn))

        y -= 24
        add_header = make_label("Add Custom Pool", size=11, bold=True)
        add_header.setFrame_(NSMakeRect(pad, y, 200, 16))
        add_header.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(add_header)

        y -= 60
        card_add = _make_inline_card(pad, y, w - pad * 2, 54)
        card_add.layer().setCornerRadius_(12)
        view.addSubview_(card_add)
        cw = w - pad * 2

        # Row 1: Name + Host + Port
        lbl_n = make_label("Name", size=8, color=TEXT_SECONDARY)
        lbl_n.setFrame_(NSMakeRect(6, 38, 35, 12))
        lbl_n.setTranslatesAutoresizingMaskIntoConstraints_(True)
        card_add.addSubview_(lbl_n)
        self._new_pool_name = NSTextField.alloc().initWithFrame_(
            NSMakeRect(6, 18, int(cw * 0.24), 18)
        )
        self._new_pool_name.setPlaceholderString_("My Pool")
        self._new_pool_name.setFocusRingType_(NSFocusRingTypeNone)
        self._new_pool_name.setFont_(NSFont.systemFontOfSize_(10))
        card_add.addSubview_(self._new_pool_name)

        host_x = int(cw * 0.26)
        lbl_h = make_label("Host", size=8, color=TEXT_SECONDARY)
        lbl_h.setFrame_(NSMakeRect(host_x, 38, 30, 12))
        lbl_h.setTranslatesAutoresizingMaskIntoConstraints_(True)
        card_add.addSubview_(lbl_h)
        self._new_pool_host = NSTextField.alloc().initWithFrame_(
            NSMakeRect(host_x, 18, int(cw * 0.36), 18)
        )
        self._new_pool_host.setPlaceholderString_("pool.example.com")
        self._new_pool_host.setFocusRingType_(NSFocusRingTypeNone)
        self._new_pool_host.setFont_(NSFont.systemFontOfSize_(10))
        card_add.addSubview_(self._new_pool_host)

        port_x = int(cw * 0.64)
        lbl_p = make_label("Port", size=8, color=TEXT_SECONDARY)
        lbl_p.setFrame_(NSMakeRect(port_x, 38, 30, 12))
        lbl_p.setTranslatesAutoresizingMaskIntoConstraints_(True)
        card_add.addSubview_(lbl_p)
        self._new_pool_port = NSTextField.alloc().initWithFrame_(
            NSMakeRect(port_x, 18, int(cw * 0.12), 18)
        )
        self._new_pool_port.setPlaceholderString_("3333")
        self._new_pool_port.setFocusRingType_(NSFocusRingTypeNone)
        self._new_pool_port.setFont_(NSFont.systemFontOfSize_(10))
        card_add.addSubview_(self._new_pool_port)

        # Row 2: Coin selector + Add button
        lbl_coin = make_label("Coin", size=8, color=TEXT_SECONDARY)
        lbl_coin.setFrame_(NSMakeRect(6, 1, 30, 12))
        lbl_coin.setTranslatesAutoresizingMaskIntoConstraints_(True)
        card_add.addSubview_(lbl_coin)

        self._new_pool_coin = NSPopUpButton.alloc().initWithFrame_(
            NSMakeRect(36, -1, 90, 18)
        )
        for c in COINS:
            self._new_pool_coin.addItemWithTitle_(c)
        self._new_pool_coin.setFont_(NSFont.systemFontOfSize_(9))
        card_add.addSubview_(self._new_pool_coin)

        add_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(int(cw * 0.78), 18, int(cw * 0.20), 20)
        )
        add_btn.setTitle_("Add")
        add_btn.setBezelStyle_(NSRoundedBezelStyle)
        add_btn.setTarget_(self)
        add_btn.setAction_(objc.selector(self.addPool_, signature=b"v@:@"))
        add_btn.setFont_(NSFont.systemFontOfSize_(10))
        card_add.addSubview_(add_btn)

        reset_btn = NSButton.alloc().initWithFrame_(NSMakeRect(pad, 10, 95, 24))
        reset_btn.setTitle_("Reset Defaults")
        reset_btn.setBezelStyle_(NSRoundedBezelStyle)
        reset_btn.setTarget_(self)
        reset_btn.setAction_(objc.selector(self.resetPools_, signature=b"v@:@"))
        reset_btn.setFont_(NSFont.systemFontOfSize_(10))
        view.addSubview_(reset_btn)

        save_btn = make_blue_button("Save Pools", NSMakeRect(w - pad - 95, 10, 95, 24))
        save_btn.setTarget_(self)
        save_btn.setAction_(objc.selector(self.savePools_, signature=b"v@:@"))
        save_btn.setKeyEquivalent_("\r")
        view.addSubview_(save_btn)

        return view

    # ── Mining tab (with coin selection, thread/core selection) ──
    def _build_settings_mining(self, w, h):
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        pad = 18
        y = h - 10

        # Coin Selection (replaces Algorithm Selection)
        y -= 22
        header0 = make_label("Cryptocurrency", size=13, bold=True)
        header0.setFrame_(NSMakeRect(pad, y, 200, 20))
        header0.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(header0)

        y -= 38
        card0 = _make_inline_card(pad, y, w - pad * 2, 32)
        view.addSubview_(card0)

        self._coin_seg = NSSegmentedControl.alloc().initWithFrame_(
            NSMakeRect(6, 4, w - pad * 2 - 12, 24)
        )
        self._coin_seg.setSegmentCount_(len(COINS))
        for i, coin_name in enumerate(COINS):
            ticker = coin_to_ticker(coin_name)
            self._coin_seg.setLabel_forSegment_(ticker, i)
        coin_idx = COINS.index(self._config.coin) if self._config.coin in COINS else 0
        self._coin_seg.setSelectedSegment_(coin_idx)
        self._coin_seg.setTarget_(self)
        self._coin_seg.setAction_(objc.selector(self.coinChanged_, signature=b"v@:@"))
        card0.addSubview_(self._coin_seg)

        # Algorithm hint (derived from coin, read-only)
        y -= 18
        self._algo_hint_label = make_label(
            f"{self._config.coin} -- {self._config.active_algorithm}",
            size=9,
            color=TEXT_SECONDARY,
        )
        self._algo_hint_label.setFrame_(NSMakeRect(pad + 4, y, w - pad * 2, 14))
        self._algo_hint_label.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(self._algo_hint_label)

        # Network
        y -= 20
        header = make_label("Network", size=13, bold=True)
        header.setFrame_(NSMakeRect(pad, y, 200, 20))
        header.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(header)

        y -= 38
        card = _make_inline_card(pad, y, w - pad * 2, 32)
        view.addSubview_(card)

        self._network_seg = NSSegmentedControl.alloc().initWithFrame_(
            NSMakeRect(6, 4, w - pad * 2 - 12, 24)
        )
        self._network_seg.setSegmentCount_(5)
        networks = ["Mainnet", "Testnet3", "Testnet4", "Signet", "Regtest"]
        for i, n in enumerate(networks):
            self._network_seg.setLabel_forSegment_(n, i)
        idx = (
            networks.index(self._config.network)
            if self._config.network in networks
            else 0
        )
        self._network_seg.setSelectedSegment_(idx)
        card.addSubview_(self._network_seg)

        # Worker
        y -= 20
        header2 = make_label("Worker", size=13, bold=True)
        header2.setFrame_(NSMakeRect(pad, y, 200, 20))
        header2.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(header2)

        y -= 48
        card2 = _make_inline_card(pad, y, w - pad * 2, 44)
        view.addSubview_(card2)

        wlbl = make_label("Worker Name", size=11)
        wlbl.setFrame_(NSMakeRect(12, 22, 100, 16))
        wlbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
        card2.addSubview_(wlbl)

        self._worker_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(120, 22, w - pad * 2 - 140, 18)
        )
        self._worker_field.setStringValue_(self._config.worker_name)
        self._worker_field.setAlignment_(NSTextAlignmentRight)
        self._worker_field.setFocusRingType_(NSFocusRingTypeNone)
        card2.addSubview_(self._worker_field)

        hint = make_label(
            "Identifies your miner on the pool", size=9, color=TEXT_SECONDARY
        )
        hint.setFrame_(NSMakeRect(12, 4, 250, 14))
        hint.setTranslatesAutoresizingMaskIntoConstraints_(True)
        card2.addSubview_(hint)

        # Payout
        y -= 20
        header3 = make_label("Payout Address", size=13, bold=True)
        header3.setFrame_(NSMakeRect(pad, y, 200, 20))
        header3.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(header3)

        y -= 48
        card3 = _make_inline_card(pad, y, w - pad * 2, 44)
        view.addSubview_(card3)

        addr_lbl = make_label("Address", size=11)
        addr_lbl.setFrame_(NSMakeRect(12, 22, 60, 16))
        addr_lbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
        card3.addSubview_(addr_lbl)

        self._address_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(78, 22, w - pad * 2 - 94, 18)
        )
        # Load per-coin address
        current_coin = self._config.coin
        self._address_field.setStringValue_(
            self._config.get_address_for_coin(current_coin)
        )
        self._address_field.setPlaceholderString_(coin_address_hint(current_coin))
        self._address_field.setFocusRingType_(NSFocusRingTypeNone)
        self._address_field.setFont_(
            NSFont.monospacedSystemFontOfSize_weight_(10, NSFontWeightRegular)
        )
        card3.addSubview_(self._address_field)

        self._addr_coin_hint = make_label(
            f"{current_coin} ({coin_to_ticker(current_coin)}) address",
            size=9,
            color=TEXT_SECONDARY,
        )
        self._addr_coin_hint.setFrame_(NSMakeRect(12, 4, 250, 14))
        self._addr_coin_hint.setTranslatesAutoresizingMaskIntoConstraints_(True)
        card3.addSubview_(self._addr_coin_hint)

        # ── Thread / Core Selection ──
        y -= 20
        header4 = make_label("Thread Config", size=13, bold=True)
        header4.setFrame_(NSMakeRect(pad, y, 200, 20))
        header4.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(header4)

        cpu_count = os.cpu_count() or 4

        y -= 72
        card4 = _make_inline_card(pad, y, w - pad * 2, 68)
        view.addSubview_(card4)

        gpu_lbl = make_label("GPU Threads", size=11)
        gpu_lbl.setFrame_(NSMakeRect(12, 42, 180, 16))
        gpu_lbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
        card4.addSubview_(gpu_lbl)

        self._gpu_threads_popup = NSPopUpButton.alloc().initWithFrame_(
            NSMakeRect(w - pad * 2 - 80, 40, 70, 20)
        )
        self._gpu_threads_popup.addItemWithTitle_("Auto")
        for n in range(1, 5):
            self._gpu_threads_popup.addItemWithTitle_(str(n))
        gpu_val = self._config.gpu_threads
        if gpu_val == 0:
            self._gpu_threads_popup.selectItemAtIndex_(0)
        elif gpu_val <= 4:
            self._gpu_threads_popup.selectItemAtIndex_(gpu_val)
        card4.addSubview_(self._gpu_threads_popup)

        sep4 = NSView.alloc().initWithFrame_(NSMakeRect(12, 35, w - pad * 2 - 24, 1))
        sep4.setWantsLayer_(True)
        _set_bg(sep4.layer(), BORDER_COLOR)
        card4.addSubview_(sep4)

        cpu_lbl = make_label("CPU Threads", size=11)
        cpu_lbl.setFrame_(NSMakeRect(12, 12, 180, 16))
        cpu_lbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
        card4.addSubview_(cpu_lbl)

        self._cpu_threads_popup = NSPopUpButton.alloc().initWithFrame_(
            NSMakeRect(w - pad * 2 - 80, 10, 70, 20)
        )
        self._cpu_threads_popup.addItemWithTitle_(f"Auto ({max(1, cpu_count - 1)})")
        for n in range(1, cpu_count + 1):
            self._cpu_threads_popup.addItemWithTitle_(str(n))
        cpu_val = self._config.cpu_threads
        if cpu_val == 0:
            self._cpu_threads_popup.selectItemAtIndex_(0)
        elif cpu_val <= cpu_count:
            self._cpu_threads_popup.selectItemAtIndex_(cpu_val)
        card4.addSubview_(self._cpu_threads_popup)

        # Save button
        save_btn = make_blue_button(
            "Save Configuration", NSMakeRect((w - 155) / 2, 10, 155, 28)
        )
        save_btn.setTarget_(self)
        save_btn.setAction_(objc.selector(self.saveMiningConfig_, signature=b"v@:@"))
        save_btn.setKeyEquivalent_("\r")
        view.addSubview_(save_btn)

        return view

    # ── About tab ──
    def _build_settings_about(self, w, h):
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        y = h - 25

        title = make_label(
            "SoloMiner", size=22, bold=True, alignment=NSTextAlignmentCenter
        )
        title.setFrame_(NSMakeRect(0, y, w, 28))
        title.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(title)

        y -= 20
        ver = make_label(
            f"Version {APP_VERSION}",
            size=12,
            color=TEXT_SECONDARY,
            alignment=NSTextAlignmentCenter,
        )
        ver.setFrame_(NSMakeRect(0, y, w, 18))
        ver.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(ver)

        y -= 50
        desc = NSTextField.wrappingLabelWithString_(
            "A lightweight, native macOS application for solo mining. "
            "Uses Apple Metal for GPU-accelerated hashing across "
            "SHA-256d, Scrypt, and RandomX algorithms. "
            "Connects to pools via the Stratum v1 protocol."
        )
        desc.setFont_(NSFont.systemFontOfSize_weight_(11, NSFontWeightRegular))
        desc.setTextColor_(TEXT_SECONDARY)
        desc.setAlignment_(NSTextAlignmentCenter)
        desc.setFrame_(NSMakeRect(30, y, w - 60, 48))
        desc.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.addSubview_(desc)

        y -= 18
        card_h = 128
        y -= card_h
        card = _make_inline_card(25, y, w - 50, card_h)
        view.addSubview_(card)

        items = [
            ("Framework", "PyObjC + AppKit"),
            ("GPU", "Apple Metal (all algos)"),
            ("Algorithms", "SHA-256d, Scrypt, RandomX"),
            ("Protocol", "Stratum v1"),
            ("Platform", "macOS (ARM + Intel)"),
        ]
        row_h = 22
        ey = card_h - 8
        for i, (k, v) in enumerate(items):
            ey -= row_h
            kl = make_label(k, size=10, color=TEXT_SECONDARY)
            kl.setFrame_(NSMakeRect(14, ey, 90, 16))
            kl.setTranslatesAutoresizingMaskIntoConstraints_(True)
            card.addSubview_(kl)

            vl = make_label(v, size=10, bold=True)
            vl.setFrame_(NSMakeRect(110, ey, w - 170, 16))
            vl.setTranslatesAutoresizingMaskIntoConstraints_(True)
            card.addSubview_(vl)

            if i < len(items) - 1:
                row_sep = NSView.alloc().initWithFrame_(
                    NSMakeRect(14, ey - 3, w - 78, 1)
                )
                row_sep.setWantsLayer_(True)
                _set_bg(row_sep.layer(), BORDER_COLOR)
                card.addSubview_(row_sep)

        return view

    @objc.typedSelector(b"v@:@")
    def pingPool_(self, sender):
        idx = sender.tag()
        if self._config and idx < len(self._config.pools):
            pool = self._config.pools[idx]
            host = pool.get("host", "")
            port = pool.get("port", 3333)
            if idx < len(self._pool_ping_labels):
                self._pool_ping_labels[idx].setStringValue_("Pinging...")
                self._pool_ping_labels[idx].setTextColor_(ACCENT_ORANGE)

            ping_idx = idx

            def _do_ping():
                online, latency, err = ping_pool(host, port)
                self._ping_results = (ping_idx, online, latency, err)
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    objc.selector(
                        PopoverViewController._pingDone_,
                        signature=b"v@:@",
                    ),
                    None,
                    False,
                )

            threading.Thread(target=_do_ping, daemon=True).start()

    @objc.typedSelector(b"v@:@")
    def _pingDone_(self, _unused):
        if hasattr(self, "_ping_results") and self._ping_results:
            idx, online, latency, err = self._ping_results
            if idx < len(self._pool_ping_labels):
                if online:
                    self._pool_ping_labels[idx].setStringValue_(f"{latency}ms")
                    self._pool_ping_labels[idx].setTextColor_(ACCENT_GREEN)
                else:
                    self._pool_ping_labels[idx].setStringValue_(err or "Offline")
                    self._pool_ping_labels[idx].setTextColor_(ACCENT_RED)
            self._ping_results = None

    # ── Pool actions ──
    @objc.typedSelector(b"v@:@")
    def poolToggled_(self, sender):
        idx = sender.tag()
        if self._config and idx < len(self._config.pools):
            self._config.pools[idx]["enabled"] = bool(sender.state())

    @objc.typedSelector(b"v@:@")
    def setActivePool_(self, sender):
        idx = sender.tag()
        if self._config:
            self._config.active_pool_index = idx
        for i, (card, cb, nl, dl, ab) in enumerate(self._pool_cards):
            if i == idx:
                ab.setTitle_("Active")
                _set_bg(card.layer(), BG_CARD_HIGHLIGHT)
            else:
                ab.setTitle_("Set")
                _set_bg(card.layer(), BG_CARD)

    @objc.typedSelector(b"v@:@")
    def deletePool_(self, sender):
        idx = sender.tag()
        if self._config and idx < len(self._config.pools):
            self._config.pools.pop(idx)
            if self._config.active_pool_index >= len(self._config.pools):
                self._config.active_pool_index = max(0, len(self._config.pools) - 1)
            save_config(self._config)
            self._rebuild_settings_pools_tab()

    def _rebuild_settings_pools_tab(self):
        if not self._settings_tab_container:
            return
        w = int(self._settings_tab_container.frame().size.width)
        h = int(self._settings_tab_container.frame().size.height)
        old = self._settings_tab_views[1]
        new_pools = self._build_settings_pools(w, h)
        self._settings_tab_views[1] = new_pools
        self._settings_tab_container.addSubview_(new_pools)
        old.removeFromSuperview()
        if self._settings_tab_seg and self._settings_tab_seg.selectedSegment() == 1:
            new_pools.setHidden_(False)

    @objc.typedSelector(b"v@:@")
    def addPool_(self, sender):
        name = str(self._new_pool_name.stringValue()).strip()
        host = str(self._new_pool_host.stringValue()).strip()
        port_str = str(self._new_pool_port.stringValue()).strip()
        if not name or not host:
            return
        try:
            port = int(port_str) if port_str else 3333
        except ValueError:
            port = 3333
        coin = str(self._new_pool_coin.titleOfSelectedItem())
        self._config.pools.append(
            {
                "name": name,
                "host": host,
                "port": port,
                "enabled": True,
                "coin": coin,
            }
        )
        save_config(self._config)
        self._rebuild_settings_pools_tab()

    @objc.typedSelector(b"v@:@")
    def resetPools_(self, sender):
        from dataclasses import asdict

        self._config.pools = [asdict(p) for p in DEFAULT_POOLS]
        self._config.active_pool_index = 0
        save_config(self._config)
        self._rebuild_settings_pools_tab()

    @objc.typedSelector(b"v@:@")
    def savePools_(self, sender):
        if self._config:
            save_config(self._config)
            append_log("Pool configuration saved")

    @objc.typedSelector(b"v@:@")
    def coinChanged_(self, sender):
        """When coin selector changes, save current address and load new one."""
        new_coin = COINS[sender.selectedSegment()]

        # Save current address for the old coin
        if hasattr(self, "_address_field") and self._address_field and self._config:
            old_coin = self._config.coin
            old_addr = str(self._address_field.stringValue())
            self._config.set_address_for_coin(old_coin, old_addr)

        # Load address for the new coin
        if self._config:
            self._config.coin = new_coin
            self._config.algorithm = coin_to_algorithm(new_coin)
            new_addr = self._config.get_address_for_coin(new_coin)
            if hasattr(self, "_address_field") and self._address_field:
                self._address_field.setStringValue_(new_addr)
                self._address_field.setPlaceholderString_(coin_address_hint(new_coin))
            if hasattr(self, "_addr_coin_hint") and self._addr_coin_hint:
                ticker = coin_to_ticker(new_coin)
                self._addr_coin_hint.setStringValue_(f"{new_coin} ({ticker}) address")
            if hasattr(self, "_algo_hint_label") and self._algo_hint_label:
                self._algo_hint_label.setStringValue_(
                    f"{new_coin} -- {self._config.active_algorithm}"
                )

    @objc.typedSelector(b"v@:@")
    def saveMiningConfig_(self, sender):
        if not self._config:
            return
        networks = ["Mainnet", "Testnet3", "Testnet4", "Signet", "Regtest"]
        idx = self._network_seg.selectedSegment()
        self._config.network = networks[idx]
        self._config.worker_name = str(self._worker_field.stringValue())

        # Coin (algorithm derived from coin)
        coin_idx = self._coin_seg.selectedSegment()
        self._config.coin = COINS[coin_idx]
        self._config.algorithm = coin_to_algorithm(self._config.coin)

        # Save per-coin address
        current_addr = str(self._address_field.stringValue())
        self._config.set_address_for_coin(self._config.coin, current_addr)
        # Also keep legacy field in sync for Bitcoin
        if self._config.coin == "Bitcoin":
            self._config.bitcoin_address = current_addr

        # Thread config
        gpu_idx = self._gpu_threads_popup.indexOfSelectedItem()
        self._config.gpu_threads = 0 if gpu_idx == 0 else gpu_idx
        cpu_idx = self._cpu_threads_popup.indexOfSelectedItem()
        self._config.cpu_threads = 0 if cpu_idx == 0 else cpu_idx

        save_config(self._config)
        append_log(
            f"Configuration saved: coin={self._config.coin}, "
            f"algo={self._config.active_algorithm}, "
            f"network={self._config.network}, "
            f"address={current_addr[:12]}..., "
            f"gpu_threads={self._config.gpu_threads or 'auto'}, "
            f"cpu_threads={self._config.cpu_threads or 'auto'}"
        )

    # ─────────────────────────────────────────
    # Embedded Stats
    # ─────────────────────────────────────────
    def _build_stats(self, w, h):
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))

        seg_w = min(260, w - 30)
        self._stats_tab_seg = NSSegmentedControl.alloc().initWithFrame_(
            NSMakeRect((w - seg_w) / 2, h - 36, seg_w, 26)
        )
        self._stats_tab_seg.setSegmentCount_(3)
        self._stats_tab_seg.setLabel_forSegment_("Overview", 0)
        self._stats_tab_seg.setLabel_forSegment_("Sessions", 1)
        self._stats_tab_seg.setLabel_forSegment_("Blocks", 2)
        self._stats_tab_seg.setSelectedSegment_(0)
        self._stats_tab_seg.setTarget_(self)
        self._stats_tab_seg.setAction_(
            objc.selector(self.statsTabChanged_, signature=b"v@:@")
        )
        view.addSubview_(self._stats_tab_seg)

        content_h = h - 42
        overview = self._build_stats_overview(w, content_h)
        sessions = self._build_stats_sessions(w, content_h)
        blocks = self._build_stats_blocks(w, content_h)

        self._stats_tab_views = [overview, sessions, blocks]
        for v in self._stats_tab_views:
            v.setFrame_(NSMakeRect(0, 0, w, content_h))
            v.setHidden_(True)
            view.addSubview_(v)
        overview.setHidden_(False)

        return view

    @objc.typedSelector(b"v@:@")
    def statsTabChanged_(self, sender):
        idx = sender.selectedSegment()
        for i, v in enumerate(self._stats_tab_views):
            v.setHidden_(i != idx)

    def _build_stats_overview(self, w, h):
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        stats = load_stats()

        card_w = int((w - 50) / 2)
        card_h = 85
        gap = 12
        start_x = 15
        row1_y = h - card_h - 15
        row2_y = row1_y - card_h - gap

        cards_data = [
            (
                "Hashes",
                self._format_hashes(stats.get("total_hashes", 0)),
                "Total Hashes",
                ACCENT_ORANGE,
            ),
            (
                "Time",
                self._format_runtime(stats.get("total_runtime_seconds", 0)),
                "Runtime",
                ACCENT_ORANGE,
            ),
            (
                "Shares",
                str(stats.get("shares_found", 0)),
                "Shares Found",
                ACCENT_ORANGE,
            ),
            (
                "Peak",
                f"{stats.get('peak_hashrate', 0) / 1e6:.2f}\nMH/s",
                "Peak Rate",
                ACCENT_ORANGE,
            ),
        ]

        positions = [
            (start_x, row1_y),
            (start_x + card_w + gap, row1_y),
            (start_x, row2_y),
            (start_x + card_w + gap, row2_y),
        ]

        for i, ((icon, value, label, color), (cx, cy)) in enumerate(
            zip(cards_data, positions)
        ):
            card = _make_inline_card(cx, cy, card_w, card_h)
            card.layer().setCornerRadius_(12)
            view.addSubview_(card)

            icon_lbl = make_label(
                icon, size=11, color=color, bold=True, alignment=NSTextAlignmentCenter
            )
            icon_lbl.setFrame_(NSMakeRect(0, 58, card_w, 18))
            icon_lbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
            card.addSubview_(icon_lbl)

            val_lbl = make_label(
                value, size=15, bold=True, alignment=NSTextAlignmentCenter
            )
            val_lbl.setFrame_(NSMakeRect(4, 22, card_w - 8, 36))
            val_lbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
            card.addSubview_(val_lbl)

            name_lbl = make_label(
                label, size=9, color=TEXT_SECONDARY, alignment=NSTextAlignmentCenter
            )
            name_lbl.setFrame_(NSMakeRect(0, 4, card_w, 14))
            name_lbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
            card.addSubview_(name_lbl)

        return view

    def _build_stats_sessions(self, w, h):
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        stats = load_stats()
        sessions = stats.get("sessions", [])

        if not sessions:
            lbl = make_label(
                "No mining sessions recorded yet.",
                size=12,
                color=TEXT_SECONDARY,
                alignment=NSTextAlignmentCenter,
            )
            lbl.setFrame_(NSMakeRect(0, h / 2 - 10, w, 18))
            lbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
            view.addSubview_(lbl)
            return view

        y = h - 12
        header_lbl = make_label(
            "Start Time              Runtime   Shares  Peak",
            size=10,
            color=TEXT_SECONDARY,
            bold=True,
        )
        header_lbl.setFrame_(NSMakeRect(12, y, w - 24, 14))
        header_lbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
        header_lbl.setFont_(
            NSFont.monospacedSystemFontOfSize_weight_(9, NSFontWeightBold)
        )
        view.addSubview_(header_lbl)

        for session in reversed(sessions[-20:]):
            y -= 18
            st = session.get("start_time", "?")
            rt = self._format_runtime(session.get("runtime_seconds", 0))
            sh = str(session.get("shares", 0))
            pk = f"{session.get('peak_hashrate', 0) / 1e6:.1f}M"
            line = f"{st}  {rt:>8}  {sh:>5}  {pk:>8}"
            row = make_label(line, size=9, color=TEXT_PRIMARY)
            row.setFrame_(NSMakeRect(12, y, w - 24, 14))
            row.setTranslatesAutoresizingMaskIntoConstraints_(True)
            row.setFont_(
                NSFont.monospacedSystemFontOfSize_weight_(9, NSFontWeightRegular)
            )
            view.addSubview_(row)

        return view

    def _build_stats_blocks(self, w, h):
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        stats = load_stats()
        blocks = stats.get("blocks", [])

        if not blocks:
            lbl = make_label(
                "No blocks found yet. Keep mining!",
                size=12,
                color=TEXT_SECONDARY,
                alignment=NSTextAlignmentCenter,
            )
            lbl.setFrame_(NSMakeRect(0, h / 2 - 10, w, 18))
            lbl.setTranslatesAutoresizingMaskIntoConstraints_(True)
            view.addSubview_(lbl)

        return view

    @staticmethod
    def _format_hashes(n):
        if n >= 1e12:
            return f"{n / 1e12:.2f}T"
        elif n >= 1e9:
            return f"{n / 1e9:.2f}G"
        elif n >= 1e6:
            return f"{n / 1e6:.2f}M"
        elif n >= 1e3:
            return f"{n / 1e3:.2f}K"
        return str(int(n))

    @staticmethod
    def _format_runtime(seconds):
        seconds = int(seconds)
        hours = seconds // 3600
        mins = (seconds % 3600) // 60
        if hours > 0:
            return f"{hours}h {mins}m"
        return f"{mins}m"

    # ─────────────────────────────────────────
    # Embedded Logs
    # ─────────────────────────────────────────
    def _build_logs(self, w, h):
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(10, 36, w - 20, h - 44))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(0)
        scroll.setWantsLayer_(True)
        scroll.layer().setCornerRadius_(10)

        tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, w - 30, h - 44))
        tv.setEditable_(False)
        tv.setFont_(NSFont.monospacedSystemFontOfSize_weight_(10, NSFontWeightRegular))
        tv.setTextColor_(LOG_DEFAULT)
        tv.setBackgroundColor_(rgba(20, 20, 22, 0.6))

        log_text = read_log() or ""
        attr_str = _build_log_attributed_string(log_text)
        tv.textStorage().setAttributedString_(attr_str)

        scroll.setDocumentView_(tv)
        view.addSubview_(scroll)
        self._logs_text_view = tv

        refresh_btn = NSButton.alloc().initWithFrame_(NSMakeRect(10, 6, 65, 24))
        refresh_btn.setTitle_("Refresh")
        refresh_btn.setBezelStyle_(NSRoundedBezelStyle)
        refresh_btn.setTarget_(self)
        refresh_btn.setAction_(
            objc.selector(self.refreshLogsAction_, signature=b"v@:@")
        )
        view.addSubview_(refresh_btn)

        clear_btn = NSButton.alloc().initWithFrame_(NSMakeRect(82, 6, 55, 24))
        clear_btn.setTitle_("Clear")
        clear_btn.setBezelStyle_(NSRoundedBezelStyle)
        clear_btn.setTarget_(self)
        clear_btn.setAction_(objc.selector(self.clearLogsAction_, signature=b"v@:@"))
        view.addSubview_(clear_btn)

        length = tv.string().length()
        tv.scrollRangeToVisible_((length, 0))

        return view

    @objc.typedSelector(b"v@:@")
    def refreshLogsAction_(self, sender):
        if hasattr(self, "_logs_text_view") and self._logs_text_view:
            log_text = read_log() or ""
            attr_str = _build_log_attributed_string(log_text)
            self._logs_text_view.textStorage().setAttributedString_(attr_str)
            length = self._logs_text_view.string().length()
            self._logs_text_view.scrollRangeToVisible_((length, 0))

    @objc.typedSelector(b"v@:@")
    def clearLogsAction_(self, sender):
        clear_log()
        if hasattr(self, "_logs_text_view") and self._logs_text_view:
            attr_str = _build_log_attributed_string("Log cleared.")
            self._logs_text_view.textStorage().setAttributedString_(attr_str)


# ─────────────────────────────────────────────
# App Delegate
# ─────────────────────────────────────────────
class SoloMinerAppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, notification):
        self._config = load_config()
        self._engine = MiningEngine()
        self._event_monitor = None

        menubar = NSMenu.alloc().init()
        app_menu_item = NSMenuItem.alloc().init()
        menubar.addItem_(app_menu_item)
        app_menu = NSMenu.alloc().init()
        app_menu.addItemWithTitle_action_keyEquivalent_(
            "Quit SoloMiner", "terminate:", "q"
        )
        app_menu_item.setSubmenu_(app_menu)

        edit_menu_item = NSMenuItem.alloc().init()
        menubar.addItem_(edit_menu_item)
        edit_menu = NSMenu.alloc().initWithTitle_("Edit")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Undo", "undo:", "z")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Redo", "redo:", "Z")
        edit_menu.addItem_(NSMenuItem.separatorItem())
        edit_menu.addItemWithTitle_action_keyEquivalent_("Cut", "cut:", "x")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Copy", "copy:", "c")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Paste", "paste:", "v")
        edit_menu.addItemWithTitle_action_keyEquivalent_(
            "Select All", "selectAll:", "a"
        )
        edit_menu_item.setSubmenu_(edit_menu)

        NSApp.setMainMenu_(menubar)

        self._status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        button = self._status_item.button()
        button.setTitle_("SoloMiner")
        button.setAction_(objc.selector(self.togglePopover_, signature=b"v@:@"))
        button.setTarget_(self)

        self._popover = NSPopover.alloc().init()
        self._popover.setBehavior_(1)  # NSPopoverBehaviorTransient
        self._popover.setContentSize_(NSMakeSize(POPOVER_WIDTH, POPOVER_HEIGHT))
        self._popover.setAnimates_(True)

        vc = PopoverViewController.alloc().init()
        vc.setEngine_(self._engine)
        vc.setConfig_(self._config)
        self._popover.setContentViewController_(vc)
        self._vc = vc

        _ = vc.view()

        append_log("SoloMiner started")

    def _startEventMonitor(self):
        """Install a global event monitor to close the popover when
        clicking outside. NSPopoverBehaviorTransient doesn't work
        reliably for accessory-policy apps."""
        if self._event_monitor is not None:
            return
        try:
            from AppKit import (
                NSEvent,
                NSEventMaskLeftMouseDown,
                NSEventMaskRightMouseDown,
            )

            mask = NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown
            delegate_ref = self

            def _handler(event):
                if delegate_ref._popover.isShown():
                    # Check if the click is outside the popover
                    try:
                        window = event.window()
                        popover_window = (
                            delegate_ref._popover.contentViewController()
                            .view()
                            .window()
                        )
                        if window is None or window != popover_window:
                            delegate_ref._popover.close()
                    except Exception:
                        delegate_ref._popover.close()
                return event

            self._event_monitor = (
                NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, _handler)
            )
        except Exception:
            pass

    def _stopEventMonitor(self):
        if self._event_monitor is not None:
            try:
                from AppKit import NSEvent

                NSEvent.removeMonitor_(self._event_monitor)
            except Exception:
                pass
            self._event_monitor = None

    @objc.typedSelector(b"v@:@")
    def togglePopover_(self, sender):
        if self._popover.isShown():
            self._popover.close()
            self._stopEventMonitor()
        else:
            if self._vc._current_screen != "dashboard":
                self._vc._navigate_to("dashboard")
            button = self._status_item.button()
            self._popover.showRelativeToRect_ofView_preferredEdge_(
                button.bounds(),
                button,
                1,
            )
            self._startEventMonitor()

    def applicationWillTerminate_(self, notification):
        self._stopEventMonitor()
        if self._engine and self._engine.is_running:
            self._engine.stop()
        append_log("SoloMiner terminated")
