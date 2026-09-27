# Running voice (STT / TTS)

Contract: `docs/27_VOICE.md`. Decisions implemented: `docs/DECISION_REGISTER.md` §2E.

> **A voice is an input method, not an identity.** Who is speaking never
> authorizes anything, and voice can never confirm an action or satisfy step-up.
> **Voice is detachable.** Track B works fully with every voice setting `null`.

## 1. Placement

```yaml
voice:
  stt: device          # device | <provider id> | null
  tts: device          # device | <provider id> | null
  diarization: null    # future slot — only null loads
  speaker_id: null     # future slot — only null loads
```

| Value | Speech recognition | Speech synthesis |
|---|---|---|
| `device` (default) | the phone's **on-device** recognizer; raw audio never leaves the phone | Android system TTS |
| `<provider id>` | `POST /api/v1/voice/transcribe` → that provider | `POST /api/v1/voice/synthesize` → that provider |
| `null` | off (no mic button) | off |

On a phone without on-device recognition (below Android 12, or no offline
model), voice input says so and does nothing. JARVIS does not fall back to the
network recognizer, which could send the audio to a third party.

## 2. On the phone

- **Push-to-talk only.** Tap **Speak**; the button reads **Stop** and the screen
  says "Listening" for as long as the microphone is open. Leaving the app
  cancels listening. There is no wake word and no background microphone.
- The microphone permission is asked for the first time you tap Speak.
- What was heard goes into the task box. **You read it and press Send**: it is
  an ordinary task from your authenticated session, authorized like any other.
- Saying "yes" or "approve" never approves anything. Pending actions are
  approved only by tapping **Approve** on the confirmation card, and
  high-impact ones also need your fingerprint or screen lock.
- **Read results aloud** (off by default) speaks the answer with the phone's
  TTS.

## 3. Optional server providers

```yaml
voice:
  stt: cloud_stt
  providers:
    - id: cloud_stt
      kind: openai_compatible               # /audio/transcriptions, /audio/speech
      endpoint: "https://api.openai.com/v1" # the only host this provider may contact
      secret_ref: "env:VOICE_PROVIDER_KEY"  # or secretstore:<handle> of class model_api_key
      stt_model: "whisper-1"
      tts_model: "tts-1"                    # optional
      tts_voice: "alloy"
      pricing: {per_audio_mb: 0.006, per_1k_chars: 0.015}   # required unless loopback
      timeout_seconds: 30
```

- A cloud STT provider receives the user's audio. That is a disclosed
  configuration choice (OD-VOI-1; the recommendation for the pilot is
  on-device only).
- Egress is declared: every request is checked against the endpoint's exact
  scheme, host and port, and redirects are never followed.
- Every provider call is budget/rate prechecked and metered as a `model_call`
  (`provider: voice:<id>`), including calls that fail.
- The key is resolved per call and sent only in that request's header.
- Endpoint rules: `https://` only, except `http://` on loopback. A non-local
  provider without `pricing` fails to load.

## 4. Audio handling

- Server STT holds the upload in a bounded in-memory buffer
  (`max_audio_bytes`) and zeroes it when the request ends. Nothing is written
  to disk, logs, the audit trail, usage records, memory or the vault.
- `audio_retained` is always `false`. This build has no retention path; the
  per-user opt-in (LIFE-002) is not built, so nothing can turn retention on.
- Audit rows name a voice event id or a refusal reason, never text or audio.

## 5. API

| Call | Result |
|---|---|
| `GET /api/v1/voice/config` | `{stt, tts}` placements (`device`/`server`/`off`) and bounds; no provider ids or endpoints |
| `POST /api/v1/voice/transcribe?language=en` with an `audio/*` body | `VoiceEvent{transcript, audio_retained:false}` plus `confidence`, `language`, `truncated` |
| `POST /api/v1/voice/synthesize {text, voice?}` | `audio/mpeg` bytes, `Cache-Control: no-store` |

A direction placed on `device` or `null` answers `503 dependency_unavailable
{dependency: voice, reason: stt_device|stt_off|…}`. Other errors: `422` for a
bad content type, empty or oversized audio, a bad language tag, or text that
is too long; `429` for rate or budget; `503` when the provider fails
(`stt_provider:<reason>`).

## 6. Tests

```
python -m pytest tests/voice -q        # VOI-T1..T5, providers, privacy, authority, contracts
lint-imports --config pyproject.toml   # includes the two voice contracts
cd android && ./gradlew :contract:test :app:testDebugUnitTest
```

Hardware: the Android recognizer and TTS engine are exercised through fakes
(JVM/Robolectric). Real-device recognition, microphone lifecycle and TTS audio
are **not** validated by CI.
