package com.hypermind.jarvis.voice

import com.hypermind.jarvis.presentation.VoiceActivity

/**
 * What push-to-talk shows in the status (docs/23 §7, docs/27): whether the
 * microphone is open or the phone is recognizing. Display only — the
 * presentation never reads it as a signal, and a task's own phase outranks it.
 */
object VoicePresence {
    fun of(state: VoiceInputState): VoiceActivity =
        when (state) {
            VoiceInputState.Listening -> VoiceActivity.LISTENING
            VoiceInputState.Processing -> VoiceActivity.TRANSCRIBING
            is VoiceInputState.Heard, is VoiceInputState.Failed, VoiceInputState.Idle -> VoiceActivity.NONE
        }
}
