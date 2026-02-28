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

LAUNCHD_LABEL = "com.cooperwang.solominer"
LAUNCHD_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")


APP_VERSION = "1.3.0"

# Bitcoin is the only supported coin. Algorithm is always SHA-256d.
ALGORITHM = "SHA-256d"
COIN = "Bitcoin"
TICKER = "BTC"
ADDRESS_HINT = "bc1q..."
DONATION_ADDRESS = "16JXoJL46hAZSjtWrKYyoMcur1VtwWAbeB"


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


def install_login_item() -> tuple:
    """Install a launchd user agent so SoloMiner starts at login.
    Returns (success: bool, message: str).

    Detects the running context:
      - If running from a .app bundle, launches the bundle executable.
      - Otherwise, launches `python3 <main.py path>` from the source tree.
    """
    import plistlib
    import sys

    try:
        # Determine what to launch
        executable = sys.executable
        main_script = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "main.py")
        )

        # Check if we're inside a .app bundle
        # e.g. /Applications/SoloMiner.app/Contents/MacOS/SoloMiner
        if ".app/Contents/" in executable:
            program_args = [executable]
        elif os.path.exists(main_script):
            program_args = [executable, main_script]
        else:
            return (False, f"Cannot find main.py at {main_script}")

        plist = {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": program_args,
            "RunAtLoad": True,
            "KeepAlive": False,
            "StandardOutPath": os.path.join(CONFIG_DIR, "launchd_stdout.log"),
            "StandardErrorPath": os.path.join(CONFIG_DIR, "launchd_stderr.log"),
        }

        # Ensure LaunchAgents directory exists
        launch_dir = os.path.dirname(LAUNCHD_PLIST)
        os.makedirs(launch_dir, exist_ok=True)

        with open(LAUNCHD_PLIST, "wb") as f:
            plistlib.dump(plist, f)

        return (True, f"Login item installed: {LAUNCHD_PLIST}")
    except Exception as e:
        return (False, f"Failed to install login item: {e}")


def uninstall_login_item() -> tuple:
    """Remove the launchd user agent plist.
    Returns (success: bool, message: str)."""
    try:
        if os.path.exists(LAUNCHD_PLIST):
            os.remove(LAUNCHD_PLIST)
            return (True, "Login item removed")
        return (True, "Login item was not installed")
    except Exception as e:
        return (False, f"Failed to remove login item: {e}")


def is_login_item_installed() -> bool:
    """Check if the launchd login item plist exists."""
    return os.path.exists(LAUNCHD_PLIST)


def validate_bitcoin_address(address: str, network: str = "Mainnet") -> tuple:
    """Validate a Bitcoin address.
    Returns (is_valid: bool, error: str).

    Checks format and prefix for:
      - Legacy P2PKH (1...)
      - Legacy P2SH (3...)
      - Native SegWit bech32 (bc1q...)
      - Taproot bech32m (bc1p...)
      - Testnet equivalents (m/n/2/tb1q/tb1p)
    Does NOT do full checksum verification (no base58check/bech32 libs)."""
    if not address or not address.strip():
        return (False, "Address is empty")

    address = address.strip()

    # Determine expected prefixes based on network
    is_testnet = network.lower() in ("testnet3", "testnet4", "signet", "regtest")

    if is_testnet:
        # Testnet: 1-prefix P2PKH uses m or n, P2SH uses 2, bech32 uses tb1
        valid_legacy = address[0] in ("m", "n", "2")
        valid_bech32 = address.lower().startswith(("tb1q", "tb1p"))
        # Regtest uses bcrt1
        valid_regtest = address.lower().startswith("bcrt1")
        if not (valid_legacy or valid_bech32 or valid_regtest):
            return (False, "Not a valid testnet/regtest address prefix")
    else:
        # Mainnet
        valid_legacy = address[0] in ("1", "3")
        valid_bech32 = address.lower().startswith(("bc1q", "bc1p"))
        if not (valid_legacy or valid_bech32):
            return (False, "Must start with 1, 3, bc1q, or bc1p")

    # Length checks
    lower = address.lower()
    if lower.startswith(("bc1", "tb1", "bcrt1")):
        # Bech32/bech32m: bc1q is 42-62 chars, bc1p is 62 chars (taproot)
        if len(address) < 14 or len(address) > 90:
            return (False, f"Bech32 address length {len(address)} out of range")
        # Character set: bech32 uses only lowercase + digits (no 1boi after prefix)
        prefix_end = address.index("1") + 1  # find the separator '1'
        data_part = lower[prefix_end:]
        bech32_chars = set("qpzry9x8gf2tvdw0s3jn54khce6mua7l")
        invalid = set(data_part) - bech32_chars
        if invalid:
            return (False, f"Invalid bech32 character(s): {''.join(sorted(invalid))}")
    else:
        # Base58 legacy: 25-34 characters
        if len(address) < 25 or len(address) > 34:
            return (False, f"Legacy address length {len(address)} out of range (25-34)")
        # Base58 character set (no 0, O, I, l)
        base58_chars = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
        invalid = set(address) - base58_chars
        if invalid:
            return (False, f"Invalid base58 character(s): {''.join(sorted(invalid))}")

    return (True, "")


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
