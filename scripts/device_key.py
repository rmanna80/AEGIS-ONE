#!/usr/bin/env python3
"""Show this computer's AEGIS signing key (created on first use).

    python scripts/device_key.py              # fingerprint and public key
    python scripts/device_key.py --export pub.pem

Incident packages made on this computer are signed with it. To check a package came
from this computer, compare the fingerprint that incident_tool.py prints with this one,
or pass it: incident_tool.py verify <package> --trust <fingerprint>.

Only the public key is ever shown or exported. The private key file stays where it is,
readable by its owner only; never copy it into the repository.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from aegis.config import Settings  # noqa: E402
from aegis.identity import DeviceKey  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--export", type=Path, help="write the public key (PEM) to this file")
    args = parser.parse_args()
    # The same key the backend uses: AEGIS_DEVICE_KEY from the environment or .env.
    key = DeviceKey.load_or_create(Settings().device_key)
    print(f"private key:  {key.path} (stays on this computer)")
    print(f"fingerprint:  {key.fingerprint}")
    print(key.public_pem(), end="")
    if args.export:
        args.export.write_text(key.public_pem())
        print(f"public key written to {args.export}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
