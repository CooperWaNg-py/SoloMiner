"""
Metal GPU SHA-256 double-hash mining kernel for macOS.
Uses Apple's Metal framework via PyObjC for GPU-accelerated Bitcoin mining.

The SHA-256d (double SHA-256) is the core of Bitcoin's proof of work:
    hash = SHA256(SHA256(block_header))
The resulting 32-byte hash is interpreted as a little-endian 256-bit integer
and must be below the share target set by the pool.

Share target vs block target:
    - Block target: derived from nbits field, extremely small on mainnet
    - Share target: derived from pool's mining.set_difficulty, much easier
    For difficulty d: share_target = DIFF1_TARGET / d
    where DIFF1_TARGET = 0x00000000FFFF0000...0000 (the difficulty-1 target)
"""

import struct
import hashlib
import threading
import logging
from typing import Optional

logger = logging.getLogger("solominer.metal_miner")

# Try to import Metal framework
try:
    import Metal
    import Foundation

    METAL_AVAILABLE = True
except ImportError:
    METAL_AVAILABLE = False
    logger.warning("Metal framework not available, falling back to CPU mining")


# Bitcoin difficulty-1 target (used to derive share targets from pool difficulty)
DIFF1_TARGET = 0x00000000FFFF0000000000000000000000000000000000000000000000000000


def difficulty_to_target(difficulty: float) -> int:
    """Convert pool share difficulty to a 256-bit target integer."""
    if difficulty <= 0:
        return (1 << 256) - 1  # accept everything
    target = int(DIFF1_TARGET / difficulty)
    # Clamp to 256 bits
    if target >= (1 << 256):
        target = (1 << 256) - 1
    return target


# Metal Shader source for SHA-256 double hash
# The hash comparison is done in Bitcoin's little-endian convention:
# SHA-256 produces state[0..7] in big-endian order.
# Bitcoin interprets the 32-byte hash with bytes reversed (little-endian uint256).
# So state[7] byte-swapped = most significant 4 bytes of the Bitcoin hash number.
# We compare reversed: starting from state[7] down to state[0], each byte-swapped.
METAL_SHADER_SOURCE = """
#include <metal_stdlib>
using namespace metal;

// SHA-256 constants
constant uint K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
    0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
    0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
    0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
    0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
    0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
};

inline uint rotr(uint x, uint n) { return (x >> n) | (x << (32 - n)); }
inline uint ch(uint x, uint y, uint z) { return (x & y) ^ (~x & z); }
inline uint maj(uint x, uint y, uint z) { return (x & y) ^ (x & z) ^ (y & z); }
inline uint sigma0(uint x) { return rotr(x, 2) ^ rotr(x, 13) ^ rotr(x, 22); }
inline uint sigma1(uint x) { return rotr(x, 6) ^ rotr(x, 11) ^ rotr(x, 25); }
inline uint gamma0(uint x) { return rotr(x, 7) ^ rotr(x, 18) ^ (x >> 3); }
inline uint gamma1(uint x) { return rotr(x, 17) ^ rotr(x, 19) ^ (x >> 10); }

inline uint swap32(uint x) {
    return ((x & 0xFF) << 24) | ((x & 0xFF00) << 8) |
           ((x >> 8) & 0xFF00) | ((x >> 24) & 0xFF);
}

void sha256_transform(thread uint *state, thread const uint *block) {
    uint W[64];
    for (int i = 0; i < 16; i++) W[i] = block[i];
    for (int i = 16; i < 64; i++)
        W[i] = gamma1(W[i-2]) + W[i-7] + gamma0(W[i-15]) + W[i-16];

    uint a = state[0], b = state[1], c = state[2], d = state[3];
    uint e = state[4], f = state[5], g = state[6], h = state[7];

    for (int i = 0; i < 64; i++) {
        uint t1 = h + sigma1(e) + ch(e, f, g) + K[i] + W[i];
        uint t2 = sigma0(a) + maj(a, b, c);
        h = g; g = f; f = e; e = d + t1;
        d = c; c = b; b = a; a = t1 + t2;
    }

    state[0] += a; state[1] += b; state[2] += c; state[3] += d;
    state[4] += e; state[5] += f; state[6] += g; state[7] += h;
}

// Each thread tries one nonce.
// header_data: 80 bytes as 20 uint32 (big-endian, matching wire format)
// target: 8 uint32 in Bitcoin little-endian convention:
//   target[0] = most significant 4 bytes of the LE uint256
//   target[7] = least significant 4 bytes
//   Each uint32 is byte-swapped from the SHA output word.
// base_nonce: starting nonce for this dispatch
// results: [found_flag, winning_nonce, best_difficulty_bits, best_nonce]
kernel void mine_sha256d(
    device const uint *header_data [[buffer(0)]],
    device const uint *target [[buffer(1)]],
    device atomic_uint *results [[buffer(2)]],
    device const uint *base_nonce_buf [[buffer(3)]],
    uint gid [[thread_position_in_grid]]
) {
    uint base_nonce = base_nonce_buf[0];
    uint nonce = base_nonce + gid;

    // === First SHA-256: hash the 80-byte block header ===

    uint state[8] = {
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
        0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
    };

    // Block 0: first 64 bytes (16 uint32)
    uint block[16];
    for (int i = 0; i < 16; i++) block[i] = header_data[i];
    sha256_transform(state, block);

    // Block 1: remaining 16 bytes + padding
    block[0] = header_data[16];
    block[1] = header_data[17];
    block[2] = header_data[18];
    block[3] = nonce;  // nonce at offset 76 (uint index 19)
    block[4] = 0x80000000;
    for (int i = 5; i < 15; i++) block[i] = 0;
    block[15] = 640;  // 80 * 8 bits
    sha256_transform(state, block);

    // === Second SHA-256: hash the 32-byte result ===

    uint hash1[8];
    for (int i = 0; i < 8; i++) hash1[i] = state[i];

    state[0] = 0x6a09e667; state[1] = 0xbb67ae85;
    state[2] = 0x3c6ef372; state[3] = 0xa54ff53a;
    state[4] = 0x510e527f; state[5] = 0x9b05688c;
    state[6] = 0x1f83d9ab; state[7] = 0x5be0cd19;

    block[0] = hash1[0]; block[1] = hash1[1];
    block[2] = hash1[2]; block[3] = hash1[3];
    block[4] = hash1[4]; block[5] = hash1[5];
    block[6] = hash1[6]; block[7] = hash1[7];
    block[8] = 0x80000000;
    for (int i = 9; i < 15; i++) block[i] = 0;
    block[15] = 256;  // 32 * 8 bits
    sha256_transform(state, block);

    // === Compare hash against target in Bitcoin LE convention ===
    //
    // SHA-256 output: state[0..7] are big-endian words.
    // Bitcoin hash as uint256 LE: byte-reverse the entire 32-byte output.
    // This means:
    //   LE word 0 (most significant)  = swap32(state[7])
    //   LE word 1                     = swap32(state[6])
    //   ...
    //   LE word 7 (least significant) = swap32(state[0])
    //
    // target[0..7] is already in this same LE word order.

    bool below_target = false;
    for (int i = 0; i < 8; i++) {
        uint hash_word = swap32(state[7 - i]);
        uint tgt_word = target[i];
        if (hash_word < tgt_word) {
            below_target = true;
            break;
        } else if (hash_word > tgt_word) {
            break;
        }
    }

    if (below_target) {
        atomic_store_explicit(&results[0], 1, memory_order_relaxed);
        atomic_store_explicit(&results[1], nonce, memory_order_relaxed);
    }

    // Track best share: count leading zero bits of the LE hash
    uint lz = 0;
    for (int i = 0; i < 8; i++) {
        uint w = swap32(state[7 - i]);
        if (w == 0) { lz += 32; }
        else { lz += clz(w); break; }
    }
    uint cur_best = atomic_load_explicit(&results[2], memory_order_relaxed);
    while (lz > cur_best) {
        if (atomic_compare_exchange_weak_explicit(
                &results[2], &cur_best, lz,
                memory_order_relaxed, memory_order_relaxed)) {
            atomic_store_explicit(&results[3], nonce, memory_order_relaxed);
            break;
        }
    }
}
"""


def build_block_header(
    version: str,
    prevhash: str,
    merkle_root: str,
    ntime: str,
    nbits: str,
    nonce: int = 0,
) -> bytes:
    """
    Build 80-byte block header from stratum job parameters.
    All fields are packed in the wire format (little-endian where appropriate).
    """
    ver = struct.pack("<I", int(version, 16))

    # prevhash from stratum is in a weird byte order: groups of 4 bytes reversed
    prev_bytes = bytes.fromhex(prevhash)
    prev_fixed = b""
    for i in range(0, 32, 4):
        prev_fixed += prev_bytes[i : i + 4][::-1]

    mr_bytes = bytes.fromhex(merkle_root)

    time_bytes = struct.pack("<I", int(ntime, 16))
    bits_bytes = struct.pack("<I", int(nbits, 16))
    nonce_bytes = struct.pack("<I", nonce)

    header = ver + prev_fixed + mr_bytes + time_bytes + bits_bytes + nonce_bytes
    assert len(header) == 80
    return header


def compute_merkle_root(
    coinb1: str, coinb2: str, extranonce1: str, extranonce2: str, merkle_branch: list
) -> str:
    """Compute the merkle root from coinbase and merkle branches."""
    coinbase = bytes.fromhex(coinb1 + extranonce1 + extranonce2 + coinb2)
    coinbase_hash = hashlib.sha256(hashlib.sha256(coinbase).digest()).digest()

    current = coinbase_hash
    for branch_hex in merkle_branch:
        branch = bytes.fromhex(branch_hex)
        current = hashlib.sha256(hashlib.sha256(current + branch).digest()).digest()

    return current.hex()


class MetalMiner:
    """
    GPU-accelerated Bitcoin miner using Apple Metal.
    Falls back to CPU mining if Metal is unavailable.
    """

    def __init__(self):
        self.device = None
        self.command_queue = None
        self.pipeline_state = None
        self._initialized = False
        self.use_gpu = METAL_AVAILABLE
        self._hashcount = 0
        self._hashcount_lock = threading.Lock()
        self.best_share_bits = 0

        if self.use_gpu:
            self._init_metal()

    def _init_metal(self):
        try:
            self.device = Metal.MTLCreateSystemDefaultDevice()
            if self.device is None:
                logger.warning("No Metal device found, falling back to CPU")
                self.use_gpu = False
                return

            self.command_queue = self.device.newCommandQueue()

            options = Metal.MTLCompileOptions.alloc().init()
            library, error = self.device.newLibraryWithSource_options_error_(
                METAL_SHADER_SOURCE, options, None
            )
            if error:
                logger.error(f"Metal shader compilation error: {error}")
                self.use_gpu = False
                return

            func = library.newFunctionWithName_("mine_sha256d")
            if func is None:
                logger.error("Could not find mine_sha256d function in shader")
                self.use_gpu = False
                return

            self.pipeline_state, error = (
                self.device.newComputePipelineStateWithFunction_error_(func, None)
            )
            if error:
                logger.error(f"Pipeline state creation error: {error}")
                self.use_gpu = False
                return

            self._initialized = True
            logger.info(f"Metal initialized: {self.device.name()}")
        except Exception as e:
            logger.error(f"Metal init failed: {e}")
            self.use_gpu = False

    @property
    def gpu_name(self) -> str:
        if self.device:
            return self.device.name()
        return "CPU"

    def _target_to_le_uints(self, target_int: int) -> list:
        """
        Convert a 256-bit target integer to 8 uint32 words in the
        Bitcoin LE convention used by the shader:
          word[0] = most significant 4 bytes (byte-swapped from BE)
          word[7] = least significant 4 bytes
        """
        # Target as 32 bytes, big-endian (natural integer representation)
        target_be = target_int.to_bytes(32, byteorder="big")
        # Bitcoin LE: reverse entire byte string
        target_le = target_be[::-1]
        # Split into uint32 words (each in native byte order)
        # But we need them in "most significant first" order for comparison
        # Since target_le[0..3] is least significant and target_le[28..31] is most:
        words = []
        for i in range(7, -1, -1):
            w = struct.unpack("<I", target_le[i * 4 : (i + 1) * 4])[0]
            words.append(w)
        return words

    def mine_range_gpu(
        self, header_data: bytes, target_int: int, base_nonce: int, count: int
    ) -> Optional[int]:
        """
        Mine a range of nonces on the GPU.
        Returns the winning nonce or None.
        """
        if not self._initialized:
            return self.mine_range_cpu(header_data, target_int, base_nonce, count)

        # Guard: clamp count so base_nonce + count doesn't exceed uint32
        base_nonce = base_nonce & 0xFFFFFFFF
        max_count = 0xFFFFFFFF - base_nonce + 1
        count = min(count, max_count)
        if count <= 0:
            return None

        # Prepare header as 20 big-endian uint32 (matching wire format)
        header_uints = []
        for i in range(0, 80, 4):
            val = struct.unpack(">I", header_data[i : i + 4])[0]
            header_uints.append(val)

        # Convert target to LE word array for shader
        target_uints = self._target_to_le_uints(target_int)

        # Create Metal buffers
        header_packed = struct.pack("=" + "I" * 20, *header_uints)
        header_buf = self.device.newBufferWithBytes_length_options_(
            header_packed, len(header_packed), Metal.MTLResourceStorageModeShared
        )

        target_packed = struct.pack("=" + "I" * 8, *target_uints)
        target_buf = self.device.newBufferWithBytes_length_options_(
            target_packed, len(target_packed), Metal.MTLResourceStorageModeShared
        )

        # Results: [found_flag, winning_nonce, best_leading_zeros, best_nonce]
        results_packed = struct.pack("=IIII", 0, 0, 0, 0)
        results_buf = self.device.newBufferWithBytes_length_options_(
            results_packed, len(results_packed), Metal.MTLResourceStorageModeShared
        )

        nonce_packed = struct.pack("=I", base_nonce & 0xFFFFFFFF)
        nonce_buf = self.device.newBufferWithBytes_length_options_(
            nonce_packed, len(nonce_packed), Metal.MTLResourceStorageModeShared
        )

        # Dispatch GPU work
        command_buffer = self.command_queue.commandBuffer()
        encoder = command_buffer.computeCommandEncoder()
        encoder.setComputePipelineState_(self.pipeline_state)
        encoder.setBuffer_offset_atIndex_(header_buf, 0, 0)
        encoder.setBuffer_offset_atIndex_(target_buf, 0, 1)
        encoder.setBuffer_offset_atIndex_(results_buf, 0, 2)
        encoder.setBuffer_offset_atIndex_(nonce_buf, 0, 3)

        max_threads = self.pipeline_state.maxTotalThreadsPerThreadgroup()
        thread_group_size = Metal.MTLSizeMake(min(max_threads, 256), 1, 1)
        grid_size = Metal.MTLSizeMake(count, 1, 1)

        encoder.dispatchThreads_threadsPerThreadgroup_(grid_size, thread_group_size)
        encoder.endEncoding()
        command_buffer.commit()
        command_buffer.waitUntilCompleted()

        # Check for GPU errors
        status = command_buffer.status()
        if status == 5:  # MTLCommandBufferStatusError
            error = command_buffer.error()
            error_msg = str(error) if error else "Unknown GPU error"
            logger.error(f"GPU command buffer error: {error_msg}")
            raise RuntimeError(f"Metal GPU error: {error_msg}")

        # Read results
        result_ptr = results_buf.contents()
        result_bytes = result_ptr.as_buffer(16)
        found, winning_nonce, best_zeros, best_nonce = struct.unpack(
            "=IIII", result_bytes
        )

        with self._hashcount_lock:
            self._hashcount += count
            if best_zeros > self.best_share_bits:
                self.best_share_bits = best_zeros

        if found:
            return winning_nonce
        return None

    def mine_range_cpu(
        self, header_data: bytes, target_int: int, base_nonce: int, count: int
    ) -> Optional[int]:
        """CPU fallback mining. Target comparison in Bitcoin LE convention."""
        for n in range(count):
            nonce = (base_nonce + n) & 0xFFFFFFFF
            test_header = header_data[:76] + struct.pack("<I", nonce)
            hash_result = hashlib.sha256(hashlib.sha256(test_header).digest()).digest()
            # Bitcoin: interpret hash as little-endian uint256
            hash_int = int.from_bytes(hash_result, byteorder="little")
            if hash_int < target_int:
                with self._hashcount_lock:
                    self._hashcount += n + 1
                return nonce

        with self._hashcount_lock:
            self._hashcount += count
        return None

    def get_and_reset_hashcount(self) -> int:
        with self._hashcount_lock:
            count = self._hashcount
            self._hashcount = 0
            return count


def create_miner(algorithm: str = "SHA-256d") -> MetalMiner:
    """Factory function to create a miner. Only SHA-256d (Bitcoin) is supported."""
    return MetalMiner()
