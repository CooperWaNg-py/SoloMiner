#!/usr/bin/env python3
"""
SoloMiner - Lightweight macOS Solo Bitcoin Miner
Menu bar application using Apple Metal for GPU-accelerated mining.

Usage:
    python3 main.py          # macOS menu bar GUI (default)
    python3 main.py --tui    # Terminal UI (curses)
"""

import sys
import os
import logging
import traceback

# Setup logging (suppress for TUI to avoid clobbering curses display)
_is_tui = "--tui" in sys.argv
logging.basicConfig(
    level=logging.INFO if not _is_tui else logging.WARNING,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
    if not _is_tui
    else [logging.NullHandler()],
)


def _crash_handler(exc_type, exc_value, exc_tb):
    """Global exception handler - writes crash log and exits cleanly."""
    # Don't intercept KeyboardInterrupt
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return

    try:
        from solominer.config import write_crash_log

        crash_path = write_crash_log(exc_type, exc_value, exc_tb)
        # Also print to stderr so it shows in console if open
        print(
            f"\n[SOLOMINER] Fatal crash. Log written to: {crash_path}",
            file=sys.stderr,
        )
        traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)
    except Exception:
        # Absolute last resort
        traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)

    # Force quit the app cleanly
    try:
        from AppKit import NSApp

        if NSApp is not None:
            NSApp.terminate_(None)
    except Exception:
        pass
    os._exit(1)


def _thread_crash_handler(args):
    """Handler for uncaught exceptions in threads (Python 3.8+).
    Logs the crash but does NOT kill the app - mining threads crashing
    should be recoverable. Only truly fatal errors (e.g. Metal GPU errors)
    will escalate."""
    # args: (exc_type, exc_value, exc_traceback, thread)
    exc_type = args.exc_type
    exc_value = args.exc_value
    exc_tb = args.exc_traceback
    thread = args.thread

    # Don't log SystemExit (normal thread termination)
    if issubclass(exc_type, SystemExit):
        return

    try:
        from solominer.config import write_crash_log, append_log

        crash_path = write_crash_log(exc_type, exc_value, exc_tb)
        thread_name = thread.name if thread else "unknown"
        append_log(
            f"[CRASH] Thread '{thread_name}' crashed: {exc_type.__name__}: "
            f"{exc_value} (logged to {crash_path})"
        )
        print(
            f"\n[SOLOMINER] Thread '{thread_name}' crashed. Log: {crash_path}",
            file=sys.stderr,
        )
    except Exception:
        traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)


# Install global exception hooks BEFORE anything else
sys.excepthook = _crash_handler
import threading

threading.excepthook = _thread_crash_handler


def main_gui():
    """Launch the macOS menu bar GUI."""
    from Foundation import NSObject
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    from solominer.ui import SoloMinerAppDelegate

    delegate = SoloMinerAppDelegate.alloc().init()
    app.setDelegate_(delegate)

    from AppKit import NSApp

    NSApp.run()


def main_tui():
    """Launch the curses-based Terminal UI."""
    from solominer.tui import run_tui

    run_tui()


if __name__ == "__main__":
    try:
        if "--tui" in sys.argv:
            main_tui()
        else:
            main_gui()
    except KeyboardInterrupt:
        print("\n[SOLOMINER] Interrupted by user.", file=sys.stderr)
        sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        _crash_handler(*sys.exc_info())
