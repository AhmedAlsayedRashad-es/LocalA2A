"""Ed25519 signatures (RFC 8032), pure Python, standard library only.

Used to sign receiver update packages on the hub and to check them on each PC.
Slow (some ms per operation) but small and dependency free; it signs one manifest per release.

API:
  generate_seed()            -> 32 random bytes (the private key)
  public_key(seed)           -> 32-byte public key
  sign(seed, msg)            -> 64-byte signature
  verify(pub, msg, sig)      -> True / False (never raises)
"""
import hashlib
import os

# Curve constants (RFC 8032 section 5.1)
p = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
d = -121665 * pow(121666, p - 2, p) % p
SQRT_M1 = pow(2, (p - 1) // 4, p)


def _sha512(s):
    return hashlib.sha512(s).digest()


def _sha512_modq(s):
    return int.from_bytes(_sha512(s), "little") % L


# Points in extended coordinates (X, Y, Z, T), x = X/Z, y = Y/Z, x*y = T/Z
def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % p
    C = 2 * P[3] * Q[3] * d % p
    D = 2 * P[2] * Q[2] % p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % p, G * H % p, F * G % p, E * H % p)


def _mul(s, P):
    Q = (0, 1, 1, 0)  # neutral element
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q):
    if (P[0] * Q[2] - Q[0] * P[2]) % p != 0:
        return False
    if (P[1] * Q[2] - Q[1] * P[2]) % p != 0:
        return False
    return True


def _recover_x(y, sign):
    if y >= p:
        return None
    x2 = (y * y - 1) * pow(d * y * y + 1, p - 2, p)
    if x2 == 0:
        if sign:
            return None
        return 0
    x = pow(x2, (p + 3) // 8, p)
    if (x * x - x2) % p != 0:
        x = x * SQRT_M1 % p
    if (x * x - x2) % p != 0:
        return None
    if (x & 1) != sign:
        x = p - x
    return x


_gy = 4 * pow(5, p - 2, p) % p
_gx = _recover_x(_gy, 0)
G = (_gx, _gy, 1, _gx * _gy % p)


def _compress(P):
    zinv = pow(P[2], p - 2, p)
    x = P[0] * zinv % p
    y = P[1] * zinv % p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % p)


def _secret_expand(seed):
    if len(seed) != 32:
        raise ValueError("bad seed length")
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def generate_seed():
    return os.urandom(32)


def public_key(seed):
    a, _ = _secret_expand(bytes(seed))
    return _compress(_mul(a, G))


def sign(seed, msg):
    seed, msg = bytes(seed), bytes(msg)
    a, prefix = _secret_expand(seed)
    A = _compress(_mul(a, G))
    r = _sha512_modq(prefix + msg)
    R = _compress(_mul(r, G))
    h = _sha512_modq(R + A + msg)
    s = (r + h * a) % L
    return R + int.to_bytes(s, 32, "little")


def verify(pub, msg, sig):
    try:
        pub, msg, sig = bytes(pub), bytes(msg), bytes(sig)
        if len(pub) != 32 or len(sig) != 64:
            return False
        A = _decompress(pub)
        if A is None:
            return False
        Rs = sig[:32]
        R = _decompress(Rs)
        if R is None:
            return False
        s = int.from_bytes(sig[32:], "little")
        if s >= L:
            return False
        h = _sha512_modq(Rs + pub + msg)
        sB = _mul(s, G)
        hA = _mul(h, A)
        return _equal(sB, _add(R, hA))
    except Exception:
        return False
