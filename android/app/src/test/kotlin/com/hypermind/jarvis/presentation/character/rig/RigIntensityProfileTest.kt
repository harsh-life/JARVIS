package com.hypermind.jarvis.presentation.character.rig

import com.hypermind.jarvis.presentation.character.Expression
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The intensity profile (assets/character/rig/JarvisCharacter.intensity.json): the bundled values,
 * and every way a profile is rejected whole.
 */
class RigIntensityProfileTest {
    private val text = RigAssetsFixture.text(RigAssets.INTENSITY)
    private val profile = requireNotNull(RigIntensityProfile.parse(text)) { "the bundled profile is valid" }

    @Test
    fun `intensities 5 and 6 are the canonical animation for every expression`() {
        for (e in Expression.entries) {
            assertEquals(RigIntensityProfile.NEUTRAL, profile.gains(e, 5))
            assertEquals(RigIntensityProfile.NEUTRAL, profile.gains(e, 6))
        }
    }

    @Test
    fun `every curve rises within its limits`() {
        for (e in Expression.entries) {
            var previous = RigIntensityProfile.Gains(0f, 0f)
            for (i in 1..10) {
                val g = profile.gains(e, i)
                assertTrue("$e $i posture", g.posture in RigIntensityProfile.POSTURE_MIN..RigIntensityProfile.GAIN_MAX)
                assertTrue("$e $i motion", g.motion in RigIntensityProfile.MOTION_MIN..RigIntensityProfile.GAIN_MAX)
                assertTrue("$e $i", g.posture >= previous.posture && g.motion >= previous.motion)
                previous = g
            }
        }
        assertThrows(IllegalArgumentException::class.java) { profile.gains(Expression.SAD, 0) }
        assertThrows(IllegalArgumentException::class.java) { profile.gains(Expression.SAD, 11) }
    }

    @Test
    fun `the bundled values are the investigation's provisional candidates`() {
        // (posture, motion) at intensity 10, and the bands below neutral.
        val tops =
            mapOf(
                Expression.HAPPY to (1.5f to 1.7f),
                Expression.SATISFIED to (1.35f to 1.4f),
                Expression.DEFAULT to (1.0f to 1.3f),
                Expression.SLEEPY to (1.5f to 1.4f),
                Expression.SURPRISED to (1.25f to 1.7f),
                Expression.ANGRY to (1.3f to 1.35f),
                Expression.SAD to (1.4f to 1.4f),
            )
        for ((e, top) in tops) {
            assertEquals(
                "$e",
                RigIntensityProfile.Gains(top.first, top.second),
                profile.gains(e, 10),
            )
        }
        assertEquals(RigIntensityProfile.Gains(0.6f, 0.25f), profile.gains(Expression.SAD, 1))
        assertEquals(RigIntensityProfile.Gains(0.65f, 0.35f), profile.gains(Expression.SLEEPY, 2))
        assertEquals(
            "Default has no posture of its own",
            RigIntensityProfile.Gains(1f, 0.25f),
            profile.gains(Expression.DEFAULT, 1),
        )
        assertEquals(Expression.DEFAULT, profile.reference)
        assertEquals(0.8f, profile.postureRampSeconds)
        assertEquals(0.5f, profile.motionRampSeconds)
    }

    @Test
    fun `the held bones are the investigation's and the remediation's, and the face is never adjusted`() {
        val shoulders = setOf("upperarm_r", "upperarm_l")
        val legs = setOf("leg_r", "leg_l")
        // Sleepy's head: its shoulders (held, at their authored extreme) cannot follow a further
        // turn, which slid the sleeve's cut border onto the hood (docs/CHARACTER_RIG.md §5.5).
        assertEquals(shoulders + setOf("head", "face"), profile.postureHolds[Expression.SLEEPY])
        // Surprised's and sad's legs sit next to the edge of their narrow authored range.
        assertEquals(shoulders + legs + "face", profile.postureHolds[Expression.SURPRISED])
        assertEquals(shoulders + setOf("hand_r", "hand_l", "face"), profile.postureHolds[Expression.ANGRY])
        assertEquals(shoulders + legs + setOf("forearm_r", "forearm_l", "face"), profile.postureHolds[Expression.SAD])
        assertEquals(setOf("hand_r", "hand_l", "face"), profile.motionHolds[Expression.ANGRY])
        for (e in Expression.entries) {
            assertTrue("$e posture", "face" in profile.postureHolds.getValue(e))
            assertTrue("$e motion", "face" in profile.motionHolds.getValue(e))
            // Every held bone exists in the bundled rig (the native test checks against the rig itself).
            for (b in profile.postureHolds.getValue(e) + profile.motionHolds.getValue(e)) {
                assertTrue(b, b in FakeRigRuntime.BONES)
            }
        }
    }

    private val root = Json.parseToJsonElement(text).jsonObject

    /** The bundled profile with one change at [path] (keys; the last one replaced or, with null, removed). */
    private fun edited(
        path: List<String>,
        value: JsonElement?,
    ): String {
        fun edit(
            obj: JsonObject,
            keys: List<String>,
        ): JsonObject {
            val head = keys.first()
            val map = obj.toMutableMap()
            if (keys.size == 1) {
                if (value == null) map.remove(head) else map[head] = value
            } else {
                map[head] = edit(obj.getValue(head).jsonObject, keys.drop(1))
            }
            return JsonObject(map)
        }
        return edit(root, path).toString()
    }

    private fun curve(vararg v: Double) = JsonArray(v.map { JsonPrimitive(it) })

    private fun names(vararg n: String) = JsonArray(n.map { JsonPrimitive(it) })

    @Test
    fun `an edit that keeps the rules is accepted`() {
        val tuned =
            edited(
                listOf("states", "sad", "posture", "curve"),
                curve(0.6, 0.7, 0.8, 0.9, 1.0, 1.0, 1.1, 1.2, 1.3, 1.45),
            )
        assertEquals(1.45f, RigIntensityProfile.parse(tuned)!!.gains(Expression.SAD, 10).posture)
    }

    @Test
    fun `anything invalid rejects the whole profile`() {
        val sadPosture = listOf("states", "sad", "posture")
        val bad =
            listOf(
                edited(listOf("schema"), JsonPrimitive("jarvis-character-intensity/2")),
                edited(listOf("guard"), JsonPrimitive("none")),
                edited(listOf("reference_state"), JsonPrimitive("furious")),
                edited(listOf("extra"), JsonPrimitive(1)),
                edited(listOf("ramp_seconds", "posture"), JsonPrimitive(0)),
                edited(listOf("ramp_seconds", "motion"), JsonPrimitive("0.5")),
                edited(listOf("ramp_seconds", "speed"), JsonPrimitive(1)),
                edited(listOf("states", "sad"), null),
                edited(listOf("states", "furious"), root.getValue("states").jsonObject.getValue("sad")),
                edited(listOf("states", "sad", "colour"), JsonPrimitive(1)),
                edited(sadPosture + "curve", curve(0.6, 0.65, 0.78, 0.9, 1.0, 1.0, 1.12, 1.22, 1.32)),
                edited(sadPosture + "curve", curve(0.6, 0.65, 0.78, 0.9, 1.0, 1.0, 1.12, 1.32, 1.22, 1.4)),
                edited(sadPosture + "curve", curve(0.6, 0.65, 0.78, 0.9, 1.0, 1.05, 1.12, 1.22, 1.32, 1.4)),
                edited(sadPosture + "curve", curve(0.6, 0.65, 0.78, 0.9, 0.99, 1.0, 1.12, 1.22, 1.32, 1.4)),
                edited(sadPosture + "curve", curve(0.5, 0.65, 0.78, 0.9, 1.0, 1.0, 1.12, 1.22, 1.32, 1.4)),
                edited(sadPosture + "curve", curve(0.6, 0.65, 0.78, 0.9, 1.0, 1.0, 1.12, 1.22, 1.32, 2.1)),
                edited(
                    listOf("states", "sad", "motion", "curve"),
                    curve(0.2, 0.35, 0.55, 0.75, 1.0, 1.0, 1.12, 1.22, 1.32, 1.4),
                ),
                edited(sadPosture + "curve", JsonArray(List(10) { JsonPrimitive("1.0") })),
                edited(sadPosture + "hold_canonical", names("face", "face")),
                edited(sadPosture + "hold_canonical", names("")),
                edited(sadPosture + "hold_canonical", JsonArray(listOf(JsonPrimitive(3)))),
                edited(sadPosture + "hold_canonical", null),
                "",
                "[]",
                text.replace("1.4]", "1e999]"),
            )
        for (doc in bad) assertNull(doc.take(120), RigIntensityProfile.parse(doc))
        assertNotNull(RigIntensityProfile.parse(text))
    }
}
