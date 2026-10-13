package com.hypermind.jarvis.presentation.character

import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.longOrNull

/**
 * How this client presents and selects the character — provider-neutral and
 * independent of the profile's state → image mapping (mirrors
 * `shared/schemas/presentation.py` `CharacterPresentationSettings`; defaults in
 * `assets/character/presentation.json`). The thresholds and timings are
 * product parameters to be tuned, not validated measurements of anything.
 */
data class CharacterSettings(
    val visible: Boolean = true,
    val profileId: String = CharacterProfile.BUILT_IN.profileId,
    val transition: Transition = Transition.CROSSFADE,
    val reducedMotion: ReducedMotion = ReducedMotion.SYSTEM,
    val minConfidence: Double = 0.6,
    val thresholds: Map<Expression, Int> = DEFAULT_THRESHOLDS,
    val transitionMillis: Long = 220,
    val minDisplayMillis: Long = 1_500,
    val holdMillis: Map<Expression, Long> = DEFAULT_HOLDS,
    val idleTimeoutMillis: Long = 300_000,
    val interruptScore: Int = 9,
    val maxResultAgeMillis: Long = 5_000,
) {
    enum class Transition(
        val wire: String,
    ) {
        CROSSFADE("crossfade"),
        NONE("none"),
    }

    /** [SYSTEM]: follow the device's animation setting; [ALWAYS]: never animate. */
    enum class ReducedMotion(
        val wire: String,
    ) {
        SYSTEM("system"),
        ALWAYS("always"),
    }

    init {
        require(minConfidence in 0.0..1.0) { "min_confidence" }
        require(thresholds.keys == Expression.entries.toSet()) { "thresholds" }
        require(thresholds.values.all { it in SCORE_RANGE }) { "thresholds" }
        require(transitionMillis in 0..MAX_TRANSITION) { "transition_ms" }
        require(minDisplayMillis in 0..MAX_MIN_DISPLAY) { "min_display_ms" }
        require(holdMillis.keys == HOLD_STATES) { "hold_ms" }
        require(holdMillis.values.all { it in HOLD_RANGE }) { "hold_ms" }
        require(idleTimeoutMillis in IDLE_RANGE) { "idle_timeout_ms" }
        require(interruptScore in SCORE_RANGE) { "interrupt_score" }
        require(maxResultAgeMillis in AGE_RANGE) { "max_result_age_ms" }
    }

    fun threshold(expression: Expression): Int = thresholds.getValue(expression)

    /** How long a selected state stays before the baseline returns; the baseline itself has none. */
    fun hold(expression: Expression): Long = holdMillis[expression] ?: Long.MAX_VALUE

    companion object {
        const val FILE = "presentation.json"
        val SCORE_RANGE = 1..10
        private const val MAX_TRANSITION = 2_000L
        private const val MAX_MIN_DISPLAY = 30_000L
        private val HOLD_RANGE = 500L..60_000L
        private val IDLE_RANGE = 10_000L..86_400_000L
        private val AGE_RANGE = 100L..60_000L

        /** Every state but the baseline [Expression.DEFAULT] has a hold. */
        val HOLD_STATES = Expression.entries.toSet() - Expression.DEFAULT

        val DEFAULT_THRESHOLDS =
            mapOf(
                Expression.HAPPY to 7,
                Expression.SATISFIED to 7,
                Expression.DEFAULT to 1,
                Expression.SLEEPY to 8,
                Expression.SURPRISED to 7,
                Expression.ANGRY to 8,
                Expression.SAD to 8,
            )

        val DEFAULT_HOLDS =
            mapOf(
                Expression.HAPPY to 5_000L,
                Expression.SATISFIED to 5_000L,
                Expression.SLEEPY to 5_000L,
                Expression.SURPRISED to 2_500L,
                Expression.ANGRY to 4_000L,
                Expression.SAD to 5_000L,
            )

        private val FIELDS =
            setOf(
                "schema_version",
                "visible",
                "profile_id",
                "transition",
                "reduced_motion",
                "min_confidence",
                "thresholds",
                "timing",
            )
        private val TIMING_FIELDS =
            setOf(
                "transition_ms",
                "min_display_ms",
                "hold_ms",
                "idle_timeout_ms",
                "interrupt_score",
                "max_result_age_ms",
            )

        /**
         * A settings document, validated as strictly as the server's schema;
         * absent fields take their defaults. Anything invalid rejects the whole
         * document (null) — the caller keeps the built-in defaults.
         */
        @Suppress("CyclomaticComplexMethod")
        fun parse(text: String): CharacterSettings? =
            try {
                val root = Json.parseToJsonElement(text).jsonObject
                require(root.keys.all { it in FIELDS }) { "unknown field" }
                root["schema_version"]?.let { require(int(it) == 1) { "schema" } }
                val timing = root["timing"]?.jsonObject ?: JsonObject(emptyMap())
                require(timing.keys.all { it in TIMING_FIELDS }) { "unknown timing field" }
                val defaults = CharacterSettings()
                CharacterSettings(
                    visible = root["visible"]?.let(::bool) ?: defaults.visible,
                    profileId =
                        root["profile_id"]?.let(::string)?.also { require(CharacterProfile.isProfileId(it)) }
                            ?: defaults.profileId,
                    transition =
                        root["transition"]?.let { w -> enumOf(Transition.entries, string(w)) { it.wire } }
                            ?: defaults.transition,
                    reducedMotion =
                        root["reduced_motion"]?.let { w -> enumOf(ReducedMotion.entries, string(w)) { it.wire } }
                            ?: defaults.reducedMotion,
                    minConfidence = root["min_confidence"]?.let(::number) ?: defaults.minConfidence,
                    thresholds =
                        root["thresholds"]?.let { byState(it, defaults.thresholds) { v -> int(v) } }
                            ?: defaults.thresholds,
                    transitionMillis = timing["transition_ms"]?.let(::long) ?: defaults.transitionMillis,
                    minDisplayMillis = timing["min_display_ms"]?.let(::long) ?: defaults.minDisplayMillis,
                    holdMillis =
                        timing["hold_ms"]?.let { byState(it, defaults.holdMillis) { v -> long(v) } }
                            ?: defaults.holdMillis,
                    idleTimeoutMillis = timing["idle_timeout_ms"]?.let(::long) ?: defaults.idleTimeoutMillis,
                    interruptScore = timing["interrupt_score"]?.let(::int) ?: defaults.interruptScore,
                    maxResultAgeMillis = timing["max_result_age_ms"]?.let(::long) ?: defaults.maxResultAgeMillis,
                )
            } catch (ignored: SerializationException) {
                null
            } catch (ignored: IllegalArgumentException) {
                null
            } catch (ignored: ClassCastException) {
                null
            }

        /** Per-state values over [defaults]: only states [defaults] has may be named. */
        private fun <T> byState(
            element: JsonElement,
            defaults: Map<Expression, T>,
            value: (JsonElement) -> T,
        ): Map<Expression, T> {
            val overrides =
                (element as JsonObject).entries.associate { (key, v) ->
                    val state = Expression.entries.firstOrNull { it.wire == key }
                    require(state != null && state in defaults) { "unknown state" }
                    state to value(v)
                }
            return defaults + overrides
        }

        private fun <E> enumOf(
            entries: List<E>,
            wire: String,
            name: (E) -> String,
        ): E = requireNotNull(entries.firstOrNull { name(it) == wire }) { "unknown value" }

        internal fun string(element: JsonElement): String {
            val primitive = element as JsonPrimitive
            require(primitive.isString) { "not a string" }
            return primitive.content
        }

        internal fun bool(element: JsonElement): Boolean {
            val primitive = element as JsonPrimitive
            require(!primitive.isString) { "not a boolean" }
            return requireNotNull(primitive.booleanOrNull) { "not a boolean" }
        }

        /** A JSON integer: no fraction, no exponent, no quotes. */
        internal fun long(element: JsonElement): Long {
            val primitive = element as JsonPrimitive
            require(!primitive.isString && INTEGER.matches(primitive.content)) { "not an integer" }
            return requireNotNull(primitive.longOrNull) { "not an integer" }
        }

        internal fun int(element: JsonElement): Int {
            val value = long(element)
            require(value in Int.MIN_VALUE..Int.MAX_VALUE) { "out of range" }
            return value.toInt()
        }

        /** A finite JSON number. */
        internal fun number(element: JsonElement): Double {
            val primitive = element as JsonPrimitive
            require(!primitive.isString && primitive.booleanOrNull == null) { "not a number" }
            val value = requireNotNull(primitive.doubleOrNull) { "not a number" }
            require(value.isFinite()) { "not finite" }
            return value
        }

        private val INTEGER = Regex("^-?[0-9]+$")
    }
}

/** The renderer's transition, from the settings and the device (pure, so it is tested without a device). */
object CharacterTransitions {
    /**
     * The cross-fade length: 0 — an instant swap — when transitions are off,
     * when reduced motion is forced, or when it follows the system and the
     * system has animations turned off.
     */
    fun fadeMillis(
        settings: CharacterSettings,
        systemReducesMotion: Boolean,
    ): Long =
        when {
            settings.transition == CharacterSettings.Transition.NONE -> 0
            settings.reducedMotion == CharacterSettings.ReducedMotion.ALWAYS -> 0
            systemReducesMotion -> 0
            else -> settings.transitionMillis
        }
}
