package com.hypermind.jarvis.voice

import com.hypermind.jarvis.presentation.VoiceActivity
import org.junit.Assert.assertEquals
import org.junit.Test

class VoicePresenceTest {
    @Test
    fun `only an open microphone or recognition in progress is shown`() {
        assertEquals(VoiceActivity.LISTENING, VoicePresence.of(VoiceInputState.Listening))
        assertEquals(VoiceActivity.TRANSCRIBING, VoicePresence.of(VoiceInputState.Processing))
        assertEquals(VoiceActivity.NONE, VoicePresence.of(VoiceInputState.Idle))
        assertEquals(VoiceActivity.NONE, VoicePresence.of(VoiceInputState.Heard("approve", 1f)))
        assertEquals(VoiceActivity.NONE, VoicePresence.of(VoiceInputState.Failed(RecognizerError.NO_MATCH)))
    }
}
