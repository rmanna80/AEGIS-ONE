"""This device's signing key (Ed25519), for incident packages.

Each computer running AEGIS gets its own key the first time it is needed. The private
key stays on that computer, readable by its owner only, outside the repository
(default ``~/.config/aegis/device_ed25519.pem``; ``AEGIS_DEVICE_KEY`` changes it). It
is never written to a manifest, a log, or a package.

What a signature shows: that the holder of this key produced these exact bytes. It
does not show that the measurements inside were accurate, and it is worth no more
than the protection of the key. Someone who can read the key file can sign anything.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from aegis.replay.evidence import SIGNATURE_NAME

DEFAULT_KEY_PATH = Path.home() / ".config" / "aegis" / "device_ed25519.pem"
SIGNATURE_SCHEMA = "aegis.manifest-signature/1"


def fingerprint(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    digest = hashlib.sha256(raw).hexdigest()
    return ":".join(digest[i : i + 4] for i in range(0, 32, 4))  # 128 bits, readable


class DeviceKey:
    def __init__(self, private_key: Ed25519PrivateKey, path: Path | None = None) -> None:
        self._key = private_key
        self.path = path
        self.public_key = private_key.public_key()
        self.fingerprint = fingerprint(self.public_key)

    @classmethod
    def load_or_create(cls, path: Path = DEFAULT_KEY_PATH) -> DeviceKey:
        if path.is_file():
            if path.stat().st_mode & 0o077:
                raise PermissionError(
                    f"{path} can be read by other users. Fix with: chmod 600 {path}"
                )
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError(f"{path} is not an Ed25519 key")
            return cls(key, path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        # Created readable by the owner only, before any key material is written.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(pem)
        return cls(key, path)

    def public_pem(self) -> str:
        return self.public_key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()

    def describe(self) -> dict[str, str]:
        """What may be published about this key: never the private part."""
        return {"algorithm": "ed25519", "fingerprint": self.fingerprint}

    def sign_manifest(self, folder: Path, manifest_name: str = "MANIFEST.json") -> dict[str, Any]:
        data = (folder / manifest_name).read_bytes()
        document = {
            "schema": SIGNATURE_SCHEMA,
            "algorithm": "ed25519",
            "signed_file": manifest_name,
            "signed_sha256": hashlib.sha256(data).hexdigest(),
            "public_key": base64.b64encode(
                self.public_key.public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )
            ).decode(),
            "fingerprint": self.fingerprint,
            "signature": base64.b64encode(self._key.sign(data)).decode(),
            "meaning": "The holder of this key produced this manifest. It does not show that "
            "the measurements were accurate.",
        }
        (folder / SIGNATURE_NAME).write_text(json.dumps(document, indent=2))
        return document


def verify_signature(folder: Path, trusted_fingerprint: str | None = None) -> dict[str, Any]:
    """Check MANIFEST.sig against MANIFEST.json. Without a trusted fingerprint it can only
    say *which* key signed: anyone can make a key and sign a forged package with it."""
    path = folder / SIGNATURE_NAME
    if not path.is_file():
        return {"signed": False, "valid": None, "note": "the package is not signed"}
    try:
        document = json.loads(path.read_text())
        raw = base64.b64decode(document["public_key"])
        public_key = Ed25519PublicKey.from_public_bytes(raw)
        signature = base64.b64decode(document["signature"])
        data = (folder / document["signed_file"]).read_bytes()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"signed": True, "valid": False, "problem": f"unreadable signature: {exc}"}
    found = fingerprint(public_key)
    result: dict[str, Any] = {"signed": True, "fingerprint": found}
    if found != document.get("fingerprint"):
        return {**result, "valid": False, "problem": "the fingerprint does not match the key"}
    try:
        public_key.verify(signature, data)
    except InvalidSignature:
        return {**result, "valid": False, "problem": "the signature does not match the manifest"}
    result["valid"] = True
    if trusted_fingerprint is None:
        result["key_trusted"] = None
        result["note"] = (
            "Signed by the key above. Compare its fingerprint with the device's own "
            "(python scripts/device_key.py on that device) before trusting it."
        )
    else:
        result["key_trusted"] = found == trusted_fingerprint.strip().lower()
        if not result["key_trusted"]:
            result["problem"] = "signed by a different key than the trusted one"
    return result
