package com.hypermind.jarvis.contract

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * Voice (docs/27) — the client's side of `VoiceConfigView` in
 * `shared/schemas/voice.py`, held to `shared/android/voice_samples.json`.
 *
 * Placement only: where speech recognition and speech synthesis run. `device`
 * (the default) means this phone's own recognizer and TTS — raw audio never
 * leaves it, and only the transcript is sent, as an ordinary task the user
 * submits. Nothing here names a provider, an endpoint or a credential, and
 * nothing in voice can confirm an action or stand in for step-up.
 */
@Serializable
enum class VoicePlacement {
    @SerialName("device")
    DEVICE,

    @SerialName("server")
    SERVER,

    @SerialName("off")
    OFF,
}

@Serializable
data class VoiceConfigView(
    val stt: VoicePlacement,
    val tts: VoicePlacement,
    @SerialName("max_audio_bytes") val maxAudioBytes: Int,
    @SerialName("max_transcript_chars") val maxTranscriptChars: Int,
    @SerialName("max_tts_chars") val maxTtsChars: Int,
) {
    init {
        require(maxTranscriptChars > 0 && maxTtsChars > 0 && maxAudioBytes > 0) { "voice bounds must be positive" }
    }

    companion object {
        /** Before the server has answered: on-device, the documented default. */
        val DEFAULT =
            VoiceConfigView(
                stt = VoicePlacement.DEVICE,
                tts = VoicePlacement.DEVICE,
                maxAudioBytes = 10_000_000,
                maxTranscriptChars = 8000,
                maxTtsChars = 2000,
            )
    }
}
