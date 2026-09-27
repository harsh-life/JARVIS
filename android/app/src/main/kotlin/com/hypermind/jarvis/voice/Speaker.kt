package com.hypermind.jarvis.voice

import android.content.Context
import android.content.SharedPreferences
import android.speech.tts.TextToSpeech
import java.util.concurrent.atomic.AtomicInteger

/** The phone's text-to-speech engine, as far as [Speaker] needs it. */
interface SpeechEngine {
    val ready: Boolean
    val maxInputLength: Int

    fun speak(
        text: String,
        utteranceId: String,
    )

    fun stop()

    fun shutdown()
}

/** Whether task results are read aloud. Off until the user turns it on. */
class VoiceSettings(
    private val prefs: SharedPreferences,
) {
    var speakResults: Boolean
        get() = prefs.getBoolean(KEY_SPEAK_RESULTS, false)
        set(value) = prefs.edit().putBoolean(KEY_SPEAK_RESULTS, value).apply()

    private companion object {
        const val KEY_SPEAK_RESULTS = "voice_speak_results"
    }
}

/**
 * Reads task status and results aloud with Android system TTS (docs/27 §1,
 * device placement). Only what the server already shows on screen is spoken,
 * only when the user enabled it, and bounded to what the engine accepts.
 * Speaking is output only: nothing it says can be heard back as an approval.
 */
class Speaker(
    private val engine: SpeechEngine,
    private val enabled: () -> Boolean,
    private val maxChars: Int,
) {
    private val counter = AtomicInteger()

    /** Whether [text] was handed to the engine. */
    fun speak(text: String): Boolean {
        if (!enabled() || !engine.ready) return false
        val bounded = text.trim().take(minOf(maxChars, engine.maxInputLength))
        if (bounded.isEmpty()) return false
        engine.speak(bounded, "jarvis-${counter.incrementAndGet()}")
        return true
    }

    fun stop() = engine.stop()

    fun shutdown() = engine.shutdown()
}

/** [SpeechEngine] over `android.speech.tts.TextToSpeech`. */
class AndroidSpeechEngine(
    context: Context,
) : SpeechEngine {
    @Volatile
    private var initialized = false

    private val tts: TextToSpeech =
        TextToSpeech(context.applicationContext) { status ->
            initialized =
                status == TextToSpeech.SUCCESS
        }

    override val ready: Boolean get() = initialized

    override val maxInputLength: Int get() = TextToSpeech.getMaxSpeechInputLength()

    override fun speak(
        text: String,
        utteranceId: String,
    ) {
        tts.speak(text, TextToSpeech.QUEUE_FLUSH, null, utteranceId)
    }

    override fun stop() {
        tts.stop()
    }

    override fun shutdown() {
        tts.shutdown()
    }
}
