"""Device signing key and signed incident packages."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from aegis.identity import DeviceKey, verify_signature
from aegis.replay.evidence import SIGNATURE_NAME, verify_manifest, write_manifest


def package(folder: Path) -> None:
    folder.mkdir()
    (folder / "events.jsonl").write_text('{"kind":"state"}\n')
    write_manifest(folder, "test")


def test_the_key_is_created_owner_only_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "keys" / "device.pem"
    first = DeviceKey.load_or_create(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert DeviceKey.load_or_create(path).fingerprint == first.fingerprint
    assert "PRIVATE" not in first.public_pem() and set(first.describe()) == {
        "algorithm",
        "fingerprint",
    }


def test_a_key_others_can_read_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "device.pem"
    DeviceKey.load_or_create(path)
    os.chmod(path, 0o644)
    with pytest.raises(PermissionError, match="chmod 600"):
        DeviceKey.load_or_create(path)


def test_a_signed_package_verifies_and_the_signature_file_is_not_a_stray_file(
    tmp_path: Path,
) -> None:
    key = DeviceKey.load_or_create(tmp_path / "device.pem")
    folder = tmp_path / "p"
    package(folder)
    key.sign_manifest(folder)
    assert verify_manifest(folder)["valid"]  # MANIFEST.sig is not an unlisted extra
    result = verify_signature(folder, key.fingerprint)
    assert result["valid"] and result["key_trusted"] and result["fingerprint"] == key.fingerprint
    untrusted = verify_signature(folder)
    assert untrusted["valid"] and untrusted["key_trusted"] is None  # says which key, no more


def test_editing_the_manifest_breaks_the_signature(tmp_path: Path) -> None:
    key = DeviceKey.load_or_create(tmp_path / "device.pem")
    folder = tmp_path / "p"
    package(folder)
    key.sign_manifest(folder)
    (folder / "events.jsonl").write_text("forged\n")
    write_manifest(folder, "test")  # a forger rewrites the manifest to match
    assert verify_manifest(folder)["valid"]  # the hashes alone are fooled...
    assert verify_signature(folder, key.fingerprint)["valid"] is False  # ...the signature is not


def test_re_signing_with_another_key_is_caught_only_against_the_trusted_key(
    tmp_path: Path,
) -> None:
    device = DeviceKey.load_or_create(tmp_path / "device.pem")
    forger = DeviceKey.load_or_create(tmp_path / "forger.pem")
    folder = tmp_path / "p"
    package(folder)
    device.sign_manifest(folder)
    (folder / "events.jsonl").write_text("forged\n")
    write_manifest(folder, "test")
    forger.sign_manifest(folder)
    assert verify_signature(folder)["valid"]  # a valid signature, by someone
    trusted = verify_signature(folder, device.fingerprint)
    assert trusted["key_trusted"] is False and "different key" in trusted["problem"]


def test_a_tampered_signature_file_is_reported(tmp_path: Path) -> None:
    key = DeviceKey.load_or_create(tmp_path / "device.pem")
    folder = tmp_path / "p"
    package(folder)
    key.sign_manifest(folder)
    document = json.loads((folder / SIGNATURE_NAME).read_text())
    document["fingerprint"] = "0000:0000:0000:0000:0000:0000:0000:0000"
    (folder / SIGNATURE_NAME).write_text(json.dumps(document))
    assert verify_signature(folder)["valid"] is False
    (folder / SIGNATURE_NAME).write_text("not json")
    assert verify_signature(folder)["valid"] is False
    (folder / SIGNATURE_NAME).unlink()
    assert verify_signature(folder) == {
        "signed": False,
        "valid": None,
        "note": "the package is not signed",
    }
