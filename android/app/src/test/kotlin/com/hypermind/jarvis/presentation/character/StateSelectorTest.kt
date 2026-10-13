package com.hypermind.jarvis.presentation.character

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.time.Instant

/**
 * OD-CHAR-2: the selection policy, held to the same shared vectors as the
 * server's reference implementation (tests/presentation/test_presentation_selection.py).
 */
class StateSelectorTest {
    private val vectors =
        Json
            .parseToJsonElement(
                File(requireNotNull(System.getProperty("jarvis.shared.dir")))
                    .resolve("presentation_selection_vectors.json")
                    .readText(),
            ).jsonObject

    private val cases = vectors.getValue("cases").jsonArray.map { it.jsonObject }

    @Test
    fun `the selector meets every shared vector`() {
        assertTrue(cases.size >= 40)
        for (case in cases) {
            val name = case.getValue("name").jsonPrimitive.content
            val settings =
                requireNotNull(
                    CharacterSettings.parse((case["settings"] ?: JsonObject(emptyMap())).toString()),
                ) { name }
            val raw =
                when (val result = case.getValue("result")) {
                    JsonNull -> null
                    is JsonPrimitive -> result.content
                    else -> result.toString()
                }
            val now = case["now"]?.jsonPrimitive?.content?.let(Instant::parse)
            val expected = Expression.entries.first { it.wire == case.getValue("expected").jsonPrimitive.content }
            assertEquals(name, expected, StateSelector.select(raw, settings, now).expression)
        }
    }

    @Test
    fun `a selection carries its score, and the fallback scores nothing`() {
        val result =
            InferenceResult(
                scores = Expression.entries.associateWith { if (it == Expression.SAD) 9 else 1 },
                confidence = 0.9,
                timestamp = Instant.parse("2026-10-13T12:00:00Z"),
                signals = setOf(SignalType.TEXT),
                uncertain = false,
            )
        assertEquals(Selection(Expression.SAD, 9), StateSelector.select(result, CharacterSettings()))
        assertEquals(
            Selection(Expression.DEFAULT, 0),
            StateSelector.select(null as InferenceResult?, CharacterSettings()),
        )
    }

    @Test
    fun `a typed result cannot be built out of range`() {
        val ok = Expression.entries.associateWith { 1 }
        val now = Instant.now()
        for (bad in listOf(ok + (Expression.HAPPY to 0), ok + (Expression.HAPPY to 11), ok - Expression.SAD)) {
            val built = runCatching { InferenceResult(bad, 0.9, now, emptySet(), false) }
            assertTrue(built.isFailure)
        }
        for (confidence in listOf(-0.01, 1.01, Double.NaN)) {
            assertTrue(runCatching { InferenceResult(ok, confidence, now, emptySet(), false) }.isFailure)
        }
    }

    @Test
    fun `the shipped settings file is the built-in defaults, and bad settings are rejected whole`() {
        val dir = File(System.getProperty("jarvis.character.dir") ?: "../../assets/character")
        assertEquals(CharacterSettings(), CharacterSettings.parse(dir.resolve("presentation.json").readText()))
        assertNotNull(CharacterSettings.parse("{}"))
        val bad =
            listOf(
                "",
                "[]",
                """{"thresholds": {"happy": 0}}""",
                """{"thresholds": {"sad": 11}}""",
                """{"thresholds": {"happy": 7.5}}""",
                """{"thresholds": {"happy": "7"}}""",
                """{"thresholds": {"furious": 5}}""",
                """{"min_confidence": 1.1}""",
                """{"min_confidence": -0.1}""",
                """{"min_confidence": true}""",
                """{"timing": {"transition_ms": -1}}""",
                """{"timing": {"transition_ms": 5000}}""",
                """{"timing": {"idle_timeout_ms": 1000}}""",
                """{"timing": {"interrupt_score": 0}}""",
                """{"timing": {"hold_ms": {"surprised": 100}}}""",
                """{"timing": {"hold_ms": {"default": 1000}}}""",
                """{"timing": {"speed": 2}}""",
                """{"reduced_motion": "sometimes"}""",
                """{"transition": "spin"}""",
                """{"schema_version": 2}""",
                """{"visible": "yes"}""",
                """{"profile_id": "../x"}""",
                """{"expression": "happy"}""",
            )
        for (text in bad) assertNull(text, CharacterSettings.parse(text))
    }

    @Test
    fun `thresholds and timing can be tuned without code`() {
        val tuned =
            requireNotNull(
                CharacterSettings.parse(
                    """{"min_confidence": 0.8, "thresholds": {"angry": 5},""" +
                        """ "timing": {"hold_ms": {"surprised": 1000}}}""",
                ),
            )
        assertEquals(5, tuned.threshold(Expression.ANGRY))
        assertEquals(7, tuned.threshold(Expression.HAPPY))
        assertEquals(1_000L, tuned.hold(Expression.SURPRISED))
        assertEquals(0.8, tuned.minConfidence, 0.0)
    }

    @Test
    fun `the fade is configurable and disappears with reduced motion or no transition`() {
        val settings = CharacterSettings(transitionMillis = 300)
        assertEquals(300L, CharacterTransitions.fadeMillis(settings, systemReducesMotion = false))
        assertEquals(0L, CharacterTransitions.fadeMillis(settings, systemReducesMotion = true))
        val always = settings.copy(reducedMotion = CharacterSettings.ReducedMotion.ALWAYS)
        assertEquals(0L, CharacterTransitions.fadeMillis(always, systemReducesMotion = false))
        val none = settings.copy(transition = CharacterSettings.Transition.NONE)
        assertEquals(0L, CharacterTransitions.fadeMillis(none, systemReducesMotion = false))
    }
}
