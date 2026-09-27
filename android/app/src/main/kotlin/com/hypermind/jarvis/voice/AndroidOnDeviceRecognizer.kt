package com.hypermind.jarvis.voice

import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer

/**
 * Android's **on-device** speech recognizer (API 31+,
 * `SpeechRecognizer.createOnDeviceSpeechRecognizer`). Audio is processed on the
 * phone; JARVIS never records, stores or uploads it (docs/27 §2 — the default
 * placement's whole promise).
 *
 * On a phone without on-device recognition this reports [available] = false
 * rather than falling back to the network recognizer, which could send the
 * audio to a third party. Must be used from the main thread, as
 * `SpeechRecognizer` requires.
 */
class AndroidOnDeviceRecognizer(
    private val context: Context,
) : Recognizer {
    private var recognizer: SpeechRecognizer? = null

    override val available: Boolean
        get() =
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
                SpeechRecognizer.isOnDeviceRecognitionAvailable(context)

    override fun start(
        language: String?,
        listener: RecognizerListener,
    ) {
        if (!available || Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
            listener.onError(RecognizerError.NOT_AVAILABLE)
            return
        }
        val instance = recognizer ?: SpeechRecognizer.createOnDeviceSpeechRecognizer(context).also { recognizer = it }
        instance.setRecognitionListener(Bridge(listener))
        val intent =
            Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
                .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, false)
                .putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
        language?.let { intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE, it) }
        instance.startListening(intent)
    }

    override fun stop() {
        recognizer?.stopListening()
    }

    override fun cancel() {
        recognizer?.cancel()
    }

    override fun destroy() {
        recognizer?.destroy()
        recognizer = null
    }

    private class Bridge(
        private val listener: RecognizerListener,
    ) : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) = listener.onReady()

        override fun onBeginningOfSpeech() = Unit

        override fun onRmsChanged(rmsdB: Float) = Unit

        // The raw audio buffer callback: deliberately ignored — never kept.
        override fun onBufferReceived(buffer: ByteArray?) = Unit

        override fun onEndOfSpeech() = listener.onEndOfSpeech()

        override fun onError(error: Int) = listener.onError(errorOf(error))

        override fun onResults(results: Bundle?) {
            val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()
            val confidence = results?.getFloatArray(SpeechRecognizer.CONFIDENCE_SCORES)?.firstOrNull()
            if (text == null) listener.onError(RecognizerError.NO_MATCH) else listener.onResult(text, confidence)
        }

        override fun onPartialResults(partialResults: Bundle?) = Unit

        override fun onEvent(
            eventType: Int,
            params: Bundle?,
        ) = Unit
    }

    companion object {
        fun errorOf(code: Int): RecognizerError =
            when (code) {
                SpeechRecognizer.ERROR_NO_MATCH -> RecognizerError.NO_MATCH
                SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> RecognizerError.SPEECH_TIMEOUT
                SpeechRecognizer.ERROR_AUDIO -> RecognizerError.AUDIO
                SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> RecognizerError.BUSY
                SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> RecognizerError.PERMISSION
                SpeechRecognizer.ERROR_CLIENT -> RecognizerError.CLIENT
                SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED,
                SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE,
                -> RecognizerError.NOT_AVAILABLE
                else -> RecognizerError.UNKNOWN
            }
    }
}
