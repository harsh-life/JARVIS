package com.hypermind.jarvis.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SpeakerAndRoutingTest {
    private class FakeEngine(
        override var ready: Boolean = true,
        override val maxInputLength: Int = 50,
    ) : SpeechEngine {
        val spoken = mutableListOf<String>()
        var stopped = 0

        override fun speak(
            text: String,
            utteranceId: String,
        ) {
            spoken += text
        }

        override fun stop() {
            stopped += 1
        }

        override fun shutdown() = Unit
    }

    @Test
    fun `nothing is spoken unless the user turned it on`() {
        val engine = FakeEngine()
        var enabled = false
        val speaker = Speaker(engine, { enabled }, maxChars = 100)
        assertFalse(speaker.speak("Done."))
        enabled = true
        assertTrue(speaker.speak("Done."))
        assertEquals(listOf("Done."), engine.spoken)
    }

    @Test
    fun `speech is bounded by the configured limit and the engine's own`() {
        val engine = FakeEngine(maxInputLength = 10)
        Speaker(engine, { true }, maxChars = 100).speak("0123456789ABCDEF")
        Speaker(engine, { true }, maxChars = 4).speak("0123456789")
        assertEquals(listOf("0123456789", "0123"), engine.spoken)
    }

    @Test
    fun `an engine that is not ready speaks nothing`() {
        val engine = FakeEngine(ready = false)
        assertFalse(Speaker(engine, { true }, maxChars = 100).speak("hello"))
        assertTrue(engine.spoken.isEmpty())
    }

    @Test
    fun `a spoken yes during a pending confirmation only edits the draft`() {
        // docs/27 §3 (VOI-T4 on the device): the only outcome of a transcript is
        // a draft. Approving remains a tap on the confirmation card.
        for (word in listOf("yes", "approve", "do it", "confirm")) {
            val outcome: VoiceRouting.Draft = VoiceRouting.route(word, currentDraft = "")
            assertEquals(word, outcome.text)
        }
        assertEquals("book a table for two", VoiceRouting.route("for two", "book a table").text)
        assertEquals("keep this", VoiceRouting.route("   ", "keep this").text)
    }

    @Test
    fun `every recognition failure has a message, and unavailability promises no upload`() {
        RecognizerError.entries.forEach { assertTrue(VoiceMessages.of(it).isNotBlank()) }
        assertTrue("won't send your voice anywhere" in VoiceMessages.of(RecognizerError.NOT_AVAILABLE))
    }
}
