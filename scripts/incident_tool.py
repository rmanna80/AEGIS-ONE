#!/usr/bin/env python3
"""List, verify and import AEGIS incident packages.

    python scripts/incident_tool.py list                      # incidents in recorded sessions
    python scripts/incident_tool.py verify <folder or .zip>   # check files against the manifest
    python scripts/incident_tool.py import <export.zip>       # unpack under data/generated/

Verification shows whether the files are the ones that were packaged. It cannot show
that the recorded measurements were accurate. To also rule out a replaced manifest,
compare the printed manifest hash with the `incident_packaged` record in the session's
audit log (scripts/verify_audit.py checks that log's own chain).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from aegis.replay.evidence import verify_manifest  # noqa: E402
from aegis.replay.incidents import import_package  # noqa: E402


def show(verification: dict, folder: Path) -> int:
    print(f"package: {folder}")
    if verification["valid"]:
        print(f"VALID: {verification['files']} files match the manifest")
    else:
        print("INVALID:")
        for problem in verification["problems"]:
            print(f"  {problem}")
    print(f"manifest sha256: {verification.get('manifest_sha256')}")
    print(verification.get("limits", ""))
    document = folder / "incident.json"
    if document.is_file():
        incident = json.loads(document.read_text())
        print(f"incident: {incident['incident_id']} - {incident['title']}")
        print(f"replay:   {incident['reproduce']['replay_this_incident']}")
    return 0 if verification["valid"] else 1


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("list", "verify", "import"):
        print(__doc__)
        return 2
    command = sys.argv[1]
    if command == "list":
        found = sorted((REPO_ROOT / "data/generated/sessions").glob("*/incidents/*/incident.json"))
        for path in found:
            incident = json.loads(path.read_text())
            print(
                f"{incident['incident_id']}  {incident['window']['seconds']:>6.1f} s  "
                f"{incident['title']}  ({path.parent})"
            )
        print(f"{len(found)} incident package(s)")
        return 0
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    target = Path(sys.argv[2])
    if command == "import":
        result = import_package(target, REPO_ROOT / "data/generated/imported_incidents")
        return show(result["verification"], Path(result["folder"]))
    if target.suffix == ".zip":
        with tempfile.TemporaryDirectory() as scratch:
            result = import_package(target, Path(scratch))
            return show(result["verification"], Path(result["folder"]))
    return show(verify_manifest(target), target)


if __name__ == "__main__":
    raise SystemExit(main())
