"""
commons/jasypt.py
─────────────────
Runtime decryption of Jasypt `ENC(...)` secrets, matching the GC platform's
jasypt-spring-boot configuration:

    algorithm  = PBEWITHHMACSHA512ANDAES_256
    salt gen   = org.jasypt.salt.RandomSaltGenerator   (16-byte salt, prepended)
    iv  gen    = org.jasypt.iv.RandomIvGenerator        (16-byte IV,   prepended)
    kdf        = PBKDF2WithHmacSHA512, keyObtentionIterations
    cipher     = AES-256/CBC/PKCS7
    output     = base64( salt | iv | ciphertext )

This lets the agent consume the SAME encrypted DB/config secrets the Java
services use — the Jasypt password stays the one master secret, and no
individual key (e.g. JWT_INTERNAL_SECRET) is ever stored in cleartext at rest.

Uses stdlib `hashlib.pbkdf2_hmac` + `cryptography` (already a dependency).
Only the platform's algorithm is supported; other Jasypt algorithms raise.
"""
from __future__ import annotations

import base64
import hashlib
import re

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_ENC_RE = re.compile(r"^\s*ENC\((?P<b64>.*)\)\s*$", re.DOTALL)
_SALT_LEN = 16
_IV_LEN = 16
_KEY_LEN = 32  # AES-256


def is_encrypted(value: str | None) -> bool:
    """True if the value is wrapped as ENC(...)."""
    return bool(value) and _ENC_RE.match(value) is not None


def decrypt(value: str, password: str, iterations: int) -> str:
    """
    Decrypt a Jasypt PBEWITHHMACSHA512ANDAES_256 value. `value` may be the raw
    base64 or the ENC(...)-wrapped form.
    """
    m = _ENC_RE.match(value)
    b64 = m.group("b64") if m else value.strip()
    raw = base64.b64decode(b64)

    salt = raw[:_SALT_LEN]
    iv = raw[_SALT_LEN : _SALT_LEN + _IV_LEN]
    ciphertext = raw[_SALT_LEN + _IV_LEN :]

    key = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, iterations, dklen=_KEY_LEN)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()

    pad_len = padded[-1]  # PKCS7
    if pad_len < 1 or pad_len > 16 or padded[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("Jasypt decrypt failed: bad padding (wrong password or iterations?)")
    return padded[:-pad_len].decode("utf-8")


def encrypt(plaintext: str, password: str, iterations: int) -> str:
    """
    Produce a Jasypt PBEWITHHMACSHA512ANDAES_256 value, wrapped as ENC(...), that
    the Java services (and decrypt() above) can read. Format: base64(salt|iv|ct),
    random 16-byte salt + 16-byte IV, PBKDF2-SHA512 key, AES-256/CBC/PKCS7.

    Uses os.urandom for salt/iv (must be unpredictable). Handy for minting new
    ENC(...) DB secrets (e.g. a JWT_SECRET row) on a host without the Jasypt jar.
    """
    import os

    salt = os.urandom(_SALT_LEN)
    iv = os.urandom(_IV_LEN)
    key = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, iterations, dklen=_KEY_LEN)

    data = plaintext.encode("utf-8")
    pad_len = 16 - (len(data) % 16)  # PKCS7
    data += bytes([pad_len]) * pad_len

    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = encryptor.update(data) + encryptor.finalize()
    return "ENC(" + base64.b64encode(salt + iv + ct).decode("ascii") + ")"


def resolve(value: str | None, password: str | None, iterations: int) -> str | None:
    """
    Return plaintext for `value`: decrypt when it is ENC(...) and a password is
    configured; otherwise pass the value through unchanged.
    """
    if not is_encrypted(value):
        return value
    if not password:
        raise ValueError(
            "An ENC(...) secret is configured but JASYPT_ENCRYPTOR_PASSWORD is not set"
        )
    return decrypt(value, password, iterations)  # type: ignore[arg-type]
