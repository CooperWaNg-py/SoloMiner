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


# ─────────────────────────────────────────────────────────────────────
# Scrypt Metal Shader (Litecoin: N=1024, r=1, p=1)
# ─────────────────────────────────────────────────────────────────────
#
# Each GPU thread processes one nonce. The full Scrypt computation is:
#   1. PBKDF2-HMAC-SHA256(header, header, 1, 128) -> B
#   2. ROMix(B, N=1024) using Salsa20/8 + BlockMix
#   3. PBKDF2-HMAC-SHA256(header, B', 1, 32) -> hash
#   4. Compare hash vs target
#
# ROMix requires 128*r*N = 128KB per thread (N=1024, r=1).
# This is stored in device memory via a large V buffer, with each thread
# getting its own 128KB slice. Threadgroup size must be limited.

SCRYPT_SHADER_SOURCE = """
#include <metal_stdlib>
using namespace metal;

// ── SHA-256 constants and helpers (reused for PBKDF2-HMAC-SHA256) ──

constant uint SK[64] = {
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

inline uint s_rotr(uint x, uint n) { return (x >> n) | (x << (32 - n)); }
inline uint s_ch(uint x, uint y, uint z) { return (x & y) ^ (~x & z); }
inline uint s_maj(uint x, uint y, uint z) { return (x & y) ^ (x & z) ^ (y & z); }
inline uint s_sigma0(uint x) { return s_rotr(x, 2) ^ s_rotr(x, 13) ^ s_rotr(x, 22); }
inline uint s_sigma1(uint x) { return s_rotr(x, 6) ^ s_rotr(x, 11) ^ s_rotr(x, 25); }
inline uint s_gamma0(uint x) { return s_rotr(x, 7) ^ s_rotr(x, 18) ^ (x >> 3); }
inline uint s_gamma1(uint x) { return s_rotr(x, 17) ^ s_rotr(x, 19) ^ (x >> 10); }

inline uint s_swap32(uint x) {
    return ((x & 0xFF) << 24) | ((x & 0xFF00) << 8) |
           ((x >> 8) & 0xFF00) | ((x >> 24) & 0xFF);
}

void s_sha256_transform(thread uint *state, thread const uint *blk) {
    uint W[64];
    for (int i = 0; i < 16; i++) W[i] = blk[i];
    for (int i = 16; i < 64; i++)
        W[i] = s_gamma1(W[i-2]) + W[i-7] + s_gamma0(W[i-15]) + W[i-16];

    uint a = state[0], b = state[1], c = state[2], d = state[3];
    uint e = state[4], f = state[5], g = state[6], h = state[7];

    for (int i = 0; i < 64; i++) {
        uint t1 = h + s_sigma1(e) + s_ch(e, f, g) + SK[i] + W[i];
        uint t2 = s_sigma0(a) + s_maj(a, b, c);
        h = g; g = f; f = e; e = d + t1;
        d = c; c = b; b = a; a = t1 + t2;
    }

    state[0] += a; state[1] += b; state[2] += c; state[3] += d;
    state[4] += e; state[5] += f; state[6] += g; state[7] += h;
}

// Full SHA-256 of a message up to 128 bytes.
// msg is in big-endian uint32 words, msg_bytes is byte length.
void s_sha256_full(thread const uint *msg, uint msg_bytes, thread uint *out) {
    out[0] = 0x6a09e667; out[1] = 0xbb67ae85;
    out[2] = 0x3c6ef372; out[3] = 0xa54ff53a;
    out[4] = 0x510e527f; out[5] = 0x9b05688c;
    out[6] = 0x1f83d9ab; out[7] = 0x5be0cd19;

    uint num_words = (msg_bytes + 3) / 4;
    uint total_blocks = (msg_bytes + 9 + 63) / 64;

    for (uint b = 0; b < total_blocks; b++) {
        uint blk[16];
        for (int i = 0; i < 16; i++) {
            uint idx = b * 16 + i;
            if (idx < num_words) {
                blk[i] = msg[idx];
            } else {
                blk[i] = 0;
            }
        }
        // Append 0x80 byte after message
        uint pad_word_idx = msg_bytes / 4;
        uint pad_byte_pos = msg_bytes % 4;
        if (pad_word_idx >= b * 16 && pad_word_idx < (b + 1) * 16) {
            uint local_idx = pad_word_idx - b * 16;
            // big-endian: pad byte goes at position (3 - pad_byte_pos) * 8
            uint shift = (3 - pad_byte_pos) * 8;
            blk[local_idx] |= (0x80u << shift);
            // Clear any garbage after the pad byte
            uint mask = (0xFFFFFFFFu << (shift)) & 0xFFFFFFFFu;
            // Actually we need the message bits plus the 0x80
            // Since we loaded from msg which should be zero-padded, just set the bit
        }
        // Length in last block
        if (b == total_blocks - 1) {
            blk[15] = msg_bytes * 8;
        }
        s_sha256_transform(out, blk);
    }
}

// ── HMAC-SHA256 ──
// Key and message are provided as big-endian uint32 arrays.
// key_bytes <= 64, msg provided with length.

void s_hmac_sha256(thread const uint *key_be, uint key_bytes,
                   thread const uint *msg_be, uint msg_bytes,
                   thread uint *out) {
    // If key > 64 bytes, hash it first (not needed for our use case, key=80 or 64)
    uint k_words[16]; // 64 bytes = 16 uint32
    for (int i = 0; i < 16; i++) k_words[i] = 0;

    if (key_bytes > 64) {
        // Hash the key
        s_sha256_full(key_be, key_bytes, out);
        for (int i = 0; i < 8; i++) k_words[i] = out[i];
        key_bytes = 32;
    } else {
        uint num_w = (key_bytes + 3) / 4;
        for (uint i = 0; i < num_w; i++) k_words[i] = key_be[i];
    }

    // ipad = key XOR 0x36363636
    uint ipad[16];
    for (int i = 0; i < 16; i++) ipad[i] = k_words[i] ^ 0x36363636;

    // opad = key XOR 0x5c5c5c5c
    uint opad[16];
    for (int i = 0; i < 16; i++) opad[i] = k_words[i] ^ 0x5c5c5c5c;

    // inner = SHA256(ipad || msg)
    // First transform ipad block (64 bytes)
    uint inner_state[8] = {
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
        0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
    };
    s_sha256_transform(inner_state, ipad);

    // Now process message blocks
    uint total_inner_len = 64 + msg_bytes;
    uint msg_full_blocks = msg_bytes / 64;
    for (uint b = 0; b < msg_full_blocks; b++) {
        uint blk[16];
        for (int i = 0; i < 16; i++) blk[i] = msg_be[b * 16 + i];
        s_sha256_transform(inner_state, blk);
    }

    // Last block of message with padding
    uint remaining_bytes = msg_bytes - msg_full_blocks * 64;
    uint remaining_words = (remaining_bytes + 3) / 4;
    uint last_blk[16];
    for (int i = 0; i < 16; i++) last_blk[i] = 0;
    for (uint i = 0; i < remaining_words; i++) {
        last_blk[i] = msg_be[msg_full_blocks * 16 + i];
    }
    // Append 0x80
    uint pad_pos = remaining_bytes;
    uint pw = pad_pos / 4;
    uint pb = pad_pos % 4;
    last_blk[pw] |= (0x80u << ((3 - pb) * 8));

    if (remaining_bytes >= 56) {
        // Need another block
        s_sha256_transform(inner_state, last_blk);
        for (int i = 0; i < 16; i++) last_blk[i] = 0;
    }
    last_blk[14] = 0;
    last_blk[15] = total_inner_len * 8;
    s_sha256_transform(inner_state, last_blk);

    // outer = SHA256(opad || inner_hash)
    uint outer_state[8] = {
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
        0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
    };
    s_sha256_transform(outer_state, opad);

    // opad(64) + hash(32) = 96 bytes, need padding
    uint hash_blk[16];
    for (int i = 0; i < 8; i++) hash_blk[i] = inner_state[i];
    hash_blk[8] = 0x80000000;
    for (int i = 9; i < 15; i++) hash_blk[i] = 0;
    hash_blk[15] = 96 * 8; // 768 bits
    s_sha256_transform(outer_state, hash_blk);

    for (int i = 0; i < 8; i++) out[i] = outer_state[i];
}

// ── PBKDF2-HMAC-SHA256(password, salt, c=1, dkLen) ──
// For Scrypt: c is always 1, which simplifies things.
// password and salt are big-endian uint32 arrays.
// Output dk is big-endian uint32 array, dkLen in bytes.

void s_pbkdf2_sha256(thread const uint *pwd_be, uint pwd_bytes,
                     thread const uint *salt_be, uint salt_bytes,
                     thread uint *dk, uint dk_bytes) {
    uint num_blocks = (dk_bytes + 31) / 32;

    for (uint block_idx = 1; block_idx <= num_blocks; block_idx++) {
        // U = HMAC(pwd, salt || INT_32_BE(block_idx))
        // Build salt || INT_32_BE(block_idx)
        uint salt_ext_words = (salt_bytes + 3) / 4 + 1;
        uint salt_ext[48]; // max salt=128 bytes + 4 = 132 bytes = 33 words
        for (uint i = 0; i < (salt_bytes + 3) / 4; i++) {
            salt_ext[i] = salt_be[i];
        }
        // Append block index as big-endian uint32
        // Need to handle partial last word of salt
        uint full_salt_words = salt_bytes / 4;
        uint leftover = salt_bytes % 4;
        if (leftover == 0) {
            salt_ext[full_salt_words] = block_idx;
        } else {
            // The last partial salt word already has bytes in the high positions
            // We need to insert block_idx bytes after the salt bytes
            // In big-endian: the 4 bytes of block_idx go right after salt_bytes
            // Byte positions in the combined word:
            uint combined[48];
            // Easier: treat as byte array conceptually
            // salt_ext already has the salt words. We need to place 4 more bytes.
            // byte offset = salt_bytes in the word array
            // word index = salt_bytes / 4, byte within = salt_bytes % 4
            uint bi_bytes[4];
            bi_bytes[0] = (block_idx >> 24) & 0xFF;
            bi_bytes[1] = (block_idx >> 16) & 0xFF;
            bi_bytes[2] = (block_idx >> 8) & 0xFF;
            bi_bytes[3] = block_idx & 0xFF;

            for (int b = 0; b < 4; b++) {
                uint byte_pos = salt_bytes + b;
                uint wi = byte_pos / 4;
                uint bi = byte_pos % 4;
                uint shift = (3 - bi) * 8;
                if (b == 0 && leftover > 0) {
                    // Clear remaining bytes in this word first
                    // (already zero-padded from salt_be, should be fine)
                }
                salt_ext[wi] |= (bi_bytes[b] << shift);
                // Make sure higher words are zeroed
                if (wi >= salt_ext_words) salt_ext_words = wi + 1;
            }
        }

        uint total_salt_bytes = salt_bytes + 4;
        uint hmac_out[8];
        s_hmac_sha256(pwd_be, pwd_bytes, salt_ext, total_salt_bytes, hmac_out);

        // c=1, so T = U1 (no further iterations needed)
        uint dk_offset = (block_idx - 1) * 8;
        uint words_to_copy = min(8u, (dk_bytes + 3) / 4 - dk_offset);
        for (uint i = 0; i < words_to_copy; i++) {
            dk[dk_offset + i] = hmac_out[i];
        }
    }
}

// ── Salsa20/8 core ──
// Operates on 16 uint32 in little-endian.

inline uint s_rotl(uint x, uint n) { return (x << n) | (x >> (32 - n)); }

void salsa20_8(thread uint *x) {
    uint orig[16];
    for (int i = 0; i < 16; i++) orig[i] = x[i];

    for (int round = 0; round < 4; round++) { // 4 double-rounds = 8 rounds
        // Column round
        x[ 4] ^= s_rotl(x[ 0] + x[12],  7);
        x[ 8] ^= s_rotl(x[ 4] + x[ 0],  9);
        x[12] ^= s_rotl(x[ 8] + x[ 4], 13);
        x[ 0] ^= s_rotl(x[12] + x[ 8], 18);
        x[ 9] ^= s_rotl(x[ 5] + x[ 1],  7);
        x[13] ^= s_rotl(x[ 9] + x[ 5],  9);
        x[ 1] ^= s_rotl(x[13] + x[ 9], 13);
        x[ 5] ^= s_rotl(x[ 1] + x[13], 18);
        x[14] ^= s_rotl(x[10] + x[ 6],  7);
        x[ 2] ^= s_rotl(x[14] + x[10],  9);
        x[ 6] ^= s_rotl(x[ 2] + x[14], 13);
        x[10] ^= s_rotl(x[ 6] + x[ 2], 18);
        x[ 3] ^= s_rotl(x[15] + x[11],  7);
        x[ 7] ^= s_rotl(x[ 3] + x[15],  9);
        x[11] ^= s_rotl(x[ 7] + x[ 3], 13);
        x[15] ^= s_rotl(x[11] + x[ 7], 18);
        // Row round
        x[ 1] ^= s_rotl(x[ 0] + x[ 3],  7);
        x[ 2] ^= s_rotl(x[ 1] + x[ 0],  9);
        x[ 3] ^= s_rotl(x[ 2] + x[ 1], 13);
        x[ 0] ^= s_rotl(x[ 3] + x[ 2], 18);
        x[ 6] ^= s_rotl(x[ 5] + x[ 4],  7);
        x[ 7] ^= s_rotl(x[ 6] + x[ 5],  9);
        x[ 4] ^= s_rotl(x[ 7] + x[ 6], 13);
        x[ 5] ^= s_rotl(x[ 4] + x[ 7], 18);
        x[11] ^= s_rotl(x[10] + x[ 9],  7);
        x[ 8] ^= s_rotl(x[11] + x[10],  9);
        x[ 9] ^= s_rotl(x[ 8] + x[11], 13);
        x[10] ^= s_rotl(x[ 9] + x[ 8], 18);
        x[12] ^= s_rotl(x[15] + x[14],  7);
        x[13] ^= s_rotl(x[12] + x[15],  9);
        x[14] ^= s_rotl(x[13] + x[12], 13);
        x[15] ^= s_rotl(x[14] + x[13], 18);
    }

    for (int i = 0; i < 16; i++) x[i] += orig[i];
}

// ── Scrypt BlockMix (r=1) ──
// B = two 64-byte (16 uint32) blocks: B[0] and B[1]
// Input/output: 32 uint32 in little-endian
// For r=1: X = B[1], X ^= B[0] -> Salsa -> Y[0], X ^= B[1] -> Salsa -> Y[1]
// Output: Y[0] || Y[1] (even blocks first, then odd)

void scrypt_block_mix(thread uint *B) {
    uint X[16];
    uint Y[32];

    // X = B[2r-1] = B[1] (last 64-byte block)
    for (int i = 0; i < 16; i++) X[i] = B[16 + i];

    // i=0: X ^= B[0], Salsa, Y_even[0] = X
    for (int i = 0; i < 16; i++) X[i] ^= B[i];
    salsa20_8(X);
    for (int i = 0; i < 16; i++) Y[i] = X[i]; // Y[0] (even block)

    // i=1: X ^= B[1], Salsa, Y_odd[0] = X
    for (int i = 0; i < 16; i++) X[i] ^= B[16 + i];
    salsa20_8(X);
    for (int i = 0; i < 16; i++) Y[16 + i] = X[i]; // Y[1] (odd block)

    // Copy back
    for (int i = 0; i < 32; i++) B[i] = Y[i];
}

// ── Scrypt ROMix (N=1024, r=1) ──
// X is 128 bytes = 32 uint32 (little-endian)
// V is device memory: V_base[i * 32] for i in 0..N-1

void scrypt_romix(thread uint *X, device uint *V_base) {
    const uint N = 1024;

    // Step 1: Fill V[0..N-1]
    for (uint i = 0; i < N; i++) {
        for (int j = 0; j < 32; j++) V_base[i * 32 + j] = X[j];
        scrypt_block_mix(X);
    }

    // Step 2: Mix
    for (uint i = 0; i < N; i++) {
        // j = Integerify(X) mod N  --  first uint32 of last 64-byte block
        uint j = X[16] & (N - 1); // N is power of 2
        // X ^= V[j]
        for (int k = 0; k < 32; k++) X[k] ^= V_base[j * 32 + k];
        scrypt_block_mix(X);
    }
}

// Convert 128 bytes from big-endian uint32 (PBKDF2 output) to little-endian uint32
void be_to_le_32(thread uint *data, uint count) {
    for (uint i = 0; i < count; i++) {
        data[i] = s_swap32(data[i]);
    }
}

void le_to_be_32(thread uint *data, uint count) {
    for (uint i = 0; i < count; i++) {
        data[i] = s_swap32(data[i]);
    }
}

// ── Main Scrypt mining kernel ──
// buffer(0): header_data - 20 uint32, big-endian (wire format, nonce at [19])
// buffer(1): target - 8 uint32, Bitcoin LE convention (same as SHA-256d shader)
// buffer(2): results - atomic [found_flag, winning_nonce, best_bits, best_nonce]
// buffer(3): base_nonce_buf - 1 uint32
// buffer(4): V_memory - N*32 uint32 per thread for ROMix scratchpad

kernel void mine_scrypt(
    device const uint *header_data [[buffer(0)]],
    device const uint *target [[buffer(1)]],
    device atomic_uint *results [[buffer(2)]],
    device const uint *base_nonce_buf [[buffer(3)]],
    device uint *V_memory [[buffer(4)]],
    uint gid [[thread_position_in_grid]]
) {
    const uint N = 1024;
    const uint V_PER_THREAD = N * 32; // 32768 uint32 = 128KB per thread

    uint base_nonce = base_nonce_buf[0];
    uint nonce = base_nonce + gid;

    // Build header with this nonce: 20 uint32 in big-endian
    uint hdr[20];
    for (int i = 0; i < 19; i++) hdr[i] = header_data[i];
    hdr[19] = nonce; // nonce word in big-endian (same convention as SHA-256d shader)

    // ── Step 1: PBKDF2-HMAC-SHA256(header, header, 1, 128) ──
    // password = header (80 bytes, big-endian), salt = header (80 bytes, big-endian)
    uint B[32]; // 128 bytes = 32 uint32 big-endian
    s_pbkdf2_sha256(hdr, 80, hdr, 80, B, 128);

    // Convert B from big-endian to little-endian for Salsa20/BlockMix
    be_to_le_32(B, 32);

    // ── Step 2: ROMix(B, N=1024) ──
    device uint *my_V = V_memory + gid * V_PER_THREAD;
    scrypt_romix(B, my_V);

    // Convert B' back to big-endian for PBKDF2
    le_to_be_32(B, 32);

    // ── Step 3: PBKDF2-HMAC-SHA256(header, B', 1, 32) ──
    uint hash[8]; // 32 bytes big-endian
    s_pbkdf2_sha256(hdr, 80, B, 128, hash, 32);

    // ── Step 4: Compare hash against target ──
    // hash[0..7] are big-endian SHA-256 output words.
    // Convert to Bitcoin LE convention for comparison:
    //   LE word 0 (most sig) = swap32(hash[7])
    //   LE word 7 (least sig) = swap32(hash[0])
    // Actually for Scrypt coins like Litecoin, the hash is just interpreted as LE uint256.
    // The PBKDF2 output bytes, when reversed, give the LE integer.
    // hash[0] is most significant BE word. swap32(hash[7]) is most significant LE comparison word.

    bool below_target = false;
    for (int i = 0; i < 8; i++) {
        uint hash_word = s_swap32(hash[7 - i]);
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

    // Track best share: count leading zero bits
    uint lz = 0;
    for (int i = 0; i < 8; i++) {
        uint w = s_swap32(hash[7 - i]);
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


# ─────────────────────────────────────────────────────────────────────
# RandomX-approximation Metal Shader
# ─────────────────────────────────────────────────────────────────────
#
# True RandomX is CPU-bound (random programs, branches) and not feasible
# on GPU. Instead we implement a memory-hard hash approximating RandomX's
# characteristics:
#   1. SHA-256(header+nonce) -> seed
#   2. Fill 256KB scratchpad with iterative SHA-256
#   3. 64 mixing rounds with pseudo-random scratchpad reads
#   4. Final SHA-256 -> compare target
#
# 256KB per thread = 65536 uint32. Threadgroup size must be limited.

RANDOMX_SHADER_SOURCE = """
#include <metal_stdlib>
using namespace metal;

// SHA-256 constants
constant uint RK[64] = {
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

inline uint r_rotr(uint x, uint n) { return (x >> n) | (x << (32 - n)); }
inline uint r_ch(uint x, uint y, uint z) { return (x & y) ^ (~x & z); }
inline uint r_maj(uint x, uint y, uint z) { return (x & y) ^ (x & z) ^ (y & z); }
inline uint r_sigma0(uint x) { return r_rotr(x, 2) ^ r_rotr(x, 13) ^ r_rotr(x, 22); }
inline uint r_sigma1(uint x) { return r_rotr(x, 6) ^ r_rotr(x, 11) ^ r_rotr(x, 25); }
inline uint r_gamma0(uint x) { return r_rotr(x, 7) ^ r_rotr(x, 18) ^ (x >> 3); }
inline uint r_gamma1(uint x) { return r_rotr(x, 17) ^ r_rotr(x, 19) ^ (x >> 10); }

inline uint r_swap32(uint x) {
    return ((x & 0xFF) << 24) | ((x & 0xFF00) << 8) |
           ((x >> 8) & 0xFF00) | ((x >> 24) & 0xFF);
}

void r_sha256_transform(thread uint *state, thread const uint *blk) {
    uint W[64];
    for (int i = 0; i < 16; i++) W[i] = blk[i];
    for (int i = 16; i < 64; i++)
        W[i] = r_gamma1(W[i-2]) + W[i-7] + r_gamma0(W[i-15]) + W[i-16];

    uint a = state[0], b = state[1], c = state[2], d = state[3];
    uint e = state[4], f = state[5], g = state[6], h = state[7];

    for (int i = 0; i < 64; i++) {
        uint t1 = h + r_sigma1(e) + r_ch(e, f, g) + RK[i] + W[i];
        uint t2 = r_sigma0(a) + r_maj(a, b, c);
        h = g; g = f; f = e; e = d + t1;
        d = c; c = b; b = a; a = t1 + t2;
    }

    state[0] += a; state[1] += b; state[2] += c; state[3] += d;
    state[4] += e; state[5] += f; state[6] += g; state[7] += h;
}

// SHA-256 of exactly 32 bytes (one block with padding)
void r_sha256_32bytes(thread const uint *input, thread uint *out) {
    out[0] = 0x6a09e667; out[1] = 0xbb67ae85;
    out[2] = 0x3c6ef372; out[3] = 0xa54ff53a;
    out[4] = 0x510e527f; out[5] = 0x9b05688c;
    out[6] = 0x1f83d9ab; out[7] = 0x5be0cd19;

    uint blk[16];
    for (int i = 0; i < 8; i++) blk[i] = input[i];
    blk[8] = 0x80000000;
    for (int i = 9; i < 15; i++) blk[i] = 0;
    blk[15] = 256; // 32 * 8
    r_sha256_transform(out, blk);
}

// SHA-256 of 80 bytes (same as in SHA-256d miner, two blocks)
void r_sha256_80bytes(thread const uint *header, uint nonce, thread uint *out) {
    out[0] = 0x6a09e667; out[1] = 0xbb67ae85;
    out[2] = 0x3c6ef372; out[3] = 0xa54ff53a;
    out[4] = 0x510e527f; out[5] = 0x9b05688c;
    out[6] = 0x1f83d9ab; out[7] = 0x5be0cd19;

    // Block 0: first 64 bytes
    uint blk[16];
    for (int i = 0; i < 16; i++) blk[i] = header[i];
    r_sha256_transform(out, blk);

    // Block 1: bytes 64-79 + nonce + padding
    blk[0] = header[16];
    blk[1] = header[17];
    blk[2] = header[18];
    blk[3] = nonce;
    blk[4] = 0x80000000;
    for (int i = 5; i < 15; i++) blk[i] = 0;
    blk[15] = 640;
    r_sha256_transform(out, blk);
}

// ── Main RandomX-approximation mining kernel ──
// buffer(0): header_data - 20 uint32, big-endian
// buffer(1): target - 8 uint32, Bitcoin LE convention
// buffer(2): results - atomic [found_flag, winning_nonce, best_bits, best_nonce]
// buffer(3): base_nonce_buf - 1 uint32
// buffer(4): scratchpad_memory - 65536 uint32 (256KB) per thread

kernel void mine_randomx(
    device const uint *header_data [[buffer(0)]],
    device const uint *target [[buffer(1)]],
    device atomic_uint *results [[buffer(2)]],
    device const uint *base_nonce_buf [[buffer(3)]],
    device uint *scratchpad_memory [[buffer(4)]],
    uint gid [[thread_position_in_grid]]
) {
    // 256KB = 65536 uint32 per thread
    // Organized as 8192 blocks of 32 bytes (8 uint32 each)
    const uint SCRATCH_WORDS = 65536;
    const uint SCRATCH_BLOCKS = 8192; // 256KB / 32 bytes
    const uint BLOCK_WORDS = 8;

    uint base_nonce = base_nonce_buf[0];
    uint nonce = base_nonce + gid;

    device uint *scratch = scratchpad_memory + gid * SCRATCH_WORDS;

    // ── Step 1: SHA-256(header + nonce) -> seed ──
    uint seed[8];
    uint hdr[20];
    for (int i = 0; i < 20; i++) hdr[i] = header_data[i];
    r_sha256_80bytes(hdr, nonce, seed);

    // ── Step 2: Fill 256KB scratchpad with iterative SHA-256 ──
    // First block = seed
    for (int i = 0; i < BLOCK_WORDS; i++) scratch[i] = seed[i];

    uint prev[8];
    for (int i = 0; i < BLOCK_WORDS; i++) prev[i] = seed[i];

    for (uint b = 1; b < SCRATCH_BLOCKS; b++) {
        uint next[8];
        r_sha256_32bytes(prev, next);
        uint offset = b * BLOCK_WORDS;
        for (int i = 0; i < BLOCK_WORDS; i++) {
            scratch[offset + i] = next[i];
            prev[i] = next[i];
        }
    }

    // ── Step 3: 64 mixing rounds with pseudo-random scratchpad reads ──
    uint state[8];
    for (int i = 0; i < 8; i++) state[i] = seed[i];

    for (uint round = 0; round < 64; round++) {
        // Derive pseudo-random scratchpad block index from state
        uint idx = state[round % 8] % SCRATCH_BLOCKS;
        uint sp_offset = idx * BLOCK_WORDS;

        // XOR scratchpad block into state
        for (int i = 0; i < BLOCK_WORDS; i++) {
            state[i] ^= scratch[sp_offset + i];
        }

        // Hash the mixed state
        uint new_state[8];
        r_sha256_32bytes(state, new_state);
        for (int i = 0; i < 8; i++) state[i] = new_state[i];

        // Write back to a different scratchpad location (data-dependent write)
        uint write_idx = state[0] % SCRATCH_BLOCKS;
        uint write_offset = write_idx * BLOCK_WORDS;
        for (int i = 0; i < BLOCK_WORDS; i++) {
            scratch[write_offset + i] ^= state[i];
        }
    }

    // ── Step 4: Final SHA-256 -> compare target ──
    uint final_hash[8];
    r_sha256_32bytes(state, final_hash);

    // Compare in Bitcoin LE convention
    bool below_target = false;
    for (int i = 0; i < 8; i++) {
        uint hash_word = r_swap32(final_hash[7 - i]);
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

    // Track best share: count leading zero bits
    uint lz = 0;
    for (int i = 0; i < 8; i++) {
        uint w = r_swap32(final_hash[7 - i]);
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
    """Factory function to create a miner. Only SHA-256d is supported."""
    return MetalMiner()

    return bytes(Y)


def _salsa20_8(data_64: bytes) -> bytes:
    """Salsa20/8 core function on a 64-byte block."""
    # Interpret as 16 little-endian uint32
    x = list(struct.unpack("<16I", data_64))
    orig = list(x)

    def R(a, b):
        return ((a << b) | (a >> (32 - b))) & 0xFFFFFFFF

    for _ in range(4):  # 8 rounds = 4 double-rounds
        # Column round
        x[4] ^= R((x[0] + x[12]) & 0xFFFFFFFF, 7)
        x[8] ^= R((x[4] + x[0]) & 0xFFFFFFFF, 9)
        x[12] ^= R((x[8] + x[4]) & 0xFFFFFFFF, 13)
        x[0] ^= R((x[12] + x[8]) & 0xFFFFFFFF, 18)
        x[9] ^= R((x[5] + x[1]) & 0xFFFFFFFF, 7)
        x[13] ^= R((x[9] + x[5]) & 0xFFFFFFFF, 9)
        x[1] ^= R((x[13] + x[9]) & 0xFFFFFFFF, 13)
        x[5] ^= R((x[1] + x[13]) & 0xFFFFFFFF, 18)
        x[14] ^= R((x[10] + x[6]) & 0xFFFFFFFF, 7)
        x[2] ^= R((x[14] + x[10]) & 0xFFFFFFFF, 9)
        x[6] ^= R((x[2] + x[14]) & 0xFFFFFFFF, 13)
        x[10] ^= R((x[6] + x[2]) & 0xFFFFFFFF, 18)
        x[3] ^= R((x[15] + x[11]) & 0xFFFFFFFF, 7)
        x[7] ^= R((x[3] + x[15]) & 0xFFFFFFFF, 9)
        x[11] ^= R((x[7] + x[3]) & 0xFFFFFFFF, 13)
        x[15] ^= R((x[11] + x[7]) & 0xFFFFFFFF, 18)
        # Row round
        x[1] ^= R((x[0] + x[3]) & 0xFFFFFFFF, 7)
        x[2] ^= R((x[1] + x[0]) & 0xFFFFFFFF, 9)
        x[3] ^= R((x[2] + x[1]) & 0xFFFFFFFF, 13)
        x[0] ^= R((x[3] + x[2]) & 0xFFFFFFFF, 18)
        x[6] ^= R((x[5] + x[4]) & 0xFFFFFFFF, 7)
        x[7] ^= R((x[6] + x[5]) & 0xFFFFFFFF, 9)
        x[4] ^= R((x[7] + x[6]) & 0xFFFFFFFF, 13)
        x[5] ^= R((x[4] + x[7]) & 0xFFFFFFFF, 18)
        x[11] ^= R((x[10] + x[9]) & 0xFFFFFFFF, 7)
        x[8] ^= R((x[11] + x[10]) & 0xFFFFFFFF, 9)
        x[9] ^= R((x[8] + x[11]) & 0xFFFFFFFF, 13)
        x[10] ^= R((x[9] + x[8]) & 0xFFFFFFFF, 18)
        x[12] ^= R((x[15] + x[14]) & 0xFFFFFFFF, 7)
        x[13] ^= R((x[12] + x[15]) & 0xFFFFFFFF, 9)
        x[14] ^= R((x[13] + x[12]) & 0xFFFFFFFF, 13)
        x[15] ^= R((x[14] + x[13]) & 0xFFFFFFFF, 18)

    result = [(x[i] + orig[i]) & 0xFFFFFFFF for i in range(16)]
    return struct.pack("<16I", *result)


def _scrypt_romix(B, N, r):
    """Scrypt ROMix step: sequentially memory-hard."""
    block_size = 128 * r
    V = []
    X = bytearray(B)

    for i in range(N):
        V.append(bytes(X))
        X = bytearray(_scrypt_blockmix_salsa8(bytes(X), r))

    for i in range(N):
        # j = Integerify(X) mod N
        j = struct.unpack("<I", X[64 * (2 * r - 1) : 64 * (2 * r - 1) + 4])[0] % N
        # X = X XOR V[j]
        vj = V[j]
        for k in range(block_size):
            X[k] ^= vj[k]
        X = bytearray(_scrypt_blockmix_salsa8(bytes(X), r))

    return bytes(X)


def scrypt_hash(header: bytes, N=1024, r=1, p=1) -> bytes:
    """
    Compute Scrypt hash of an 80-byte block header.
    Returns 32-byte hash.
    Litecoin parameters: N=1024, r=1, p=1.
    """
    # Step 1: PBKDF2-HMAC-SHA256(password=header, salt=header, c=1, dkLen=128*r*p)
    dk_len = 128 * r * p
    B = hashlib.pbkdf2_hmac("sha256", header, header, 1, dklen=dk_len)

    # Step 2: ROMix each 128*r block
    blocks = []
    block_size = 128 * r
    for i in range(p):
        block_i = B[i * block_size : (i + 1) * block_size]
        blocks.append(_scrypt_romix(block_i, N, r))
    B_prime = b"".join(blocks)

    # Step 3: PBKDF2-HMAC-SHA256(password=header, salt=B', c=1, dkLen=32)
    return hashlib.pbkdf2_hmac("sha256", header, B_prime, 1, dklen=32)


class ScryptMiner:
    """
    GPU-accelerated Scrypt miner for Litecoin-family coins.
    Falls back to CPU mining if Metal is unavailable.

    Scrypt parameters: N=1024, r=1, p=1 (Litecoin standard).
    ROMix requires 128KB per thread, so threadgroup size is limited.
    """

    # N=1024, r=1 => 128KB per thread => 32768 uint32 per thread
    SCRYPT_N = 1024
    SCRYPT_V_WORDS_PER_THREAD = 1024 * 32  # N * 32 uint32

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
                logger.warning("No Metal device found for Scrypt, falling back to CPU")
                self.use_gpu = False
                return

            self.command_queue = self.device.newCommandQueue()

            options = Metal.MTLCompileOptions.alloc().init()
            library, error = self.device.newLibraryWithSource_options_error_(
                SCRYPT_SHADER_SOURCE, options, None
            )
            if error:
                logger.error(f"Scrypt Metal shader compilation error: {error}")
                self.use_gpu = False
                return

            func = library.newFunctionWithName_("mine_scrypt")
            if func is None:
                logger.error("Could not find mine_scrypt function in shader")
                self.use_gpu = False
                return

            self.pipeline_state, error = (
                self.device.newComputePipelineStateWithFunction_error_(func, None)
            )
            if error:
                logger.error(f"Scrypt pipeline state creation error: {error}")
                self.use_gpu = False
                return

            self._initialized = True
            logger.info(f"Scrypt Metal initialized: {self.device.name()}")
        except Exception as e:
            logger.error(f"Scrypt Metal init failed: {e}")
            self.use_gpu = False

    @property
    def gpu_name(self) -> str:
        if self.device:
            return self.device.name()
        return "CPU (Scrypt)"

    def _target_to_le_uints(self, target_int: int) -> list:
        """
        Convert a 256-bit target integer to 8 uint32 words in the
        Bitcoin LE convention used by the shader.
        """
        target_be = target_int.to_bytes(32, byteorder="big")
        target_le = target_be[::-1]
        words = []
        for i in range(7, -1, -1):
            w = struct.unpack("<I", target_le[i * 4 : (i + 1) * 4])[0]
            words.append(w)
        return words

    def mine_range_gpu(
        self, header_data: bytes, target_int: int, base_nonce: int, count: int
    ) -> Optional[int]:
        """
        Mine a range of nonces on the GPU using Scrypt.
        Returns the winning nonce or None.

        Each thread needs 128KB for the ROMix V-table, so we limit the
        dispatch size and threadgroup size to avoid exceeding GPU memory.
        """
        if not self._initialized:
            return self.mine_range_cpu(header_data, target_int, base_nonce, count)

        # Guard: clamp to uint32 range
        base_nonce = base_nonce & 0xFFFFFFFF
        max_count = 0xFFFFFFFF - base_nonce + 1
        count = min(count, max_count)
        if count <= 0:
            return None

        # Prepare header as 20 big-endian uint32
        header_uints = []
        for i in range(0, 80, 4):
            val = struct.unpack(">I", header_data[i : i + 4])[0]
            header_uints.append(val)

        target_uints = self._target_to_le_uints(target_int)

        # Limit batch size based on GPU max buffer length and memory budget.
        # 128KB per thread for V-table. Also respect device.maxBufferLength().
        max_buf_bytes = (
            self.device.maxBufferLength()
            if hasattr(self.device, "maxBufferLength")
            else 512 * 1024 * 1024
        )
        v_bytes_per_thread = self.SCRYPT_V_WORDS_PER_THREAD * 4  # 128KB
        max_threads_for_memory = min(4096, max_buf_bytes // v_bytes_per_thread)
        max_threads_for_memory = max(1, max_threads_for_memory)

        remaining = count
        current_base = base_nonce

        while remaining > 0:
            batch = min(remaining, max_threads_for_memory)

            # Create Metal buffers
            header_packed = struct.pack("=" + "I" * 20, *header_uints)
            header_buf = self.device.newBufferWithBytes_length_options_(
                header_packed, len(header_packed), Metal.MTLResourceStorageModeShared
            )

            target_packed = struct.pack("=" + "I" * 8, *target_uints)
            target_buf = self.device.newBufferWithBytes_length_options_(
                target_packed, len(target_packed), Metal.MTLResourceStorageModeShared
            )

            results_packed = struct.pack("=IIII", 0, 0, 0, 0)
            results_buf = self.device.newBufferWithBytes_length_options_(
                results_packed, len(results_packed), Metal.MTLResourceStorageModeShared
            )

            nonce_packed = struct.pack("=I", current_base & 0xFFFFFFFF)
            nonce_buf = self.device.newBufferWithBytes_length_options_(
                nonce_packed, len(nonce_packed), Metal.MTLResourceStorageModeShared
            )

            # V memory: N * 32 uint32 per thread * 4 bytes = 128KB per thread
            v_buffer_size = batch * v_bytes_per_thread
            v_buf = self.device.newBufferWithLength_options_(
                v_buffer_size, Metal.MTLResourceStorageModePrivate
            )
            if v_buf is None:
                logger.error(
                    f"Scrypt V buffer allocation failed: "
                    f"{v_buffer_size / 1024 / 1024:.1f} MB for {batch} threads"
                )
                raise RuntimeError(
                    f"Metal buffer allocation failed ({v_buffer_size} bytes)"
                )

            # Dispatch
            command_buffer = self.command_queue.commandBuffer()
            encoder = command_buffer.computeCommandEncoder()
            encoder.setComputePipelineState_(self.pipeline_state)
            encoder.setBuffer_offset_atIndex_(header_buf, 0, 0)
            encoder.setBuffer_offset_atIndex_(target_buf, 0, 1)
            encoder.setBuffer_offset_atIndex_(results_buf, 0, 2)
            encoder.setBuffer_offset_atIndex_(nonce_buf, 0, 3)
            encoder.setBuffer_offset_atIndex_(v_buf, 0, 4)

            # Limit threadgroup size due to memory pressure
            max_tg = self.pipeline_state.maxTotalThreadsPerThreadgroup()
            thread_group_size = Metal.MTLSizeMake(min(max_tg, 32), 1, 1)
            grid_size = Metal.MTLSizeMake(batch, 1, 1)

            encoder.dispatchThreads_threadsPerThreadgroup_(grid_size, thread_group_size)
            encoder.endEncoding()
            command_buffer.commit()
            command_buffer.waitUntilCompleted()

            # Check for GPU errors
            status = command_buffer.status()
            if status == 5:  # MTLCommandBufferStatusError
                error = command_buffer.error()
                error_msg = str(error) if error else "Unknown GPU error"
                logger.error(f"Scrypt GPU command buffer error: {error_msg}")
                raise RuntimeError(f"Scrypt Metal GPU error: {error_msg}")

            # Read results
            result_ptr = results_buf.contents()
            result_bytes = result_ptr.as_buffer(16)
            found, winning_nonce, best_zeros, best_nonce = struct.unpack(
                "=IIII", result_bytes
            )

            with self._hashcount_lock:
                self._hashcount += batch
                if best_zeros > self.best_share_bits:
                    self.best_share_bits = best_zeros

            if found:
                return winning_nonce

            remaining -= batch
            current_base = (current_base + batch) & 0xFFFFFFFF

        return None

    def mine_range_cpu(
        self, header_data: bytes, target_int: int, base_nonce: int, count: int
    ) -> Optional[int]:
        """CPU fallback mining using Scrypt. Returns winning nonce or None."""
        for n in range(count):
            nonce = (base_nonce + n) & 0xFFFFFFFF
            test_header = header_data[:76] + struct.pack("<I", nonce)
            hash_result = scrypt_hash(test_header)
            hash_int = int.from_bytes(hash_result, byteorder="little")

            # Track best share bits
            lz = 256 - hash_int.bit_length() if hash_int > 0 else 256
            if lz > self.best_share_bits:
                self.best_share_bits = lz

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


# ─────────────────────────────────────────────────────────────────────
# RandomX mining (GPU-accelerated approximation with Metal, CPU fallback)
# ─────────────────────────────────────────────────────────────────────
#
# RandomX is extremely complex (custom VM, random programs, etc.).
# A full implementation requires the randomx C library.
# The GPU shader implements a memory-hard hash that approximates
# RandomX's characteristics:
#   1. SHA-256(header+nonce) -> seed
#   2. Fill 256KB scratchpad with iterative SHA-256
#   3. 64 mixing rounds with pseudo-random scratchpad reads/writes
#   4. Final SHA-256 -> compare target
#
# The CPU fallback uses a simplified simulation with SHA-512 + 2MB scratchpad.


def _randomx_hash_simulate(header: bytes) -> bytes:
    """
    Simulated RandomX hash. Uses iterative SHA-512 with a 2MB
    scratchpad to approximate RandomX's memory-hardness and
    computation cost, producing a 32-byte hash.
    """
    # Initial state from header
    seed = hashlib.sha512(header).digest()

    # Build 2MB scratchpad (similar to RandomX's dataset access pattern)
    SCRATCHPAD_SIZE = 2 * 1024 * 1024  # 2MB
    BLOCK_SIZE = 64  # SHA-512 output
    num_blocks = SCRATCHPAD_SIZE // BLOCK_SIZE
    scratchpad = bytearray(SCRATCHPAD_SIZE)

    # Fill scratchpad
    current = seed
    for i in range(num_blocks):
        offset = i * BLOCK_SIZE
        scratchpad[offset : offset + BLOCK_SIZE] = current
        current = hashlib.sha512(current).digest()

    # Mix phase: simulate RandomX's random memory access pattern
    state = bytearray(seed)
    for i in range(64):
        # Pseudo-random scratchpad index from current state
        idx = struct.unpack_from("<I", state, i % 60)[0] % (num_blocks - 1)
        sp_block = scratchpad[idx * BLOCK_SIZE : (idx + 1) * BLOCK_SIZE]
        # XOR mix
        for j in range(BLOCK_SIZE):
            state[j] ^= sp_block[j]
        state = bytearray(hashlib.sha512(bytes(state)).digest())

    # Final hash
    return hashlib.sha256(bytes(state)).digest()


class RandomXMiner:
    """
    GPU-accelerated RandomX-approximation miner for Monero-family coins.
    Falls back to CPU mining if Metal is unavailable.

    True RandomX is fundamentally CPU-bound. The GPU shader implements
    a memory-hard hash (iterative SHA-256 + 256KB scratchpad with
    pseudo-random access) that approximates RandomX's characteristics.
    """

    # 256KB per thread = 65536 uint32
    SCRATCH_WORDS_PER_THREAD = 65536

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
                logger.warning("No Metal device found for RandomX, falling back to CPU")
                self.use_gpu = False
                return

            self.command_queue = self.device.newCommandQueue()

            options = Metal.MTLCompileOptions.alloc().init()
            library, error = self.device.newLibraryWithSource_options_error_(
                RANDOMX_SHADER_SOURCE, options, None
            )
            if error:
                logger.error(f"RandomX Metal shader compilation error: {error}")
                self.use_gpu = False
                return

            func = library.newFunctionWithName_("mine_randomx")
            if func is None:
                logger.error("Could not find mine_randomx function in shader")
                self.use_gpu = False
                return

            self.pipeline_state, error = (
                self.device.newComputePipelineStateWithFunction_error_(func, None)
            )
            if error:
                logger.error(f"RandomX pipeline state creation error: {error}")
                self.use_gpu = False
                return

            self._initialized = True
            logger.info(f"RandomX Metal initialized: {self.device.name()}")
        except Exception as e:
            logger.error(f"RandomX Metal init failed: {e}")
            self.use_gpu = False

    @property
    def gpu_name(self) -> str:
        if self.device:
            return self.device.name()
        return "CPU (RandomX)"

    def _target_to_le_uints(self, target_int: int) -> list:
        """
        Convert a 256-bit target integer to 8 uint32 words in the
        Bitcoin LE convention used by the shader.
        """
        target_be = target_int.to_bytes(32, byteorder="big")
        target_le = target_be[::-1]
        words = []
        for i in range(7, -1, -1):
            w = struct.unpack("<I", target_le[i * 4 : (i + 1) * 4])[0]
            words.append(w)
        return words

    def mine_range_gpu(
        self, header_data: bytes, target_int: int, base_nonce: int, count: int
    ) -> Optional[int]:
        """
        Mine a range of nonces on the GPU using RandomX-approximation.
        Returns the winning nonce or None.

        Each thread needs 256KB for the scratchpad, so dispatch size is limited.
        """
        if not self._initialized:
            return self.mine_range_cpu(header_data, target_int, base_nonce, count)

        # Guard: clamp to uint32 range
        base_nonce = base_nonce & 0xFFFFFFFF
        max_count = 0xFFFFFFFF - base_nonce + 1
        count = min(count, max_count)
        if count <= 0:
            return None

        # Prepare header as 20 big-endian uint32
        header_uints = []
        for i in range(0, 80, 4):
            val = struct.unpack(">I", header_data[i : i + 4])[0]
            header_uints.append(val)

        target_uints = self._target_to_le_uints(target_int)

        # Limit batch size based on GPU max buffer length and memory budget.
        # 256KB per thread for scratchpad. Also respect device.maxBufferLength().
        max_buf_bytes = (
            self.device.maxBufferLength()
            if hasattr(self.device, "maxBufferLength")
            else 512 * 1024 * 1024
        )
        scratch_bytes_per_thread = self.SCRATCH_WORDS_PER_THREAD * 4  # 256KB
        max_threads_for_memory = min(2048, max_buf_bytes // scratch_bytes_per_thread)
        max_threads_for_memory = max(1, max_threads_for_memory)

        remaining = count
        current_base = base_nonce

        while remaining > 0:
            batch = min(remaining, max_threads_for_memory)

            # Create Metal buffers
            header_packed = struct.pack("=" + "I" * 20, *header_uints)
            header_buf = self.device.newBufferWithBytes_length_options_(
                header_packed, len(header_packed), Metal.MTLResourceStorageModeShared
            )

            target_packed = struct.pack("=" + "I" * 8, *target_uints)
            target_buf = self.device.newBufferWithBytes_length_options_(
                target_packed, len(target_packed), Metal.MTLResourceStorageModeShared
            )

            results_packed = struct.pack("=IIII", 0, 0, 0, 0)
            results_buf = self.device.newBufferWithBytes_length_options_(
                results_packed, len(results_packed), Metal.MTLResourceStorageModeShared
            )

            nonce_packed = struct.pack("=I", current_base & 0xFFFFFFFF)
            nonce_buf = self.device.newBufferWithBytes_length_options_(
                nonce_packed, len(nonce_packed), Metal.MTLResourceStorageModeShared
            )

            # Scratchpad memory: 65536 uint32 per thread * 4 bytes = 256KB per thread
            scratch_buffer_size = batch * scratch_bytes_per_thread
            scratch_buf = self.device.newBufferWithLength_options_(
                scratch_buffer_size, Metal.MTLResourceStorageModePrivate
            )
            if scratch_buf is None:
                logger.error(
                    f"RandomX scratch buffer allocation failed: "
                    f"{scratch_buffer_size / 1024 / 1024:.1f} MB for {batch} threads"
                )
                raise RuntimeError(
                    f"Metal buffer allocation failed ({scratch_buffer_size} bytes)"
                )

            # Dispatch
            command_buffer = self.command_queue.commandBuffer()
            encoder = command_buffer.computeCommandEncoder()
            encoder.setComputePipelineState_(self.pipeline_state)
            encoder.setBuffer_offset_atIndex_(header_buf, 0, 0)
            encoder.setBuffer_offset_atIndex_(target_buf, 0, 1)
            encoder.setBuffer_offset_atIndex_(results_buf, 0, 2)
            encoder.setBuffer_offset_atIndex_(nonce_buf, 0, 3)
            encoder.setBuffer_offset_atIndex_(scratch_buf, 0, 4)

            # Limit threadgroup size due to memory pressure
            max_tg = self.pipeline_state.maxTotalThreadsPerThreadgroup()
            thread_group_size = Metal.MTLSizeMake(min(max_tg, 32), 1, 1)
            grid_size = Metal.MTLSizeMake(batch, 1, 1)

            encoder.dispatchThreads_threadsPerThreadgroup_(grid_size, thread_group_size)
            encoder.endEncoding()
            command_buffer.commit()
            command_buffer.waitUntilCompleted()

            # Check for GPU errors
            status = command_buffer.status()
            if status == 5:  # MTLCommandBufferStatusError
                error = command_buffer.error()
                error_msg = str(error) if error else "Unknown GPU error"
                logger.error(f"RandomX GPU command buffer error: {error_msg}")
                raise RuntimeError(f"RandomX Metal GPU error: {error_msg}")

            # Read results
            result_ptr = results_buf.contents()
            result_bytes = result_ptr.as_buffer(16)
            found, winning_nonce, best_zeros, best_nonce = struct.unpack(
                "=IIII", result_bytes
            )

            with self._hashcount_lock:
                self._hashcount += batch
                if best_zeros > self.best_share_bits:
                    self.best_share_bits = best_zeros

            if found:
                return winning_nonce

            remaining -= batch
            current_base = (current_base + batch) & 0xFFFFFFFF

        return None

    def mine_range_cpu(
        self, header_data: bytes, target_int: int, base_nonce: int, count: int
    ) -> Optional[int]:
        """CPU fallback mining using RandomX (simulated). Returns winning nonce or None."""
        for n in range(count):
            nonce = (base_nonce + n) & 0xFFFFFFFF
            test_header = header_data[:76] + struct.pack("<I", nonce)
            hash_result = _randomx_hash_simulate(test_header)
            hash_int = int.from_bytes(hash_result, byteorder="little")

            # Track best share bits
            lz = 256 - hash_int.bit_length() if hash_int > 0 else 256
            if lz > self.best_share_bits:
                self.best_share_bits = lz

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


def create_miner(algorithm: str = "SHA-256d"):
    """Factory function to create the appropriate miner for the given algorithm."""
    if algorithm == "Scrypt":
        return ScryptMiner()
    elif algorithm == "RandomX":
        return RandomXMiner()
    else:
        return MetalMiner()
