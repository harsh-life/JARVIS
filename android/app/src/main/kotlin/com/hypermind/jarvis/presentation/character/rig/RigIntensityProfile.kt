package com.hypermind.jarvis.presentation.character.rig

import com.hypermind.jarvis.presentation.character.Expression
import com.hypermind.jarvis.presentation.character.IntensityPolicy
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonObject

/**
 * How the animated character presents an intensity (`character/rig/JarvisCharacter.intensity.json`,
 * schema `jarvis-character-intensity/1`; docs/CHARACTER_RIG.md §5): for each expression, a
 * posture-emphasis gain and an idle-motion gain per intensity 1–10, and the bones each layer keeps
 * canonical. Separate from the rig's contract, which it never changes.
 *
 * It turns a presented (expression, intensity) pair into two numbers for the native layers; it
 * decides no expression and reads no content. Intensities 5 and 6 are exactly gain 1, the
 * canonical animation. The gains are provisional product parameters, not a measurement.
 */
class RigIntensityProfile private constructor(
    /** The expression whose held pose is the posture reference (Default). */
    val reference: Expression,
    val postureRampSeconds: Float,
    val motionRampSeconds: Float,
    private val posture: Map<Expression, FloatArray>,
    private val motion: Map<Expression, FloatArray>,
    /** Bones the posture layer keeps canonical, per expression. */
    val postureHolds: Map<Expression, Set<String>>,
    /** Bones the idle-motion layer keeps canonical, per expression. */
    val motionHolds: Map<Expression, Set<String>>,
) {
    data class Gains(
        val posture: Float,
        val motion: Float,
    ) {
        val neutral: Boolean get() = posture == 1f && motion == 1f
    }

    /** The gains presenting [expression] at [intensity] (1–10). */
    fun gains(
        expression: Expression,
        intensity: Int,
    ): Gains {
        require(intensity in IntensityPolicy.RANGE) { "intensity out of range" }
        return Gains(posture.getValue(expression)[intensity - 1], motion.getValue(expression)[intensity - 1])
    }

    companion object {
        const val SCHEMA = "jarvis-character-intensity/1"
        const val GUARD = "authored_range"

        /** Posture never drops below this share of a held pose: it carries the expression's identity. */
        const val POSTURE_MIN = 0.6f

        /** Idle motion never drops below this share of its authored amplitude: the loop stays alive. */
        const val MOTION_MIN = 0.25f

        /** The largest gain either layer may have (the bridge's own limit is looser). */
        const val GAIN_MAX = 2.0f

        val NEUTRAL = Gains(1f, 1f)

        private val ROOT_FIELDS = setOf("schema", "reference_state", "ramp_seconds", "guard", "states")
        private val RAMP_FIELDS = setOf("posture", "motion")
        private val STATE_FIELDS = setOf("posture", "motion")
        private val LAYER_FIELDS = setOf("curve", "hold_canonical")
        private val RAMP_RANGE = 0.05..5.0
        private const val CURVE_SIZE = 10

        /**
         * A profile, validated whole: an unknown key, state or version, a missing state, a value
         * that is not a finite number, a curve that is not ten non-decreasing values within its
         * limits or that is not exactly 1 at intensities 5 and 6, or a repeated or empty bone name
         * rejects it (null). Whether the bones exist in the rig is checked when it is applied.
         */
        fun parse(text: String): RigIntensityProfile? =
            try {
                read(Json.parseToJsonElement(text).jsonObject)
            } catch (ignored: SerializationException) {
                null
            } catch (ignored: IllegalArgumentException) {
                null
            } catch (ignored: ClassCastException) {
                null
            } catch (ignored: NoSuchElementException) {
                null
            }

        private fun read(root: JsonObject): RigIntensityProfile {
            require(root.keys == ROOT_FIELDS) { "fields" }
            require(string(root.getValue("schema")) == SCHEMA) { "schema" }
            require(string(root.getValue("guard")) == GUARD) { "guard" }
            val reference = state(string(root.getValue("reference_state")))
            val ramps = root.getValue("ramp_seconds").jsonObject
            require(ramps.keys == RAMP_FIELDS) { "ramp fields" }
            val states = root.getValue("states").jsonObject
            require(states.keys == Expression.entries.map { it.wire }.toSet()) { "states" }
            val posture = mutableMapOf<Expression, FloatArray>()
            val motion = mutableMapOf<Expression, FloatArray>()
            val postureHolds = mutableMapOf<Expression, Set<String>>()
            val motionHolds = mutableMapOf<Expression, Set<String>>()
            for ((wire, value) in states) {
                val e = state(wire)
                val layers = value.jsonObject
                require(layers.keys == STATE_FIELDS) { "layers of $wire" }
                val p = layers.getValue("posture").jsonObject
                val m = layers.getValue("motion").jsonObject
                posture[e] = curve(p, POSTURE_MIN)
                motion[e] = curve(m, MOTION_MIN)
                postureHolds[e] = holds(p)
                motionHolds[e] = holds(m)
            }
            return RigIntensityProfile(
                reference = reference,
                postureRampSeconds = ramp(ramps.getValue("posture")),
                motionRampSeconds = ramp(ramps.getValue("motion")),
                posture = posture,
                motion = motion,
                postureHolds = postureHolds,
                motionHolds = motionHolds,
            )
        }

        private fun curve(
            layer: JsonObject,
            min: Float,
        ): FloatArray {
            require(layer.keys == LAYER_FIELDS) { "layer fields" }
            val values = (layer.getValue("curve") as JsonArray).map { number(it).toFloat() }
            require(values.size == CURVE_SIZE) { "ten values" }
            require(values.all { it in min..GAIN_MAX }) { "a gain out of range" }
            require(values.zipWithNext().all { (a, b) -> b >= a }) { "not non-decreasing" }
            // Intensities 5 and 6 present the canonical animation exactly.
            require(
                values[IntensityPolicy.NEUTRAL - 1] == 1f && values[IntensityPolicy.NEUTRAL] == 1f,
            ) { "neutral is not 1" }
            return values.toFloatArray()
        }

        private fun holds(layer: JsonObject): Set<String> {
            val names = (layer.getValue("hold_canonical") as JsonArray).map(::string)
            require(names.all { it.isNotEmpty() } && names.size == names.toSet().size) { "bone names" }
            return names.toSet()
        }

        private fun ramp(element: JsonElement): Float {
            val v = number(element)
            require(v in RAMP_RANGE) { "ramp" }
            return v.toFloat()
        }

        private fun state(wire: String): Expression =
            requireNotNull(Expression.entries.firstOrNull { it.wire == wire }) { "unknown state" }

        private fun string(element: JsonElement): String {
            val p = element as JsonPrimitive
            require(p.isString) { "not a string" }
            return p.content
        }

        private fun number(element: JsonElement): Double {
            val p = element as JsonPrimitive
            require(!p.isString && p.booleanOrNull == null) { "not a number" }
            val v = requireNotNull(p.doubleOrNull) { "not a number" }
            require(v.isFinite()) { "not finite" }
            return v
        }
    }
}
