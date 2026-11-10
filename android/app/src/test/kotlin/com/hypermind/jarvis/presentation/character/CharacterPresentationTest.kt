package com.hypermind.jarvis.presentation.character

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.cancel
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Test

/** OD-CHAR-4 end to end on virtual time: server report text → policy → expression and intensity. */
@OptIn(ExperimentalCoroutinesApi::class)
class CharacterPresentationTest {
    private val dispatcher = StandardTestDispatcher()
    private val scope = TestScope(dispatcher)
    private val settings = CharacterSettings(idleTimeoutMillis = 60_000)

    private fun presentation() =
        CharacterPresentation(
            scope = scope.backgroundScope,
            clock = { scope.testScheduler.currentTime },
            settings = settings,
        ).also { scope.runCurrent() }

    private fun body(vararg scores: Pair<String, Int>): String {
        val all =
            mapOf(
                "happy" to 1,
                "satisfied" to 1,
                "default" to 5,
                "sleepy" to 1,
                "surprised" to 1,
                "angry" to 1,
                "sad" to 1,
            ) +
                scores
        return """{"schema_version": 1, "computed_at": "2026-11-10T12:00:00Z", "window_s": 1800,
                  "scores": {${all.entries.joinToString { "\"${it.key}\": ${it.value}" }}},
                  "components": {"engagement": 50, "productivity": 50, "steadiness": 100, "failure": 0,
                                 "pressure": 10, "surprise": 0, "inactivity": 0}}"""
    }

    @After
    fun tearDown() = scope.cancel()

    @Test
    fun `it starts at default and presents a report`() {
        val character = presentation()
        assertEquals(PresentedCharacter(Expression.DEFAULT, 5), character.presented.value)
        character.onActivityReport(body("satisfied" to 8))
        assertEquals(PresentedCharacter(Expression.SATISFIED, 6), character.presented.value)
        assertEquals(Expression.SATISFIED, character.expression.value)
    }

    @Test
    fun `a report goes stale on its own, and the baseline returns`() {
        val character = presentation()
        character.onActivityReport(body("happy" to 9))
        assertEquals(Expression.HAPPY, character.expression.value)
        scope.advanceTimeBy(settings.activityMaxAgeMillis + 2)
        scope.runCurrent()
        assertEquals(Expression.SLEEPY, character.expression.value) // and the local idle (60 s) has passed
    }

    @Test
    fun `a surprise is brief without any further input`() {
        val character = presentation()
        character.onActivityReport(body("surprised" to 10, "sad" to 8))
        assertEquals(Expression.SURPRISED, character.expression.value)
        scope.advanceTimeBy(settings.hold(Expression.SURPRISED) + 1)
        scope.runCurrent()
        assertEquals(PresentedCharacter(Expression.SAD, 4), character.presented.value)
    }

    @Test
    fun `garbage or no answer is discarded`() {
        val character = presentation()
        character.onActivityReport(body("angry" to 9))
        character.onActivityReport("""{"scores": "lots"}""")
        character.onActivityReport(null)
        assertEquals(PresentedCharacter(Expression.ANGRY, 7), character.presented.value)
    }

    @Test
    fun `local activity wakes the character from reported inactivity`() {
        val character = presentation()
        scope.advanceTimeBy(60_001)
        character.onActivityReport(body("sleepy" to 10))
        assertEquals(PresentedCharacter(Expression.SLEEPY, 10), character.presented.value)
        character.onActivity()
        assertEquals(PresentedCharacter(Expression.DEFAULT, 5), character.presented.value)
    }

    @Test
    fun `a preview is pinned until reset`() {
        val character = presentation()
        character.preview(Expression.ANGRY)
        character.onActivityReport(body("happy" to 10))
        scope.advanceTimeBy(30_000)
        assertEquals(PresentedCharacter(Expression.ANGRY, 5), character.presented.value)
        character.reset()
        assertEquals(PresentedCharacter(Expression.HAPPY, 10), character.presented.value)
    }

    @Test
    fun `it asks as often as the settings say`() {
        assertEquals(15_000L, presentation().pollIntervalMillis)
    }
}
