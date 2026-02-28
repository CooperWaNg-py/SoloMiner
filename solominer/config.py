"""
Configuration management for SoloMiner.
Persists settings to ~/Library/Application Support/SoloMiner/config.json
"""

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional


CONFIG_DIR = os.path.expanduser("~/Library/Application Support/SoloMiner")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
LOG_FILE = os.path.join(CONFIG_DIR, "activity.log")
STATS_FILE = os.path.join(CONFIG_DIR, "stats.json")
CRASH_LOG_FILE = os.path.join(CONFIG_DIR, "crash.log")


APP_VERSION = "1.3.0"

# Bitcoin is the only supported coin. Algorithm is always SHA-256d.
ALGORITHM = "SHA-256d"
COIN = "Bitcoin"
TICKER = "BTC"
ADDRESS_HINT = "bc1q..."


@dataclass
class PoolConfig:
    name: str = "public-pool.io(3333)"
    host: str = "public-pool.io"
    port: int = 3333
    enabled: bool = True


DEFAULT_POOLS = [
    PoolConfig("public-pool.io(3333)", "public-pool.io", 3333, True),
    PoolConfig("VKBIT SOLO", "eu.vkbit.com", 3555, True),
    PoolConfig("nerdminer.io", "pool.nerdminer.io", 3333, True),
    PoolConfig("CKPool Solo (EU)", "eusolo.ckpool.org", 3333, True),
    PoolConfig("CKPool Solo (US)", "solo.ckpool.org", 3333, False),
]


@dataclass
class MinerConfig:
    # General
    start_at_login: bool = False
    restart_on_stall: bool = True
    stall_timeout_minutes: int = 10

    # Mining
    network: str = "Mainnet"  # Mainnet, Testnet3, Testnet4, Signet, Regtest
    worker_name: str = "SoloMiner"
    bitcoin_address: str = ""

    # Performance
    performance_mode: str = "Full Speed"  # Auto, Full Speed, Eco Mode
    gpu_threads: int = 0  # 0 = auto (use max), 1-N = specific count
    cpu_threads: int = 0  # 0 = auto (use os.cpu_count()), 1-N = specific

    # Pools
    pools: list = field(default_factory=lambda: [asdict(p) for p in DEFAULT_POOLS])

    # Active pool index
    active_pool_index: int = 0


def ensure_config_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


def load_config() -> MinerConfig:
    ensure_config_dir()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)

            # ── Migration from older multi-coin format ──
            # Pull Bitcoin address from old per-coin addresses dict if present
            if "addresses" in data:
                addrs = data.pop("addresses", {})
                if "Bitcoin" in addrs and addrs["Bitcoin"]:
                    data.setdefault("bitcoin_address", addrs["Bitcoin"])
                # Also check old per-algorithm format
                if "SHA-256d" in addrs and addrs["SHA-256d"]:
                    data.setdefault("bitcoin_address", addrs["SHA-256d"])

            # Remove stale fields from old multi-coin config
            for stale_key in ("coin", "algorithm", "addresses"):
                data.pop(stale_key, None)

            # Clean pool dicts: remove stale coin/algorithm keys
            for pool in data.get("pools", []):
                pool.pop("coin", None)
                pool.pop("algorithm", None)

            # Remove any keys not in MinerConfig fields to avoid __init__ errors
            valid_fields = {f.name for f in MinerConfig.__dataclass_fields__.values()}
            data = {k: v for k, v in data.items() if k in valid_fields}

            return MinerConfig(**data)
        except Exception:
            pass
    return MinerConfig()


def save_config(config: MinerConfig):
    ensure_config_dir()
    with open(CONFIG_FILE, "w") as f:
        json.dump(asdict(config), f, indent=2)


def load_stats() -> dict:
    ensure_config_dir()
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "total_hashes": 0,
        "total_runtime_seconds": 0,
        "shares_found": 0,
        "peak_hashrate": 0.0,
        "sessions": [],
        "blocks": [],
    }


def save_stats(stats: dict):
    ensure_config_dir()
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2)


def append_log(message: str):
    ensure_config_dir()
    import datetime

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{timestamp}] {message}\n")


def read_log() -> str:
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            return f.read()
    return ""


def clear_log():
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)


def ping_pool(host: str, port: int, timeout: float = 3.0) -> tuple:
    """TCP ping a pool to check if it's online.
    Returns (is_online: bool, latency_ms: float, error: str)."""
    import socket
    import time as _time

    try:
        ip = socket.gethostbyname(host)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        start = _time.time()
        sock.connect((ip, port))
        latency = (_time.time() - start) * 1000
        sock.close()
        return (True, round(latency, 1), "")
    except socket.gaierror:
        return (False, 0, "DNS failed")
    except socket.timeout:
        return (False, 0, "Timeout")
    except ConnectionRefusedError:
        return (False, 0, "Refused")
    except Exception as e:
        return (False, 0, str(e))


def write_crash_log(exc_type, exc_value, exc_tb):
    """Write a crash report to ~/.solominer/crash.log and return the path.
    Designed to be as safe as possible - no dependencies on the rest of the app."""
    import datetime
    import traceback
    import platform

    try:
        ensure_config_dir()
    except Exception:
        pass

    try:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
        tb_text = "".join(tb_lines)

        report = (
            f"{'=' * 72}\n"
            f"SOLOMINER CRASH REPORT\n"
            f"{'=' * 72}\n"
            f"Time:     {timestamp}\n"
            f"Version:  {APP_VERSION}\n"
            f"Python:   {platform.python_version()}\n"
            f"macOS:    {platform.mac_ver()[0]}\n"
            f"Arch:     {platform.machine()}\n"
            f"{'=' * 72}\n"
            f"\n{tb_text}\n"
        )

        with open(CRASH_LOG_FILE, "a") as f:
            f.write(report)

        return CRASH_LOG_FILE
    except Exception:
        # Last resort: try writing to /tmp
        try:
            fallback = "/tmp/solominer_crash.log"
            with open(fallback, "a") as f:
                f.write(f"[{datetime.datetime.now()}] {exc_type}: {exc_value}\n")
            return fallback
        except Exception:
            return None
