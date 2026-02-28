"""
Mining engine that orchestrates the stratum client and Metal GPU miner.
Manages the mining loop, job switching, hashrate tracking, and share submission.

Thread architecture:
    - Main thread: NSTimer drains ui_queue, updates AppKit views
    - stratum-recv thread: receives pool messages, fires callbacks
    - stratum-keepalive thread: sends pings, detects inactivity
    - mining-loop thread: dispatches GPU/CPU work, submits shares

All status updates flow through _set_status() which is lock-protected
and can be read safely by the main thread's NSTimer.
"""

import hashlib
import queue
import struct
import threading
import time
import logging
import random
from typing import Optional, Callable

from .stratum import StratumClient, StratumJob
from .metal_miner import (
    MetalMiner,
    create_miner,
    build_block_header,
    compute_merkle_root,
    difficulty_to_target,
)
from .config import (
    append_log,
    load_stats,
    save_stats,
    write_crash_log,
)

logger = logging.getLogger("solominer.engine")

# How many nonces per GPU dispatch
GPU_BATCH_SIZE = 1 << 22  # ~4M per batch
CPU_BATCH_SIZE = 1 << 16  # ~65K per batch for CPU fallback

# How often to persist stats to disk (seconds)
STATS_PERSIST_INTERVAL = 30

# Target share interval: we want ~1 share every N seconds
TARGET_SHARE_INTERVAL = 20  # seconds

# Initial difficulty suggestion (before we know hashrate).
# Conservative: at 100 MH/s this gives ~1 share per ~21 seconds.
INITIAL_SUGGEST_DIFFICULTY = 0.5

# How long to wait after auth before suggesting a measured difficulty
HASHRATE_MEASUREMENT_PERIOD = 15  # seconds


class MiningEngine:
    """
    High-level mining engine.

    Controls:
    - Connecting to pool via Stratum
    - Receiving jobs
    - Running GPU/CPU mining loop
    - Submitting shares
    - Tracking hashrate and statistics

    Thread safety:
    - All stratum callbacks run on the recv thread
    - Mining loop runs on its own thread
    - UI updates are queued and drained by the main thread NSTimer
    - Status is protected by _status_lock for safe cross-thread reads
    """

    def __init__(self):
        self.stratum: Optional[StratumClient] = None
        self.miner: Optional[MetalMiner] = None
        self.current_job: Optional[StratumJob] = None
        self._job_lock = threading.Lock()
        self._job_event = threading.Event()
        self._mining_threads: list = []
        self._running = False

        # Thread/core configuration
        self._gpu_threads = 0  # 0 = auto
        self._cpu_threads = 0  # 0 = auto

        # Stats (accessed from both mining thread and main thread via timer)
        self._stats_lock = threading.Lock()
        self.hashrate: float = 0.0
        self.shares_accepted: int = 0
        self.shares_rejected: int = 0
        self.shares_submitted: int = 0
        self.best_share_bits: int = 0
        self.jobs_received: int = 0
        self.uptime_start: Optional[float] = None
        self.difficulty: float = 0.0
        self._hashes_since_last: int = 0
        self._last_hashrate_time: float = 0
        self._peak_hashrate: float = 0.0
        self._last_stats_persist: float = 0

        # Thread-safe status string for UI
        self._status: str = "Idle"
        self._status_lock = threading.Lock()

        # Thread-safe UI update queue
        # The main thread NSTimer drains this queue to update AppKit views
        self.ui_queue: queue.Queue = queue.Queue()

        # Performance mode
        self._performance_mode = "Full Speed"
        self._eco_sleep = 0.05

        # Difficulty auto-tuning
        self._initial_diff_suggested = False
        self._hashrate_diff_suggested = False
        self._last_difficulty_suggest_time: float = 0

        # Reconnect support
        self._reconnect_enabled = True
        self._reconnect_params: Optional[tuple] = None

    @property
    def active_thread_count(self) -> int:
        return len([t for t in self._mining_threads if t.is_alive()])

    @property
    def gpu_threads_config(self) -> int:
        return self._gpu_threads

    @property
    def cpu_threads_config(self) -> int:
        return self._cpu_threads

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def uptime_seconds(self) -> float:
        if self.uptime_start:
            return time.time() - self.uptime_start
        return 0

    @property
    def peak_hashrate(self) -> float:
        return self._peak_hashrate

    @property
    def status(self) -> str:
        with self._status_lock:
            return self._status

    def _set_status(self, s: str):
        with self._status_lock:
            old = self._status
            self._status = s
        if old != s:
            append_log(f"[ENGINE] Status: {old} -> {s}")
            logger.info(f"Engine status: {old} -> {s}")

    def set_performance_mode(self, mode: str):
        self._performance_mode = mode
        append_log(f"[ENGINE] Performance mode: {mode}")

    def set_thread_config(self, gpu_threads: int = 0, cpu_threads: int = 0):
        """Set the number of GPU dispatch threads and CPU mining threads.
        0 = auto (let the system decide)."""
        self._gpu_threads = max(0, gpu_threads)
        self._cpu_threads = max(0, cpu_threads)
        append_log(
            f"[ENGINE] Thread config: GPU={self._gpu_threads or 'auto'}, "
            f"CPU={self._cpu_threads or 'auto'}"
        )

    def set_algorithm(self, algorithm: str):
        """Set the mining algorithm. Only SHA-256d is supported."""
        append_log(f"[ENGINE] Algorithm: SHA-256d")

    def set_coin(self, coin: str):
        """Set the mining coin. Only Bitcoin (SHA-256d) is supported."""
        append_log(f"[ENGINE] Coin: Bitcoin (algorithm: SHA-256d)")

    def start(
        self, host: str, port: int, address: str, worker: str, network: str = "Mainnet"
    ):
        """Start mining: connect to pool and begin hashing."""
        if self._running:
            append_log("[ENGINE] Already running, ignoring start")
            return

        self._running = True
        self._reconnect_enabled = True
        self.uptime_start = time.time()
        self.shares_accepted = 0
        self.shares_rejected = 0
        self.shares_submitted = 0
        self.best_share_bits = 0
        self.jobs_received = 0
        self._hashes_since_last = 0
        self._last_stats_persist = time.time()
        self._reconnect_params = (host, port, address, worker, network)
        self._initial_diff_suggested = False
        self._hashrate_diff_suggested = False

        self._set_status("Starting")
        append_log(f"[ENGINE] Starting miner -> {host}:{port} ({network})")
        append_log(f"[ENGINE] Worker: {address}.{worker}")

        # Initialize miner (SHA-256d only)
        self.miner = create_miner()
        gpu_info = (
            f"Algorithm: SHA-256d, "
            f"GPU: {self.miner.gpu_name}, "
            f"Metal: {'Yes' if self.miner.use_gpu else 'No (CPU)'}"
        )
        append_log(f"[ENGINE] {gpu_info}")

        # Connect stratum
        self._set_status("Connecting")
        self._connect_stratum(host, port, address, worker)

    def _connect_stratum(self, host, port, address, worker):
        """Create and connect a stratum client with all callbacks wired."""
        self.stratum = StratumClient(host, port, address, worker)
        self.stratum.on_job = self._on_job
        self.stratum.on_authorized = self._on_authorized
        self.stratum.on_difficulty = self._on_difficulty
        self.stratum.on_share_result = self._on_share_result
        self.stratum.on_disconnect = self._on_disconnect
        self.stratum.on_error = self._on_error
        # Wire up stratum status changes to engine status
        self.stratum.on_status_change = self._on_stratum_status
        self.stratum.connect()

    def stop(self):
        """Stop mining."""
        if not self._running:
            return

        self._running = False
        self._reconnect_enabled = False
        self._set_status("Stopping")

        if self.stratum:
            self.stratum.disconnect()
            self.stratum = None

        self._job_event.set()  # Wake up mining threads so they exit

        for t in self._mining_threads:
            if t.is_alive():
                t.join(timeout=5)
        self._mining_threads = []

        self._save_session_stats()

        self.current_job = None
        self.hashrate = 0
        self.uptime_start = None
        self._set_status("Idle")

    def _save_session_stats(self):
        try:
            stats = load_stats()
            runtime = self.uptime_seconds
            stats["total_runtime_seconds"] += runtime
            stats["shares_found"] += self.shares_accepted
            if self._peak_hashrate > stats.get("peak_hashrate", 0):
                stats["peak_hashrate"] = self._peak_hashrate

            session = {
                "start_time": time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(self.uptime_start or time.time()),
                ),
                "runtime_seconds": runtime,
                "shares": self.shares_accepted,
                "peak_hashrate": self._peak_hashrate,
            }
            stats.setdefault("sessions", []).append(session)
            save_stats(stats)
            append_log(
                f"[ENGINE] Session saved: {runtime:.0f}s, "
                f"{self.shares_accepted} shares, "
                f"peak {self._peak_hashrate / 1e6:.2f} MH/s"
            )
        except Exception as e:
            logger.error(f"Failed to save stats: {e}")
            append_log(f"[ENGINE ERROR] Failed to save stats: {e}")

    # ── Stratum callbacks (run on background recv thread) ──

    def _on_stratum_status(self, status: str):
        """
        Called from stratum client when its internal status changes.
        This propagates stratum protocol states to the engine.
        Maps stratum states to engine states.
        """
        # Don't overwrite "Mining" status with earlier stratum states
        current = self.status
        if current == "Mining" and status in (
            "Subscribed",
            "Authorizing",
            "Authorized",
        ):
            return
        self._set_status(status)

    def _on_job(self, job: StratumJob):
        old_job_id = None
        with self._job_lock:
            if self.current_job:
                old_job_id = self.current_job.job_id
            self.current_job = job
        self._job_event.set()  # Wake mining thread

        with self._stats_lock:
            self.jobs_received += 1
            job_num = self.jobs_received

        if old_job_id:
            append_log(
                f"[ENGINE] Job #{job_num}: {old_job_id} -> {job.job_id} "
                f"(clean={job.clean_jobs})"
            )
        else:
            append_log(f"[ENGINE] First job: {job.job_id}")

        # Transition to Mining status on first job
        self._set_status("Mining")

        # Start mining threads if not running
        alive = [t for t in self._mining_threads if t.is_alive()]
        if not alive:
            import os

            if self.miner and self.miner.use_gpu:
                # GPU mining (SHA-256d only): launch N dispatch threads
                # Metal handles parallelism internally, so 1 is usually fine
                n_threads = self._gpu_threads if self._gpu_threads > 0 else 1
            else:
                # CPU mining (SHA-256d fallback, Scrypt, RandomX)
                n_threads = (
                    self._cpu_threads
                    if self._cpu_threads > 0
                    else max(1, (os.cpu_count() or 4) - 1)
                )
            self._mining_threads = []
            for i in range(n_threads):
                t = threading.Thread(
                    target=self._mining_loop,
                    args=(i,),
                    daemon=True,
                    name=f"mining-loop-{i}",
                )
                t.start()
                self._mining_threads.append(t)
            append_log(f"[ENGINE] Started {n_threads} mining thread(s)")

    def _on_authorized(self, success: bool):
        if success:
            append_log("[ENGINE] Pool authorized - waiting for first job")
            # Only update status if we haven't already started mining
            # (pools may send jobs before auth response)
            if self.status != "Mining":
                self._set_status("Authorized")

            # Suggest a safe initial difficulty immediately.
            # This replaces the old hardcoded 0.0001 that caused share flooding.
            # After measuring actual hashrate (~15s), we'll refine this.
            if not self._initial_diff_suggested and self.stratum:
                self.stratum.suggest_difficulty(INITIAL_SUGGEST_DIFFICULTY)
                self._initial_diff_suggested = True
                self._last_difficulty_suggest_time = time.time()
                append_log(
                    f"[ENGINE] Suggested initial difficulty: "
                    f"{INITIAL_SUGGEST_DIFFICULTY}"
                )
        else:
            self._set_status("Auth Failed")
            append_log("[ENGINE ERROR] Pool authorization FAILED")

    def _on_difficulty(self, diff: float):
        with self._stats_lock:
            old = self.difficulty
            self.difficulty = diff
        if old != diff:
            append_log(f"[ENGINE] Pool difficulty: {old} -> {diff}")

    def _on_share_result(self, accepted: bool, error_msg: Optional[str]):
        with self._stats_lock:
            if accepted:
                self.shares_accepted += 1
                append_log(
                    f"[ENGINE] Share ACCEPTED "
                    f"({self.shares_accepted}/{self.shares_submitted})"
                )
            else:
                self.shares_rejected += 1
                append_log(
                    f"[ENGINE] Share REJECTED: {error_msg} "
                    f"({self.shares_rejected} rejected)"
                )

    def _on_disconnect(self):
        append_log("[ENGINE] Disconnected from pool")

        # Auto-reconnect
        if self._running and self._reconnect_enabled and self._reconnect_params:
            delay = 5 + random.uniform(0, 5)  # Jitter to avoid thundering herd
            append_log(f"[ENGINE] Reconnecting in {delay:.1f}s...")
            self._set_status("Reconnecting")
            threading.Timer(delay, self._reconnect).start()
        else:
            self._set_status("Disconnected")

    def _reconnect(self):
        if not self._running or not self._reconnect_params:
            return
        host, port, address, worker, network = self._reconnect_params
        append_log(f"[ENGINE] Reconnecting to {host}:{port}...")
        self._set_status("Reconnecting")

        # Reset current job so mining thread waits
        with self._job_lock:
            self.current_job = None

        if self.stratum:
            try:
                self.stratum.disconnect()
            except Exception:
                pass

        self._connect_stratum(host, port, address, worker)

    def _on_error(self, msg: str):
        append_log(f"[ENGINE ERROR] {msg}")

    # ── Mining loop (runs on dedicated background thread) ──

    def _mining_loop(self, thread_idx=0):
        """Main mining loop running on a background thread.
        thread_idx partitions the nonce space when multiple threads run."""
        nonce_offset = 0
        self._last_hashrate_time = time.time()
        current_job_id = None

        append_log(f"[ENGINE] Mining loop {thread_idx} started")

        # Wait briefly for the pool's share difficulty to stabilize.
        # After auth, the engine suggests difficulty INITIAL_SUGGEST_DIFFICULTY
        # and the pool responds with mining.set_difficulty. Give it time to
        # arrive before we start mining against the wrong target.
        time.sleep(2.0)
        with self._stats_lock:
            d = self.difficulty
        if d <= 0:
            # Still no difficulty from pool; wait a bit more
            for _ in range(10):
                time.sleep(0.3)
                with self._stats_lock:
                    d = self.difficulty
                if d > 0:
                    break
        append_log(f"[ENGINE] Mining with pool difficulty: {d}")

        while self._running:
            # Wait for a job
            with self._job_lock:
                job = self.current_job
            if job is None:
                self._job_event.wait(timeout=1.0)
                self._job_event.clear()
                continue

            # Detect job switch -> reset nonce
            if job.job_id != current_job_id:
                current_job_id = job.job_id
                # Partition nonce space across threads
                n_threads = max(1, len(self._mining_threads))
                partition_size = 0xFFFFFFFF // n_threads
                nonce_offset = (
                    random.randint(0, partition_size) + thread_idx * partition_size
                ) & 0xFFFFFFFF
                append_log(
                    f"[ENGINE] Mining job {job.job_id} thread={thread_idx}, "
                    f"nonce_start=0x{nonce_offset:08x}"
                )

            try:
                # Generate extranonce2
                extranonce2 = format(
                    random.getrandbits(job.extranonce2_size * 8),
                    f"0{job.extranonce2_size * 2}x",
                )

                # Compute merkle root
                merkle_root = compute_merkle_root(
                    job.coinb1,
                    job.coinb2,
                    job.extranonce1,
                    extranonce2,
                    job.merkle_branch,
                )

                # Build block header (nonce=0, GPU/CPU will iterate)
                header = build_block_header(
                    job.version, job.prevhash, merkle_root, job.ntime, job.nbits, 0
                )

                # Use SHARE target (from pool difficulty), NOT block target!
                # Pool difficulty 1 = DIFF1_TARGET, much easier than block target
                with self._stats_lock:
                    share_difficulty = self.difficulty
                if share_difficulty <= 0:
                    share_difficulty = 1.0
                share_target = difficulty_to_target(share_difficulty)

                batch_size = (
                    GPU_BATCH_SIZE
                    if self.miner and self.miner.use_gpu
                    else CPU_BATCH_SIZE
                )

                # Mine batch against share target
                if self.miner and self.miner.use_gpu:
                    result = self.miner.mine_range_gpu(
                        header, share_target, nonce_offset, batch_size
                    )
                else:
                    result = self.miner.mine_range_cpu(
                        header, share_target, nonce_offset, batch_size
                    )

                # Update hashrate (every second)
                now = time.time()
                elapsed = now - self._last_hashrate_time
                if elapsed >= 1.0:
                    hashes = (
                        self.miner.get_and_reset_hashcount()
                        if self.miner
                        else batch_size
                    )
                    with self._stats_lock:
                        self.hashrate = hashes / elapsed
                        self._hashes_since_last += hashes
                        if self.hashrate > self._peak_hashrate:
                            self._peak_hashrate = self.hashrate
                    self._last_hashrate_time = now

                    # Auto-tune difficulty based on measured hashrate.
                    # After HASHRATE_MEASUREMENT_PERIOD seconds of mining,
                    # compute optimal difficulty for ~1 share per
                    # TARGET_SHARE_INTERVAL seconds, then suggest it to the
                    # pool. This replaces the old 0.0001 that caused flooding.
                    if (
                        not self._hashrate_diff_suggested
                        and self._last_difficulty_suggest_time > 0
                        and (now - self._last_difficulty_suggest_time)
                        > HASHRATE_MEASUREMENT_PERIOD
                    ):
                        with self._stats_lock:
                            measured_hr = self.hashrate
                        if measured_hr > 0:
                            # diff = hashrate * interval / 2^32
                            optimal_diff = measured_hr * TARGET_SHARE_INTERVAL / (2**32)
                            # Clamp to reasonable range
                            optimal_diff = max(0.001, min(optimal_diff, 1000000))
                            # Round to 4 significant figures for readability
                            optimal_diff = float(f"{optimal_diff:.4g}")
                            append_log(
                                f"[ENGINE] Measured hashrate: "
                                f"{measured_hr / 1e6:.1f} MH/s -> "
                                f"optimal difficulty: {optimal_diff} "
                                f"(target: ~1 share per {TARGET_SHARE_INTERVAL}s)"
                            )
                            if self.stratum and self.stratum.connected:
                                self.stratum.suggest_difficulty(optimal_diff)
                            self._hashrate_diff_suggested = True

                    # Persist cumulative stats periodically (not every second)
                    if now - self._last_stats_persist > STATS_PERSIST_INTERVAL:
                        try:
                            with self._stats_lock:
                                h = self._hashes_since_last
                                self._hashes_since_last = 0
                            stats = load_stats()
                            stats["total_hashes"] += h
                            save_stats(stats)
                            self._last_stats_persist = now
                        except Exception:
                            pass

                # Handle found share
                if result is not None:
                    # The GPU loads the header as big-endian uint32 words and
                    # replaces the nonce word (index 19) with its candidate.
                    # The original nonce in the header is little-endian, so the
                    # GPU value is byte-swapped relative to the actual nonce.
                    # Stratum expects: format(actual_nonce, "08x")
                    # So we byte-swap the GPU result to get the real nonce value.
                    if self.miner and self.miner.use_gpu:
                        actual_nonce = struct.unpack("<I", struct.pack(">I", result))[0]
                        nonce_hex = format(actual_nonce, "08x")
                    else:
                        actual_nonce = result
                        nonce_hex = format(result, "08x")

                    with self._stats_lock:
                        self.shares_submitted += 1

                    append_log(
                        f"[ENGINE] *** SHARE FOUND *** "
                        f"nonce=0x{result:08x} hex={nonce_hex} "
                        f"job={job.job_id}"
                    )
                    if self.stratum:
                        self.stratum.submit_share(
                            job.job_id, extranonce2, job.ntime, nonce_hex
                        )

                nonce_offset = (nonce_offset + batch_size) & 0xFFFFFFFF

                # Check for stale job
                with self._job_lock:
                    if self.current_job and self.current_job.job_id != current_job_id:
                        continue  # New job arrived, loop back

                # Eco mode throttle
                if self._performance_mode == "Eco Mode":
                    time.sleep(self._eco_sleep)

            except RuntimeError as e:
                # Metal/GPU errors (buffer allocation failures, command buffer errors)
                # Log to crash file and back off longer
                import sys

                write_crash_log(type(e), e, e.__traceback__)
                logger.error(f"GPU error in mining loop: {e}", exc_info=True)
                append_log(f"[ENGINE ERROR] GPU error: {e} (logged to crash.log)")
                # Back off to let GPU recover (memory pressure, etc.)
                time.sleep(5)

            except Exception as e:
                logger.error(f"Mining loop error: {e}", exc_info=True)
                append_log(f"[ENGINE ERROR] Mining loop: {e}")
                time.sleep(1)

        append_log(f"[ENGINE] Mining loop {thread_idx} stopped")
