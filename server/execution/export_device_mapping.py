"""`python -m server.execution.export_device_mapping [export|check]`.

`export` rewrites `shared/android/device_mapping.json` from the table in
`server/execution/android.py`; `check` exits non-zero when the committed
artifact has drifted from it (docs/23 §5.1: one table, two consumers).
"""

from __future__ import annotations

import sys

from server.execution.android import (
    MAPPING_ARTIFACT_PATH,
    MAPPING_VERSION,
    check_artifact,
    export_mapping,
)


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "check"
    if command == "export":
        MAPPING_ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        MAPPING_ARTIFACT_PATH.write_text(export_mapping(), encoding="ascii")
        print(f"wrote {MAPPING_ARTIFACT_PATH} ({MAPPING_VERSION})")
        return 0
    if command == "check":
        if not check_artifact():
            print(
                "shared/android/device_mapping.json is stale — "
                "run: python -m server.execution.export_device_mapping export"
            )
            return 1
        print(f"device mapping up to date ({MAPPING_VERSION})")
        return 0
    print("usage: python -m server.execution.export_device_mapping [export|check]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
