"""Suite-wide test setup.

`JARVIS_TEST_SIMULATE_NO_LANDLOCK=1` runs the suite as a host without Landlock
would (macOS, pre-5.13 Linux): the kernel reports no Landlock ABI, so normal
`system.restricted` fails closed and the tests that need it skip with
`tests.support.NO_LANDLOCK_REASON`. It exists to prove, on a Linux machine,
that the suite is correct on such a host (OD-BG-2) — it changes nothing in
the server and is never read by it.
"""

from __future__ import annotations

import os

if os.environ.get("JARVIS_TEST_SIMULATE_NO_LANDLOCK") == "1":
    from server.execution import confinement

    confinement.landlock_abi = lambda: 0  # type: ignore[assignment]
