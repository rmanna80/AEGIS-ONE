#!/usr/bin/env python3
"""Check that a session's command audit log has not been edited.

    .venv/bin/python scripts/verify_audit.py                       # newest session
    .venv/bin/python scripts/verify_audit.py path/to/audit.jsonl

Each record carries the SHA-256 hash of the one before it. A changed, removed or
reordered line breaks the chain, and this reports the first line that does not fit.
It cannot detect a log that was truncated at the end or replaced wholesale; keep a
copy of the final hash elsewhere if that matters.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from aegis.security import verify_audit_log  # noqa: E402


def main() -> int:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        logs = sorted((REPO_ROOT / "data/generated/sessions").glob("*/audit.jsonl"))
        if not logs:
            sys.exit("No audit log found under data/generated/sessions.")
        path = logs[-1]
    result = verify_audit_log(path)
    print(f"audit log: {path}")
    if result["valid"]:
        lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        print(f"VALID: {result['records']} records, chain intact")
        if lines:
            print(f"final hash: {lines[-1]['hash']}")
        return 0
    print(
        f"INVALID: the chain breaks at line {result['first_bad_line']} "
        f"({result['records']} records before it are intact)"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
