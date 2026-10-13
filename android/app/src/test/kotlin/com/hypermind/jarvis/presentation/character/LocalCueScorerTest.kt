package com.hypermind.jarvis.presentation.character

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant

/** OD-CHAR-2: the pilot's deterministic cue scorer, through the real selector. */
class LocalCueScorerTest {
    private val now = Instant.parse("2026-10-13T12:00:00Z")
    private val scorer = LocalCueScorer { now }
    private val settings = CharacterSettings()

    private fun selected(input: InferenceInput): Expression =
        StateSelector
            .select(
                scorer.score(input),
                settings,
                now,
            ).expression

    private fun said(text: String) = selected(InferenceInput(utterance = text))

    @Test
    fun `clear cues select their states`() {
        assertEquals(Expression.HAPPY, said("Yay, that's awesome news!"))
        assertEquals(Expression.SATISFIED, said("Perfect, thanks"))
        assertEquals(Expression.SURPRISED, said("Whoa, no way"))
        assertEquals(Expression.SURPRISED, said("you did what?!"))
        assertEquals(Expression.ANGRY, said("ugh, that's not what I asked"))
        assertEquals(Expression.SAD, said("Unfortunately my cat passed away"))
    }

    @Test
    fun `ordinary requests stay default`() {
        for (text in listOf("set a timer for ten minutes", "what's on my calendar tomorrow", "", "   ")) {
            assertEquals(text, Expression.DEFAULT, said(text))
        }
        val result = scorer.score(InferenceInput(utterance = "open the settings"))
        assertTrue(result.uncertain)
    }

    @Test
    fun `negated cues do not count`() {
        assertEquals(Expression.DEFAULT, said("this is not awesome"))
        assertEquals(Expression.DEFAULT, said("I'm not sad about it"))
        assertEquals(Expression.DEFAULT, said("that wasn't perfect"))
    }

    @Test
    fun `mixed positive and negative cues are uncertain, so default`() {
        val result = scorer.score(InferenceInput(utterance = "thanks, but unfortunately it broke"))
        assertTrue(result.uncertain)
        assertEquals(Expression.DEFAULT, StateSelector.select(result, settings, now).expression)
    }

    @Test
    fun `loudness alone never selects a state`() {
        val loud = InferenceInput(prosody = ProsodyFeatures(peakOverMeanDb = 30f))
        assertEquals(Expression.DEFAULT, selected(loud))
        val result = scorer.score(loud)
        assertTrue(result.uncertain)
        assertTrue(result.score(Expression.SURPRISED) < settings.threshold(Expression.SURPRISED))
        assertEquals(setOf(SignalType.PROSODY), result.signals)
        val shoutedRequest = InferenceInput(utterance = "turn the lights off", prosody = ProsodyFeatures(30f))
        assertEquals(Expression.DEFAULT, selected(shoutedRequest))
        val shoutedSurprise = InferenceInput(utterance = "whoa", prosody = ProsodyFeatures(30f))
        assertEquals(9, scorer.score(shoutedSurprise).score(Expression.SURPRISED))
    }

    @Test
    fun `a verified task completion selects satisfied`() {
        val result = scorer.score(InferenceInput(events = listOf(PresentationEvent.TaskCompleted)))
        assertEquals(setOf(SignalType.EVENT), result.signals)
        assertEquals(Expression.SATISFIED, StateSelector.select(result, settings, now).expression)
    }

    @Test
    fun `sleepy is never scored from words - it comes from the idle policy`() {
        for (text in listOf("I'm so sleepy", "goodnight", "so tired")) {
            assertEquals(1, scorer.score(InferenceInput(utterance = text)).score(Expression.SLEEPY))
        }
    }

    @Test
    fun `it reports only the signals it used, never context`() {
        val result =
            scorer.score(InferenceInput(utterance = "wow", spoken = true, context = listOf("earlier", "turns")))
        assertEquals(setOf(SignalType.TRANSCRIPT), result.signals)
        assertEquals(now, result.timestamp)
    }
}
