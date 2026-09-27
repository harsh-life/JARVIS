"""Voice — STT / TTS / speaker context (docs/27, PRD VOICE-001..004, EMO-005).

> **1. A voice is an input method, not an identity.** Who is speaking never
> authorizes anything.
> **2. Voice is detachable.** Track B works fully with every voice provider
> disabled.

The default placement is on the device (`voice.stt: device`, `voice.tts:
device`): Android speech recognition and system TTS, so raw audio never leaves
the phone and only the transcript reaches the server — as ordinary task input,
through the ordinary task endpoint. There is no voice execution path.

This package is the optional server half, used only when a direction is placed
on a configured provider:

* `providers` — the replaceable `SpeechToText` / `TextToSpeech` interfaces.
* `openai_compatible` — one adapter family; declared-origin egress only.
* `service` — placement, bounds, metering, audit; audio zeroed after use.
* `speaker` — the `[FUTURE]` speaker slots; context only, never authority.
* `audio` — the in-memory audio buffer.
"""
