package com.hypermind.jarvis.presentation.character

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import kotlin.random.Random

/**
 * OD-CHAR-4: the activity policy, held to the same shared vectors as the reference implementation
 * (tests/presentation/test_activity_policy.py), and the settings block that parameterises it.
 */
class ActivityPolicyTest {
    private val shared = File(requireNotNull(System.getProperty("jarvis.shared.dir")))
    private val vectors =
        Json.parseToJsonElement(
            shared.resolve("presentation_activity_policy_vectors.json").readText(),
        )
    private val samples = Json.parseToJsonElement(shared.resolve("presentation_activity_samples.json").readText())

    private val base =
        mapOf(
            Expression.HAPPY to 1,
            Expression.SATISFIED to 1,
            Expression.DEFAULT to 5,
            Expression.SLEEPY to 1,
            Expression.SURPRISED to 1,
            Expression.ANGRY to 1,
            Expression.SAD to 1,
        )

    /** The case's overrides over the defaults, through the real settings parser. */
    private fun settings(overrides: JsonObject?): CharacterSettings =
        requireNotNull(CharacterSettings.parse(overrides?.toString() ?: "{}")) { "settings" }

    /** A valid report over the base scores, or — for "invalid" — one that fails validation (null). */
    private fun report(element: kotlinx.serialization.json.JsonElement): ActivityReport? {
        if (element is JsonPrimitive) return ActivityReport.parse("""{"scores": {"happy": 11}}""")
        val scores =
            base + element.jsonObject.mapKeys { (k, _) -> Expression.parse(k) }.mapValues { it.value.jsonPrimitive.int }
        val body =
            """{"schema_version": 1, "computed_at": "2026-11-10T12:00:00Z", "window_s": 1800,
               "scores": {${scores.entries.joinToString { "\"${it.key.wire}\": ${it.value}" }}},
               "components": {"engagement": 0, "productivity": 0, "steadiness": 0, "failure": 0,
                              "pressure": 0, "surprise": 0, "inactivity": 0}}"""
        return requireNotNull(ActivityReport.parse(body)) { "a vector's report must be valid" }
    }

    @Test
    fun `the policy meets every shared vector`() {
        val cases =
            vectors.jsonObject
                .getValue("cases")
                .jsonArray
                .map { it.jsonObject }
        assertTrue(cases.size >= 20)
        for (case in cases) {
            val name = case.getValue("name").jsonPrimitive.content
            val policy = ActivityPolicy(settings(case["settings"]?.jsonObject), 0)
            for ((i, step) in case
                .getValue("steps")
                .jsonArray
                .map { it.jsonObject }
                .withIndex()) {
                val at = step.getValue("at").jsonPrimitive.long
                when {
                    "report" in step -> policy.onReport(report(step.getValue("report")), at)
                    step["activity"]?.jsonPrimitive?.boolean == true -> policy.onLocalActivity(at)
                    else -> policy.tick(at)
                }
                val expect = step.getValue("expect").jsonArray
                val wanted =
                    PresentedCharacter(Expression.parse(expect[0].jsonPrimitive.content), expect[1].jsonPrimitive.int)
                assertEquals("$name, step $i at $at", wanted, policy.presented)
            }
        }
    }

    @Test
    fun `the wire samples are accepted and rejected exactly as by the reference schema`() {
        val root = samples.jsonObject
        for (sample in root.getValue("valid").jsonArray.map { it.jsonObject }) {
            val name = sample.getValue("name").jsonPrimitive.content
            assertTrue(name, ActivityReport.parse(sample.getValue("text").jsonPrimitive.content) != null)
        }
        val invalid = root.getValue("invalid").jsonArray.map { it.jsonObject }
        assertTrue(invalid.size >= 15)
        for (sample in invalid) {
            val name = sample.getValue("name").jsonPrimitive.content
            assertNull(name, ActivityReport.parse(sample.getValue("text").jsonPrimitive.content))
        }
        assertNull(ActivityReport.parse(null))
    }

    @Test
    fun `an invalid report never moves the character`() {
        val good =
            requireNotNull(
                ActivityReport.parse(
                    samples.jsonObject
                        .getValue("valid")
                        .jsonArray[0]
                        .jsonObject
                        .getValue("text")
                        .jsonPrimitive.content,
                ),
            )
        for (sample in samples.jsonObject
            .getValue("invalid")
            .jsonArray
            .map { it.jsonObject }) {
            val policy = ActivityPolicy(CharacterSettings(), 0)
            policy.onReport(good, 0)
            val before = policy.presented
            policy.onReport(ActivityReport.parse(sample.getValue("text").jsonPrimitive.content), 1_000)
            assertEquals(sample.getValue("name").jsonPrimitive.content, before, policy.presented)
        }
    }

    @Test
    fun `any input sequence stays in range and respects cooldowns`() {
        val settings = CharacterSettings()
        repeat(25) { seed ->
            val random = Random(seed)
            val policy = ActivityPolicy(settings, 0)
            val left = mutableMapOf<Expression, Long>()
            var previous = Expression.DEFAULT
            var now = 0L
            repeat(400) {
                now += listOf(500L, 1_000L, 5_000L, 15_000L, 60_000L).random(random)
                val roll = random.nextDouble()
                when {
                    roll < 0.7 ->
                        policy.onReport(
                            ActivityReport.of(Expression.entries.associateWith { random.nextInt(1, 11) }),
                            now,
                        )
                    roll < 0.8 -> policy.onReport(null, now)
                    roll < 0.9 -> policy.onLocalActivity(now)
                    else -> policy.tick(now)
                }
                val shown = policy.shown
                assertTrue(policy.presented.intensity in IntensityPolicy.RANGE)
                if (shown != previous) {
                    if (previous != Expression.DEFAULT) left[previous] = now
                    // A baseline (DEFAULT, or SLEEPY without evidence) has no cooldown to respect.
                    val last = left[shown]?.takeIf { shown !in CharacterSettings.BASELINE_STATES }
                    if (last != null) {
                        assertTrue("seed $seed: $shown re-entered early", now - last >= settings.cooldown(shown))
                    }
                }
                previous = shown
            }
        }
    }

    @Test
    fun `next change names the minimum display, the surprise hold and staleness`() {
        val policy = ActivityPolicy(CharacterSettings(), 0)
        policy.onReport(ActivityReport.of(base + (Expression.SURPRISED to 10)), 1_000)
        assertEquals(2_500L, policy.nextChangeAt(1_000))
        policy.tick(2_500)
        assertEquals(3_500L, policy.nextChangeAt(2_500))
        policy.tick(3_500)
        assertEquals(Expression.DEFAULT, policy.shown)
        assertEquals(61_001L, policy.nextChangeAt(3_500))
    }

    @Test
    fun `the activity block is optional, tunable, and rejected whole when invalid`() {
        val defaults = requireNotNull(CharacterSettings.parse("{}"))
        assertEquals(15_000L, defaults.activityPollMillis)
        assertEquals(60_000L, defaults.activityMaxAgeMillis)
        assertEquals(2, defaults.activityExitMargin)
        assertEquals(60_000L, defaults.cooldown(Expression.SURPRISED))
        assertEquals(0L, defaults.cooldown(Expression.DEFAULT))
        val tuned =
            requireNotNull(
                CharacterSettings.parse(
                    """{"activity": {"poll_interval_ms": 30000, "max_age_ms": 90000, "exit_margin": 1,
                       "cooldown_ms": {"angry": 120000}}}""",
                ),
            )
        assertEquals(30_000L, tuned.activityPollMillis)
        assertEquals(1, tuned.activityExitMargin)
        assertEquals(120_000L, tuned.cooldown(Expression.ANGRY))
        assertEquals(30_000L, tuned.cooldown(Expression.SAD))
        val bad =
            listOf(
                """{"activity": 4}""",
                """{"activity": {"poll_interval_ms": 1000}}""",
                """{"activity": {"max_age_ms": 700000}}""",
                """{"activity": {"poll_interval_ms": 60000, "max_age_ms": 60000}}""",
                """{"activity": {"exit_margin": 6}}""",
                """{"activity": {"exit_margin": "2"}}""",
                """{"activity": {"cooldown_ms": {"angry": -1}}}""",
                """{"activity": {"cooldown_ms": {"default": 1000}}}""",
                """{"activity": {"mood_decay": 5}}""",
            )
        for (text in bad) assertNull(text, CharacterSettings.parse(text))
    }
}
