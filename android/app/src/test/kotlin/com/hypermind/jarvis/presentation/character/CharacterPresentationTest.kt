package com.hypermind.jarvis.presentation.character

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.cancel
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.Instant

/** OD-CHAR-2 end to end on virtual time: input → provider → selector → controller → expression. */
@OptIn(ExperimentalCoroutinesApi::class)
class CharacterPresentationTest {
    private val dispatcher = StandardTestDispatcher()
    private val scope = TestScope(dispatcher)
    private val settings = CharacterSettings(idleTimeoutMillis = 60_000, minDisplayMillis = 1_000)

    private fun presentation(provider: PresentationInferenceProvider = LocalCueScorer { Instant.now() }) =
        CharacterPresentation(
            scope = scope.backgroundScope,
            clock = { scope.testScheduler.currentTime },
            settings = settings,
            provider = provider,
            inferenceContext = dispatcher,
        ).also { scope.runCurrent() }

    @After
    fun tearDown() = scope.cancel()

    @Test
    fun `it starts at default, reacts to the current request, and returns to default`() {
        val character = presentation()
        assertEquals(Expression.DEFAULT, character.expression.value)
        character.onUtterance("yay, amazing")
        scope.runCurrent()
        assertEquals(Expression.HAPPY, character.expression.value)
        scope.advanceTimeBy(5_001)
        assertEquals(Expression.DEFAULT, character.expression.value)
    }

    @Test
    fun `idle becomes sleepy and the next request wakes it`() {
        val character = presentation()
        scope.advanceTimeBy(60_001)
        assertEquals(Expression.SLEEPY, character.expression.value)
        character.onUtterance("what's the weather")
        scope.runCurrent()
        assertEquals(Expression.DEFAULT, character.expression.value)
    }

    @Test
    fun `a verified completion shows satisfied`() {
        val character = presentation()
        character.onEvent(PresentationEvent.TaskCompleted)
        scope.runCurrent()
        assertEquals(Expression.SATISFIED, character.expression.value)
    }

    @Test
    fun `only the latest of overlapping inferences is applied`() {
        val slow = CompletableDeferred<InferenceResult?>()
        var calls = 0
        val character =
            presentation { input ->
                calls++
                if (input.utterance == "first") slow.await() else LocalCueScorer { Instant.now() }.score(input)
            }
        character.onUtterance("first")
        scope.runCurrent() // the first inference is under way (slow)
        character.onUtterance("whoa") // and is overtaken
        scope.runCurrent()
        slow.complete(LocalCueScorer { Instant.now() }.score(InferenceInput(utterance = "so sad")))
        scope.runCurrent()
        assertEquals(Expression.SURPRISED, character.expression.value)
        assertEquals(2, calls)
    }

    @Test
    fun `a failing or empty provider leaves the baseline`() {
        val failing = presentation { error("model unavailable") }
        failing.onUtterance("yay")
        scope.runCurrent()
        assertEquals(Expression.DEFAULT, failing.expression.value)
        val empty = presentation { null }
        empty.onUtterance("yay")
        scope.runCurrent()
        assertEquals(Expression.DEFAULT, empty.expression.value)
    }

    @Test
    fun `malformed or stale model output is rejected`() {
        val stale = presentation { LocalCueScorer { Instant.now().minusSeconds(60) }.score(it) }
        stale.onUtterance("yay")
        scope.runCurrent()
        assertEquals(Expression.DEFAULT, stale.expression.value)
        val malformed = presentation { InferenceResult.parse("""{"happy_score": 10}""") }
        malformed.onUtterance("yay")
        scope.runCurrent()
        assertEquals(Expression.DEFAULT, malformed.expression.value)
    }

    @Test
    fun `the provider sees only the last few utterances of this session`() {
        val seen = mutableListOf<InferenceInput>()
        val character =
            presentation {
                seen += it
                null
            }
        for (n in 1..6) character.onUtterance("request $n")
        scope.runCurrent()
        assertEquals(listOf("request 3", "request 4", "request 5"), seen.last().context)
        assertEquals("request 6", seen.last().utterance)
    }

    @Test
    fun `a preview is pinned until reset`() {
        val character = presentation()
        character.preview(Expression.ANGRY)
        character.onUtterance("yay")
        scope.advanceTimeBy(120_000)
        assertEquals(Expression.ANGRY, character.expression.value)
        character.reset()
        assertEquals(Expression.SLEEPY, character.expression.value)
    }
}
