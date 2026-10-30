"""Keys and encryption shared with the phone.

Pairing: ECDH on P-256 (built into every Android version the app supports),
HKDF-SHA256 for a 32-byte message key plus a 6-digit code that both screens
show, so the user can confirm nobody sits in between. Messages: AES-256-GCM.
The Android side implements exactly the same steps.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

INFO = b"headsup pairing v1"
AAD = b"headsup"


def new_private_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def public_b64(key: ec.EllipticCurvePrivateKey) -> str:
    """The public key as base64 of its X.509 SubjectPublicKeyInfo (Android's PublicKey.getEncoded)."""
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(der).decode()


def derive(private: ec.EllipticCurvePrivateKey, phone_pk_b64: str) -> tuple[bytes, str]:
    """The shared message key and the 6-digit code, from HA's key and the phone's public key."""
    phone_der = base64.b64decode(phone_pk_b64)
    peer = serialization.load_der_public_key(phone_der)
    if not isinstance(peer, ec.EllipticCurvePublicKey):
        raise ValueError("not an EC key")
    shared = private.exchange(ec.ECDH(), peer)
    ha_der = base64.b64decode(public_b64(private))
    salt = hashlib.sha256(phone_der + ha_der).digest()
    okm = HKDF(algorithm=hashes.SHA256(), length=36, salt=salt, info=INFO).derive(shared)
    code = int.from_bytes(okm[32:36], "big") % 1_000_000
    return okm[:32], f"{code:06d}"


def seal(key: bytes, payload: dict[str, Any]) -> str:
    """Encrypt a JSON payload: base64(12-byte nonce + ciphertext and tag)."""
    nonce = os.urandom(12)
    data = json.dumps(payload, separators=(",", ":")).encode()
    return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, data, AAD)).decode()


def unseal(key: bytes, blob: str) -> dict[str, Any]:
    raw = base64.b64decode(blob)
    return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], AAD))


def mac(key: bytes, text: str) -> str:
    return hmac.new(key, text.encode(), hashlib.sha256).hexdigest()


def mac_ok(key: bytes, text: str, given: str) -> bool:
    return hmac.compare_digest(mac(key, text), str(given))
