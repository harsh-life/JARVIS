package com.hypermind.jarvis.voice

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** Why recognition ended without text. */
enum class RecognizerError {
    NO_MATCH,
    SPEECH_TIMEOUT,
    AUDIO,
    BUSY,
    PERMISSION,
    NOT_AVAILABLE,
    CLIENT,
    UNKNOWN,
}

/** What a recognizer reports. Callbacks may arrive on any thread. */
interface RecognizerListener {
    fun onReady()

    fun onEndOfSpeech()

    fun onResult(
        text: String,
        confidence: Float?,
    )

    fun onError(error: RecognizerError)
}

/**
 * One speech recognizer on this phone. The only implementation shipped is
 * [AndroidOnDeviceRecognizer]: the audio is processed on the device and never
 * sent anywhere (docs/27 §2). A recognizer that would have to send audio off
 * the phone reports itself unavailable instead of being used.
 */
interface Recognizer {
    val available: Boolean

    fun start(
        language: String?,
        listener: RecognizerListener,
    )

    /** Stop listening and deliver what was heard. */
    fun stop()

    /** Abandon: nothing is delivered. */
    fun cancel()

    fun destroy()
}

sealed interface VoiceInputState {
    data object Idle : VoiceInputState

    /** The microphone is open — always visible to the user while it is. */
    data object Listening : VoiceInputState

    data object Processing : VoiceInputState

    data class Heard(
        val transcript: String,
        val confidence: Float?,
    ) : VoiceInputState {
        override fun toString(): String = "Heard(${transcript.length} chars, confidence=$confidence)"
    }

    data class Failed(
        val error: RecognizerError,
    ) : VoiceInputState
}

/**
 * Speech to a task draft (docs/27 §3). Push-to-talk only: listening starts when
 * the user taps, and ends when they tap again, when they stop speaking, when
 * the app leaves the screen ([cancel]), or on an error. There is no wake word
 * and no background listening (OD-VOI-2).
 *
 * The result is text for the task box — bounded to [maxChars] — and nothing
 * else: it is handed to [onTranscript], never to a task, a confirmation or
 * step-up. A result from a session the user already cancelled is dropped.
 */
class SpeechInput(
    private val recognizer: Recognizer,
    private val maxChars: Int,
    private val onTranscript: (String) -> Unit,
) {
    private val _state = MutableStateFlow<VoiceInputState>(VoiceInputState.Idle)
    val state: StateFlow<VoiceInputState> = _state.asStateFlow()

    private var session = 0

    val available: Boolean get() = recognizer.available

    @Synchronized
    fun start(language: String? = null) {
        if (_state.value == VoiceInputState.Listening || _state.value == VoiceInputState.Processing) return
        if (!recognizer.available) {
            _state.value = VoiceInputState.Failed(RecognizerError.NOT_AVAILABLE)
            return
        }
        session += 1
        val mine = session
        _state.value = VoiceInputState.Listening
        recognizer.start(language, Listener(mine))
    }

    @Synchronized
    fun stopListening() {
        if (_state.value != VoiceInputState.Listening) return
        _state.value = VoiceInputState.Processing
        recognizer.stop()
    }

    /** Interruption or the user's cancel: the microphone closes, nothing is kept. */
    @Synchronized
    fun cancel() {
        session += 1
        if (_state.value == VoiceInputState.Listening || _state.value == VoiceInputState.Processing) {
            recognizer.cancel()
        }
        _state.value = VoiceInputState.Idle
    }

    fun release() {
        cancel()
        recognizer.destroy()
    }

    private inner class Listener(
        private val mine: Int,
    ) : RecognizerListener {
        override fun onReady() = Unit

        override fun onEndOfSpeech() {
            synchronized(this@SpeechInput) {
                if (mine == session && _state.value == VoiceInputState.Listening) {
                    _state.value = VoiceInputState.Processing
                }
            }
        }

        override fun onResult(
            text: String,
            confidence: Float?,
        ) {
            val transcript = text.trim().take(maxChars)
            val deliver =
                synchronized(this@SpeechInput) {
                    if (mine != session) return
                    _state.value =
                        if (transcript.isEmpty()) {
                            VoiceInputState.Failed(RecognizerError.NO_MATCH)
                        } else {
                            VoiceInputState.Heard(transcript, confidence?.coerceIn(0f, 1f))
                        }
                    transcript.isNotEmpty()
                }
            if (deliver) onTranscript(transcript)
        }

        override fun onError(error: RecognizerError) {
            synchronized(this@SpeechInput) {
                if (mine == session) _state.value = VoiceInputState.Failed(error)
            }
        }
    }
}
