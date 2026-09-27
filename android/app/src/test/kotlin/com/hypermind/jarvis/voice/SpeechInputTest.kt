package com.hypermind.jarvis.voice

import android.speech.SpeechRecognizer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * docs/27 — push-to-talk on the phone's own recognizer, driven through a fake
 * [Recognizer]. No microphone or hardware is involved: these tests pin the
 * state machine, not Android's recognizer.
 */
class SpeechInputTest {
    private class FakeRecognizer(
        override var available: Boolean = true,
    ) : Recognizer {
        var listener: RecognizerListener? = null
        var starts = 0
        var stops = 0
        var cancels = 0
        var destroyed = false
        var language: String? = null

        override fun start(
            language: String?,
            listener: RecognizerListener,
        ) {
            starts += 1
            this.language = language
            this.listener = listener
        }

        override fun stop() {
            stops += 1
        }

        override fun cancel() {
            cancels += 1
        }

        override fun destroy() {
            destroyed = true
        }
    }

    private val recognizer = FakeRecognizer()
    private val delivered = mutableListOf<String>()
    private val input = SpeechInput(recognizer, maxChars = 20) { delivered += it }

    @Test
    fun `listen, stop, and a heard transcript goes to the draft only`() {
        input.start("en-IN")
        assertEquals(VoiceInputState.Listening, input.state.value)
        assertEquals("en-IN", recognizer.language)
        input.stopListening()
        assertEquals(VoiceInputState.Processing, input.state.value)
        recognizer.listener!!.onResult("  call mum  ", 0.9f)
        assertEquals(VoiceInputState.Heard("call mum", 0.9f), input.state.value)
        assertEquals(listOf("call mum"), delivered)
    }

    @Test
    fun `the transcript is bounded and confidence clamped`() {
        input.start()
        recognizer.listener!!.onResult("x".repeat(100), 3f)
        val heard = input.state.value as VoiceInputState.Heard
        assertEquals(20, heard.transcript.length)
        assertEquals(1f, heard.confidence)
    }

    @Test
    fun `cancel closes the microphone and a late result is dropped`() {
        input.start()
        val stale = recognizer.listener!!
        input.cancel()
        assertEquals(1, recognizer.cancels)
        assertEquals(VoiceInputState.Idle, input.state.value)
        stale.onResult("approve", 1f)
        stale.onError(RecognizerError.AUDIO)
        assertEquals(VoiceInputState.Idle, input.state.value)
        assertTrue(delivered.isEmpty())
    }

    @Test
    fun `a second start while listening does not open a second session`() {
        input.start()
        input.start()
        assertEquals(1, recognizer.starts)
    }

    @Test
    fun `an unavailable on-device recognizer is reported, never replaced`() {
        recognizer.available = false
        input.start()
        assertEquals(VoiceInputState.Failed(RecognizerError.NOT_AVAILABLE), input.state.value)
        assertEquals(0, recognizer.starts)
        assertFalse(input.available)
    }

    @Test
    fun `errors and silence end the session without text`() {
        input.start()
        recognizer.listener!!.onError(RecognizerError.NO_MATCH)
        assertEquals(VoiceInputState.Failed(RecognizerError.NO_MATCH), input.state.value)
        input.start()
        recognizer.listener!!.onResult("   ", null)
        assertEquals(VoiceInputState.Failed(RecognizerError.NO_MATCH), input.state.value)
        assertTrue(delivered.isEmpty())
    }

    @Test
    fun `end of speech moves to processing, and release destroys the recognizer`() {
        input.start()
        recognizer.listener!!.onEndOfSpeech()
        assertEquals(VoiceInputState.Processing, input.state.value)
        input.release()
        assertTrue(recognizer.destroyed)
        assertEquals(VoiceInputState.Idle, input.state.value)
    }

    @Test
    fun `the transcript never reaches a log line through toString`() {
        assertFalse("secret plan" in VoiceInputState.Heard("my secret plan", null).toString())
    }

    @Test
    fun `platform error codes map explicitly`() {
        assertEquals(RecognizerError.NO_MATCH, AndroidOnDeviceRecognizer.errorOf(SpeechRecognizer.ERROR_NO_MATCH))
        assertEquals(
            RecognizerError.PERMISSION,
            AndroidOnDeviceRecognizer.errorOf(SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS),
        )
        assertEquals(
            RecognizerError.NOT_AVAILABLE,
            AndroidOnDeviceRecognizer.errorOf(SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE),
        )
        assertEquals(RecognizerError.UNKNOWN, AndroidOnDeviceRecognizer.errorOf(-12345))
    }
}
