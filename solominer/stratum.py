"""
Stratum v1 protocol client for Bitcoin mining.
Handles connection to mining pools, job reception, and share submission.

Protocol flow:
    1. TCP connect
    2. mining.subscribe  -> receive extranonce1, extranonce2_size
    3. mining.authorize  -> receive true/false
    4. mining.suggest_difficulty -> (optional, request low diff for faster shares)
    5. Pool sends mining.set_difficulty + mining.notify (may arrive before auth response)
    6. Client mines and calls mining.submit for shares

Message routing:
    - Messages WITH "method" field = server-initiated notifications
    - Messages WITHOUT "method" field = responses to our requests (matched by "id")

Based on analysis of NerdMiner_v2 (GPL), Bitcoin Wiki stratum spec, and Braiins docs.
"""

import json
import socket
import struct
import threading
import time
import logging
from typing import Optional, Callable

from .config import append_log, APP_VERSION

logger = logging.getLogger("solominer.stratum")


class StratumJob:
    """Represents a mining job received from the pool."""

    __slots__ = (
        "job_id",
        "prevhash",
        "coinb1",
        "coinb2",
        "merkle_branch",
        "version",
        "nbits",
        "ntime",
        "clean_jobs",
        "extranonce1",
        "extranonce2_size",
        "target",
    )

    def __init__(self, params, extranonce1, extranonce2_size):
        if not params or len(params) < 8:
            raise ValueError(
                f"mining.notify requires at least 8 params, got {len(params) if params else 0}"
            )
        self.job_id = params[0]
        self.prevhash = params[1]
        self.coinb1 = params[2]
        self.coinb2 = params[3]
        self.merkle_branch = params[4]
        self.version = params[5]
        self.nbits = params[6]
        self.ntime = params[7]
        self.clean_jobs = params[8] if len(params) > 8 else False
        self.extranonce1 = extranonce1
        self.extranonce2_size = extranonce2_size
        self.target = self._nbits_to_target(self.nbits)

    @staticmethod
    def _nbits_to_target(nbits_hex: str) -> int:
        nbits = int(nbits_hex, 16)
        exponent = nbits >> 24
        mantissa = nbits & 0x007FFFFF
        if exponent <= 3:
            target = mantissa >> (8 * (3 - exponent))
        else:
            target = mantissa << (8 * (exponent - 3))
        return target


class StratumClient:
    """
    Stratum v1 protocol client with robust message handling.

    Usage:
        client = StratumClient(host, port, address, worker)
        client.on_job = my_job_callback
        client.on_authorized = my_auth_callback
        client.connect()
        ...
        client.submit_share(job_id, extranonce2, ntime, nonce)
        client.disconnect()
    """

    # Keepalive: send ping if no data sent for this many seconds
    KEEPALIVE_INTERVAL = 60
    # Inactivity: reconnect if no data received for this many seconds
    INACTIVITY_TIMEOUT = 120

    def __init__(self, host: str, port: int, address: str, worker: str):
        self.host = host
        self.port = port
        self.address = address
        self.worker = worker

        self._socket: Optional[socket.socket] = None
        self._recv_thread: Optional[threading.Thread] = None
        self._keepalive_thread: Optional[threading.Thread] = None
        self._running = False
        self._msg_id = 0
        self._lock = threading.Lock()
        self._disconnected = (
            False  # ensures _handle_disconnect fires once per connection
        )

        # Message ID tracking: maps msg_id -> purpose string
        self._pending_requests: dict = {}

        # Pool-assigned values
        self.extranonce1: Optional[str] = None
        self.extranonce2_size: int = 4
        self.difficulty: float = 1.0

        # Timestamps for keepalive/inactivity
        self._last_send_time: float = 0
        self._last_recv_time: float = 0

        # Track jobs received before authorization (common with pools)
        self._jobs_before_auth = 0

        # Callbacks
        self.on_job: Optional[Callable[[StratumJob], None]] = None
        self.on_authorized: Optional[Callable[[bool], None]] = None
        self.on_difficulty: Optional[Callable[[float], None]] = None
        self.on_share_result: Optional[Callable[[bool, Optional[str]], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None
        self.on_log: Optional[Callable[[str], None]] = None
        self.on_status_change: Optional[Callable[[str], None]] = None

        self.authorized = False
        self.connected = False

    def _log(self, msg: str):
        """Log to both Python logger and the on_log callback / activity log."""
        logger.info(msg)
        append_log(f"[STRATUM] {msg}")
        if self.on_log:
            self.on_log(msg)

    def _log_debug(self, msg: str):
        """Debug level log - protocol details."""
        logger.debug(msg)
        append_log(f"[STRATUM DEBUG] {msg}")

    def _log_error(self, msg: str):
        logger.error(msg)
        append_log(f"[STRATUM ERROR] {msg}")
        if self.on_log:
            self.on_log(f"ERROR: {msg}")

    def _set_status(self, status: str):
        """Push status change to engine via callback."""
        self._log(f"Status -> {status}")
        if self.on_status_change:
            self.on_status_change(status)

    def _next_id(self) -> int:
        with self._lock:
            self._msg_id += 1
            return self._msg_id

    def _reset_state(self):
        """Reset all connection state for a fresh start.
        Closes the existing socket if one is open."""
        # Close existing socket to prevent leaks
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
        # Signal old threads to stop
        self._running = False
        self._disconnected = False
        self.extranonce1 = None
        self.extranonce2_size = 4
        self.difficulty = 1.0
        self.authorized = False
        self.connected = False
        self._pending_requests.clear()
        self._msg_id = 0
        self._jobs_before_auth = 0

    def connect(self):
        """Connect to the stratum server and start receiving."""
        self._reset_state()

        try:
            self._log(f"Connecting to {self.host}:{self.port}...")
            self._set_status("Connecting")

            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.settimeout(30)

            # DNS resolution (log it for debugging)
            self._log(f"Resolving DNS for {self.host}...")
            try:
                ip = socket.gethostbyname(self.host)
                self._log(f"Resolved {self.host} -> {ip}")
            except socket.gaierror as e:
                self._log_error(f"DNS resolution failed for {self.host}: {e}")
                self._set_status("DNS Failed")
                self._close_socket()
                if self.on_error:
                    self.on_error(f"DNS resolution failed: {e}")
                return

            self._socket.connect((self.host, self.port))
            self._running = True
            self.connected = True
            self._last_recv_time = time.time()
            self._last_send_time = time.time()

            self._log(f"TCP connected to {self.host}:{self.port}")
            self._set_status("Connected")

            # Start receiver thread
            self._recv_thread = threading.Thread(
                target=self._recv_loop, daemon=True, name="stratum-recv"
            )
            self._recv_thread.start()

            # Start keepalive thread
            self._keepalive_thread = threading.Thread(
                target=self._keepalive_loop, daemon=True, name="stratum-keepalive"
            )
            self._keepalive_thread.start()

            # Step 1: Subscribe
            self._set_status("Subscribing")
            sub_id = self._next_id()
            self._pending_requests[sub_id] = "subscribe"
            self._send(
                {
                    "id": sub_id,
                    "method": "mining.subscribe",
                    "params": [f"SoloMiner/{APP_VERSION}"],
                }
            )
            self._log(f">> mining.subscribe (id={sub_id})")

        except socket.timeout:
            self._log_error(f"Connection timed out to {self.host}:{self.port}")
            self._set_status("Timeout")
            self._close_socket()
            self.connected = False
            if self.on_error:
                self.on_error("Connection timed out")
        except ConnectionRefusedError:
            self._log_error(f"Connection refused by {self.host}:{self.port}")
            self._set_status("Refused")
            self._close_socket()
            self.connected = False
            if self.on_error:
                self.on_error("Connection refused")
        except Exception as e:
            self._log_error(f"Connection failed: {e}")
            self._set_status("Error")
            self._close_socket()
            self.connected = False
            if self.on_error:
                self.on_error(f"Connection failed: {e}")

    def disconnect(self):
        """Disconnect from the stratum server."""
        self._log("Disconnecting...")
        self._running = False
        self.connected = False
        self.authorized = False
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
        self._set_status("Disconnected")
        self._log("Disconnected")

    def submit_share(self, job_id: str, extranonce2: str, ntime: str, nonce: str):
        """Submit a share to the pool."""
        worker_str = f"{self.address}.{self.worker}" if self.address else self.worker
        submit_id = self._next_id()
        self._pending_requests[submit_id] = "submit"
        self._send(
            {
                "id": submit_id,
                "method": "mining.submit",
                "params": [worker_str, job_id, extranonce2, ntime, nonce],
            }
        )
        self._log(
            f">> mining.submit (id={submit_id}): "
            f"job={job_id}, en2={extranonce2}, nonce={nonce}"
        )

    def suggest_difficulty(self, diff: float):
        """Ask the pool to set our share difficulty.

        Call this after measuring hashrate to target a reasonable share rate
        (e.g. ~1 share per 15-30 seconds). Do NOT call with tiny values like
        0.0001 -- that floods the pool and gets shares rejected.
        """
        diff_id = self._next_id()
        self._pending_requests[diff_id] = "suggest_difficulty"
        self._send(
            {
                "id": diff_id,
                "method": "mining.suggest_difficulty",
                "params": [diff],
            }
        )
        self._log(f">> mining.suggest_difficulty({diff}) (id={diff_id})")

    def _send(self, msg: dict):
        if not self._socket:
            self._log_error("Cannot send: socket is None")
            return
        try:
            data = json.dumps(msg) + "\n"
            self._socket.sendall(data.encode())
            self._last_send_time = time.time()
            self._log_debug(f"SEND: {data.strip()}")
        except Exception as e:
            self._log_error(f"Send error: {e}")
            self._handle_disconnect()

    def _recv_loop(self):
        buf = ""
        self._log("Receiver thread started")
        while self._running:
            try:
                data = self._socket.recv(4096)
                if not data:
                    self._log_error("Connection closed by pool (empty recv)")
                    self._handle_disconnect()
                    return
                self._last_recv_time = time.time()
                try:
                    buf += data.decode("utf-8", errors="replace")
                except Exception:
                    buf += data.decode("latin-1")

                # Guard against unbounded buffer growth
                if len(buf) > 1_000_000:
                    self._log_error("Receive buffer overflow (>1MB without newline)")
                    self._handle_disconnect()
                    return

                # Process all complete lines (multiple JSON messages per recv)
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        self._log_debug(f"RECV: {line[:500]}")
                        try:
                            msg = json.loads(line)
                            self._handle_message(msg)
                        except json.JSONDecodeError as e:
                            self._log_error(
                                f"JSON parse error: {e} | line: {line[:200]}"
                            )

            except socket.timeout:
                continue
            except OSError as e:
                # Socket was closed during disconnect
                if self._running:
                    self._log_error(f"Socket error: {e}")
                    self._handle_disconnect()
                return
            except Exception as e:
                if self._running:
                    self._log_error(f"Recv error: {e}")
                    self._handle_disconnect()
                return
        self._log("Receiver thread stopped")

    def _keepalive_loop(self):
        """Send keepalive pings and detect pool inactivity."""
        self._log("Keepalive thread started")
        while self._running:
            time.sleep(5)  # Check every 5 seconds

            now = time.time()

            # Keepalive: send a ping if idle too long
            # NOTE: We use mining.suggest_difficulty as keepalive (harmless).
            # DO NOT use mining.subscribe here - it would corrupt extranonce1!
            if now - self._last_send_time > self.KEEPALIVE_INTERVAL:
                self._log("Sending keepalive (suggest_difficulty)...")
                ping_id = self._next_id()
                self._pending_requests[ping_id] = "keepalive"
                self._send(
                    {
                        "id": ping_id,
                        "method": "mining.suggest_difficulty",
                        "params": [self.difficulty],
                    }
                )

            # Inactivity: detect dead connection
            if now - self._last_recv_time > self.INACTIVITY_TIMEOUT:
                self._log_error(
                    f"Pool inactivity timeout ({self.INACTIVITY_TIMEOUT}s with no data)"
                )
                self._handle_disconnect()
                return

    def _close_socket(self):
        """Close the socket safely. Can be called from any thread."""
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None

    def _handle_disconnect(self):
        """Handle connection loss. Guaranteed to fire on_disconnect only once
        per connection via _disconnected flag."""
        with self._lock:
            if self._disconnected:
                return  # Already handled for this connection
            self._disconnected = True
            was_connected = self.connected
            self._running = False
            self.connected = False
            self.authorized = False
        if was_connected:
            self._log("Connection lost")
            self._set_status("Disconnected")
        if self.on_disconnect:
            self.on_disconnect()

    def _handle_message(self, msg: dict):
        # Server-initiated notification: has "method" field
        if "method" in msg:
            self._handle_server_method(msg)
        # Response to our request: has "id" and "result" (or "error")
        elif "id" in msg:
            self._handle_response(msg)
        else:
            self._log_error(f"Unknown message format: {json.dumps(msg)[:200]}")

    def _handle_server_method(self, msg: dict):
        method = msg.get("method", "")
        params = msg.get("params", [])

        if method == "mining.notify":
            job_id = params[0] if params else "?"
            clean = params[8] if len(params) > 8 else False
            branch_count = len(params[4]) if len(params) > 4 else 0
            self._log(
                f"<< mining.notify: job={job_id}, "
                f"clean={clean}, branches={branch_count}"
            )

            if self.extranonce1 is not None:
                try:
                    job = StratumJob(params, self.extranonce1, self.extranonce2_size)
                    if not self.authorized:
                        self._jobs_before_auth += 1
                        self._log(
                            f"   Job received before auth "
                            f"(#{self._jobs_before_auth}) - processing anyway"
                        )
                    if self.on_job:
                        self.on_job(job)
                except Exception as e:
                    self._log_error(f"Failed to parse job: {e}")
            else:
                self._log_error(
                    "Received mining.notify but extranonce1 not set yet - "
                    "subscribe may have failed"
                )

        elif method == "mining.set_difficulty":
            if params:
                old_diff = self.difficulty
                self.difficulty = float(params[0])
                self._log(f"<< mining.set_difficulty: {old_diff} -> {self.difficulty}")
                if self.on_difficulty:
                    self.on_difficulty(self.difficulty)

        elif method == "mining.set_extranonce":
            if len(params) >= 2:
                old_en1 = self.extranonce1
                self.extranonce1 = params[0]
                self.extranonce2_size = params[1]
                self._log(
                    f"<< mining.set_extranonce: "
                    f"en1={old_en1}->{self.extranonce1}, "
                    f"en2_size={self.extranonce2_size}"
                )

        elif method == "client.get_version":
            # Pool asking for our version - respond
            msg_id = msg.get("id")
            if msg_id is not None:
                self._send(
                    {"id": msg_id, "result": f"SoloMiner/{APP_VERSION}", "error": None}
                )
                self._log("<< client.get_version -> responded")

        elif method == "client.show_message":
            human_msg = params[0] if params else ""
            self._log(f"<< Pool message: {human_msg}")

        elif method == "client.reconnect":
            host = params[0] if len(params) > 0 else None
            port = params[1] if len(params) > 1 else None
            wait = params[2] if len(params) > 2 else 0
            self._log(f"<< client.reconnect: host={host}, port={port}, wait={wait}")
            # We don't auto-reconnect to a different host for security
            if host and host != self.host:
                self._log("   Ignoring reconnect to different host (security)")
            else:
                self._log("   Will reconnect via disconnect handler")
                self._handle_disconnect()

        else:
            self._log(f"<< Unknown server method: {method} params={params}")

    def _handle_response(self, msg: dict):
        msg_id = msg.get("id")
        result = msg.get("result")
        error = msg.get("error")

        # Look up what request this responds to
        purpose = self._pending_requests.pop(msg_id, None)

        if purpose is None:
            # Could be a late response or unknown ID
            self._log_debug(
                f"Response for unknown id={msg_id}: result={result}, error={error}"
            )
            return

        if purpose == "subscribe":
            self._handle_subscribe_response(msg_id, result, error)
        elif purpose == "authorize":
            self._handle_authorize_response(msg_id, result, error)
        elif purpose == "submit":
            self._handle_submit_response(msg_id, result, error)
        elif purpose == "suggest_difficulty":
            if error:
                self._log_debug(
                    f"suggest_difficulty response (id={msg_id}): "
                    f"error={error} (pool may not support this)"
                )
            else:
                self._log_debug(f"suggest_difficulty accepted (id={msg_id})")
        elif purpose == "keepalive":
            self._log_debug(f"Keepalive pong (id={msg_id})")
        else:
            self._log(f"Response for '{purpose}' (id={msg_id}): result={result}")

    def _handle_subscribe_response(self, msg_id, result, error):
        if error:
            self._log_error(f"Subscribe FAILED: {error}")
            self._set_status("Subscribe Failed")
            if self.on_error:
                self.on_error(f"Subscribe error: {error}")
            return

        # Parse subscribe result - format varies by pool:
        # Standard: [ [[subscriptions...]], extranonce1, extranonce2_size ]
        # result[0] = subscription details (varies, can be ignored)
        # result[1] = extranonce1 (critical)
        # result[2] = extranonce2_size (critical)
        if not result or not isinstance(result, list):
            self._log_error(f"Subscribe response has unexpected format: {result}")
            self._set_status("Subscribe Failed")
            if self.on_error:
                self.on_error(f"Bad subscribe response: {result}")
            return

        if len(result) >= 3:
            self.extranonce1 = str(result[1]) if result[1] is not None else ""
            self.extranonce2_size = int(result[2]) if result[2] is not None else 4
        elif len(result) >= 2:
            self.extranonce1 = str(result[1]) if isinstance(result[1], str) else ""
            self.extranonce2_size = 4
            self._log(
                "Subscribe response has only 2 fields "
                "(using default extranonce2_size=4)"
            )
        else:
            self._log_error(
                f"Subscribe result too short ({len(result)} fields): {result}"
            )
            self._set_status("Subscribe Failed")
            return

        if not self.extranonce1:
            self._log_error("Subscribe returned empty extranonce1!")
            self._set_status("Subscribe Failed")
            return

        self._log(
            f"<< Subscribe OK: extranonce1={self.extranonce1}, "
            f"extranonce2_size={self.extranonce2_size}"
        )
        self._set_status("Subscribed")

        # Step 2: Authorize
        worker_str = f"{self.address}.{self.worker}" if self.address else self.worker
        auth_id = self._next_id()
        self._pending_requests[auth_id] = "authorize"
        self._send(
            {
                "id": auth_id,
                "method": "mining.authorize",
                "params": [worker_str, "x"],
            }
        )
        self._log(f">> mining.authorize (id={auth_id}) as '{worker_str}'")
        self._set_status("Authorizing")

    def _handle_authorize_response(self, msg_id, result, error):
        if error:
            self._log_error(f"Authorization FAILED: {error}")
            self.authorized = False
            self._set_status("Auth Failed")
            if self.on_authorized:
                self.on_authorized(False)
            return

        self.authorized = bool(result)
        if self.authorized:
            self._log("Authorization SUCCESSFUL")
            self._set_status("Authorized")
            # NOTE: We no longer suggest difficulty here.
            # The engine will call suggest_difficulty() after measuring
            # hashrate, with a value that targets ~1 share per 20 seconds.
            # This avoids flooding the pool with hundreds of shares/sec.
        else:
            self._log_error(f"Authorization denied: result={result}")
            self._set_status("Auth Failed")

        if self.on_authorized:
            self.on_authorized(self.authorized)

    def _handle_submit_response(self, msg_id, result, error):
        # If error field is present and non-null, it's a rejection regardless of result
        accepted = bool(result) and not error
        err_msg = None
        if error:
            if isinstance(error, list) and len(error) >= 2:
                err_msg = str(error[1])
            else:
                err_msg = str(error)

        if accepted:
            self._log(f"<< Share ACCEPTED (id={msg_id})")
        else:
            self._log(f"<< Share REJECTED (id={msg_id}): {err_msg or 'unknown reason'}")

        if self.on_share_result:
            self.on_share_result(accepted, err_msg)
