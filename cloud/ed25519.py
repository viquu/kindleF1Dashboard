#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pure-Python Ed25519 (RFC 8032), stdlib only — no `cryptography`, no PyNaCl.

Canonical implementation for this repo. Used in two places, kept in sync:
  1. cloud/sign_release.py — Muse-side signing (this file, full sign+verify).
  2. kindle/f1dash.py — Kindle-side verification ONLY (verify subset embedded
     there, because the Kindle receives f1dash.py as a single file; see the
     header comment above _ed25519_verify in f1dash.py).

The Kindle runs Python 3.9 with no third-party crypto, so this must stay
stdlib-only (hashlib). Verification of a ~112KB file takes ~1-2s on a
desktop; on the PW1's slow ARM it is slower but still well inside the
5-minute Wi-Fi window of one Always On cycle.

Reference: D. J. Bernstein et al., public-domain ed25519.py, as in RFC 8032.
Test vectors below are RFC 8032 §7.1 tests 1 and 2.
"""
import hashlib
import os

B = 256
Q = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493


def _H(m):
    return hashlib.sha512(m).digest()


def _inv(x):
    return pow(x, Q - 2, Q)


_D = -121665 * _inv(121666) % Q
_I = pow(2, (Q - 1) // 4, Q)


def _xrecover(y):
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (Q + 3) // 8, Q)
    if (x * x - xx) % Q != 0:
        x = (x * _I) % Q
    if x % 2 != 0:
        x = Q - x
    return x


_By = 4 * _inv(5)
_Bx = _xrecover(_By)
_BASE = [_Bx % Q, _By % Q]


def _edwards(P, Qpt):
    x1, y1 = P
    x2, y2 = Qpt
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _D * x1 * x2 * y1 * y2)
    # Curve is twisted Edwards with a = -1, so y3 = (y1*y2 - a*x1*x2) /
    # (1 - d*x1*x2*y1*y2) = (y1*y2 + x1*x2) / (...). A minus here puts
    # every sum off the curve (caught by cross-checking vs `cryptography`).
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _D * x1 * x2 * y1 * y2)
    return [x3 % Q, y3 % Q]


def _scalarmult(P, e):
    if e == 0:
        return [0, 1]
    Qpt = _scalarmult(P, e // 2)
    Qpt = _edwards(Qpt, Qpt)
    if e & 1:
        Qpt = _edwards(Qpt, P)
    return Qpt


def _encodeint(y):
    return y.to_bytes(32, "little")


def _encodepoint(P):
    x, y = P
    bits = [(y >> i) & 1 for i in range(B - 1)] + [x & 1]
    return bytes([sum(bits[i * 8 + j] << j for j in range(8))
                  for i in range(B // 8)])


def _bit(h, i):
    return (h[i // 8] >> (i % 8)) & 1


def _decodeint(s):
    return int.from_bytes(s, "little")


def _decodepoint(s):
    y = sum(2 ** i * _bit(s, i) for i in range(0, 255))
    x = _xrecover(y)
    if x & 1 != _bit(s, 255):
        x = Q - x
    P = [x, y]
    if not _isoncurve(P):
        raise ValueError("point not on curve")
    return P


def _isoncurve(P):
    x, y = P
    return (-x * x + y * y - 1 - _D * x * x * y * y) % Q == 0


def _Hint(m):
    return int.from_bytes(_H(m), "little")


def publickey(seed):
    """32-byte seed -> 32-byte public key."""
    h = _H(seed)
    a = 2 ** 254 + sum(2 ** i * _bit(h, i) for i in range(3, 254))
    return _encodepoint(_scalarmult(_BASE, a))


def signature(message, seed, pubkey):
    """Sign message bytes with the 32-byte seed. Returns 64-byte signature."""
    h = _H(seed)
    a = 2 ** 254 + sum(2 ** i * _bit(h, i) for i in range(3, 254))
    r = _Hint(h[32:] + message)
    R = _scalarmult(_BASE, r)
    S = (r + _Hint(_encodepoint(R) + pubkey + message) * a) % L
    return _encodepoint(R) + _encodeint(S)


def verify(signature_bytes, message, pubkey):
    """True iff signature_bytes is a valid Ed25519 signature of message
    under pubkey. Never raises for malformed input — returns False."""
    try:
        if len(signature_bytes) != 64 or len(pubkey) != 32:
            return False
        R = _decodepoint(signature_bytes[:32])
        A = _decodepoint(pubkey)
        S = _decodeint(signature_bytes[32:])
        if S >= L:
            return False
        h = _Hint(signature_bytes[:32] + pubkey + message)
        return _scalarmult(_BASE, S) == _edwards(R, _scalarmult(A, h))
    except Exception:
        return False


def generate_keypair():
    """Returns (seed_bytes, pubkey_bytes) from os.urandom."""
    seed = os.urandom(32)
    return seed, publickey(seed)


# RFC 8032 §7.1 test vectors 1 and 2 (secret, public, message, signature).
_TEST_VECTORS = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
     "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
     "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
]


def selftest():
    """RFC 8032 vectors + a fresh-key round-trip. Returns True if all pass."""
    ok = True
    for i, (sec, pub, msg, sig) in enumerate(_TEST_VECTORS, 1):
        seed = bytes.fromhex(sec)
        pubkey = bytes.fromhex(pub)
        message = bytes.fromhex(msg)
        expected = bytes.fromhex(sig)
        derived = publickey(seed)
        if derived != pubkey:
            print("vector %d: publickey mismatch" % i)
            ok = False
        if signature(message, seed, pubkey) != expected:
            print("vector %d: signature mismatch" % i)
            ok = False
        if not verify(expected, message, pubkey):
            print("vector %d: verify failed" % i)
            ok = False
        if verify(expected, message + b"x", pubkey):
            print("vector %d: tampered message accepted" % i)
            ok = False
    seed, pubkey = generate_keypair()
    msg = os.urandom(1000)
    sig = signature(msg, seed, pubkey)
    if not verify(sig, msg, pubkey) or verify(sig, msg + b"x", pubkey):
        print("round-trip failed")
        ok = False
    print("ed25519 selftest: %s" % ("PASS" if ok else "FAIL"))
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
