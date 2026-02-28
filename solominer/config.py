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


ALGORITHMS = ["SHA-256d", "Scrypt", "RandomX"]

APP_VERSION = "1.2.0"

# ── Coin registry ──
# Maps coin name -> { algorithm, ticker, address_hint }
# The algorithm is determined by the coin, not the other way around.
COIN_REGISTRY = {
    "Bitcoin": {"algorithm": "SHA-256d", "ticker": "BTC", "address_hint": "bc1q..."},
    "Litecoin": {"algorithm": "Scrypt", "ticker": "LTC", "address_hint": "ltc1q..."},
    "Dogecoin": {"algorithm": "Scrypt", "ticker": "DOGE", "address_hint": "D..."},
    "Monero": {"algorithm": "RandomX", "ticker": "XMR", "address_hint": "4..."},
}

COINS = list(COIN_REGISTRY.keys())  # ["Bitcoin", "Litecoin", "Dogecoin", "Monero"]


def coin_to_algorithm(coin: str) -> str:
    """Get the mining algorithm for a given coin."""
    return COIN_REGISTRY.get(coin, {}).get("algorithm", "SHA-256d")


def coin_to_ticker(coin: str) -> str:
    """Get the ticker symbol for a given coin."""
    return COIN_REGISTRY.get(coin, {}).get("ticker", "???")


def coin_address_hint(coin: str) -> str:
    """Get the address placeholder hint for a given coin."""
    return COIN_REGISTRY.get(coin, {}).get("address_hint", "...")


def algorithm_to_coins(algorithm: str) -> list:
    """Get all coins that use a given algorithm."""
    return [c for c, info in COIN_REGISTRY.items() if info["algorithm"] == algorithm]


@dataclass
class PoolConfig:
    name: str = "public-pool.io(3333)"
    host: str = "public-pool.io"
    port: int = 3333
    enabled: bool = True
    coin: str = "Bitcoin"  # Coin name (determines algorithm)

    @property
    def algorithm(self) -> str:
        return coin_to_algorithm(self.coin)


DEFAULT_POOLS = [
    PoolConfig("public-pool.io(3333)", "public-pool.io", 3333, True, "Bitcoin"),
    PoolConfig("VKBIT SOLO", "eu.vkbit.com", 3555, True, "Bitcoin"),
    PoolConfig("nerdminer.io", "pool.nerdminer.io", 3333, True, "Bitcoin"),
    PoolConfig("CKPool Solo (EU)", "eusolo.ckpool.org", 3333, True, "Bitcoin"),
    PoolConfig("CKPool Solo (US)", "solo.ckpool.org", 3333, False, "Bitcoin"),
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
    bitcoin_address: str = ""  # Legacy single address (kept for migration)
    coin: str = "Bitcoin"  # Selected cryptocurrency (determines algorithm)
    algorithm: str = "SHA-256d"  # Derived from coin (kept for backward compat)

    # Per-coin wallet addresses
    # Each cryptocurrency has its own payout address
    addresses: dict = field(
        default_factory=lambda: {
            "Bitcoin": "",
            "Litecoin": "",
            "Dogecoin": "",
            "Monero": "",
        }
    )

    # Performance
    performance_mode: str = "Full Speed"  # Auto, Full Speed, Eco Mode
    gpu_threads: int = 0  # 0 = auto (use max), 1-N = specific count
    cpu_threads: int = 0  # 0 = auto (use os.cpu_count()), 1-N = specific

    # Pools (each pool now has a coin field)
    pools: list = field(default_factory=lambda: [asdict(p) for p in DEFAULT_POOLS])

    # Active pool index
    active_pool_index: int = 0

    @property
    def active_algorithm(self) -> str:
        """Get the mining algorithm based on the selected coin."""
        return coin_to_algorithm(self.coin)

    def get_address_for_coin(self, coin: str) -> str:
        """Get the wallet address for a specific coin.
        Falls back to the legacy bitcoin_address if per-coin not set."""
        addr = self.addresses.get(coin, "")
        if not addr and coin == "Bitcoin" and self.bitcoin_address:
            return self.bitcoin_address
        return addr

    def set_address_for_coin(self, coin: str, address: str):
        """Set the wallet address for a specific coin."""
        self.addresses[coin] = address

    # Legacy compat shims (old code may call these)
    def get_address_for_algo(self, algo: str) -> str:
        """Legacy: get address by algorithm. Maps to first coin using that algo."""
        coins = algorithm_to_coins(algo)
        if coins:
            return self.get_address_for_coin(coins[0])
        return ""

    def set_address_for_algo(self, algo: str, address: str):
        """Legacy: set address by algorithm. Maps to first coin using that algo."""
        coins = algorithm_to_coins(algo)
        if coins:
            self.set_address_for_coin(coins[0], address)


def ensure_config_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


def load_config() -> MinerConfig:
    ensure_config_dir()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)

            # ── Migration v1.1 -> v1.2: per-algorithm -> per-coin addresses ──
            addrs = data.get("addresses", {})
            old_addr = data.get("bitcoin_address", "")

            # Detect old per-algorithm format (keys are algorithm names)
            is_old_format = any(k in addrs for k in ALGORITHMS)
            is_new_format = any(k in addrs for k in COINS)

            if is_old_format and not is_new_format:
                # Migrate: SHA-256d -> Bitcoin, Scrypt -> Litecoin, RandomX -> Monero
                new_addrs = {}
                new_addrs["Bitcoin"] = addrs.get("SHA-256d", old_addr or "")
                new_addrs["Litecoin"] = addrs.get("Scrypt", "")
                new_addrs["Dogecoin"] = ""  # New coin, no old address
                new_addrs["Monero"] = addrs.get("RandomX", "")
                data["addresses"] = new_addrs
            elif "addresses" not in data:
                data["addresses"] = {
                    "Bitcoin": old_addr,
                    "Litecoin": "",
                    "Dogecoin": "",
                    "Monero": "",
                }

            # Ensure all coins have entries
            addrs = data["addresses"]
            for coin_name in COINS:
                if coin_name not in addrs:
                    addrs[coin_name] = ""

            # ── Migration: algorithm field -> coin field ──
            if "coin" not in data:
                old_algo = data.get("algorithm", "SHA-256d")
                # Map old algorithm to first matching coin
                algo_to_coin = {
                    "SHA-256d": "Bitcoin",
                    "Scrypt": "Litecoin",
                    "RandomX": "Monero",
                }
                data["coin"] = algo_to_coin.get(old_algo, "Bitcoin")
            # Keep algorithm in sync with coin
            data["algorithm"] = coin_to_algorithm(data.get("coin", "Bitcoin"))

            # ── Migration: pool algorithm field -> coin field ──
            for pool in data.get("pools", []):
                if "coin" not in pool:
                    old_algo = pool.pop("algorithm", "SHA-256d")
                    algo_to_coin = {
                        "SHA-256d": "Bitcoin",
                        "Scrypt": "Litecoin",
                        "RandomX": "Monero",
                    }
                    pool["coin"] = algo_to_coin.get(old_algo, "Bitcoin")
                # Remove stale algorithm key from pool dicts (PoolConfig has it as property now)
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
