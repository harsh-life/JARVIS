package com.hypermind.jarvis.presentation

/**
 * The non-text signals for a [PresentationState]: tone, motion and a glyph —
 * several at once, so no state is told apart by colour alone (and so a future
 * character can express the same signals in its own way). Colours are chosen
 * by the renderer from the tone; nothing here names one.
 */
data class PresentationSignal(
    val tone: Tone,
    val motion: Motion,
    val glyph: Glyph,
    /** Draw attention (the user needs to act). */
    val emphasis: Boolean,
) {
    enum class Tone { CALM, ACTIVE, ATTENTION, SUCCESS, PROBLEM, BLOCKED }

    enum class Motion { STILL, BREATHING, WORKING, PULSING }

    enum class Glyph { READY, LISTENING, WORKING, APPROVAL, WAITING, DONE, PROBLEM, STOPPED, OFFLINE }

    companion object {
        fun of(state: PresentationState): PresentationSignal =
            when (state.taskStatus) {
                TaskPhase.IDLE -> PresentationSignal(Tone.CALM, Motion.BREATHING, Glyph.READY, false)
                TaskPhase.LISTENING -> PresentationSignal(Tone.ACTIVE, Motion.PULSING, Glyph.LISTENING, false)
                TaskPhase.SUBMITTING, TaskPhase.THINKING ->
                    PresentationSignal(Tone.ACTIVE, Motion.WORKING, Glyph.WORKING, false)
                TaskPhase.EXECUTING -> PresentationSignal(Tone.ACTIVE, Motion.WORKING, Glyph.WORKING, false)
                TaskPhase.WAITING_FOR_CONFIRMATION ->
                    PresentationSignal(Tone.ATTENTION, Motion.PULSING, Glyph.APPROVAL, true)
                TaskPhase.WAITING_FOR_PLATFORM ->
                    PresentationSignal(
                        Tone.ATTENTION,
                        Motion.PULSING,
                        Glyph.WAITING,
                        true,
                    )
                TaskPhase.COMPLETED -> PresentationSignal(Tone.SUCCESS, Motion.STILL, Glyph.DONE, false)
                TaskPhase.CANCELLED -> PresentationSignal(Tone.CALM, Motion.STILL, Glyph.STOPPED, false)
                TaskPhase.FAILED -> PresentationSignal(Tone.PROBLEM, Motion.STILL, Glyph.PROBLEM, true)
                TaskPhase.BLOCKED -> PresentationSignal(Tone.BLOCKED, Motion.STILL, Glyph.STOPPED, true)
                TaskPhase.OFFLINE -> PresentationSignal(Tone.CALM, Motion.STILL, Glyph.OFFLINE, false)
                TaskPhase.RECONNECTING -> PresentationSignal(Tone.CALM, Motion.BREATHING, Glyph.OFFLINE, false)
            }
    }
}
