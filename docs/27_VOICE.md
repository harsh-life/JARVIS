# 27_VOICE.md
## JARVIS / Hypermind Track B — Voice (STT / TTS / Speaker Context)

**Package:** next-build subsystem contract · **Depth:** compact · **Written:** 2026-09-24
**Numbering:** slot `27` is already cited for voice by `02` §10, `14` (INV-14 / VOICE-002) and `15` §2. This document fills it.
**Status:** formalizes the owner's **voice decision** (owner-ratified 2026-09-24): provider-based and detachable; STT, TTS, future diarization/speaker processing; provider replacement; speaker identity never an authentication root; raw audio per canonical retention.
**Authority:** below `00_CANONICAL_PRD.md` (VOICE-001..004, EMO-005, PRD #27 audio discarded by default) and `docs/DECISION_REGISTER.md`. Entities and endpoints already exist: `01` §12.1 (`VoiceEvent`, `SpeakerContext` with `is_authorization_signal` structurally false), `02` §10 (`/api/v1/voice/transcribe`, `/synthesize`).
**Code this plugs into:** `server/voice/` (placeholder), `shared/schemas/voice.py`, `server/config/schema.py` (`VoiceConfig{stt, diarization, speaker_id, tts}`), the Android client (`23` §1).

---

## 0. The rules this document exists to enforce

> **1. A voice is an input method, not an identity.** Who is speaking never authorizes anything.
> **2. Voice is detachable.** Track B works fully with every voice provider disabled.

---

## 1. Providers

`[PROPOSED]` two provider interfaces, each replaceable by config:

```
SpeechToText  : transcribe(audio, language?) -> {transcript, confidence?}
TextToSpeech  : synthesize(text, voice?) -> audio
```

| Placement | STT | TTS | Default |
|---|---|---|---|
| **On device** | Android speech recognition | Android system TTS (donor pipeline) | `[PROPOSED]` **default** — audio never leaves the phone |
| **Server** | a configured provider behind `POST /voice/transcribe` | behind `POST /voice/synthesize` | optional |

- `[PROPOSED]` a server-side provider is a network component: declared egress (`10`), key by `secret_ref` (`12`), every call metered (`13`). A cloud STT receives user audio; that is a disclosed configuration choice.
- `[FUTURE]` diarization and speaker processing plug in as further providers (`VoiceConfig.diarization`, `speaker_id`).

---

## 2. Audio handling

- `[LOCKED]` raw audio is **discarded after transcription by default**; `VoiceEvent.audio_retained` defaults false; retention only on the user's explicit opt-in (LIFE-002, PRD #27).
- `[PROPOSED]` on-device STT never sends audio to the server at all; only the transcript is sent, as ordinary task input.
- `[PROPOSED]` server STT holds audio in memory for the request only; never written to disk, logs, traces (`19`), or memory (`21`).

---

## 3. Speaker identity and confirmation

- `[LOCKED]` `SpeakerContext.is_authorization_signal` is structurally false; `speaker_id` is never mapped to a `user_id` for authorization (VOICE-002, INV-14). Detected affect is context only and never stored as relationship content (EMO-005).
- `[PROPOSED, security-critical]` **voice cannot confirm.** A spoken "yes" cannot satisfy a confirmation token, and no voice signal can satisfy step-up. Confirmation of `consequential`/`high_irreversible` actions always requires an explicit action on the authenticated device's confirmation screen (`23` §5.4). Anyone in the room can say "yes."
- `[PROPOSED]` a voice-originated task is an ordinary `execute` task from the authenticated device session; the transcript is its user input. The session, not the voice, is what identifies the user.

---

## 4. Configuration (shape exists in `15`; values `[PROPOSED]`)

```yaml
voice:
  stt: device          # device | <provider id> | null
  tts: device          # device | <provider id> | null
  diarization: null    # future
  speaker_id: null     # future; context only when set
```

---

## 5. Open items

| ID | Question | Status |
|---|---|---|
| OD-VOI-1 | Whether any server STT provider is enabled at pilot | `[OPEN — OWNER]`, rec on-device only |
| OD-VOI-2 | Wake-word / always-listening | `[FUTURE]`, out of scope |

---

## 6. Acceptance hooks (`[PROPOSED]` IDs)

- **VOI-T1** with all voice providers null, Track B is fully functional.
- **VOI-T2** raw audio is not persisted unless the user opted in (PRD #27).
- **VOI-T3** `is_authorization_signal` can never be true (INV-14).
- **VOI-T4** no voice input can satisfy a confirmation or step-up.
- **VOI-T5** server-side voice calls are metered and egress-bound; audio never appears in logs, traces, or memory.

---

*End of 27. Next: `28_DASHBOARD_OPERATOR_CONSOLE.md`.*
