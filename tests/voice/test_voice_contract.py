"""docs/27 — the Android client parses `GET /voice/config` from the same bytes
(android/contract VoiceSampleTest)."""

from __future__ import annotations

from tests.tools.export_voice_samples import PATH, render_file


def test_the_shared_voice_samples_are_current():
    """Regenerate: `python -m tests.tools.export_voice_samples export`."""

    assert PATH.read_text(encoding="ascii") == render_file()
