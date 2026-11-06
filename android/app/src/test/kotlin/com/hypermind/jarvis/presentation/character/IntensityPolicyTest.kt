package com.hypermind.jarvis.presentation.character

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Presentation intensity: the deterministic threshold-relative mapping, the baselines and the dead
 * band, held to the same shared vectors as the reference implementation
 * (tests/presentation/test_presentation_intensity.py).
 */
class IntensityPolicyTest {
    private val vectors =
        Json
            .parseToJsonElement(
                File(requireNotNull(System.getProperty("jarvis.shared.dir")))
                    .resolve("presentation_intensity_vectors.json")
                    .readText(),
            ).jsonObject

    private fun section(name: String) = vectors.getValue(name).jsonArray.map { it.jsonObject }

    @Test
    fun `the policy meets every shared mapping vector`() {
        val cases = section("mapping")
        assertTrue(cases.size >= 200)
        for (case in cases) {
            val name = case.getValue("name").jsonPrimitive.content
            val score = case.getValue("score").jsonPrimitive.int
            val threshold = case.getValue("threshold").jsonPrimitive.int
            val floor = case.getValue("floor").jsonPrimitive.int
            val expected = case.getValue("expected")
            if (expected is JsonNull) {
                assertThrows(name, IllegalArgumentException::class.java) {
                    IntensityPolicy.forSelection(score, threshold, floor)
                }
                assertNull(name, IntensityPolicy.forSelectionOrNull(score, threshold, floor))
            } else {
                assertEquals(name, expected.jsonPrimitive.int, IntensityPolicy.forSelection(score, threshold, floor))
            }
        }
    }

    @Test
    fun `the owner's table, at the common floor of 4`() {
        assertEquals(4, IntensityPolicy.DEFAULT_FLOOR)
        // (score, threshold) to intensity
        val table =
            mapOf(
                (7 to 7) to 4,
                (8 to 7) to 6,
                (9 to 7) to 8,
                (10 to 7) to 10,
                (8 to 8) to 4,
                (9 to 8) to 7,
                (10 to 8) to 10,
            )
        for ((input, expected) in table) {
            assertEquals("$input", expected, IntensityPolicy.forSelection(input.first, input.second))
        }
        assertNull("not eligible", IntensityPolicy.forSelectionOrNull(7, 8))
        assertEquals("threshold 10 admits only 10", 10, IntensityPolicy.forSelection(10, 10))
    }

    @Test
    fun `every intensity is in range and rises with the score`() {
        for (floor in IntensityPolicy.RANGE) {
            for (threshold in CharacterSettings.SCORE_RANGE) {
                var previous = 0
                for (score in threshold..10) {
                    val i = IntensityPolicy.forSelection(score, threshold, floor)
                    assertTrue(i in IntensityPolicy.RANGE)
                    assertTrue("monotone", i >= previous)
                    previous = i
                }
                val atThreshold = IntensityPolicy.forSelection(threshold, threshold, floor)
                assertEquals("at the threshold: the floor", if (threshold == 10) 10 else floor, atThreshold)
                assertEquals("a 10: the top", 10, IntensityPolicy.forSelection(10, threshold, floor))
            }
        }
    }

    @Test
    fun `the baselines and the dead band meet the shared vectors`() {
        val settings = CharacterSettings()
        for (case in section("baseline")) {
            val state = Expression.parse(case.getValue("state").jsonPrimitive.content)
            assertEquals(case.getValue("expected").jsonPrimitive.int, settings.baselineIntensity(state))
        }
        for (case in section("dead_band")) {
            val c = { key: String -> case.getValue(key).jsonPrimitive.int }
            assertEquals("$case", c("expected"), IntensityPolicy.retained(c("current"), c("candidate"), c("dead_band")))
        }
    }

    @Test
    fun `a presented intensity is always in range`() {
        assertThrows(IllegalArgumentException::class.java) { PresentedCharacter(Expression.HAPPY, 0) }
        assertThrows(IllegalArgumentException::class.java) { PresentedCharacter(Expression.HAPPY, 11) }
        assertEquals(10, PresentedCharacter(Expression.HAPPY, 10).intensity)
    }

    @Test
    fun `the intensity block is optional, tunable, and rejected whole when invalid`() {
        val defaults = requireNotNull(CharacterSettings.parse("{}"))
        assertEquals(4, defaults.intensityFloor)
        assertEquals(2, defaults.intensityDeadBand)
        assertEquals(5, defaults.baselineIntensity(Expression.DEFAULT))
        assertEquals(2, defaults.baselineIntensity(Expression.SLEEPY))
        val tuned =
            requireNotNull(
                CharacterSettings.parse("""{"intensity": {"floor": 3, "dead_band": 1, "baseline": {"sleepy": 3}}}"""),
            )
        assertEquals(3, tuned.intensityFloor)
        assertEquals(1, tuned.intensityDeadBand)
        assertEquals(3, tuned.baselineIntensity(Expression.SLEEPY))
        assertEquals(5, tuned.baselineIntensity(Expression.DEFAULT))
        val bad =
            listOf(
                """{"intensity": 4}""",
                """{"intensity": {"floor": 0}}""",
                """{"intensity": {"floor": 11}}""",
                """{"intensity": {"floor": 4.5}}""",
                """{"intensity": {"dead_band": -1}}""",
                """{"intensity": {"dead_band": 10}}""",
                """{"intensity": {"baseline": {"happy": 5}}}""",
                """{"intensity": {"baseline": {"sleepy": 0}}}""",
                """{"intensity": {"gain": 2}}""",
            )
        for (text in bad) assertNull(text, CharacterSettings.parse(text))
    }
}
