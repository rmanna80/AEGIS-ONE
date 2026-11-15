#!/usr/bin/env python3
"""List, verify and import AEGIS incident packages.

    python scripts/incident_tool.py list                           # incidents in recorded sessions
    python scripts/incident_tool.py verify <folder or .zip>        # files, manifest, signature
    python scripts/incident_tool.py verify <package> --trust <fingerprint>
    python scripts/incident_tool.py import <export.zip>            # unpack under data/generated/

Verification shows whether the files are the ones that were packaged, and which key
signed them. Pass --trust with the fingerprint of the device that should have made the
package (python scripts/device_key.py on that device) to check it was that one.
Nothing here shows that the recorded measurements were accurate.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from aegis.identity import verify_signature  # noqa: E402
from aegis.replay.evidence import verify_manifest  # noqa: E402
from aegis.replay.incidents import import_package  # noqa: E402


def show(folder: Path, trust: str | None) -> int:
    verification: dict[str, Any] = verify_manifest(folder)
    signature = verify_signature(folder, trust)
    print(f"package: {folder}")
    if verification["valid"]:
        print(f"FILES VALID: {verification['files']} files match the manifest")
    else:
        print("FILES INVALID:")
        for problem in verification["problems"]:
            print(f"  {problem}")
    print(f"manifest sha256: {verification.get('manifest_sha256')}")
    if not signature["signed"]:
        print("SIGNATURE: none (the package is not signed)")
    elif not signature["valid"]:
        print(f"SIGNATURE INVALID: {signature.get('problem')}")
    else:
        print(f"SIGNATURE VALID: signed by key {signature['fingerprint']}")
        if signature.get("key_trusted") is True:
            print("  that is the trusted key you gave")
        elif signature.get("key_trusted") is False:
            print(f"  NOT the trusted key you gave: {signature.get('problem')}")
        else:
            print(f"  {signature['note']}")
    print(verification.get("limits", ""))
    document = folder / "incident.json"
    if document.is_file():
        incident = json.loads(document.read_text())
        print(f"incident: {incident['incident_id']} - {incident['title']}")
        print(f"replay:   {incident['reproduce']['replay_this_incident']}")
    ok = (
        verification["valid"]
        and signature["valid"] is not False
        and signature.get("key_trusted") is not False
    )
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("list", "verify", "import"))
    parser.add_argument("target", nargs="?", type=Path)
    parser.add_argument("--trust", help="fingerprint of the device that should have signed it")
    args = parser.parse_args()
    if args.command == "list":
        found = sorted((REPO_ROOT / "data/generated/sessions").glob("*/incidents/*/incident.json"))
        for path in found:
            incident = json.loads(path.read_text())
            print(
                f"{incident['incident_id']}  {incident['window']['seconds']:>6.1f} s  "
                f"{incident['title']}  ({path.parent})"
            )
        print(f"{len(found)} incident package(s)")
        return 0
    if args.target is None:
        parser.error("give a package folder or .zip")
    if args.command == "import":
        result = import_package(args.target, REPO_ROOT / "data/generated/imported_incidents")
        return show(Path(result["folder"]), args.trust)
    if args.target.suffix == ".zip":
        with tempfile.TemporaryDirectory() as scratch:
            result = import_package(args.target, Path(scratch))
            return show(Path(result["folder"]), args.trust)
    return show(args.target, args.trust)


if __name__ == "__main__":
    raise SystemExit(main())
