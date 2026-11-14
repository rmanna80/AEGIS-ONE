"""Tamper-evident evidence manifests.

A manifest lists every file of a package with its SHA-256 and size. Verifying it
shows whether the files are the ones that were packaged. What it cannot show:

* that the recorded measurements were accurate, or their source trustworthy;
* that nothing was altered before packaging;
* that the manifest itself was not replaced along with the files. For that, compare
  the manifest's own hash with a copy kept elsewhere: AEGIS writes it into the
  session's hash-chained audit log, and prints it at packaging time.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

MANIFEST_NAME = "MANIFEST.json"
MANIFEST_SCHEMA = "aegis.evidence-manifest/1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_manifest(folder: Path, description: str) -> dict[str, Any]:
    """Hash every file under ``folder`` (except the manifest) and write the manifest."""
    files = {}
    for path in sorted(p for p in folder.rglob("*") if p.is_file() and p.name != MANIFEST_NAME):
        files[path.relative_to(folder).as_posix()] = {
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "description": description,
        "created_utc_s": round(time.time(), 3),
        "hash_algorithm": "sha256",
        "files": files,
    }
    text = json.dumps(manifest, indent=2, sort_keys=True)
    (folder / MANIFEST_NAME).write_text(text)
    manifest["manifest_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    return manifest


def verify_manifest(folder: Path, expected_manifest_sha256: str | None = None) -> dict[str, Any]:
    """Compare the files under ``folder`` with its manifest."""
    manifest_path = folder / MANIFEST_NAME
    if not manifest_path.is_file():
        return {"valid": False, "problems": ["the package has no MANIFEST.json"], "files": 0}
    text = manifest_path.read_text()
    problems: list[str] = []
    actual_hash = hashlib.sha256(text.encode()).hexdigest()
    if expected_manifest_sha256 is not None and actual_hash != expected_manifest_sha256:
        problems.append("the manifest itself is not the one that was recorded at packaging")
    try:
        manifest = json.loads(text)
        listed: dict[str, dict[str, Any]] = manifest["files"]
    except (ValueError, KeyError, TypeError):
        return {"valid": False, "problems": ["MANIFEST.json is unreadable"], "files": 0}
    present = {
        p.relative_to(folder).as_posix()
        for p in folder.rglob("*")
        if p.is_file() and p.name != MANIFEST_NAME
    }
    for name, entry in sorted(listed.items()):
        if name not in present:
            problems.append(f"missing: {name}")
        elif sha256_file(folder / name) != entry.get("sha256"):
            problems.append(f"modified: {name}")
    problems += [f"not in the manifest: {name}" for name in sorted(present - set(listed))]
    return {
        "valid": not problems,
        "problems": problems,
        "files": len(listed),
        "manifest_sha256": actual_hash,
        "manifest_hash_checked": expected_manifest_sha256 is not None,
        "limits": "Shows the files are unchanged since packaging. Says nothing about whether "
        "the measurements in them were accurate.",
    }
