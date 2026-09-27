package com.hypermind.jarvis.voice

/**
 * The one place a transcript goes (docs/27 §3): into the task box as a draft,
 * which the user reads and sends as an ordinary task. That is the only outcome
 * there is — a spoken "yes", "approve" or "confirm" while an action is waiting
 * for confirmation changes the draft and nothing else. Approving stays a tap on
 * the confirmation card (plus biometric step-up where required); anyone in the
 * room can say "yes".
 */
object VoiceRouting {
    data class Draft(
        val text: String,
    )

    fun route(
        transcript: String,
        currentDraft: String,
    ): Draft {
        val heard = transcript.trim()
        if (heard.isEmpty()) return Draft(currentDraft)
        val joined = if (currentDraft.isBlank()) heard else "${currentDraft.trimEnd()} $heard"
        return Draft(joined)
    }
}

/** What the user is told when recognition did not produce text. */
object VoiceMessages {
    fun of(error: RecognizerError): String =
        when (error) {
            RecognizerError.NO_MATCH, RecognizerError.SPEECH_TIMEOUT -> "Didn't catch that. Tap Speak to try again."
            RecognizerError.PERMISSION -> "JARVIS needs microphone permission to listen."
            RecognizerError.NOT_AVAILABLE ->
                "On-device speech recognition isn't available on this phone, so JARVIS won't send your " +
                    "voice anywhere. Type instead."
            RecognizerError.BUSY -> "The recognizer is busy. Try again in a moment."
            RecognizerError.AUDIO, RecognizerError.CLIENT, RecognizerError.UNKNOWN -> "Listening stopped. Try again."
        }
}
