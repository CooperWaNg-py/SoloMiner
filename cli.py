#!/usr/bin/env python3
"""
SoloMiner CLI - Terminal-based solo Bitcoin miner with live console output.

Usage:
    python3 cli.py --address bc1q... [--pool public-pool.io] [--port 3333]
    python3 cli.py --benchmark
"""

import argparse
import signal
import sys
import os
import time
import threading
import datetime

from solominer.config import load_config, append_log, clear_log, read_log, LOG_FILE
from solominer.engine import MiningEngine
from solominer.metal_miner import difficulty_to_target, DIFF1_TARGET


# ── ANSI colors ──
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"
WHITE = "\033[37m"


def ts():
    """Timestamp string."""
    return datetime.datetime.now().strftime("%H:%M:%S")


def format_hashrate(hr):
    if hr >= 1e9:
        return f"{hr / 1e9:.2f} GH/s"
    elif hr >= 1e6:
        return f"{hr / 1e6:.2f} MH/s"
    elif hr >= 1e3:
        return f"{hr / 1e3:.2f} KH/s"
    return f"{hr:.0f} H/s"


def format_uptime(secs):
    secs = int(secs)
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def format_target_bits(target_int):
    """Show how many leading zero bits the target requires."""
    if target_int <= 0:
        return "256"
    return str(256 - target_int.bit_length())


def log(msg, color=WHITE):
    """Print a timestamped log line."""
    print(f"{DIM}{ts()}{RESET} {color}{msg}{RESET}")


def run_benchmark():
    log("SoloMiner Benchmark", BOLD)
    print(f"{'=' * 50}")

    from solominer.metal_miner import MetalMiner

    miner = MetalMiner()
    log(f"GPU:   {miner.gpu_name}", CYAN)
    log(f"Metal: {'Yes' if miner.use_gpu else 'No (CPU fallback)'}", CYAN)
    print()

    header = b"\x00" * 80
    target = (1 << 256) - 1

    batch = 1 << 22 if miner.use_gpu else 1 << 18
    iterations = 10

    log(f"Running {iterations} batches of {batch:,} hashes...", DIM)
    start = time.time()
    for i in range(iterations):
        if miner.use_gpu:
            miner.mine_range_gpu(header, target, 0, batch)
        else:
            miner.mine_range_cpu(header, target, 0, batch)
        pct = (i + 1) / iterations * 100
        sys.stdout.write(f"\r  {CYAN}[{'#' * int(pct // 5):20s}]{RESET} {pct:.0f}%")
        sys.stdout.flush()
    elapsed = time.time() - start
    total = batch * iterations
    rate = total / elapsed

    print()
    print()
    log(f"Result: {BOLD}{GREEN}{format_hashrate(rate)}{RESET}", WHITE)
    log(f"Total:  {total:,} hashes in {elapsed:.2f}s", DIM)
    log(f"Best:   {miner.best_share_bits} leading zero bits", DIM)


def run_miner(args):
    config = load_config()

    address = args.address or config.bitcoin_address
    pool_cfg = config.pools[config.active_pool_index] if config.pools else {}
    pool = args.pool or pool_cfg.get("host", "public-pool.io")
    port = args.port or pool_cfg.get("port", 3333)
    worker = args.worker or config.worker_name
    network = args.network or config.network

    if not address:
        log("Error: No Bitcoin address. Use --address bc1q...", RED)
        sys.exit(1)

    print()
    print(f"  {BOLD}{CYAN}SoloMiner CLI{RESET}")
    print(f"  {'=' * 46}")
    print(f"  {DIM}Pool:{RESET}    {WHITE}{pool}:{port}{RESET}")
    print(
        f"  {DIM}Address:{RESET} {WHITE}{address[:20]}...{address[-8:]}{RESET}"
        if len(address) > 30
        else f"  {DIM}Address:{RESET} {WHITE}{address}{RESET}"
    )
    print(f"  {DIM}Worker:{RESET}  {WHITE}{worker}{RESET}")
    print(f"  {DIM}Network:{RESET} {WHITE}{network}{RESET}")
    print(f"  {'=' * 46}")
    print()

    engine = MiningEngine()

    # Handle Ctrl+C
    stop_event = threading.Event()

    def on_signal(sig, frame):
        print()
        log("Shutting down...", YELLOW)
        stop_event.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    # Watch the activity log file for new lines and print them
    last_log_pos = 0
    log_file = LOG_FILE

    def drain_logs():
        """Print any new log lines since last check."""
        nonlocal last_log_pos
        try:
            if os.path.exists(log_file):
                with open(log_file, "r") as f:
                    f.seek(last_log_pos)
                    new_lines = f.read()
                    last_log_pos = f.tell()
                if new_lines.strip():
                    for line in new_lines.strip().split("\n"):
                        # Color-code log lines
                        if "ERROR" in line:
                            color = RED
                        elif (
                            "ACCEPTED" in line
                            or "SUCCESSFUL" in line
                            or "Authorized" in line
                        ):
                            color = GREEN
                        elif "REJECTED" in line or "FAILED" in line:
                            color = RED
                        elif "SHARE FOUND" in line:
                            color = f"{BOLD}{GREEN}"
                        elif "[STRATUM]" in line:
                            color = BLUE
                        elif "[ENGINE]" in line:
                            color = CYAN
                        else:
                            color = DIM
                        print(f"  {color}{line}{RESET}")
        except Exception:
            pass

    # Mark current log position (skip old entries)
    if os.path.exists(log_file):
        last_log_pos = os.path.getsize(log_file)

    log("Starting miner...", YELLOW)
    engine.start(pool, port, address, worker, network)

    last_status = ""
    last_stats_line = ""
    try:
        while not stop_event.is_set():
            stop_event.wait(1.0)

            # Print new log lines
            drain_logs()

            status = engine.status
            hr = engine.hashrate
            uptime = engine.uptime_seconds
            accepted = engine.shares_accepted
            rejected = engine.shares_rejected
            submitted = engine.shares_submitted
            diff = engine.difficulty
            best_bits = engine.miner.best_share_bits if engine.miner else 0

            # Status change announcement
            if status != last_status:
                if status == "Mining":
                    log(f"Status: {GREEN}{BOLD}{status}{RESET}", WHITE)
                elif status in ("Auth Failed", "Disconnected"):
                    log(f"Status: {RED}{BOLD}{status}{RESET}", WHITE)
                else:
                    log(f"Status: {YELLOW}{status}{RESET}", WHITE)
                last_status = status

            # Stats line (overwrite in place)
            if status == "Mining" and hr > 0:
                share_target = difficulty_to_target(diff if diff > 0 else 1.0)
                target_bits = format_target_bits(share_target)

                stats = (
                    f"  {BOLD}{format_hashrate(hr):>12}{RESET}  "
                    f"{DIM}|{RESET} "
                    f"{GREEN}A:{accepted}{RESET} "
                    f"{RED}R:{rejected}{RESET} "
                    f"{DIM}S:{submitted}{RESET}  "
                    f"{DIM}|{RESET} "
                    f"Diff:{diff:.4f}  "
                    f"Best:{best_bits}/{target_bits} bits  "
                    f"{DIM}|{RESET} "
                    f"{format_uptime(uptime)}"
                )
                # Only reprint if changed
                if stats != last_stats_line:
                    sys.stdout.write(f"\r\033[K{stats}")
                    sys.stdout.flush()
                    last_stats_line = stats

    except KeyboardInterrupt:
        pass

    # Final newline after stats line
    print()
    print()

    engine.stop()
    drain_logs()

    print()
    print(f"  {BOLD}Session Summary{RESET}")
    print(f"  {'-' * 40}")
    print(f"  Peak hashrate: {GREEN}{format_hashrate(engine.peak_hashrate)}{RESET}")
    print(
        f"  Shares:        {GREEN}{engine.shares_accepted} accepted{RESET}, "
        f"{RED}{engine.shares_rejected} rejected{RESET}, "
        f"{DIM}{engine.shares_submitted} submitted{RESET}"
    )
    print(f"  Uptime:        {format_uptime(engine.uptime_seconds)}")
    if engine.miner:
        print(f"  Best share:    {engine.miner.best_share_bits} leading zero bits")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="SoloMiner CLI - Solo Bitcoin mining from your terminal"
    )
    parser.add_argument("--address", "-a", help="Bitcoin address for mining payout")
    parser.add_argument("--pool", "-p", help="Pool hostname (default: public-pool.io)")
    parser.add_argument("--port", type=int, help="Pool port (default: 3333)")
    parser.add_argument("--worker", "-w", help="Worker name (default: SoloMiner)")
    parser.add_argument(
        "--network",
        "-n",
        default=None,
        choices=["Mainnet", "Testnet3", "Testnet4", "Signet", "Regtest"],
    )
    parser.add_argument(
        "--benchmark", action="store_true", help="Run GPU benchmark only"
    )

    args = parser.parse_args()

    if args.benchmark:
        run_benchmark()
    else:
        run_miner(args)


if __name__ == "__main__":
    main()
