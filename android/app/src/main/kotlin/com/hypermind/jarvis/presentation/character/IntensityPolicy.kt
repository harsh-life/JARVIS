package com.hypermind.jarvis.presentation.character

import kotlin.math.abs

/**
 * Presentation intensity: how strongly the *selected* expression is presented, on a 1–10 scale
 * (mirrors `presentation_intensity` in `shared/schemas/presentation.py`, held to it by
 * `shared/android/presentation_intensity_vectors.json`).
 *
 * It is not the score. A score says how well an expression presents JARVIS's current operational
 * activity, and [ActivityPolicy] uses it to choose; this says how strongly the chosen one is
 * animated. Neither measures anyone's emotions. The intensity is derived deterministically here —
 * no model judges it — and like the expression it is presentation only: nothing that
 * authenticates, authorizes, confirms or executes reads it.
 */
object IntensityPolicy {
    val RANGE = 1..10

    /** The canonical animation, exactly as authored: intensities 5 and 6 both present it. */
    const val NEUTRAL = 5

    /** The intensity of a selection that only just meets its state's threshold. */
    const val DEFAULT_FLOOR = 4

    /** A repeated selection of the shown state changes its intensity only by at least this much. */
    const val DEFAULT_DEAD_BAND = 2

    /** Fixed intensities of the two baseline states, which have no score (they are not selected). */
    val DEFAULT_BASELINES = mapOf(Expression.DEFAULT to NEUTRAL, Expression.SLEEPY to 2)

    /**
     * The threshold-relative mapping, common to every state: a score at its state's [threshold]
     * presents at [floor], a score of 10 at 10, linearly between, rounded half up, in integers:
     *
     *     intensity = floor + round_half_up((score - threshold) * (10 - floor) / (10 - threshold))
     *
     * A threshold of 10 admits only a score of 10, which presents at 10. Throws
     * IllegalArgumentException for a value out of range or a score below its threshold
     * ([ActivityPolicy] lifts a held score to its threshold first).
     */
    fun forSelection(
        score: Int,
        threshold: Int,
        floor: Int = DEFAULT_FLOOR,
    ): Int {
        require(score in CharacterSettings.SCORE_RANGE) { "score out of range" }
        require(threshold in CharacterSettings.SCORE_RANGE) { "threshold out of range" }
        require(floor in RANGE) { "floor out of range" }
        require(score >= threshold) { "the score does not meet its threshold" }
        val max = RANGE.last
        if (threshold == max) return max
        val span = max - threshold
        val numerator = (score - threshold) * (max - floor)
        // round_half_up(n / span) for n >= 0, without floating point.
        return floor + (2 * numerator + span) / (2 * span)
    }

    /** [forSelection], or null where it would throw. */
    fun forSelectionOrNull(
        score: Int,
        threshold: Int,
        floor: Int = DEFAULT_FLOOR,
    ): Int? =
        try {
            forSelection(score, threshold, floor)
        } catch (ignored: IllegalArgumentException) {
            null
        }

    /**
     * The same state selected again: its new intensity replaces the shown one only if it differs by
     * at least [deadBand], so small fluctuations never move the animation.
     */
    fun retained(
        current: Int,
        candidate: Int,
        deadBand: Int = DEFAULT_DEAD_BAND,
    ): Int = if (abs(candidate - current) >= deadBand) candidate else current
}

/**
 * What the character shows: the expression the controller committed and how strongly to present
 * it. Decided together by [ActivityPolicy] (through `CharacterController`); the renderer only
 * presents it.
 */
data class PresentedCharacter(
    val expression: Expression,
    val intensity: Int,
) {
    init {
        require(intensity in IntensityPolicy.RANGE) { "intensity out of range" }
    }
}
