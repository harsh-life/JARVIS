"""`python -m tests.tools.export_voice_samples [export|check]`.

Writes `shared/android/voice_samples.json`: `GET /api/v1/voice/config` bodies
exactly as the server renders them (`VoiceService.view`) for the three
placements. The Android client parses each with its strict model
(android/contract VoiceSampleTest).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from server.config.schema import VoiceConfig
from server.security.usage import UsageLimits, UsagePolicy
from server.voice.service import VoiceService

PATH = Path(__file__).resolve().parents[2] / "shared" / "android" / "voice_samples.json"

_LIMITS = UsageLimits(per_user_calls_per_minute=1, per_device_calls_per_minute=1, global_calls_per_minute=1,
                      per_user_daily_cost_limit=0.0, global_daily_cost_limit=0.0)


def _view(config: VoiceConfig) -> dict:
    return json.loads(VoiceService(config=config, usage=UsagePolicy(limits=_LIMITS)).view().model_dump_json())


def samples() -> dict:
    server = VoiceConfig.model_validate({"stt": "local", "tts": None, "providers": [
        {"id": "local", "endpoint": "http://127.0.0.1:9000/v1", "stt_model": "whisper"}]})
    return {
        "config_default_device": _view(VoiceConfig()),
        "config_server_stt_tts_off": _view(server),
        "config_all_off": _view(VoiceConfig(stt=None, tts=None)),
    }


def render_file() -> str:
    return json.dumps(samples(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "check"
    if command == "export":
        PATH.write_text(render_file(), encoding="ascii")
        print(f"wrote {PATH}")
        return 0
    ok = PATH.exists() and PATH.read_text(encoding="ascii") == render_file()
    print("voice samples up to date" if ok else "voice samples stale — run export")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
